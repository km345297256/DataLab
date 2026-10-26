"""Original-file, version, permission and origin contracts for domain sources."""
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from app.domain.external.data_source import SourceConnectorError
from app.infrastructure.external.data_sources.open_data_domain_adapters import DomainAdapters, safe_file_url


DOI = "10.57760/sciencedb.016cy"
DATASET = "17c6681c2497428cb0cbca2f94ca8827"
FILE_ID = "3a1cf0bba3b57be6221adf2206cfccbc"
GBIF_KEY = "345820cc-a0a8-4d76-b7eb-fba85b21ad08"
GBIF_URL = "https://ipt.gbif.fr/archive.do?r=plants"


def science_index(**changes):
    return {"doi": DOI, "url": f"https://www.scidb.cn/detail?dataSetId={DATASET}",
            "titles": [{"title": "Soil and crop measurements"}], "descriptions": [{"description": "<p>Observed soil nutrients</p>"}],
            "rightsList": [{"rightsIdentifier": "cc-by-4.0", "rightsUri": "https://creativecommons.org/licenses/by/4.0/"}],
            "publicationYear": 2026, **changes}


def science_record(**changes):
    return {"@type": "Dataset", "identifier": f"https://doi.org/{DOI}", "version": "V1",
            "conditionsOfAccess": "unrestricted", "isAccessibleForFree": True,
            "license": "https://creativecommons.org/licenses/by/4.0/", **changes}


def science_file(**changes):
    return {"id": FILE_ID, "datasetId": DATASET, "version": "V1", "fileName": "field_data.xlsx",
            "path": "/V1/field_data.xlsx", "md5": "0ddd8ed4062137b4b8416cf1ec1de683", "size": 10416,
            "dir": False, "link": False, "index": 0, "parentFileCount": 1, **changes}


def science_fetch(files=None, record=None, index=None):
    return AsyncMock(side_effect=[{"data": {"attributes": index or science_index()}},
                                 record or science_record(), {"code": 20000, "data": files if files is not None else [science_file()]}])


@pytest.mark.asyncio
async def test_sciencedb_topic_search_uses_index_not_unfiltered_recent_listing():
    fetch = AsyncMock(return_value={"data": [{"attributes": science_index()}], "meta": {"total": 11}})
    result = await DomainAdapters(fetch).search("sciencedb", "土壤 soil", 2)
    params = fetch.call_args.args[1]
    assert fetch.call_args.args[0] == "https://api.datacite.org/dois"
    assert params["page[number]"] == 2 and params["page[size]"] == 10
    assert "prefix:(10.57760 OR 10.11922)" in params["query"] and "土壤 soil" in params["query"]
    item = result["results"][0]
    assert item["external_id"] == DOI and item["access"] == "needs_resolution"
    assert item["description"] == "Observed soil nutrients" and result["total_count"] == 11


@pytest.mark.asyncio
async def test_sciencedb_search_skips_foreign_or_restricted_dois():
    rows = [science_index(doi="10.57760/other.123"), science_index(rightsList=[{"rights": "restricted access"}]), science_index()]
    fetch = AsyncMock(return_value={"data": [{"attributes": r} for r in rows], "meta": {"total": 3}})
    assert len((await DomainAdapters(fetch).search("sciencedb", "soil", 1))["results"]) == 1


@pytest.mark.asyncio
async def test_sciencedb_resolves_original_file_permission_version_checksum_and_license():
    fetch = science_fetch(files=[science_file(size=9 * 1024**3, url="https://evil.test/payload")])
    result = (await DomainAdapters(fetch).resolve({"source_id": "sciencedb", "external_id": DOI, "metadata": {"version": "V999"}}))[0]
    assert result["size"] == 9 * 1024**3 and result["available"]
    assert result["source_filename"] == "field_data.xlsx" and result["version"] == "V1"
    assert result["checksum"] == "md5:0ddd8ed4062137b4b8416cf1ec1de683"
    assert result["url"] == f"https://china.scidb.cn/download?fileId={FILE_ID}"
    assert result["license"] == science_record()["license"]
    assert fetch.call_args.kwargs["json"]["path"] == "/V1"
    assert fetch.call_args.kwargs["json"]["version"] == "V1"


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"conditionsOfAccess": "RESTRICTED"}, {"isAccessibleForFree": False}, {"conditionsOfAccess": "embargoed"}])
async def test_sciencedb_restricted_metadata_does_not_fetch_file_tree(changes):
    fetch = science_fetch(record=science_record(**changes))
    assert await DomainAdapters(fetch).resolve({"source_id": "sciencedb", "external_id": DOI}) == []
    assert fetch.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    f"https://evil.test/detail?dataSetId={DATASET}", f"https://www.scidb.cn.evil.test/detail?dataSetId={DATASET}",
    f"http://www.scidb.cn/detail?dataSetId={DATASET}", f"https://user@www.scidb.cn/detail?dataSetId={DATASET}",
    f"https://www.scidb.cn/detail?dataSetId={DATASET}&dataSetId={DATASET}",
    f"https://www.scidb.cn/detail?dataSetId={DATASET}&url=https://evil.test", "https://www.scidb.cn/detail?dataSetId=../abc"])
