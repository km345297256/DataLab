"""Contract tests for public search, original files and disabled auth sources."""
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from app.domain.external.data_source import SourceConnectorError
from app.infrastructure.external.data_sources.open_data_additional_adapters import AdditionalAdapters, safe_file_url


def openml_record(**changes):
    return {"id": "61", "file_id": "61", "name": "iris", "version": "1", "format": "ARFF",
            "visibility": "public", "status": "active", "licence": "Public",
            "url": "https://openml.org/data/v1/download/61/iris.arff",
            "parquet_url": "https://data.openml.org/datasets/0000/0061/dataset_61.pq",
            "md5_checksum": "ad484452702105cbf3d30f8deaba39a9", **changes}


def search_hit(**changes):
    return {"_source": {"data_id": "61", "name": "Iris", "description": "<p>Plant measurements</p>",
            "visibility": "public", "status": "active", "version": 1, "format": "ARFF", "licence": "Public",
            "qualities": {"NumberOfInstances": 150}, **changes}}


@pytest.mark.asyncio
async def test_openml_topic_search_has_pagination_public_filter_and_no_download_claim():
    fetch = AsyncMock(return_value={"hits": {"total": {"value": 29}, "hits": [search_hit()]}})
    result = await AdditionalAdapters(fetch).search("openml", "plant measurements", 2)
    query = fetch.call_args.kwargs["json"]
    assert query["from"] == 10 and query["size"] == 10
    assert fetch.call_args.kwargs["method"] == "POST"
    assert query["query"]["bool"]["must"]["multi_match"]["query"] == "plant measurements"
    assert {"term": {"visibility": "public"}} in query["query"]["bool"]["filter"]
    assert result["total_count"] == 29
    item = result["results"][0]
    assert item["source_url"] == "https://www.openml.org/d/61"
    assert item["description"] == "Plant measurements"
    assert item["access"] == "needs_resolution"
    assert "url" not in item["metadata"]


@pytest.mark.asyncio
async def test_search_filters_unpublished_records_even_if_upstream_filter_fails():
    fetch = AsyncMock(return_value={"hits": {"total": 3, "hits": [search_hit(), search_hit(visibility="private"), search_hit(status="deactivated")]}})
    assert len((await AdditionalAdapters(fetch).search("openml", "iris", 1))["results"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"hits": {"total": True, "hits": []}}, {"hits": {"total": 1, "hits": "bad"}},
    {"hits": {"total": 11, "hits": [search_hit()] * 11}}, {"hits": {"total": 1, "hits": [search_hit(data_id="../../etc")]}}])
async def test_openml_rejects_malformed_search(payload):
    with pytest.raises(SourceConnectorError):
        await AdditionalAdapters(AsyncMock(return_value=payload)).search("openml", "iris", 1)


@pytest.mark.asyncio
async def test_openml_partial_search_failure_is_not_a_success():
    with pytest.raises(SourceConnectorError):
        await AdditionalAdapters(AsyncMock(return_value={"timed_out": True})).search("openml", "iris", 1)


@pytest.mark.asyncio
async def test_openml_resolves_authoritative_original_format_with_checksum_and_license():
    fetch = AsyncMock(return_value={"data_set_description": openml_record()})
    candidate = {"source_id": "openml", "external_id": "61", "url": "https://evil.test/data", "metadata": {"file_id": "9"}}
    option = (await AdditionalAdapters(fetch).resolve(candidate))[0]
    assert fetch.call_args.args[0].endswith("/data/61")
    assert option["url"] == openml_record()["url"]
    assert option["source_filename"] == "iris.arff"
    assert option["checksum"] == "md5:ad484452702105cbf3d30f8deaba39a9"
    assert option["size"] is None and option["available"]
    assert option["license"] == "Public"
    original_id = option["id"]
    fetch.return_value = {"data_set_description": openml_record(version="2")}
    assert (await AdditionalAdapters(fetch).resolve(candidate))[0]["id"] != original_id


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"visibility": "private"}, {"status": "in_preparation"}])
async def test_openml_resolve_rechecks_permission(changes):
    fetch = AsyncMock(return_value={"data_set_description": openml_record(**changes)})
    assert await AdditionalAdapters(fetch).resolve({"source_id": "openml", "external_id": "61"}) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"id": "62"}, {"file_id": "62"}, {"url": "https://evil.test/iris.arff"}, {"md5_checksum": "bad"}])
