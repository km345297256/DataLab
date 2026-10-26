"""Repository routing, immutable file manifests and download trust boundaries."""
import pytest

from app.domain.external.data_source import SourceConnectorError
from app.infrastructure.external.data_sources.open_data_catalog_adapters import (
    CatalogAdapters, INSTALLATIONS, safe_file_url,
)

PIDS = {"borealis": "doi:10.5683/SP3/YZBCGR", "dataverse-nl": "doi:10.34894/2T18NN",
        "recherche-data-gouv": "doi:10.57745/TJBYIY"}
MD5 = "2ba2f1694a4fed2e7804d1b674be1393"
PANGAEA = "10.1594/PANGAEA.841672"


def record(source):
    prefix, suffix = PIDS[source][4:].split("/", 1)
    return {"status": "OK", "data": {"protocol": "doi", "authority": prefix, "identifier": suffix,
        "latestVersion": {"datasetPersistentId": PIDS[source], "versionNumber": 1, "versionMinorNumber": 2,
            "versionState": "RELEASED", "license": {"name": "CC BY 4.0"}, "files": [{"restricted": False,
            "dataFile": {"id": 1234, "filename": "sample.tab", "filesize": 990,
                "originalFileName": "sample.xlsx", "originalFileSize": 800,
                "originalFileFormat": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "checksum": {"type": "MD5", "value": MD5}}}]}}}


def search_row(source):
    return {"global_id": PIDS[source], "type": "dataset", "versionState": "RELEASED", "fileCount": 1,
            "name": "A data table", "description": "<p>observations</p>", "majorVersion": 1, "minorVersion": 2}


def pangaea_record():
    return {"@type": "Dataset", "identifier": "https://doi.org/" + PANGAEA,
            "isAccessibleForFree": True, "license": "https://creativecommons.org/licenses/by/3.0/",
            "distribution": [{"contentUrl": "https://doi.pangaea.de/" + PANGAEA + "?format=textfile",
                              "encodingFormat": "text/tab-separated-values"}]}


@pytest.mark.asyncio
@pytest.mark.parametrize("source", sorted(INSTALLATIONS))
async def test_dataverse_manifest_keeps_files_after_the_old_hundred_file_cutoff(source):
    from copy import deepcopy
    payload = record(source)
    template = payload["data"]["latestVersion"]["files"][0]
    files = [deepcopy(template) for _ in range(101)]
    for index, item in enumerate(files):
        item["dataFile"].update(id=index + 1, originalFileName=f"part-{index}.xlsx")
    payload["data"]["latestVersion"]["files"] = files
    async def fetch(*args, **kwargs):
        return payload
    options = await CatalogAdapters(fetch).resolve({"source_id": source, "external_id": PIDS[source]})
    assert len(options) == 101 and options[-1]["source_filename"] == "part-100.xlsx"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", PIDS)
async def test_dataverse_search_scopes_local_published_records_and_preserves_version(source):
    async def fetch(url, params=None):
        assert url == INSTALLATIONS[source][0] + "/api/search"
        assert params["q"] == "water AND temperature"
        assert params["start"] == 10 and params["per_page"] == 10
        assert params["fq"] == ["publicationStatus:Published", f'dsPersistentId:"doi:{INSTALLATIONS[source][2]}"']
        foreign = {**search_row(source), "global_id": "doi:10.7910/DVN/WZBHHR"}
        empty = {**search_row(source), "fileCount": 0}
        return {"status": "OK", "data": {"items": [foreign, empty, search_row(source)], "total_count": 21}}

    result = await CatalogAdapters(fetch).search(source, "water temperature", 2)
    assert result["total_count"] == 21
    candidate, = result["results"]
    assert candidate["source_id"] == source and candidate["external_id"] == PIDS[source]
    assert candidate["metadata"]["version"] == "1.2"
    assert candidate["description"] == "observations"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", PIDS)