async def test_sciencedb_rejects_unsafe_index_landing_pages_before_file_requests(url):
    fetch = science_fetch(index=science_index(url=url))
    with pytest.raises(SourceConnectorError):
        await DomainAdapters(fetch).resolve({"source_id": "sciencedb", "external_id": DOI})
    assert fetch.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"datasetId": "a" * 32}, {"version": "V2"}, {"id": "../../etc"},
    {"path": "/V1/../field_data.xlsx"}, {"path": "/V2/field_data.xlsx"}, {"path": "/V1/folder/nested.xlsx"},
    {"md5": "not-a-checksum"}, {"fileName": "../payload.xlsx"}, {"size": True},
    {"index": -1}, {"index": 3, "parentFileCount": 4}, {"parentFileCount": 0}])
async def test_sciencedb_rejects_mismatched_or_malformed_files(changes):
    with pytest.raises(SourceConnectorError):
        await DomainAdapters(science_fetch(files=[science_file(**changes)])).resolve({"source_id": "sciencedb", "external_id": DOI})


@pytest.mark.asyncio
async def test_sciencedb_skips_external_links_and_empty_files_are_not_available():
    fetch = science_fetch(files=[science_file(link=True), science_file(id="b" * 32, index=1, parentFileCount=2, size=0)])
    results = await DomainAdapters(fetch).resolve({"source_id": "sciencedb", "external_id": DOI})
    assert len(results) == 1 and not results[0]["available"]


@pytest.mark.asyncio
async def test_sciencedb_recurses_directories_and_advances_source_page_index():
    directory = science_file(id="a" * 32, dir=True, path="/V1/folder", fileName="folder", parentFileCount=2)
    root = science_file(index=1, parentFileCount=2)
    nested = science_file(id="b" * 32, path="/V1/folder/field_data.xlsx")
    fetch = AsyncMock(side_effect=[{"data": {"attributes": science_index()}}, science_record(),
        {"code": 20000, "data": [directory]}, {"code": 20000, "data": [root]}, {"code": 20000, "data": [nested]}])
    results = await DomainAdapters(fetch).resolve({"source_id": "sciencedb", "external_id": DOI})
    assert len(results) == 2 and results[0]["id"] != results[1]["id"]
    assert fetch.call_args_list[3].kwargs["json"]["lastIndex"] == 1
    assert fetch.call_args_list[4].kwargs["json"]["path"] == "/V1/folder"


@pytest.mark.asyncio
async def test_sciencedb_repeated_file_or_paging_response_is_rejected():
    fetch = AsyncMock(side_effect=[{"data": {"attributes": science_index()}}, science_record(),
        {"code": 20000, "data": [science_file(parentFileCount=2)]}, {"code": 20000, "data": [science_file(parentFileCount=2)]}])
    with pytest.raises(SourceConnectorError):
        await DomainAdapters(fetch).resolve({"source_id": "sciencedb", "external_id": DOI})


@pytest.mark.asyncio
async def test_sciencedb_reads_entire_file_tree_beyond_old_file_and_page_limits():
    visited = []
    async def fetch(url, params=None, **kwargs):
        if "datacite" in url:
            return {"data": {"attributes": science_index()}}
        if url.endswith("/json"):
            return science_record()
        index = kwargs["json"]["lastIndex"]
        visited.append(index)
        return {"code": 20000, "data": [science_file(id=f"{index + 1:032x}",
                fileName=f"part-{index}.csv", path=f"/V1/part-{index}.csv", index=index, parentFileCount=1001)]}
    options = await DomainAdapters(fetch).resolve({"source_id": "sciencedb", "external_id": DOI})
    assert len(options) == 1001 and visited == list(range(1001))
    assert options[-1]["source_filename"] == "part-1000.csv"


