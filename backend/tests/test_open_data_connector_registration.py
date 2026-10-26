from types import SimpleNamespace
from unittest.mock import AsyncMock
from pydantic import ValidationError

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.application.errors.exceptions import BadRequestError
from app.application.services.data_connector_service import build_data_connector_service
from app.domain.models.data_connector import ConnectorResult
from app.domain.models.open_data_registry import connector_sources, source_definitions
from app.domain.external.data_source import SourceConnectorError
from app.interfaces.api import data_connector_routes
from app.interfaces.dependencies import get_current_user
from app.interfaces.schemas.session import ChatRequest


@pytest.fixture
def environment():
    sources = SimpleNamespace(search=AsyncMock(return_value={
        "results": [dict(external_id="record-1", title="Climate observations", description="See /srv/private/file",
            source_url="https://zenodo.org/records/123?secret=TOKEN", access="needs_resolution", formats=["csv"],
            metadata={"url": "http://minio.internal/private", "secret": "TOKEN"}, coverage={"storage_path": "/srv/private"})],
        "next_cursor": "cursor-2", "total_count": 9,
    }), verify=AsyncMock(return_value=[dict(id="file-1", filename="observations.csv", available=True)]))
    service = build_data_connector_service(SimpleNamespace(), topic_client=SimpleNamespace(), open_data_sources=sources)
    return sources, service


def test_new_sources_are_available_for_picker_and_chat_selection_with_executable_schemas(environment):
    _, service = environment
    catalog = {manifest.id: manifest for manifest in service.catalog()}
    assert set(catalog) == {"ncbi-datasets-v2"} | {source.id for source in source_definitions() if source.connector_enabled}
    request = ChatRequest(message="Find data", data_connectors=[source.id for source in connector_sources()])
    assert set(request.data_connectors) <= set(catalog)
    for key in request.data_connectors:
        manifest = catalog[key]
        assert [operation.name for operation in manifest.operations] == ["search"]
        assert manifest.operations[0].output_schema == ConnectorResult.model_json_schema()
        assert manifest.operations[0].effects == ["network"]
        assert manifest.auth == "none"
    assert {operation.name for operation in catalog["ncbi-datasets-v2"].operations} == {"search", "describe", "acquire"}


def test_connector_selection_keeps_a_bounded_limit_consistent_with_discovery():
    from app.domain.models.open_data import MAX_SELECTED_DATA_SOURCES, SearchInput
    ids = [f"source-{index}" for index in range(MAX_SELECTED_DATA_SOURCES)]
    assert ChatRequest(message="Find data", data_connectors=ids).data_connectors == ids
    assert SearchInput(query="Find data", source_ids=ids).source_ids == ids
    with pytest.raises(ValidationError):
        ChatRequest(message="Find data", data_connectors=ids + ["source-over-limit"])
    with pytest.raises(ValidationError):
        SearchInput(query="Find data", source_ids=ids + ["source-over-limit"])


@pytest.mark.asyncio
@pytest.mark.parametrize("source_id", [source.id for source in connector_sources()])
async def test_generic_search_reuses_adapter_pagination_and_exposes_only_public_metadata(environment, source_id):
    sources, service = environment
    result = await service.call("alice", "s", source_id, "search", {"query": "climate", "cursor": "page-1"})
    sources.search.assert_awaited_once_with(source_id, "climate", "page-1")
    assert result.next_cursor == "cursor-2" and result.total_count is None
    assert result.kind == "metadata" and not result.files_downloaded and not result.files
    encoded = result.model_dump_json()
    assert "TOKEN" not in encoded and "minio.internal" not in encoded and "/srv/private" not in encoded
    assert result.resources[0].metadata["formats"] == ["csv"]
    assert result.resources[0].source_url == "https://zenodo.org/records/123"
    assert result.resources[0].metadata["access"] == "downloadable"
    assert result.resources[0].metadata["availability_verified_at"]
    assert result.resources[0].metadata["available_file_count"] == 1
    sources.verify.assert_awaited_once()