async def test_resolve_reloads_exact_repository_and_original_file(source):
    async def fetch(url, params=None):
        assert url == INSTALLATIONS[source][0] + "/api/datasets/:persistentId/"
        assert params == {"persistentId": PIDS[source]}
        return record(source)

    candidate = {"source_id": source, "external_id": PIDS[source].lower(), "metadata": {"version": "1.2"},
                 "url": "http://127.0.0.1/private"}
    option, = await CatalogAdapters(fetch).resolve(candidate)
    assert option["source_filename"] == "sample.xlsx" and option["size"] == 800
    assert option["version"] == "1.2" and option["checksum"] == "md5:" + MD5
    assert option["content_type"].endswith("spreadsheetml.sheet")
    assert option["license"] == "CC BY 4.0"
    assert option["url"] == INSTALLATIONS[source][0] + "/api/access/datafile/1234?format=original"
    assert safe_file_url(source, option["url"])


@pytest.mark.asyncio
async def test_requested_version_is_fetched_and_cannot_silently_upgrade():
    source = "dataverse-nl"
    old = record(source)["data"]["latestVersion"]
    current = record(source)
    current["data"]["latestVersion"]["versionNumber"] = 2
    current["data"]["latestVersion"]["files"][0]["dataFile"]["id"] = 9999

    async def fetch(url, params=None):
        return {"status": "OK", "data": old} if url.endswith("/versions/1.2") else current

    option, = await CatalogAdapters(fetch).resolve({"source_id": source, "external_id": PIDS[source], "metadata": {"version": "1.2"}})
    assert option["version"] == "1.2" and "/1234?" in option["url"]


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["restricted", "embargo", "guestbook", "draft"])
async def test_restricted_and_consent_gated_files_are_not_available(gate):
    payload = record("borealis")
    version = payload["data"]["latestVersion"]
    if gate == "restricted":
        version["files"][0]["restricted"] = True
    elif gate == "embargo":
        version["files"][0]["dataFile"]["embargo"] = {"dateAvailable": "2099-01-01"}
    elif gate == "guestbook":
        payload["data"]["guestbookId"] = 44
    else:
        version["versionState"] = "DRAFT"

    async def fetch(*args): return payload

    assert await CatalogAdapters(fetch).resolve({"source_id": "borealis", "external_id": PIDS["borealis"]}) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,digest", [("SHA-1", "a" * 40), ("SHA-256", "b" * 64), ("SHA-512", "c" * 128)])
async def test_source_checksums_are_preserved(kind, digest):
    payload = record("dataverse-nl")
    payload["data"]["latestVersion"]["files"][0]["dataFile"]["checksum"] = {"type": kind, "value": digest}
    async def fetch(*args): return payload
    option, = await CatalogAdapters(fetch).resolve({"source_id": "dataverse-nl", "external_id": PIDS["dataverse-nl"]})
    assert option["checksum"] == kind.lower().replace("-", "") + ":" + digest


@pytest.mark.asyncio
async def test_large_files_have_no_adapter_size_cap():
    payload = record("borealis")
    payload["data"]["latestVersion"]["files"][0]["dataFile"]["originalFileSize"] = 200 * 1024**3
    async def fetch(*args): return payload
    option, = await CatalogAdapters(fetch).resolve({"source_id": "borealis", "external_id": PIDS["borealis"]})
    assert option["available"] and option["size"] == 200 * 1024**3


@pytest.mark.asyncio
async def test_manifest_doi_mismatch_is_rejected():
    payload = record("borealis")
    payload["data"]["identifier"] = "SP3/ABCDEF"
    async def fetch(*args): return payload
    with pytest.raises(SourceConnectorError, match="invalid_candidate"):
        await CatalogAdapters(fetch).resolve({"source_id": "borealis", "external_id": PIDS["borealis"]})


@pytest.mark.asyncio
async def test_pangaea_search_uses_documented_datacite_discovery():
    async def fetch(url, params=None):
        assert url == "https://api.datacite.org/dois"
        assert params == {"query": "prefix:10.1594 AND (water AND temperature)", "page[number]": 2, "page[size]": 10}
        return {"data": [{"attributes": {"doi": PANGAEA.lower(), "titles": [{"title": "Temperature"}],
                "descriptions": [{"description": "Measurements"}], "types": {"resourceTypeGeneral": "Dataset"}}}],
                "meta": {"total": 22}}
    result = await CatalogAdapters(fetch).search("pangaea", "water temperature", 2)
    assert result["total_count"] == 22 and result["results"][0]["external_id"] == PANGAEA


