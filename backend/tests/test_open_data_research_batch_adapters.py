"""Contracts across independently hosted Dataverse repositories."""
from copy import deepcopy
import pytest

from app.domain.external.data_source import SourceConnectorError
from app.domain.models.open_data_research_batch_definitions import DEFINITIONS, REPOSITORY_CONFIG
from app.infrastructure.external.data_sources.open_data_research_batch_adapters import ResearchBatchAdapters, SOURCES, safe_file_url

EXAMPLES = {'dataverse-no': 'doi:10.18710/Q39V7O', 'dans-ssh': 'doi:10.17026/DANS-29R-WYYR', 'dans-lifesciences': 'doi:10.17026/LS/EGLNWR', 'dans-physical': 'doi:10.17026/DANS-ZVQ-DQ4W', 'dans-archaeology': 'doi:10.17026/AR/VRLKYQ', 'aussda': 'doi:10.11587/KZIQ4A', 'gro-data': 'doi:10.25625/0ADDBM', 'darus': 'doi:10.18419/DARUS-2040', 'cifor-icraf': 'doi:10.17528/CIFOR/DATA.00314', 'ucla-dataverse': 'doi:10.25346/S66YI6KV', 'unimi-dataverse': 'doi:10.13130/RD_UNIMI/YMWBMU', 'polen-datahub': 'doi:10.57979/8AFSNH', 'uminho-datarepositorium': 'doi:10.34622/DATAREPOSITORIUM/TFICCJ', 'bsc-dataverse': 'doi:10.82201/TE7JWA', 'ird-datasuds': 'doi:10.23708/SNGQHV'}
EXAMPLES.update({'kuleuven-rdr': 'doi:10.48804/QQU5VO', 'cuhk-data': 'doi:10.48668/9AKTCT', 'hkust-dataspace': 'doi:10.14711/dataset/KURDVK', 'heidata': 'doi:10.11588/DATA/AAKAF9', 'edmond': 'doi:10.17617/3.Q2GHPJ'})

MD5 = "1234567890abcdef1234567890abcdef"


def manifest(source):
    doi = EXAMPLES[source][4:]
    authority, identifier = doi.split("/", 1)
    return {"status": "OK", "data": {"protocol": "doi", "authority": authority, "identifier": identifier,
        "latestVersion": {"datasetPersistentId": EXAMPLES[source], "versionNumber": 2, "versionMinorNumber": 1,
            "versionState": "RELEASED", "license": {"name": "CC BY 4.0"}, "files": [{"restricted": False,
                "dataFile": {"id": 123, "filename": "measurements.tab", "filesize": 110,
                    "originalFileName": "measurements.xlsx", "originalFileSize": 200,
                    "originalFileFormat": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    "checksum": {"type": "MD5", "value": MD5}}}]}}}


@pytest.mark.asyncio
@pytest.mark.parametrize("source", sorted(SOURCES))
async def test_each_repository_retains_complete_published_file_manifest(source):
    payload = manifest(source)
    template = payload["data"]["latestVersion"]["files"][0]
    files = [deepcopy(template) for _ in range(101)]
    for index, item in enumerate(files):
        item["dataFile"].update(id=index + 1, originalFileName=f"part-{index}.xlsx")
    payload["data"]["latestVersion"]["files"] = files
    async def fetch(*args, **kwargs):
        return payload
    options = await ResearchBatchAdapters(fetch).resolve({"source_id": source, "external_id": EXAMPLES[source]})
    assert len(options) == 101 and options[-1]["source_filename"] == "part-100.xlsx"


@pytest.mark.parametrize("source", sorted(SOURCES))
@pytest.mark.asyncio
async def test_all_hosts_resolve_original_file_with_pinned_manifest(source):
    calls = []
    async def fetch(url, params=None):
        calls.append((url,params))
        return manifest(source)
    option, = await ResearchBatchAdapters(fetch).resolve({"source_id": source, "external_id": EXAMPLES[source], "metadata": {"version":"2.1"}})
    assert calls == [(REPOSITORY_CONFIG[source]["base_url"] + "/api/datasets/:persistentId/",
                      {"persistentId": EXAMPLES[source] if source == "hkust-dataspace" else "doi:" + EXAMPLES[source][4:].upper()})]
    assert option["source_id"] == source and option["version"] == "2.1"
    assert option["source_filename"] == "measurements.xlsx" and option["size"] == 200
    assert option["checksum"] == "md5:" + MD5 and option["license"] == "CC BY 4.0"
    assert option.get("probe_without_range", False) is (source == "cuhk-data")
    assert safe_file_url(source,option["url"])
    assert all(not safe_file_url(other,option["url"]) for other in SOURCES if other != source)


@pytest.mark.parametrize("source", sorted(SOURCES))
@pytest.mark.asyncio
async def test_all_searches_scope_the_repository_and_skip_harvested_records(source):
    async def fetch(url,params=None):
        assert url == REPOSITORY_CONFIG[source]["base_url"] + "/api/search"
        assert params["q"] == "water AND temperature" and params["start"] == 10
        row = {"global_id":EXAMPLES[source],"type":"dataset","versionState":"RELEASED","majorVersion":2,"minorVersion":1,"name":"Observations"}
        return {"status":"OK","data":{"items":[row,{**row,"global_id":"doi:10.99999/FOREIGN"}],"total_count":33}}
    result=await ResearchBatchAdapters(fetch).search(source,"water temperature",2)
    assert len(result["results"]) == 1 and result["total_count"] == 33
    assert result["results"][0]["metadata"]["version"] == "2.1"


