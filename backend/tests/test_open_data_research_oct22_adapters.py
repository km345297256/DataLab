from copy import deepcopy
import json
from urllib.parse import urlencode

import pytest

from app.domain.external.data_source import SourceConnectorError
from app.infrastructure.external.data_sources.open_data_research_oct22_adapters import (
    ResearchOct22Adapters, safe_file_url, NASA, BIO, RODARE,
)

BUCKET = "54bd5ddb-cfb6-4154-b6d6-d7c0ba6f7150"
NASA_FILE = {"file_name": "GLDS-1_array_expression.csv", "file_size": 11_000_000_000,
             "remote_url": "/geode-py/ws/studies/OSD-1/download?source=datamanager&file=GLDS-1_array_expression.csv",
             "date_updated": 1682567913.289, "restricted": False, "visible": True,
             "category": "Processed Data Files", "subcategory": "Normalized Data"}
BIO_INFO = {"httpLink": "https://ftp.ebi.ac.uk/biostudies/fire/S-BSST/S-BSST0-99/S-BSST7", "isPublic": True, "modified": 1488321383000}
BIO_FILE = {"path": "experiment/observations.tsv", "type": "file", "size": 200}
BIO_RECORD = {"accno": "S-BSST7", "attributes": [{"name": "License", "value": "CC0"}],
              "section": {"files": [BIO_FILE], "subsections": []}}
RODARE_FILE = {"key": "observations.csv", "size": 100, "bucket": BUCKET,
               "checksum": "md5:" + "a" * 32, "links": {"self": RODARE + "/api/files/" + BUCKET + "/observations.csv"}}
RODARE_RECORD = {"id": 2886, "updated": "2024-05-23T13:27:14Z", "files": [RODARE_FILE],
                 "metadata": {"access_right": "open", "resource_type": {"type": "dataset"}, "title": "Experiments",
                              "license": {"id": "CC-BY-4.0"}}}


def adapter(fetch):
    async def text(url, params=None, *, accept):
        assert accept == "application/vnd.zenodo.v1+json"
        return json.dumps(await fetch(url, params) if params is not None else await fetch(url))
    return ResearchOct22Adapters(fetch, text)


def candidate(source, external, metadata=None):
    return {"source_id": source, "external_id": external, "metadata": metadata or {}}


@pytest.mark.asyncio
async def test_nasa_search_is_own_repository_with_pagination():
    async def fetch(url, params):
        assert url == NASA + "/osdr/data/search"
        assert params == {"term": "plants", "type": "cgene", "from": 10, "size": 10}
        return {"hits": {"total": 12, "hits": [{"_source": {"Accession": "OSD-1", "Study Title": "Plants", "Study Description": "Experiment"}}]}}
    results = await adapter(fetch).search("nasa-osdr", "plants", 2)
    assert results["total_count"] == 12
    assert results["results"][0]["external_id"] == "OSD-1"


@pytest.mark.asyncio
async def test_nasa_full_manifest_preserves_large_original_files():
    rows = [{**NASA_FILE, "file_name": f"data{i}.csv", "remote_url": f"/geode-py/ws/studies/OSD-1/download?source=datamanager&file=data{i}.csv"} for i in range(150)]
    async def fetch(url):
        assert url == NASA + "/osdr/data/osd/files/1"
        return {"success": True, "valid_input": ["1"], "studies": {"OSD-1": {"file_count": len(rows), "study_files": rows}}}
    files = await adapter(fetch).resolve(candidate("nasa-osdr", "OSD-1"))
    assert len(files) == 150 and files[-1]["source_filename"] == "data149.csv"
    assert files[0]["size"] == 11_000_000_000 and files[0]["license"] is None
    assert files[0]["version"] == "1682567913.289"


