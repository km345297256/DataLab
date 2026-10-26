import pytest
import httpx

from app.domain.external.data_source import SourceConnectorError
from app.infrastructure.external.data_sources.open_data_regional_expansion_adapters import RegionalExpansionAdapters, safe_file_url, is_sdg_observation_sample
from app.infrastructure.external.data_sources.open_data_sources import OpenDataSources

MACAU_ID = "283a0991-8a41-4c5d-97b3-f3acc1c585a2"
MACAU_URL = f"https://api.data.gov.mo/datadir/downloadSingleFile?fileId=2529&dataDirId={MACAU_ID}"
SDG_URL = "https://unstats.un.org/sdgapi/v1/sdg/Series/EG_ACS_ELEC/GeoArea/156/DataSlice"


def macau():
    return {"code": 0, "data": {"status": None, "title": "人口統計", "info": {"openType": 0, "updateTime": "2026-01-01"},
                               "file": [{"fileId": 2529, "fileName": "I.1.xlsx", "suffix": "xlsx"}]}}


@pytest.mark.parametrize("source,url", [("macau-open-data", MACAU_URL), ("un-sdg", SDG_URL)])
def test_entity_routes_are_allowed(source, url):
    assert safe_file_url(source, url)


@pytest.mark.parametrize("url", [
    MACAU_URL.replace("https:", "http:"), MACAU_URL.replace("api.data.gov.mo", "api.data.gov.mo.evil.test"),
    MACAU_URL.replace("https://", "https://user:pass@"), MACAU_URL + "#fragment", MACAU_URL + "&token=secret",
    MACAU_URL + "&fileId=2529", MACAU_URL.replace("fileId=2529", "fileId=-1"), MACAU_URL.replace("fileId=2529", "fileId=0"),
    MACAU_URL.replace("/datadir/", "/datadir/../"), MACAU_URL.replace("/datadir/", "/datadir/%2e%2e/"),
    MACAU_URL.replace("api.data.gov.mo", "127.0.0.1"), MACAU_URL.replace("api.data.gov.mo", "api.data.gov.mo:444"),
    MACAU_URL.replace("dataDirId=", "redirect="), "https://api.data.gov.mo/datadir/search/download",
])
def test_macau_rejects_untrusted_or_authenticated_downloads(url):
    assert not safe_file_url("macau-open-data", url)


@pytest.mark.parametrize("url", [
    SDG_URL + "?page=1", SDG_URL.replace("DataSlice", "Dimensions"), SDG_URL.replace("unstats.un.org", "unstats.un.org.evil.test"),
    SDG_URL.replace("/156/", "/0/"), SDG_URL.replace("/156/", "/%31%35%36/"), SDG_URL + "#fragment",
])
def test_sdg_only_complete_official_geography_slices(url):
    assert not safe_file_url("un-sdg", url)