@pytest.mark.asyncio
async def test_sciencedb_changing_directory_count_is_not_reported_as_complete():
    fetch = AsyncMock(side_effect=[{"data": {"attributes": science_index()}}, science_record(),
        {"code": 20000, "data": [science_file(parentFileCount=3)]},
        {"code": 20000, "data": [science_file(id="b" * 32, index=1, parentFileCount=2)]}])
    with pytest.raises(SourceConnectorError, match="inconsistent_source_snapshot"):
        await DomainAdapters(fetch).resolve({"source_id": "sciencedb", "external_id": DOI})


@pytest.mark.asyncio
async def test_sciencedb_missing_announced_following_page_is_not_a_complete_manifest():
    fetch = AsyncMock(side_effect=[{"data": {"attributes": science_index()}}, science_record(),
        {"code": 20000, "data": [science_file(parentFileCount=2)]}, {"code": 20000, "data": []}])
    with pytest.raises(SourceConnectorError, match="inconsistent_source_snapshot"):
        await DomainAdapters(fetch).resolve({"source_id": "sciencedb", "external_id": DOI})


@pytest.mark.asyncio
async def test_sciencedb_permission_change_blocks_previously_selected_file():
    candidate = {"source_id": "sciencedb", "external_id": DOI}
    assert await DomainAdapters(science_fetch()).resolve(candidate)
    assert await DomainAdapters(science_fetch(record=science_record(conditionsOfAccess="RESTRICTED"))).resolve(candidate) == []


@pytest.mark.asyncio
async def test_sciencedb_version_change_changes_file_selection_identity():
    first = (await DomainAdapters(science_fetch()).resolve({"source_id": "sciencedb", "external_id": DOI}))[0]
    second = (await DomainAdapters(science_fetch(record=science_record(version="V2"), files=[science_file(version="V2", path="/V2/field_data.xlsx")])).resolve({"source_id": "sciencedb", "external_id": DOI}))[0]
    assert first["id"] != second["id"] and second["version"] == "V2"


def gbif_record(**changes):
    return {"key": GBIF_KEY, "type": "OCCURRENCE", "title": "Plant observations", "description": "<p>Historical plants</p>",
            "version": "1.5", "doi": "10.15468/3kvaeh", "license": "http://creativecommons.org/licenses/by/4.0/legalcode",
            "recordCount": 199, "endpoints": [{"type": "DWC_ARCHIVE", "url": GBIF_URL}], **changes}


@pytest.mark.asyncio
async def test_gbif_search_is_paginated_and_metadata_only_records_are_excluded():
    fetch = AsyncMock(return_value={"results": [gbif_record(), gbif_record(type="METADATA"), gbif_record(deleted="2026-01-01")], "count": 45})
    result = await DomainAdapters(fetch).search("gbif", "plants", 3)
    assert fetch.call_args.args[1] == {"q": "plants", "limit": 10, "offset": 20}
    assert result["total_count"] == 45 and len(result["results"]) == 1
    assert result["results"][0]["coverage"]["records"] == 199


@pytest.mark.asyncio
async def test_gbif_resolve_pins_published_version_and_preserves_archive():
    fetch = AsyncMock(return_value=gbif_record())
    result = (await DomainAdapters(fetch).resolve({"source_id": "gbif", "external_id": GBIF_KEY}))[0]
    assert result["url"] == GBIF_URL + "&v=1.5"
    assert result["version"] == "1.5" and result["content_type"] == "application/zip"
    assert result["record_url"] == f"https://www.gbif.org/dataset/{GBIF_KEY}"
    fetch.return_value = gbif_record(version="1.6")
    second = (await DomainAdapters(fetch).resolve({"source_id": "gbif", "external_id": GBIF_KEY}))[0]
    assert second["id"] != result["id"] and second["url"].endswith("v=1.6")


@pytest.mark.asyncio
async def test_gbif_upgrades_only_known_legacy_http_nodes():
    fetch = AsyncMock(return_value=gbif_record(endpoints=[{"type": "DWC_ARCHIVE", "url": GBIF_URL.replace("https:", "http:")}]))
    result = (await DomainAdapters(fetch).resolve({"source_id": "gbif", "external_id": GBIF_KEY}))[0]
    assert result["url"] == GBIF_URL + "&v=1.5"


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["https://publisher.example/archive.zip", "https://evil.test/archive.do?r=plants", "https://ipt.gbif.fr/eml.do?r=plants", "http://user@ipt.gbif.fr/archive.do?r=plants", GBIF_URL + "&v=1.4"])
async def test_gbif_unsupported_host_metadata_and_conflicting_versions_are_not_downloads(url):
    fetch = AsyncMock(return_value=gbif_record(endpoints=[{"type": "DWC_ARCHIVE", "url": url}]))
    assert await DomainAdapters(fetch).resolve({"source_id": "gbif", "external_id": GBIF_KEY}) == []


