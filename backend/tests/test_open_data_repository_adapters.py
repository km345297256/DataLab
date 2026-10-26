"""Anonymous public repository contracts and fixed-version original files."""
import pytest

from app.domain.external.data_source import SourceConnectorError
from app.domain.models.data_candidate import CandidateInput
from app.infrastructure.external.data_sources.open_data_repository_adapters import (
    DATAVERSE, FIGSHARE_API, RepositoryAdapters, safe_file_url,
)


MD5 = "127a8dc5e7cb5185a5b269685d78a52c"
PID = "doi:10.7910/DVN/WZBHHR"


def figshare_record():
    return {"id": 123, "version": 2, "title": "Iris data", "doi": "10.6084/m9.figshare.123.v2",
            "defined_type": 3, "license": {"name": "CC BY 4.0"}, "files": [{"id": 456, "name": "iris.csv", "size": 300,
            "is_link_only": False, "download_url": "https://figshare.com/ndownloader/files/456",
            "computed_md5": MD5}]}


def dataverse_record():
    return {"status": "OK", "data": {"protocol": "doi", "authority": "10.7910", "identifier": "DVN/WZBHHR",
            "latestVersion": {"datasetPersistentId": PID, "versionNumber": 1, "versionMinorNumber": 0,
            "versionState": "RELEASED", "license": {"name": "CC0 1.0"}, "files": [{"restricted": False, "dataFile": {
                "id": 4369253, "filename": "IrisColor_Data.tab", "filesize": 130795,
                "contentType": "text/tab-separated-values", "originalFileName": "IrisColor_Data.xlsx",
                "originalFileSize": 82570,
                "originalFileFormat": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "checksum": {"type": "MD5", "value": MD5}}}]}}}