@pytest.mark.parametrize("patch", [
    {"restricted": True}, {"restricted": None}, {"visible": False}, {"category": "Study Metadata Files"},
    {"subcategory": "Processing Info"}, {"file_name": "metadata.zip"}, {"file_name": "SampleTable.csv"},
    {"file_name": "analysis.py"}, {"remote_url": "/geode-py/ws/studies/OSD-2/download?source=datamanager&file=GLDS-1_array_expression.csv"},
    {"remote_url": "https://evil.example/data.csv"},
])
@pytest.mark.asyncio
async def test_nasa_restricted_metadata_and_foreign_records_are_filtered(patch):
    async def fetch(*args):
        return {"success": True, "valid_input": ["1"], "studies": {"OSD-1": {"file_count": 1, "study_files": [{**NASA_FILE, **patch}]}}}
    assert not await adapter(fetch).resolve(candidate("nasa-osdr", "OSD-1"))


@pytest.mark.asyncio
async def test_nasa_incomplete_manifest_is_not_reported_as_complete():
    async def fetch(*args):
        return {"success": True, "valid_input": ["1"], "studies": {"OSD-1": {"file_count": 200, "study_files": [NASA_FILE]}}}
    with pytest.raises(SourceConnectorError, match="incomplete_file_manifest"):
        await adapter(fetch).resolve(candidate("nasa-osdr", "OSD-1"))


def test_nasa_storage_redirect_is_limited_to_own_bucket_study_and_signed_parameters():
    url = "https://genelab-repo-prod.s3.amazonaws.com/genelab-data/GLDS-1/array/expression.csv?" + urlencode({"AWSAccessKeyId": "test", "Signature": "test", "Expires": "1792642681", "versionId": "abc"})
    assert safe_file_url("nasa-osdr", url, "OSD-1")
    assert not safe_file_url("nasa-osdr", url, "OSD-2")
    assert not safe_file_url("nasa-osdr", url.replace("genelab-repo-prod", "other-bucket"), "OSD-1")
    assert not safe_file_url("nasa-osdr", url + "&redirect=https://evil.example", "OSD-1")
    assert not safe_file_url("nasa-osdr", url + "&Signature=second", "OSD-1")


@pytest.mark.asyncio
async def test_biostudies_public_search_with_file_filter():
    async def fetch(url, params):
        assert url == BIO + "/api/v1/search" and params["files"] == "[1 TO *]" and params["page"] == 1
        row = {"accession": "S-BSST7", "title": "DNA copy number", "files": 3, "isPublic": True}
        return {"totalHits": 3, "hits": [row, {**row, "isPublic": False}, {**row, "files": 0}]}
    result = await adapter(fetch).search("ebi-biostudies", "copy number", 1)
    assert len(result["results"]) == 1 and result["total_count"] == 3


@pytest.mark.asyncio
async def test_biostudies_all_nested_file_groups_and_large_manifest():
    record = deepcopy(BIO_RECORD)
    files = [{**BIO_FILE, "path": f"data/part-{i}.tsv"} for i in range(150)]
    record["section"] = {"subsections": [[{"files": [files[:75]]}], {"files": files[75:]}]}
    async def fetch(url):return deepcopy(BIO_INFO if url.endswith("/info") else record)
    result = await adapter(fetch).resolve(candidate("ebi-biostudies", "S-BSST7"))
    assert len(result) == 150 and result[-1]["source_filename"] == "data/part-149.tsv"
    assert all(row["license"] == "CC0" and safe_file_url("ebi-biostudies", row["url"]) for row in result)


@pytest.mark.parametrize("patch", [{"isPublic": False}, {"httpLink": "https://evil.example/S-BSST7"},
                                  {"httpLink": BIO_INFO["httpLink"].replace("S-BSST7", "S-BSST8")}])
@pytest.mark.asyncio
async def test_biostudies_closed_or_foreign_storage_skips_detail_request(patch):
    async def fetch(url):
        assert url.endswith("/info")
        return {**BIO_INFO, **patch}
    assert not await adapter(fetch).resolve(candidate("ebi-biostudies", "S-BSST7"))


