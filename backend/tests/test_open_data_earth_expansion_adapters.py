from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

import pytest

from app.domain.external.data_source import SourceConnectorError
from app.domain.models.open_data_earth_expansion_definitions import DEFINITIONS
from app.infrastructure.external.data_sources.open_data_earth_expansion_adapters import (
    EarthExpansionAdapters, SOURCES, safe_file_url, NOAA_BASE,
)

POWER_QUERY = "latitude=39.9 longitude=116.4 start=20240101 end=20240102 parameters=T2M,PRECTOTCORR"
POWER_DATA = {"type": "Feature", "geometry": {"coordinates": [116.4, 39.9, 221]},
    "header": {"start": "20240101", "end": "20240102", "time_standard": "UTC", "fill_value": -999},
    "properties": {"parameter": {"T2M": {"20240101": -4.38, "20240102": -2.77},
                                 "PRECTOTCORR": {"20240101": 0, "20240102": 0}}}}
NOAA_DATA = "# File Creation: Fri Oct  9 13:08:07 2026\n# Usage policy\nyear,month,decimal date,average\n1958,3,1958.2027,315.71\n1958,4,1958.2877,317.45\n"
CERN_FILE = {"availability": "online", "key": "4e_2011.csv", "size": 2271,
    "checksum": "adler32:0eb9cb89", "version_id": "0d3086e9-0a15-49dc-9d6d-94a1a34ea38f",
    "uri": "root://eospublic.cern.ch//eos/opendata/cms/derived-data/run1-physics-candidates/higgs/13424-v5/4e_2011.csv"}
CERN_RECORD = {"id": "5200", "updated": "2025-06-06T08:39:29.971931+00:00", "metadata": {
    "recid": "5200", "title": "CMS event candidates", "type": {"primary": "Dataset"},
    "license": {"attribution": "CC0-1.0"}, "_files": [CERN_FILE]}}


async def no_text(*args, **kwargs):
    raise AssertionError("unexpected text call")


@pytest.mark.asyncio
async def test_usgs_scope_count_and_full_export_never_adds_limit():
    calls = []
    async def fetch(url, params=None):
        calls.append((url, params))
        assert url.endswith("/count")
        assert params == {"starttime": "2024-01-01", "endtime": "2024-01-02", "minmagnitude": "6", "format": "geojson"}
        return {"count": 2, "maxAllowed": 20000}
    a = EarthExpansionAdapters(fetch, no_text)
    result = await a.search("usgs-earthquake", "start=2024-01-01 end=2024-01-02 minmagnitude=6", 1)
    c, = result["results"]
    option, = await a.resolve(c)
    assert option["size"] is None and option["checksum"] is None and option["version"] is None
    assert safe_file_url("usgs-earthquake", option["url"])
    query = parse_qs(urlsplit(option["url"]).query)
    assert query["format"] == ["csv"] and "limit" not in query and "offset" not in query
    assert c["metadata"]["observations"] == 2 and len(calls) == 2


@pytest.mark.asyncio
async def test_usgs_provider_limit_is_not_silently_truncated():
    async def fetch(*args):return {"count": 20001, "maxAllowed": 20000}
    with pytest.raises(SourceConnectorError):
        await EarthExpansionAdapters(fetch, no_text).search("usgs-earthquake", "start=2024-01-01 end=2024-02-01", 1)


@pytest.mark.asyncio
async def test_power_verifies_real_values_and_exports_original_csv():
    async def fetch(url, params=None):
        assert params["format"] == "JSON" and params["time-standard"] == "UTC"
        return deepcopy(POWER_DATA)
    a = EarthExpansionAdapters(fetch, no_text)
    c, = (await a.search("nasa-power", POWER_QUERY, 1))["results"]
    option, = await a.resolve(c)
    assert c["metadata"]["observations"] == 2 and option["source_filename"].endswith(".csv")
    assert safe_file_url("nasa-power", option["url"])
    assert parse_qs(urlsplit(option["url"]).query)["parameters"] == ["T2M,PRECTOTCORR"]
    assert option["version"] is None  # API version is not an immutable observation version.