def dataverse_search_row():
    return {"name": "Iris Data", "global_id": PID, "type": "dataset", "versionState": "RELEASED",
            "description": "<p>Observed iris colours</p>", "majorVersion": 1, "minorVersion": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("source,count", [("figshare", 101), ("harvard-dataverse", 10001)])
async def test_complete_published_manifest_has_no_internal_file_count_cap(source, count):
    from copy import deepcopy
    payload = figshare_record() if source == "figshare" else dataverse_record()
    if source == "figshare":
        template = payload["files"][0]
        payload["files"] = [{**template, "id": index + 1, "name": f"part-{index}.csv",
                             "download_url": f"https://figshare.com/ndownloader/files/{index + 1}"} for index in range(count)]
        candidate = {"source_id": source, "external_id": "123", "metadata": {"version": 2}}
    else:
        template = payload["data"]["latestVersion"]["files"][0]
        files = []
        for index in range(count):
            item = deepcopy(template)
            item["dataFile"].update(id=index + 1, originalFileName=f"part-{index}.xlsx")
            files.append(item)
        payload["data"]["latestVersion"]["files"] = files
        candidate = {"source_id": source, "external_id": PID, "metadata": {"version": "1.0"}}
    async def fetch(*args, **kwargs):
        return payload
    options = await RepositoryAdapters(fetch).resolve(candidate)
    assert len(options) == count and len({option["id"] for option in options}) == count
    assert options[-1]["source_filename"].startswith(f"part-{count - 1}.")


@pytest.mark.asyncio
async def test_figshare_search_uses_public_dataset_api_and_page_hint():
    calls = []

    async def fetch(url, params=None, **kwargs):
        calls.append((url, params, kwargs))
        return [figshare_record() for _ in range(10)]

    result = await RepositoryAdapters(fetch).search("figshare", "iris", 2)
    assert calls == [(FIGSHARE_API + "/articles/search", None, {"method": "POST", "json": {
        "search_for": "iris", "item_type": 3, "page": 2, "page_size": 10}})]
    assert result["total_count"] == 21
    assert result["results"][0]["metadata"] == {"doi": "10.6084/m9.figshare.123.v2", "version": 2, "published": None}
    assert result["results"][0]["source_url"] == "https://doi.org/10.6084/m9.figshare.123.v2"


@pytest.mark.asyncio
async def test_dataverse_search_scopes_published_datasets_and_keeps_version():
    calls = []

    async def fetch(url, params=None, **kwargs):
        calls.append((url, params))
        return {"status": "OK", "data": {"items": [dataverse_search_row()], "total_count": 17}}

    result = await RepositoryAdapters(fetch).search("harvard-dataverse", "iris", 2)
    assert calls[0][1] == {"q": "iris", "type": "dataset",
                           "fq": ["publicationStatus:Published", 'dsPersistentId:"doi:10.7910/DVN/"'],
                           "per_page": 10, "start": 10, "sort": "score"}
    assert result["total_count"] == 17
    candidate = result["results"][0]
    assert candidate["metadata"]["version"] == "1.0"
    assert candidate["metadata"]["doi"] == "10.7910/dvn/wzbhhr"
    assert candidate["description"] == "Observed iris colours"
    assert candidate["source_url"] == "https://doi.org/10.7910/DVN/WZBHHR"
    assert CandidateInput.public_source(candidate["source_url"]) == candidate["source_url"]


@pytest.mark.asyncio
@pytest.mark.parametrize("query,expected", [
    ("human iris color data", "human AND iris AND color AND data"),
    ('"human iris color data"', '"human iris color data"'),
    ('human "iris color" data', 'human AND "iris color" AND data'),
    ("iris OR retina", "iris OR retina"),
    ("iris AND color", "iris AND color"),
    ("iris NOT plant", "iris NOT plant"),
    ("iris", "iris"),
    ('title:"human iris" data', 'title:"human iris" data'),
    ("(iris OR retina) data", "(iris OR retina) data"),
    ('"iris color', '"iris color'),
])
async def test_dataverse_topic_terms_use_and_without_rewriting_explicit_query(query, expected):
    async def fetch(url, params=None, **kwargs):
        assert params["q"] == expected
        return {"status": "OK", "data": {"items": [], "total_count": 0}}

    await RepositoryAdapters(fetch).search("harvard-dataverse", query, 1)


@pytest.mark.asyncio
async def test_dataverse_harvested_external_records_do_not_break_local_results():
    external = {**dataverse_search_row(), "global_id": "doi:10.3886/ICPSR38259.V1"}

    async def fetch(url, params=None, **kwargs):
        return {"status": "OK", "data": {"items": [external, dataverse_search_row()], "total_count": 2}}

    result = await RepositoryAdapters(fetch).search("harvard-dataverse", "iris", 1)
    assert len(result["results"]) == 1
    assert result["results"][0]["external_id"] == PID


@pytest.mark.asyncio
async def test_figshare_resolve_refetches_manifest_uses_stable_endpoint_and_fixed_version():
    calls = []

    async def fetch(url, params=None, **kwargs):
        calls.append(url)
        return figshare_record()

    candidate = {"source_id": "figshare", "external_id": "123", "metadata": {"version": 2},
                 "source_url": "http://localhost/private", "url": "http://localhost/private"}
    option, = await RepositoryAdapters(fetch).resolve(candidate)
    assert calls == [FIGSHARE_API + "/articles/123/versions/2"]
    assert option["url"] == "https://ndownloader.figshare.com/files/456"
    assert option["checksum"] == "md5:" + MD5
    assert option["source_filename"] == "iris.csv" and option["available"]
    assert option["record_url"] == "https://doi.org/10.6084/m9.figshare.123.v2"
    assert option["license"] == "CC BY 4.0"


@pytest.mark.asyncio
async def test_dataverse_downloads_original_with_its_size_and_checksum():
    async def fetch(url, params=None, **kwargs):
        assert url == DATAVERSE + "/api/datasets/:persistentId/"
        assert params == {"persistentId": PID}
        return dataverse_record()

    candidate = {"source_id": "harvard-dataverse", "external_id": PID, "metadata": {"version": "1.0"}}
    option, = await RepositoryAdapters(fetch).resolve(candidate)
    assert option["source_filename"] == "IrisColor_Data.xlsx"
    assert option["size"] == 82570
    assert option["content_type"].endswith("spreadsheetml.sheet")
    assert option["url"] == DATAVERSE + "/api/access/datafile/4369253?format=original"
    assert option["checksum"] == "md5:" + MD5
    assert option["record_url"] == "https://doi.org/10.7910/DVN/WZBHHR"
    assert CandidateInput.public_source(option["record_url"]) == option["record_url"]
    assert option["license"] == "CC0 1.0"


@pytest.mark.asyncio
async def test_dataverse_pinned_version_is_not_replaced_by_new_latest():
    original = dataverse_record()["data"]["latestVersion"]
    current = dataverse_record()
    current["data"]["latestVersion"]["versionNumber"] = 2
    current["data"]["latestVersion"]["files"][0]["dataFile"]["id"] = 999
    calls = []

    async def fetch(url, params=None, **kwargs):
        calls.append(url)
        return {"status": "OK", "data": original} if url.endswith("/versions/1.0") else current

    option, = await RepositoryAdapters(fetch).resolve({
        "source_id": "harvard-dataverse", "external_id": PID, "metadata": {"version": "1.0"}})
    assert calls[-1].endswith("/versions/1.0")
    assert "/4369253?" in option["url"]


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["is_confidential", "is_embargoed", "is_metadata_record"])
async def test_figshare_restricted_or_metadata_record_has_no_files(flag):
    record = figshare_record()
    record[flag] = True

    async def fetch(*args, **kwargs):
        return record

    assert await RepositoryAdapters(fetch).resolve({"source_id": "figshare", "external_id": "123"}) == []


