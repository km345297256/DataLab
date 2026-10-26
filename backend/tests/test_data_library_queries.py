"""Whole-library overview and server-side filtering must stay owner-scoped."""
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Header
from httpx import ASGITransport, AsyncClient

from app.application.errors.exceptions import BadRequestError
from app.application.services.data_library_service import DataLibraryService
from app.domain.models.data_library import DataLibraryPage, DataLibraryStats
from app.infrastructure.repositories.mongo_data_library_repository import MongoDataLibraryRepository
from app.interfaces.api import data_library_routes
from app.interfaces.dependencies import get_current_user
from app.interfaces.errors.exception_handlers import register_exception_handlers


@pytest.mark.asyncio
async def test_overview_reads_only_saved_metadata_and_uses_authenticated_owner():
    repo = SimpleNamespace(stats=AsyncMock(return_value={
        "file_count": 250, "total_bytes": 912345, "updated_at": datetime(2026, 10, 21, tzinfo=UTC),
        "formats": ["csv", "nii.gz", "none"],
    }))
    files = SimpleNamespace(get_file_info=AsyncMock(side_effect=AssertionError("Storage must not be scanned")))
    service = DataLibraryService(repo, files, None)
    overview = await service.stats("owner-a")
    assert overview.file_count == 250 and overview.total_bytes == 912345
    assert overview.formats == ["csv", "nii.gz", "none"]
    repo.stats.assert_awaited_once_with("owner-a")
    files.get_file_info.assert_not_awaited()


def test_mongo_naive_utc_stats_timestamp_serializes_with_an_explicit_timezone():
    stats = DataLibraryStats(updated_at=datetime(2026, 10, 20, 16))
    assert stats.model_dump(mode="json")["updated_at"] == "2026-10-20T16:00:00Z"


@pytest.mark.asyncio
async def test_list_normalizes_dates_and_format_but_preserves_literal_filename():
    repo = SimpleNamespace(page=AsyncMock(return_value=[]))
    service = DataLibraryService(repo, None, None)
    start = datetime(2026, 10, 21, tzinfo=UTC).astimezone(timezone(timedelta(hours=8)))
    end = start + timedelta(days=1)
    await service.list("alice", filename="[a].CSV", file_format="CSV", updated_from=start, updated_to=end)
    repo.page.assert_awaited_once_with(
        "alice", cursor=None, query="", source_type=None, filename="[a].CSV", file_format="csv",
        updated_from=start.astimezone(UTC), updated_to=end.astimezone(UTC),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("start,end", [
    (datetime(2026, 10, 21), None),
    (None, datetime(2026, 10, 21)),
    (datetime(2026, 10, 22, tzinfo=UTC), datetime(2026, 10, 21, tzinfo=UTC)),
    (datetime(2026, 10, 21, tzinfo=UTC), datetime(2026, 10, 21, tzinfo=UTC)),
])
async def test_invalid_update_window_is_rejected_before_repository_access(start, end):
    repo = SimpleNamespace(page=AsyncMock())
    with pytest.raises(BadRequestError):
        await DataLibraryService(repo, None, None).list("alice", updated_from=start, updated_to=end)
    repo.page.assert_not_awaited()


@pytest.mark.asyncio
async def test_old_list_call_does_not_require_new_repository_keywords():
    class OldRepository:
        async def page(self, owner, *, cursor=None, query="", source_type=None):
            assert (owner, cursor, query, source_type) == ("alice", None, "legacy", "upload")
            return []
    assert (await DataLibraryService(OldRepository(), None, None).list(
        "alice", query=" legacy ", source_type="upload",
    )).items == []


@pytest.mark.asyncio
async def test_repository_quotes_filename_regex_and_keeps_filters_inside_page_query():
    cursor = SimpleNamespace(to_list=AsyncMock(return_value=[]))
    cursor.sort = lambda *args: cursor
    cursor.limit = lambda *args: cursor
    from unittest.mock import Mock
    collection = SimpleNamespace(find=Mock(return_value=cursor))
    repo = MongoDataLibraryRepository({"data_library": collection})
    await repo.page("alice", filename="a[1].*", file_format="$csv")
    query = collection.find.call_args.args[0]
    assert query["owner_id"] == "alice"
    assert query["_id"]["$gte"] == repo.prefix("alice")
    conditions = query["$expr"]["$anyElementTrue"][0]["$map"]["in"]["$and"]
    assert conditions[0]["$regexMatch"]["regex"] == r"a\[1\]\.\*"
    assert conditions[1]["$eq"][1] == {"$literal": "$csv"}
    cursor.to_list.assert_awaited_once_with(length=21)


@pytest.mark.asyncio
async def test_stats_route_precedes_asset_lookup_and_preserves_owner_boundary():
    service = SimpleNamespace(stats=AsyncMock(side_effect=lambda owner: DataLibraryStats(file_count=7 if owner == "alice" else 0)),
                              list=AsyncMock(return_value=DataLibraryPage(items=[])))
    app = FastAPI()
    app.include_router(data_library_routes.router, prefix="/api/v1")
    register_exception_handlers(app)
    def current_user(x_test_user: str = Header("alice")):
        return SimpleNamespace(id=x_test_user)
    app.dependency_overrides[get_current_user] = current_user
    app.dependency_overrides[data_library_routes.get_data_library_service] = lambda: service
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        alice = await client.get("/api/v1/data-library/stats")
        bob = await client.get("/api/v1/data-library/stats", headers={"x-test-user": "bob"})
        assert alice.status_code == bob.status_code == 200
        assert alice.json()["data"]["file_count"] == 7
        assert bob.json()["data"] == {"file_count": 0, "total_bytes": 0, "updated_at": None, "formats": []}
        assert (await client.get("/api/v1/data-library")).status_code == 200
        service.list.assert_awaited_with("alice", cursor=None, query="", source_type=None)
        filtered = await client.get("/api/v1/data-library", params={
            "filename": "a[1].CSV", "file_format": "csv", "updated_from": "2026-10-20T16:00:00Z",
            "updated_to": "2026-10-21T16:00:00Z", "q": "ocean", "source_type": "upload",
        })
        assert filtered.status_code == 200
        service.list.assert_awaited_with(
            "alice", cursor=None, query="ocean", source_type="upload", filename="a[1].CSV", file_format="csv",
            updated_from=datetime(2026, 10, 20, 16, tzinfo=UTC), updated_to=datetime(2026, 10, 21, 16, tzinfo=UTC),
        )
        for invalid in ("2026-10-21", "2026-10-21T00:00:00", "not-a-date", "1792540800"):
            assert (await client.get("/api/v1/data-library", params={"updated_from": invalid})).status_code == 422


@pytest.mark.asyncio
async def test_http_reversed_update_range_returns_client_error():
    repo = SimpleNamespace(page=AsyncMock(return_value=[]))
    app = FastAPI()
    app.include_router(data_library_routes.router, prefix="/api/v1")
    register_exception_handlers(app)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id="alice")
    app.dependency_overrides[data_library_routes.get_data_library_service] = lambda: DataLibraryService(repo, None, None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/data-library", params={
            "updated_from": "2026-10-22T00:00:00+08:00", "updated_to": "2026-10-21T00:00:00+08:00",
        })
    assert response.status_code == 400
    repo.page.assert_not_awaited()