@pytest.mark.asyncio
async def test_pangaea_original_table_declared_in_fresh_jsonld():
    async def fetch(url, params=None):
        assert url == "https://doi.pangaea.de/" + PANGAEA
        assert params == {"format": "metadata_jsonld"}
        return pangaea_record()
    option, = await CatalogAdapters(fetch).resolve({"source_id": "pangaea", "external_id": PANGAEA.lower()})
    assert option["filename"] == "pangaea_841672.tab" and option["size"] is None
    assert option["checksum"] is None and option["license"].endswith("/by/3.0/")
    assert option["version"] == PANGAEA and safe_file_url("pangaea", option["url"])


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["restricted", "collection", "external_url", "html"])
async def test_pangaea_only_its_declared_public_entity_table(gate):
    payload = pangaea_record()
    if gate == "restricted": payload["isAccessibleForFree"] = False
    elif gate == "collection": payload["distribution"] = []
    elif gate == "external_url": payload["distribution"][0]["contentUrl"] = "https://example.com/data.csv"
    else: payload["distribution"][0]["encodingFormat"] = "text/html"
    async def fetch(*args): return payload
    assert await CatalogAdapters(fetch).resolve({"source_id": "pangaea", "external_id": PANGAEA}) == []


@pytest.mark.parametrize("source,url", [
    ("borealis", "https://olrc2.scholarsportal.info/dataverse/10.5683/SP3/YZBCGR/1989b2-1234.orig?X-Amz-Signature=test"),
    ("dataverse-nl", "https://objectstore.surf.nl/store/10.34894/2T18NN/190069-1234?X-Amz-Signature=test"),
    ("recherche-data-gouv", "https://prod-datainra.opens3r-tls.stockage.inrae.fr/10.57745/TJBYIY/180992-1234.orig?X-Amz-Signature=test"),
    ("pangaea", "https://doi.pangaea.de/10.1594/PANGAEA.841672?format=textfile"),
])
def test_named_storage_paths_allow_only_their_source(source, url):
    assert safe_file_url(source, url)
    assert not safe_file_url("unknown", url)
    assert not safe_file_url(source, url.replace("https://", "https://attacker.example/"))


@pytest.mark.parametrize("url", [
    "http://borealisdata.ca/api/access/datafile/1234?format=original",
    "https://borealisdata.ca.evil.example/api/access/datafile/1234?format=original",
    "https://x:b@borealisdata.ca/api/access/datafile/1234?format=original",
    "https://borealisdata.ca:8443/api/access/datafile/1234?format=original",
    "https://borealisdata.ca/api/access/datafile/1234?format=original&key=secret",
    "https://borealisdata.ca/api/access/datafile/1234?format=original#bad",
    "https://borealisdata.ca/api/access/datafile/0?format=original",
    "https://borealisdata.ca/api/access/datafile/../1234?format=original",
    "https://borealisdata.ca/api/access/datafile/%2e%2e/1234?format=original",
    "https://olrc2.scholarsportal.info/other/10.5683/SP3/YZBCGR/data.csv",
    "https://olrc2.scholarsportal.info/dataverse/10.5683/SP3/YZBCGR/../private",
    "https://doi.pangaea.de/10.1594/PANGAEA.841672?format=html",
])
def test_download_allowlist_rejects_cross_host_and_non_file_paths(url):
    assert not safe_file_url("borealis", url)


@pytest.mark.asyncio
@pytest.mark.parametrize("query,page", [("", 1), ("x" * 501, 1), ("iris", 0), ("iris", True), ("iris", 101)])
async def test_invalid_search_inputs_never_make_network_call(query, page):
    async def fetch(*args): raise AssertionError("network called")
    with pytest.raises(SourceConnectorError, match="invalid_query"):
        await CatalogAdapters(fetch).search("pangaea", query, page)


@pytest.mark.asyncio
@pytest.mark.parametrize("source,external", [("borealis", "doi:10.7910/DVN/WZBHHR"), ("dataverse-nl", "http://localhost"),
    ("recherche-data-gouv", "doi:10.57745/ABCDEF/../../secret"), ("pangaea", "10.1594/PANGAEA.841672?format=html")])
async def test_invalid_identifiers_never_make_network_call(source, external):
    async def fetch(*args): raise AssertionError("network called")
    with pytest.raises(SourceConnectorError, match="invalid_candidate"):
        await CatalogAdapters(fetch).resolve({"source_id": source, "external_id": external})