@pytest.mark.asyncio
async def test_figshare_link_only_file_does_not_become_a_data_download():
    record = figshare_record()
    record["files"][0].update(is_link_only=True, download_url="https://unregistered.example/article")

    async def fetch(*args, **kwargs):
        return record

    assert await RepositoryAdapters(fetch).resolve({"source_id": "figshare", "external_id": "123"}) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["http://127.0.0.1/data.csv", "https://evil.example/data.csv",
                                  "https://ndownloader.figshare.com/files/999"])
async def test_figshare_rejects_manifest_file_origin_or_id_mismatch(url):
    record = figshare_record()
    record["files"][0]["download_url"] = url

    async def fetch(*args, **kwargs):
        return record

    with pytest.raises(SourceConnectorError, match="unsafe_download_url"):
        await RepositoryAdapters(fetch).resolve({"source_id": "figshare", "external_id": "123"})


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["guestbook", "restricted", "embargo", "draft"])
async def test_dataverse_access_requirements_are_not_bypassed(condition):
    record = dataverse_record()
    version = record["data"]["latestVersion"]
    if condition == "guestbook":
        record["data"]["guestbookId"] = 12
    elif condition == "restricted":
        version["files"][0]["restricted"] = True
    elif condition == "embargo":
        version["files"][0]["dataFile"]["embargo"] = {"dateAvailable": "2030-01-01"}
    else:
        version["versionState"] = "DRAFT"

    async def fetch(*args, **kwargs):
        return record

    assert await RepositoryAdapters(fetch).resolve({"source_id": "harvard-dataverse", "external_id": PID}) == []