@pytest.mark.asyncio
async def test_generic_search_omits_records_without_verified_data_and_keeps_pagination(environment):
    sources, service = environment
    sources.verify.return_value = []
    result = await service.call("alice", "s", "zenodo", "search", {"query": "climate"})
    assert result.resources == [] and result.total_count is None
    assert result.next_cursor == "cursor-2"


@pytest.mark.asyncio
async def test_verification_failure_is_not_a_successful_empty_result(environment):
    sources, service = environment
    sources.verify.side_effect = SourceConnectorError("rate_limited")
    with pytest.raises(SourceConnectorError):
        await service.call("alice", "s", "zenodo", "search", {"query": "climate"})


@pytest.mark.asyncio
async def test_generic_search_preserves_verified_result_when_another_probe_fails(environment):
    sources, service = environment
    page = sources.search.return_value
    page["results"].append({**page["results"][0], "external_id": "record-2"})
    sources.verify.side_effect = [[dict(id="file-1", filename="observations.csv", available=True)], SourceConnectorError("rate_limited")]
    result = await service.call("alice", "s", "zenodo", "search", {"query": "climate"})
    assert len(result.resources) == 1
    assert any("尚未完成核验" in note for note in result.resources[0].metadata["system_notes"])
    assert not any("尚未完成核验" in note for note in result.resources[0].metadata["limitations"])


@pytest.mark.asyncio
async def test_catalog_and_metadata_separate_source_content_from_system_acquisition_notes(environment):
    sources, service = environment
    source = sources.search.return_value["results"][0]
    source.update(limitations=["仅覆盖部分经济体。"], system_notes=["获取后生成 CSV 快照。"])
    sources.search.return_value["warnings"] = ["部分来源暂时不可用。"]
    sources.verify.return_value = [dict(id="large-file", filename="data.csv.gz", available=True, size=2**40,
        system_notes=["只读取文件样本核验。"], url="https://zenodo.org/private?token=TOKEN")]
    result = await service.call("alice", "s", "zenodo", "search", {"query": "climate"})
    metadata = result.resources[0].metadata
    assert metadata["size"] == 2**40 and metadata["formats"] == ["csv.gz"]
    assert metadata["limitations"] == ["仅覆盖部分经济体。"]
    assert metadata["system_notes"] == ["已核验 1 个可获取文件", "获取后生成 CSV 快照。", "只读取文件样本核验。", "部分来源暂时不可用。"]
    assert "TOKEN" not in result.model_dump_json() and "_source_system_notes" not in result.model_dump_json()
    manifest = next(item for item in service.catalog() if item.id == "zenodo")
    assert "获取限额" not in manifest.model_dump_json()


@pytest.mark.asyncio
async def test_generic_registration_does_not_admit_unavailable_options(environment):
    sources, service = environment
    sources.verify.return_value = [dict(id="private-file", filename="data.csv", available=False)]
    result = await service.call("alice", "s", "zenodo", "search", {"query": "climate"})
    assert not result.resources


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,arguments", [
    ("acquire", {"query": "climate"}), ("search", {"query": "climate", "url": "http://localhost"}),
    ("search", {"query": "x" * 501}), ("search", {"query": "climate", "owner": "someone-else"}),
])
async def test_generic_registration_rejects_undeclared_effects_and_untrusted_arguments(environment, operation, arguments):
    sources, service = environment
    with pytest.raises(BadRequestError):
        await service.call("alice", "s", "zenodo", operation, arguments)
    sources.search.assert_not_called()


@pytest.mark.asyncio
async def test_catalog_and_generic_call_route_include_new_sources(environment):
    _, service = environment
    app = FastAPI()
    app.include_router(data_connector_routes.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="alice")
    app.dependency_overrides[data_connector_routes.owned_session] = lambda: ("alice", "s")
    app.dependency_overrides[data_connector_routes.get_data_connector_service] = lambda: service
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        catalog = await client.get("/data-connectors")
        assert catalog.status_code == 200
        assert {entry["id"] for entry in catalog.json()["data"]} >= {"zenodo", "datacite", "worldbank"}
        response = await client.post("/sessions/s/data-connectors/worldbank/call", json={"operation": "search", "arguments": {"query": "population"}})
        assert response.status_code == 200 and response.json()["data"]["files_downloaded"] is False