@pytest.mark.parametrize("case", ["restricted","embargo","guestbook","draft","foreign"])
@pytest.mark.asyncio
async def test_access_and_identity_are_not_inferred_from_search(case):
    data=manifest("dataverse-no")
    version=data["data"]["latestVersion"]
    if case == "restricted":version["files"][0]["restricted"]=True
    elif case == "embargo":version["files"][0]["dataFile"]["embargo"]={"endDate":"2099-01-01"}
    elif case == "guestbook":data["data"]["guestbookId"]=2
    elif case == "draft":version["versionState"]="DRAFT"
    else:data["data"]["identifier"]="FOREIGN"
    async def fetch(*args):return data
    if case == "foreign":
        with pytest.raises(SourceConnectorError):await ResearchBatchAdapters(fetch).resolve({"source_id":"dataverse-no","external_id":EXAMPLES["dataverse-no"]})
    else:
        assert not await ResearchBatchAdapters(fetch).resolve({"source_id":"dataverse-no","external_id":EXAMPLES["dataverse-no"]})


@pytest.mark.parametrize("filename", ["README.csv","measurements_headers.csv","codebook.xlsx","data_dictionary.csv","10001_vi_en.tab"])
@pytest.mark.asyncio
async def test_variable_metadata_is_not_entity_data(filename):
    data=manifest("aussda");data["data"]["latestVersion"]["files"][0]["dataFile"]["originalFileName"]=filename
    async def fetch(*args):return data
    assert not await ResearchBatchAdapters(fetch).resolve({"source_id":"aussda","external_id":EXAMPLES["aussda"]})


@pytest.mark.asyncio
async def test_fixed_versions_and_large_original_files():
    old=manifest("gro-data")["data"]["latestVersion"]
    old["files"][0]["dataFile"]["originalFileSize"]=500*1024**3
    latest=manifest("gro-data");latest["data"]["latestVersion"]["versionNumber"]=3
    async def fetch(url,params=None):return {"status":"OK","data":old} if url.endswith("/versions/2.1") else latest
    option,=await ResearchBatchAdapters(fetch).resolve({"source_id":"gro-data","external_id":EXAMPLES["gro-data"],"metadata":{"version":"2.1"}})
    assert option["size"]==500*1024**3 and option["available"] and option["version"]=="2.1"


@pytest.mark.parametrize("url",[
    "http://dataverse.no/api/access/datafile/123?format=original",
    "https://dataverse.no.evil.example/api/access/datafile/123?format=original",
    "https://x:y@dataverse.no/api/access/datafile/123?format=original",
    "https://dataverse.no:444/api/access/datafile/123?format=original",
    "https://dataverse.no/api/access/datafile/123?format=original&key=secret",
    "https://dataverse.no/api/access/datafile/%2e%2e/private?format=original",
    "https://dataverse.no/api/access/datafile/123?format=original#fragment",
    "https://uit-dataverseno-prod01.s3-oslo.educloud.no/private/secret",
    "https://uit-dataverseno-prod01.s3-oslo.educloud.no/10.18710/Q39V7O/../secret",
])
def test_exact_download_routes_only(url):
    assert not safe_file_url("dataverse-no",url)


@pytest.mark.parametrize("source,url",[
    ("dataverse-no","https://uit-dataverseno-prod01.s3-oslo.educloud.no/10.18710/Q39V7O/16557-123.orig?signature=x"),
    ("dans-ssh","https://objectstore.surf.nl/store/10.17026/dans-29r-wyyr/186ea-123.orig?signature=x"),
    ("dans-lifesciences","https://objectstore.surf.nl/store/10.17026/LS/EGLNWR/1928f-123.orig?signature=x"),
    ("darus","https://s3.tik.uni-stuttgart.de/fokus-dv-prod-1/10.18419/darus-2040/17ae3-123.orig?signature=x"),
    ("polen-datahub","https://stor-004.a.acnca.pt/repositorio-polen/10.57979/8AFSNH/19625-123.orig?signature=x"),
    ("kuleuven-rdr","https://rdmo.icts.kuleuven.be/dataverse/10.48804/QQU5VO/17fb1528471-4616415478e8?signature=x"),
    ("edmond","https://edmond-objstor-prod.s3.gwdg.de/10.17617/3.Q2GHPJ/19f6b9304c7-c764ab65944e?signature=x"),
])
def test_observed_named_storage_is_allowed(source,url):
    assert safe_file_url(source,url)
    assert not safe_file_url(source,url.replace("https://","https://evil.example/"))


def test_definitions_match_implemented_sources_without_duplicate_institution_claim():
    assert {item["id"] for item in DEFINITIONS}==SOURCES
    assert sum(config["institution"]=="DANS" for config in REPOSITORY_CONFIG.values())==4
    assert all(not item["doi_prefixes"] for item in DEFINITIONS if item["id"].startswith("dans-"))


def test_connector_network_declarations_include_named_storage_hosts():
    for definition in DEFINITIONS:
        assert all(host in definition["domains"] for host, _ in REPOSITORY_CONFIG[definition["id"]]["storage"])


@pytest.mark.asyncio
async def test_hkust_resolves_case_insensitive_doi_with_provider_canonical_namespace():
    async def fetch(url, params=None):
        assert params["persistentId"] == "doi:10.14711/dataset/KURDVK"
        return manifest("hkust-dataspace")
    option, = await ResearchBatchAdapters(fetch).resolve({
        "source_id": "hkust-dataspace", "external_id": "doi:10.14711/DATASET/kurdvk"})
    assert option["external_id"] == "doi:10.14711/dataset/KURDVK"