@pytest.mark.asyncio
async def test_power_all_missing_is_not_an_entity_result():
    data = deepcopy(POWER_DATA)
    for values in data["properties"]["parameter"].values():
        for key in values:values[key] = -999
    async def fetch(*args):return data
    assert not (await EarthExpansionAdapters(fetch, no_text).search("nasa-power", POWER_QUERY, 1))["results"]


@pytest.mark.parametrize("left,right", [('"', '"'), ("'", "'"), ("“", "”")])
@pytest.mark.asyncio
async def test_planner_outer_quotes_preserve_observation_parameters(left, right):
    async def fetch(url, params=None):
        return deepcopy(POWER_DATA) if "power.larc" in url else {"count": 2, "maxAllowed": 20000}
    a = EarthExpansionAdapters(fetch, no_text)
    for source, query in [("nasa-power", POWER_QUERY), ("usgs-earthquake", "start=2024-01-01 end=2024-01-02 minmagnitude=6")]:
        plain = await a.search(source, query, 1)
        quoted = await a.search(source, left + query + right, 1)
        assert quoted == plain


@pytest.mark.parametrize("coordinate", [float("nan"), float("inf"), "116.4", True])
@pytest.mark.asyncio
async def test_power_response_rejects_nonfinite_or_non_numeric_coordinates(coordinate):
    data = deepcopy(POWER_DATA); data["geometry"]["coordinates"][0] = coordinate
    async def fetch(*args):return data
    with pytest.raises(SourceConnectorError):
        await EarthExpansionAdapters(fetch, no_text).search("nasa-power", POWER_QUERY, 1)


@pytest.mark.parametrize("query", [
    "climate Beijing", "latitude=91 longitude=0 start=20240101 end=20240102", 
    "latitude=0 longitude=0 start=20240230 end=20240301", 
    "latitude=0 latitude=1 longitude=0 start=20240101 end=20240102",
    "latitude=0 longitude=0 start=20240101 end=20240102 url=https://evil.example",
    "latitude=0 longitude=0 start=20240101 end=20240102 parameters=UNKNOWN",
    "latitude=0 longitude=0 start=20240101 end=20240102 parameters=T2M,T2M",
    "latitude=NaN longitude=0 start=20240101 end=20240102",
])
@pytest.mark.asyncio
async def test_power_does_not_guess_places_or_accept_arbitrary_parameters(query):
    async def fetch(*args):raise AssertionError("must validate before request")
    with pytest.raises(SourceConnectorError):await EarthExpansionAdapters(fetch, no_text).search("nasa-power", query, 1)


@pytest.mark.asyncio
async def test_noaa_real_csv_rows_and_source_creation_version():
    async def text(url, **kwargs):
        assert url == NOAA_BASE + "co2/co2_mm_mlo.csv"
        return NOAA_DATA
    a = EarthExpansionAdapters(None, text)
    c, = (await a.search("noaa-gml", "二氧化碳", 1))["results"]
    option, = await a.resolve(c)
    assert option["version"] == "Fri Oct  9 13:08:07 2026"
    assert option["checksum"] is None and option["source_filename"] == "co2_mm_mlo.csv"
    assert c["metadata"]["observation_months"] == 2
    assert safe_file_url("noaa-gml", option["url"])
    assert not (await a.search("noaa-gml", "plant taxonomy", 1))["results"]
    c["metadata"]["version"] = "old snapshot"
    with pytest.raises(SourceConnectorError):await a.resolve(c)


@pytest.mark.parametrize("body", ["# NOAA metadata only", "year,month,decimal date,average\n", "<html>login</html>"])
@pytest.mark.asyncio
async def test_noaa_metadata_and_empty_files_are_rejected(body):
    async def text(*args, **kwargs):return body
    with pytest.raises(SourceConnectorError):await EarthExpansionAdapters(None, text).search("noaa-gml", "CO2", 1)