@pytest.mark.asyncio
async def test_biostudies_no_guessed_license_and_filters_code_document_paths():
    record = deepcopy(BIO_RECORD);record["attributes"] = []
    record["section"]["files"] += [{**BIO_FILE, "path": name} for name in ["readme.txt", "../private.csv", "analysis.py", "paper.pdf", "https://evil.example/data.csv"]]
    async def fetch(url):return deepcopy(BIO_INFO if url.endswith("/info") else record)
    rows = await adapter(fetch).resolve(candidate("ebi-biostudies", "S-BSST7"))
    assert len(rows) == 1 and rows[0]["license"] is None


@pytest.mark.asyncio
async def test_rodare_search_filters_public_datasets_and_pins_version():
    async def fetch(url, params):
        assert url == RODARE + "/api/records/" and params["page"] == 2 and "access_right:open" in params["q"]
        row = deepcopy(RODARE_RECORD); private = deepcopy(row);private["metadata"]["access_right"] = "restricted"
        return {"hits": {"total": 12, "hits": [row, private]}}
    result = await adapter(fetch).search("rodare", "physics", 2)
    assert len(result["results"]) == 1 and result["results"][0]["metadata"]["version"] == RODARE_RECORD["updated"]


@pytest.mark.asyncio
async def test_rodare_complete_manifest_md5_and_source_record_version():
    record = deepcopy(RODARE_RECORD)
    record["files"] = [{**RODARE_FILE, "key": f"file-{i}.csv", "links": {"self": RODARE + f"/api/files/{BUCKET}/file-{i}.csv"}} for i in range(150)]
    async def fetch(url):
        assert url == RODARE + "/api/records/2886"
        return record
    client = adapter(fetch)
    rows = await client.resolve(candidate("rodare", "2886", {"version": record["updated"]}))
    assert len(rows) == 150 and rows[-1]["source_filename"] == "file-149.csv"
    assert rows[0]["checksum"] == RODARE_FILE["checksum"] and rows[0]["license"] == "CC-BY-4.0"
    with pytest.raises(SourceConnectorError, match="inconsistent_source_snapshot"):
        await client.resolve(candidate("rodare", "2886", {"version": "old"}))


@pytest.mark.parametrize("patch", [{"access_right": "restricted"}, {"access_right": "closed"}, {"resource_type": {"type": "software"}}])
@pytest.mark.asyncio
async def test_rodare_closed_or_non_data_record_cannot_resolve(patch):
    record = deepcopy(RODARE_RECORD);record["metadata"].update(patch)
    async def fetch(*args):return record
    assert not await adapter(fetch).resolve(candidate("rodare", "2886"))


@pytest.mark.parametrize("url", [
    "https://127.0.0.1/data.csv", "http://rodare.hzdr.de/api/files/" + BUCKET + "/data.csv",
    RODARE + "/api/files/" + BUCKET + "/%2e%2e/private.csv", RODARE + "/api/files/" + BUCKET + "/data.csv#x",
    RODARE + "/api/files/" + BUCKET + "/data.csv?download=https://evil.example",
    RODARE + "/api/files/" + BUCKET + "/data%252fprivate.csv", "https://user@rodare.hzdr.de/api/files/" + BUCKET + "/data.csv",
])
def test_reject_unsafe_urls(url):
    for source in ("nasa-osdr", "ebi-biostudies", "rodare"):
        assert not safe_file_url(source, url)


@pytest.mark.asyncio
async def test_unknown_source_and_invalid_identity_never_fetch():
    async def fetch(*args):raise AssertionError("invalid input must not fetch")
    a = adapter(fetch)
    for source, external in [("bad", "1"), ("nasa-osdr", "../1"), ("ebi-biostudies", "https://evil.example"), ("rodare", "1/2")]:
        with pytest.raises(SourceConnectorError):await a.resolve(candidate(source, external))