async def test_openml_rejects_identity_url_and_checksum_changes(changes):
    with pytest.raises(SourceConnectorError):
        await AdditionalAdapters(AsyncMock(return_value={"data_set_description": openml_record(**changes)})).resolve({"source_id": "openml", "external_id": "61"})


@pytest.mark.parametrize("url", [
    "http://openml.org/data/v1/download/61/iris.arff",
    "https://www.openml.org.evil.test/data/v1/download/61/iris.arff",
    "https://user@www.openml.org/data/v1/download/61/iris.arff",
    "https://www.openml.org:444/data/v1/download/61/iris.arff",
    "https://www.openml.org/data/v1/download/61/%2e%2e",
    "https://www.openml.org/data/v1/download/61/a%2fb",
    "https://www.openml.org/data/v1/download/61/a%0ab",
    "https://www.openml.org/data/v1/download/61/iris.arff?redirect=https://evil.test",
    "https://data.openml.org/datasets/0000/0061/dataset_61.pq",
    "https://other.s3.amazonaws.com/iris.arff", "https://127.0.0.1/iris.arff"])
def test_openml_file_origin_is_download_only(url):
    assert not safe_file_url("openml", url)


def test_original_download_origins_are_accepted():
    assert safe_file_url("openml", "https://openml.org/data/v1/download/61/iris.arff")
    assert safe_file_url("openml", "https://www.openml.org/data/download/61/iris.arff")
    assert safe_file_url("dryad", "https://datadryad.org/api/v2/files/2115309/download")
    assert not safe_file_url("dryad", "https://arbitrary.s3.amazonaws.com/file.csv")


def dryad_record(**changes):
    return {"identifier": "doi:10.5061/dryad.example", "title": "Soil", "abstract": "<p>Observed values</p>",
            "storageSize": 5 * 1024**3, "visibility": "public", "curationStatus": "Published",
            "license": "https://spdx.org/licenses/CC0-1.0.html", "versionNumber": 1,
            "_links": {"stash:version": {"href": "/api/v2/versions/42"}}, **changes}


@pytest.mark.asyncio
async def test_dryad_search_uses_paging_and_retains_large_dataset_size():
    fetch = AsyncMock(return_value={"total": 11, "_embedded": {"stash:datasets": [dryad_record()]}})
    result = await AdditionalAdapters(fetch).search("dryad", "soil", 2)
    assert fetch.call_args.args[1] == {"q": "soil", "page": 2, "per_page": 10}
    assert result["results"][0]["size"] == 5 * 1024**3
    assert result["results"][0]["metadata"]["doi"] == "10.5061/dryad.example"


@pytest.mark.asyncio
async def test_dryad_public_metadata_does_not_pretend_file_api_is_anonymous():
    files = {"_embedded": {"stash:files": [{"_links": {"stash:download": {"href": "/api/v2/files/8/download"}},
            "path": "observations.csv", "size": 5 * 1024**3, "mimeType": "text/csv",
            "digestType": "sha-256", "digest": "a" * 64}]}}
    fetch = AsyncMock(side_effect=[dryad_record(), files])
    option = (await AdditionalAdapters(fetch).resolve({"source_id": "dryad", "external_id": "10.5061/dryad.example"}))[0]
    assert option["available"] is False
    assert option["size"] == 5 * 1024**3
    assert option["checksum"] == "sha256:" + "a" * 64
    assert option["url"] == "https://datadryad.org/api/v2/files/8/download"
    assert fetch.call_args_list[1].args[0] == "https://datadryad.org/api/v2/versions/42/files"