@pytest.mark.asyncio
async def test_cern_online_entity_manifest_preserves_adler32_version_license():
    async def fetch(url, params=None):
        if url.endswith("/api/records/"):
            assert params == {"q": "higgs csv", "type": "Dataset", "size": 10, "page": 1}
            software = deepcopy(CERN_RECORD);software["metadata"]["type"]["primary"] = "Software"
            return {"hits": {"total": 2, "hits": [deepcopy(CERN_RECORD), software]}}
        assert url.endswith("/api/records/5200")
        return deepcopy(CERN_RECORD)
    a = EarthExpansionAdapters(fetch, no_text)
    c, = (await a.search("cern-open-data", "higgs csv", 1))["results"]
    option, = await a.resolve(c)
    assert option["checksum"] == "adler32:0eb9cb89" and option["version"] == CERN_FILE["version_id"]
    assert option["size"] == 2271 and option["license"] == "CC0-1.0"
    assert safe_file_url("cern-open-data", option["url"])
    assert option["url"].startswith("https://opendata.cern.ch/eos/opendata/cms/")
    c["metadata"]["version"] = "changed"
    with pytest.raises(SourceConnectorError):await a.resolve(c)


@pytest.mark.parametrize("patch", [
    {"availability": "offline"}, {"key": "README.csv"}, {"key": "analysis.py"},
    {"uri": "root://evil.example//eos/opendata/cms/data.csv"},
    {"uri": "root://eospublic.cern.ch//eos/opendata/cms/../private/4e_2011.csv"},
    {"key": "mismatched.csv"}, {"size": 0},
])
def test_cern_does_not_expose_docs_code_restricted_or_foreign_paths(patch):
    data = deepcopy(CERN_RECORD["metadata"]);data["_files"][0].update(patch)
    assert not EarthExpansionAdapters._cern_files(data, "5200")


def test_cern_large_files_are_not_capped():
    data = deepcopy(CERN_RECORD["metadata"]);data["_files"][0]["size"] = 10 * 1024**3
    assert EarthExpansionAdapters._cern_files(data, "5200")[0]["size"] == 10 * 1024**3


def test_cern_complete_manifest_retains_last_file_after_one_hundred_entries():
    data = deepcopy(CERN_RECORD["metadata"])
    data["_files"] = [{**CERN_FILE, "key": f"part-{i}.csv",
                       "uri": f"root://eospublic.cern.ch//eos/opendata/cms/part-{i}.csv"} for i in range(125)]
    options = EarthExpansionAdapters._cern_files(data, "5200")
    assert len(options) == 125 and options[-1]["source_filename"] == "part-124.csv"


@pytest.mark.parametrize("source,url", [
    ("noaa-gml", NOAA_BASE + "co2/co2_mm_mlo.csv?redirect=https://evil.example"),
    ("noaa-gml", NOAA_BASE + "co2/../private.csv"),
    ("cern-open-data", "https://opendata.cern.ch.evil.example/eos/opendata/cms/a.csv"),
    ("cern-open-data", "https://opendata.cern.ch/eos/opendata/cms/a.py"),
    ("cern-open-data", "https://opendata.cern.ch/eos/opendata/cms/%2e%2e/a.csv"),
    ("cern-open-data", "https://user@opendata.cern.ch/eos/opendata/cms/a.csv"),
    ("usgs-earthquake", "https://earthquake.usgs.gov/fdsnws/event/1/query?format=csv&eventid=us7000mfpw&limit=1"),
    ("usgs-earthquake", "https://earthquake.usgs.gov/fdsnws/event/1/query?format=csv&format=geojson&eventid=us7000mfpw"),
    ("nasa-power", "https://power.larc.nasa.gov/api/temporal/daily/point?format=CSV&community=RE&time-standard=UTC&latitude=999&longitude=0&start=20240101&end=20240102&parameters=T2M"),
])
def test_download_allowlist_is_origin_path_and_parameter_exact(source, url):
    assert not safe_file_url(source, url)


def test_definitions_correspond_to_implemented_services_and_honest_query_scope():
    assert {row["id"] for row in DEFINITIONS} == SOURCES
    assert all(row["limitation"] for row in DEFINITIONS)
    assert next(x for x in DEFINITIONS if x["id"] == "nasa-power")["discovery_mode"] == "observations"