@pytest.mark.asyncio
async def test_gbif_published_hosted_dataset_retains_original_extension_and_deduplicates_endpoint():
    endpoint = {"type": "DWC_ARCHIVE", "url": "https://hosted-datasets.gbif.org/datasets/backbone/2023-08-28/simple.txt.gz"}
    fetch = AsyncMock(return_value=gbif_record(endpoints=[endpoint, deepcopy(endpoint)]))
    results = await DomainAdapters(fetch).resolve({"source_id": "gbif", "external_id": GBIF_KEY})
    assert len(results) == 1 and results[0]["filename"].endswith("simple.txt.gz")


@pytest.mark.asyncio
async def test_gbif_deleted_dataset_and_missing_version_are_not_available():
    for changes in ({"deleted": "2026-01-01"}, {"version": None}, {"type": "METADATA"}):
        assert await DomainAdapters(AsyncMock(return_value=gbif_record(**changes))).resolve({"source_id": "gbif", "external_id": GBIF_KEY}) == []


@pytest.mark.parametrize("source,url", [
    ("sciencedb", f"https://china.scidb.cn/download?fileId={FILE_ID}"),
    ("sciencedb", f"https://download.scidb.cn/download?fileId={FILE_ID}"),
    ("gbif", GBIF_URL + "&v=1.5"),
    ("gbif", "https://ipt.gbif.pt/ipt/archive.do?r=plants&v=2.0"),
    ("gbif", "https://hosted-datasets.gbif.org/datasets/backbone/2023-08-28/backbone.zip")])
def test_domain_file_allowlist_accepts_only_supported_file_origins(source, url):
    assert safe_file_url(source, url)


@pytest.mark.parametrize("source,url", [
    ("sciencedb", f"https://china.scidb.cn.evil.test/download?fileId={FILE_ID}"),
    ("sciencedb", f"http://china.scidb.cn/download?fileId={FILE_ID}"),
    ("sciencedb", f"https://user@china.scidb.cn/download?fileId={FILE_ID}"),
    ("sciencedb", f"https://china.scidb.cn:444/download?fileId={FILE_ID}"),
    ("sciencedb", f"https://china.scidb.cn/download?fileId={FILE_ID}&fileId={FILE_ID}"),
    ("sciencedb", f"https://china.scidb.cn/download?fileId={FILE_ID}&username=someone"),
    ("sciencedb", f"https://china.scidb.cn/download?fileId={FILE_ID}#anything"),
    ("sciencedb", "https://china.scidb.cn/download?fileId=../../etc"),
    ("sciencedb", "https://china.scidb.cn/download?fileId=" + "a" * 25),
    ("gbif", "https://ipt.gbif.fr/archive.do?r=plants&r=other"),
    ("gbif", "https://ipt.gbif.fr/archive.do?r=../plants"),
    ("gbif", "https://ipt.gbif.fr/archive.do?r=plants&url=https://evil.test"),
    ("gbif", "https://ipt.gbif.fr/archive.do?r=plants&v=latest"),
    ("gbif", "https://ipt.gbif.fr/archive.do?r=plants%0a"),
    ("gbif", "https://ipt.gbif.fr/other/archive.do?r=plants"),
    ("gbif", "https://hosted-datasets.gbif.org/datasets/%2e%2e/secret.zip"),
    ("gbif", "https://hosted-datasets.gbif.org/datasets/%252e%252e/secret.zip"),
    ("gbif", "https://hosted-datasets.gbif.org/datasets/metadata.xml"),
    ("gbif", "https://127.0.0.1/archive.zip")])
def test_domain_file_allowlist_rejects_unsafe_or_non_file_routes(source, url):
    assert not safe_file_url(source, url)


@pytest.mark.asyncio
@pytest.mark.parametrize("source,payload", [("sciencedb", {"data": [], "meta": {"total": True}}), ("gbif", {"results": "bad", "count": 1})])
async def test_invalid_search_shapes_fail_closed(source, payload):
    with pytest.raises(SourceConnectorError):
        await DomainAdapters(AsyncMock(return_value=payload)).search(source, "soil", 1)


@pytest.mark.asyncio
async def test_foreign_or_invalid_candidate_id_never_reaches_provider():
    fetch = AsyncMock()
    for source, external in (("sciencedb", "10.5281/zenodo.123"), ("gbif", "../../etc")):
        with pytest.raises(SourceConnectorError):
            await DomainAdapters(fetch).resolve({"source_id": source, "external_id": external})
    assert fetch.await_count == 0