@pytest.mark.asyncio
async def test_macau_search_excludes_registration_and_metadata_only():
    async def fetch(url, params=None, **kwargs):
        assert url == "https://api.data.gov.mo/datadir/search"
        assert kwargs["method"] == "POST"
        assert kwargs["json"]["page"] == 2 and kwargs["json"]["rows"] == 10
        assert kwargs["json"]["key"] == "人口"
        row = {"id": MACAU_ID, "openType": 0, "isFile": 1, "name": "人口☯人口简体☯Population", "dataFormat": "CSV"}
        return {"code": 0, "data": {"total": 4, "items": [row, dict(row, openType=1), dict(row, isFile=0), dict(row, dataFormat="PDF")]}}
    page = await RegionalExpansionAdapters(fetch).search("macau-open-data", '"人口"', 2)
    assert len(page["results"]) == 1
    assert page["results"][0]["title"] == "人口"
    assert page["total_count"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("open_type,status", [(1, None), (1, 1), (0, 1), (0, 2), (None, None), (False, None)])
async def test_fresh_manifest_excludes_any_registration_state(open_type, status):
    async def fetch(url, params=None, **kwargs):
        data = macau()
        data["data"]["info"]["openType"] = open_type
        data["data"]["status"] = status
        return data
    assert await RegionalExpansionAdapters(fetch).resolve({"source_id": "macau-open-data", "external_id": MACAU_ID}) == []


@pytest.mark.asyncio
async def test_macau_preserves_fresh_real_format_and_ignores_candidate_urls():
    async def fetch(url, params=None, **kwargs):
        assert url == "https://api.data.gov.mo/datadir/detail/" + MACAU_ID
        data = macau()
        data["data"]["file"] += [{"fileId": 2, "fileName": "README.csv"}, {"fileId": 3, "fileName": "info.pdf"},
                                  {"fileId": 4, "fileName": "../secret.csv"}]
        return data
    candidate = {"source_id": "macau-open-data", "external_id": MACAU_ID,
                 "formats": ["CSV"], "source_url": "https://evil.test/", "metadata": {"url": "https://evil.test/"}}
    options = await RegionalExpansionAdapters(fetch).resolve(candidate)
    assert len(options) == 1
    assert options[0]["url"] == MACAU_URL and options[0]["source_filename"] == "I.1.xlsx"
    assert options[0]["size"] is None and options[0]["license"] == "https://data.gov.mo/UseClause"


def sdg():
    return [{"code": "EG_ACS_ELEC", "description": "Population with access to electricity", "indicator": ["7.1.1"], "release": "2026.Q3"},
            {"code": "SE_ACS_ELECT", "description": "Schools with access to electricity", "indicator": ["4.a.1"], "release": "2026.Q3"}]


@pytest.mark.asyncio
async def test_sdg_matches_all_terms_or_code_and_returns_named_whole_geography_files():
    calls = []
    async def fetch(url, params=None, **kwargs):
        calls.append((url, params))
        if url.endswith("/Series/List"):
            assert params == {"allreleases": "false"}
            return sdg()
        assert url.endswith("/Series/EG_ACS_ELEC/GeoAreas")
        return [{"geoAreaCode": "156", "geoAreaName": "China"}, {"geoAreaCode": "1", "geoAreaName": "World"},
                {"geoAreaCode": "156", "geoAreaName": "duplicate"}, {"geoAreaCode": "../", "geoAreaName": "bad"}]
    adapter = RegionalExpansionAdapters(fetch)
    page = await adapter.search("un-sdg", "population electricity", 1)
    assert [row["external_id"] for row in page["results"]] == ["EG_ACS_ELEC:1"]
    assert len((await adapter.search("un-sdg", "EG_ACS_ELEC", 1))["results"]) == 1
    world = await adapter.resolve(page["results"][0])
    assert len(world) == 1 and "World" in world[0]["filename"]
    china = await adapter.search("un-sdg", "EG_ACS_ELEC geo=156", 1)
    assert china["results"][0]["external_id"] == "EG_ACS_ELEC:156"
    options = await adapter.resolve(china["results"][0])
    assert len(options) == 1 and options[0]["url"] == SDG_URL
    assert "China" in options[0]["kind"] and options[0]["license"] is None
    assert not any("pageSize" in str(p) or "timePeriod" in str(p) for _, p in calls)


@pytest.mark.asyncio
async def test_sdg_removed_series_has_no_download_and_modified_release_changes_identity():
    release = "2026.Q3"
    async def fetch(url, params=None, **kwargs):
        if url.endswith("/Series/List"):
            return [dict(sdg()[0], release=release)] if release else []
        return [{"geoAreaCode": "156", "geoAreaName": "China"}]
    adapter = RegionalExpansionAdapters(fetch)
    candidate = {"source_id": "un-sdg", "external_id": "EG_ACS_ELEC:156"}
    first = (await adapter.resolve(candidate))[0]
    release = "2026.Q4"
    assert (await adapter.resolve(candidate))[0]["id"] != first["id"]
    release = None
    assert await adapter.resolve(candidate) == []


@pytest.mark.asyncio
async def test_macau_source_error_does_not_become_an_empty_success():
    async def fetch(*args, **kwargs):
        return {"code": 12, "data": {}}
    with pytest.raises(SourceConnectorError):
        await RegionalExpansionAdapters(fetch).resolve({"source_id": "macau-open-data", "external_id": MACAU_ID})


@pytest.mark.parametrize("complete", [True, False])
def test_sdg_empty_or_wrong_geography_is_not_entity_data(complete):
    assert not is_sdg_observation_sample(b'{"series":"EG_ACS_ELEC","geoAreaCode":156,"geoAreaName":"China","dimensions":[]}', "EG_ACS_ELEC:156", complete=complete)
    assert not is_sdg_observation_sample(b'{"series":"EG_ACS_ELEC","geoAreaCode":1,"dimensions":[{"timePeriodStart":"2000","value":"99"}]}', "EG_ACS_ELEC:156", complete=complete)


def test_sdg_accepts_only_read_complete_observations_in_prefix():
    prefix = b'{"series":"EG_ACS_ELEC","geoAreaCode":156,"geoAreaName":"China","dimensions":['
    assert not is_sdg_observation_sample(prefix + b'{"timePeriodStart":"2000","value":"9', "EG_ACS_ELEC:156", complete=False)
    body = prefix + b'{"timePeriodStart":"2000","value":"99"}, {"timePeriodStart":"2001","value":"9'
    assert is_sdg_observation_sample(body, "EG_ACS_ELEC:156", complete=False)
    assert not is_sdg_observation_sample(body, "EG_ACS_ELEC:156", complete=True)
    assert not is_sdg_observation_sample(prefix + b'{"timePeriodStart":"2000","value":null}]}', "EG_ACS_ELEC:156", complete=True)
    assert not is_sdg_observation_sample(prefix + b'{"description":"not an observation"}]}', "EG_ACS_ELEC:156", complete=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("dimensions,expected", [([], 0), ([{"timePeriodStart": "2000", "value": None}], 0),
                                                ([{"timePeriodStart": "2000", "value": "96.7"}], 1)])
async def test_facade_verification_rejects_empty_sdg_observations(dimensions, expected):
    async def handle(request):
        if request.url.path.endswith("/Series/List"):
            return httpx.Response(200, json=sdg())
        if request.url.path.endswith("/GeoAreas"):
            return httpx.Response(200, json=[{"geoAreaCode": "156", "geoAreaName": "China"}])
        assert str(request.url) == SDG_URL
        return httpx.Response(200, json={"series": "EG_ACS_ELEC", "geoAreaCode": 156,
                                        "geoAreaName": "China", "dimensions": dimensions})
    source = OpenDataSources(transport=httpx.MockTransport(handle))
    page = await source.search("un-sdg", "EG_ACS_ELEC geo=156")
    assert len(page["results"]) == 1
    assert len(await source.verify(page["results"][0])) == expected


@pytest.mark.asyncio
async def test_sdg_observations_disappearing_before_download_are_not_saved():
    empty = False
    async def handle(request):
        if request.url.path.endswith("/Series/List"):
            return httpx.Response(200, json=sdg())
        if request.url.path.endswith("/GeoAreas"):
            return httpx.Response(200, json=[{"geoAreaCode": "156", "geoAreaName": "China"}])
        return httpx.Response(200, json={"series": "EG_ACS_ELEC", "geoAreaCode": 156, "geoAreaName": "China",
                                        "dimensions": [] if empty else [{"timePeriodStart": "2000", "value": "96.7"}]})
    source = OpenDataSources(transport=httpx.MockTransport(handle))
    candidate = (await source.search("un-sdg", "EG_ACS_ELEC geo=156"))["results"][0]
    verified = await source.verify(candidate)
    assert len(verified) == 1
    empty = True
    with pytest.raises(SourceConnectorError) as caught:
        await source.download(candidate, verified[0])
    assert caught.value.reason == "no_observations"