@pytest.mark.asyncio
async def test_dataverse_unknown_original_size_does_not_use_transformed_size():
    record = dataverse_record()
    del record["data"]["latestVersion"]["files"][0]["dataFile"]["originalFileSize"]

    async def fetch(*args, **kwargs):
        return record

    option, = await RepositoryAdapters(fetch).resolve({"source_id": "harvard-dataverse", "external_id": PID})
    assert option["size"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("source,payload", [("figshare", {}), ("figshare", [None]),
                                          ("figshare", [figshare_record()] * 11),
                                          ("harvard-dataverse", {"status": "ERROR"}),
                                          ("harvard-dataverse", {"status": "OK", "data": {"items": [], "total_count": -1}})])
async def test_invalid_search_responses_are_classified(source, payload):
    async def fetch(*args, **kwargs):
        return payload

    with pytest.raises(SourceConnectorError, match="invalid_response"):
        await RepositoryAdapters(fetch).search(source, "iris", 1)


@pytest.mark.asyncio
async def test_dataverse_manifest_cannot_change_dataset_or_pin_version():
    record = dataverse_record()
    record["data"]["identifier"] = "DVN/ATTACK"

    async def fetch(*args, **kwargs):
        return record

    with pytest.raises(SourceConnectorError, match="invalid_candidate"):
        await RepositoryAdapters(fetch).resolve({"source_id": "harvard-dataverse", "external_id": PID})


@pytest.mark.asyncio
@pytest.mark.parametrize("external", ["http://127.0.0.1", "doi:10.7910/DVN/../admin", "doi:10.5281/zenodo.123"])
async def test_unregistered_dataverse_identifiers_never_make_network_requests(external):
    async def fetch(*args, **kwargs):
        pytest.fail("invalid identifiers must not make requests")

    with pytest.raises(SourceConnectorError, match="invalid_candidate"):
        await RepositoryAdapters(fetch).resolve({"source_id": "harvard-dataverse", "external_id": external})


@pytest.mark.parametrize("source,url", [
    ("figshare", "https://ndownloader.figshare.com/files/456"),
    ("figshare", "https://s3-eu-west-1.amazonaws.com/pfigshare-u-files/456/data.csv?X-Amz-Signature=opaque"),
    ("figshare", "https://s3.eu-west-1.amazonaws.com/pstorage-digitalscience-874864551/456/data.csv?X-Amz-Signature=opaque"),
    ("harvard-dataverse", DATAVERSE + "/api/access/datafile/4369253?format=original"),
    ("harvard-dataverse", "https://dvn-cloud-iqss.s3.amazonaws.com/10.7910/DVN/WZBHHR/file.orig?X-Amz-Signature=opaque"),
])
def test_only_provider_file_endpoints_and_exact_buckets_are_allowed(source, url):
    assert safe_file_url(source, url)


@pytest.mark.parametrize("source,url", [
    ("figshare", "https://ndownloader.figshare.com.evil.example/files/456"),
    ("figshare", "https://s3-eu-west-1.amazonaws.com/untrusted-bucket/456/data.csv"),
    ("figshare", "https://s3-eu-west-1.amazonaws.com/pfigshare-u-files/../456/data.csv"),
    ("figshare", "https://s3-eu-west-1.amazonaws.com/pfigshare-u-files/456/data%0A.csv"),
    ("figshare", "https://ndownloader.figshare.com/files/456?redirect=http://localhost"),
    ("figshare", "https://user:password@ndownloader.figshare.com/files/456"),
    ("figshare", "https://ndownloader.figshare.com:444/files/456"),
    ("harvard-dataverse", DATAVERSE + "/api/access/datafile/4369253?format=subset"),
    ("harvard-dataverse", "https://dvn-cloud-iqss.s3.amazonaws.com/private/key"),
    ("harvard-dataverse", "https://unregistered.s3.amazonaws.com/10.7910/DVN/WZBHHR/file"),
    ("harvard-dataverse", "https://dvn-cloud-iqss.s3.amazonaws.com/10.7910/DVN/WZBHHR/%2e%2e"),
    ("harvard-dataverse", "http://169.254.169.254/latest/meta-data"),
])
def test_file_url_policy_rejects_wrong_buckets_paths_credentials_and_transforms(source, url):
    assert not safe_file_url(source, url)