@pytest.mark.asyncio
async def test_dryad_rejects_foreign_version_link_without_requesting_it():
    record = deepcopy(dryad_record())
    record["_links"]["stash:version"]["href"] = "https://evil.test/api/v2/versions/42"
    fetch = AsyncMock(return_value=record)
    with pytest.raises(SourceConnectorError):
        await AdditionalAdapters(fetch).resolve({"source_id": "dryad", "external_id": "10.5061/dryad.example"})
    assert fetch.await_count == 1


@pytest.mark.asyncio
async def test_dryad_restricted_record_does_not_query_files():
    fetch = AsyncMock(return_value=dryad_record(visibility="private"))
    assert await AdditionalAdapters(fetch).resolve({"source_id": "dryad", "external_id": "10.5061/dryad.example"}) == []
    assert fetch.await_count == 1


def dryad_file(index):
    return {"_links": {"stash:download": {"href": f"/api/v2/files/{index + 1}/download"}},
            "path": f"part-{index}.csv", "size": 1000, "mimeType": "text/csv"}


def dryad_page(start, end, *, total=125, next_page=None):
    return {"_embedded": {"stash:files": [dryad_file(i) for i in range(start, end)]},
            "count": end - start, "total": total,
            "_links": {"next": {"href": f"/api/v2/versions/42/files?page={next_page}&per_page=100"}} if next_page else {}}


@pytest.mark.asyncio
async def test_dryad_collects_all_version_file_pages_without_removing_auth_gate():
    fetch = AsyncMock(side_effect=[dryad_record(), dryad_page(0, 100, next_page=2), dryad_page(100, 125)])
    options = await AdditionalAdapters(fetch).resolve({"source_id": "dryad", "external_id": "10.5061/dryad.example"})
    assert len(options) == 125 and options[-1]["source_filename"] == "part-124.csv"
    assert all(not option["available"] for option in options)
    assert [call.args[1]["page"] for call in fetch.call_args_list[1:]] == [1, 2]
    assert all(call.args[0] == "https://datadryad.org/api/v2/versions/42/files" for call in fetch.call_args_list[1:])


@pytest.mark.asyncio
@pytest.mark.parametrize("next_href", [
    "/api/v2/versions/42/files?page=1&per_page=100",
    "https://evil.test/api/v2/versions/42/files?page=2&per_page=100",
    "/api/v2/versions/99/files?page=2&per_page=100",
    "/api/v2/versions/42/files?page=2&page=3&per_page=100",
])
async def test_dryad_rejects_repeated_or_foreign_next_cursor_before_request(next_href):
    page = dryad_page(0, 100, next_page=2)
    page["_links"]["next"]["href"] = next_href
    fetch = AsyncMock(side_effect=[dryad_record(), page])
    with pytest.raises(SourceConnectorError, match="invalid_response"):
        await AdditionalAdapters(fetch).resolve({"source_id": "dryad", "external_id": "10.5061/dryad.example"})
    assert fetch.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("last_page", [dryad_page(0, 25), dryad_page(100, 100), dryad_page(100, 125, total=130)])
async def test_dryad_duplicate_missing_or_changed_manifest_is_not_complete(last_page):
    fetch = AsyncMock(side_effect=[dryad_record(), dryad_page(0, 100, next_page=2), last_page])
    with pytest.raises(SourceConnectorError, match="inconsistent_source_snapshot"):
        await AdditionalAdapters(fetch).resolve({"source_id": "dryad", "external_id": "10.5061/dryad.example"})
    assert fetch.await_count == 3


@pytest.mark.asyncio
async def test_dryad_short_page_with_explicit_next_is_not_truncated():
    fetch = AsyncMock(side_effect=[dryad_record(), dryad_page(0, 2, total=3, next_page=2), dryad_page(2, 3, total=3)])
    options = await AdditionalAdapters(fetch).resolve({"source_id": "dryad", "external_id": "10.5061/dryad.example"})
    assert len(options) == 3 and fetch.await_count == 3
