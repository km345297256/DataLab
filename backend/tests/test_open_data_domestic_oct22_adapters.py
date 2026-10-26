import copy
import json

import pytest
import httpx

from app.domain.external.data_source import SourceConnectorError
from app.infrastructure.external.data_sources.open_data_domestic_oct22_adapters import (
    CENC_URL, NMC, NMC_LICENSE, DomesticOct22Adapters, is_domestic_observation_sample, safe_file_url,
)
from app.infrastructure.external.data_sources.open_data_sources import OpenDataSources


def event(**fields):
    return dict(id="CD.20261022090712.222", time="2026-10-22 09:07:12", latitude=33.96,
                longitude=88.59, depth=10, magnitude=3, location="西藏那曲市双湖县", **fields)


def weather(code="Wqsps"):
    return {"station": {"code": code}, "publish_time": "2026-10-22 10:50",
            "weather": {"temperature": 13.7, "humidity": 43, "rain": 0, "airpressure": 9999},
            "wind": {"speed": 2.9}}


STATIONS = {"ABJ": [{"code": "Wqsps", "province": "北京市", "city": "北京", "url": "/publish/forecast/ABJ/beijing.html"},
                     {"code": "niStC", "province": "北京市", "city": "昌平", "url": "/publish/forecast/ABJ/changping.html"}],
            "AHE": [{"code": "HeBei", "province": "河北省", "city": "石家庄", "url": "/publish/forecast/AHE/shijiazhuang.html"}]}


async def fetch(url, params=None, **kwargs):
    if url == CENC_URL:
        return [event()]
    if url == NMC + "/rest/province/all":
        return [{"code": code} for code in STATIONS]
    if url.startswith(NMC + "/rest/province/"):
        return copy.deepcopy(STATIONS[url.rsplit("/", 1)[1]])
    if url.startswith(NMC + "/rest/real/"):
        return weather(url.rsplit("/", 1)[1])
    raise AssertionError(url)


@pytest.mark.parametrize("source,url", [("cenc-earthquake", CENC_URL), ("nmc-weather", NMC + "/rest/real/Wqsps")])
def test_only_official_entity_endpoints_allowed(source, url):
    assert safe_file_url(source, url)
    for bad in [url.replace("https:", "http:"), url.replace(".cn/", ".cn.evil.test/"), url + "?token=abc", url + "#x",
                url.replace("https://", "https://secret@"), url.replace(".cn/", ".cn/../"), url + "\n"]:
        assert not safe_file_url(source, bad)
    assert not safe_file_url("unknown", url)


@pytest.mark.parametrize("query", ["地震", '"地震"', "earthquakes", "西藏那曲市双湖县", "2026 地震"])
@pytest.mark.asyncio
async def test_cenc_discovers_whole_current_catalog_with_actual_coverage(query):
    adapter = DomesticOct22Adapters(fetch)
    page = await adapter.search("cenc-earthquake", query, 1)
    row = page["results"][0]
    assert row["coverage"] == {"start": "2026-10-22 09:07:12", "end": "2026-10-22 09:07:12", "timezone": "Asia/Shanghai", "records": 1}
    row["source_url"] = "https://evil.test/data.json"
    option = (await adapter.resolve(row))[0]
    assert option["url"] == CENC_URL and option["version"] is None and option["size"] is None
    assert (await adapter.search("cenc-earthquake", query, 2))["results"] == []


@pytest.mark.parametrize("query", ["2020 地震", "基因表达", "人口统计"])
@pytest.mark.asyncio
async def test_cenc_does_not_claim_unavailable_historical_or_unrelated_data(query):
    assert (await DomesticOct22Adapters(fetch).search("cenc-earthquake", query, 1))["results"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [[], {"status": "ok"}, [dict(event(), magnitude="3")], [dict(event(), latitude=100)],
                                  [dict(event(), time="2026-02-30 09:07:12")], [event(), event()]])
async def test_cenc_invalid_or_metadata_only_catalog_is_rejected(data):
    async def invalid(*args, **kwargs):
        return data
    with pytest.raises(SourceConnectorError):
        await DomesticOct22Adapters(invalid).search("cenc-earthquake", "地震", 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("query,expected", [("北京天气", ["Wqsps"]), ('"昌平 最新气温"', ["niStC"]),
                                           ("河北省气象", ["HeBei"]), ("station=Wqsps", ["Wqsps"])])
async def test_weather_uses_explicit_location_from_fresh_official_catalog(query, expected):
    adapter = DomesticOct22Adapters(fetch)
    page = await adapter.search("nmc-weather", query, 1)
    assert [r["external_id"] for r in page["results"]] == expected
    for row in page["results"]:
        row["metadata"]["url"] = "https://evil.test/"
        option = (await adapter.resolve(row))[0]
        assert option["url"] == NMC + "/rest/real/" + row["external_id"]
        assert option["license"] == NMC_LICENSE and option["version"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["天气", "北京2020年气温", "北京历史降雨", "北京近30天气温", "去年北京天气", "北京昨天气温",
                                   "北京今年气温", "北京气候", "北京年均降雨"])
async def test_no_default_city_and_no_history_misrepresentation(query):
    assert (await DomesticOct22Adapters(fetch).search("nmc-weather", query, 1))["results"] == []


@pytest.mark.asyncio
async def test_catalog_cache_and_bad_station_identity():
    calls = []
    async def counted(*args, **kwargs):
        calls.append(args[0])
        return await fetch(*args, **kwargs)
    adapter = DomesticOct22Adapters(counted)
    await adapter.search("nmc-weather", "北京", 1)
    await adapter.search("nmc-weather", "河北省", 1)
    assert calls.count(NMC + "/rest/province/all") == 1
    with pytest.raises(SourceConnectorError):
        await adapter.resolve({"source_id": "nmc-weather", "external_id": "NoSuch"})
    with pytest.raises(SourceConnectorError):
        await adapter.resolve({"source_id": "nmc-weather", "external_id": "../xx"})


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [{"station": "", "publish_time": "", "weather": "", "wind": ""}, weather("Wrong"),
                                  {**weather(), "weather": {"temperature": 9999, "humidity": 9999}, "wind": {"speed": 9999}}])
async def test_weather_empty_mismatched_and_all_missing_responses_are_not_data(data):
    async def no_observations(url, *args, **kwargs):
        return data if "/rest/real/" in url else await fetch(url, *args, **kwargs)
    with pytest.raises(SourceConnectorError):
        await DomesticOct22Adapters(no_observations).resolve({"source_id": "nmc-weather", "external_id": "Wqsps"})


def test_source_specific_sample_validation_covers_json_prefix_and_city_identity():
    first = json.dumps(event()).encode()
    assert is_domestic_observation_sample("cenc-earthquake", b"[\n" + first + b",\n{\"id\":", "current-catalog")
    assert not is_domestic_observation_sample("cenc-earthquake", b"[]", "current-catalog")
    assert not is_domestic_observation_sample("cenc-earthquake", b'[{"id":"metadata"}]', "current-catalog")
    assert is_domestic_observation_sample("nmc-weather", json.dumps(weather()).encode(), "Wqsps")
    assert not is_domestic_observation_sample("nmc-weather", json.dumps(weather()).encode(), "Other")
    assert not is_domestic_observation_sample("nmc-weather", b'{}', "Wqsps")


@pytest.mark.asyncio
async def test_upstream_station_urls_cannot_expand_download_authority():
    async def bad(url, *args, **kwargs):
        data = await fetch(url, *args, **kwargs)
        if url == NMC + "/rest/province/ABJ":
            data[0]["url"] = "https://evil.test/city"
        return data
    with pytest.raises(SourceConnectorError):
        await DomesticOct22Adapters(bad).search("nmc-weather", "北京", 1)


@pytest.mark.asyncio
async def test_province_search_supports_autonomous_region_short_name():
    async def regional(url, *args, **kwargs):
        if url.endswith("/all"):
            return [{"code": "ANX"}]
        if url.endswith("/ANX"):
            return [{"code": "YeChn", "province": "宁夏回族自治区", "city": "银川", "url": "/publish/forecast/ANX/yinchuan.html"}]
        raise AssertionError(url)
    assert (await DomesticOct22Adapters(regional).search("nmc-weather", "宁夏天气", 1))["results"][0]["external_id"] == "YeChn"


@pytest.mark.asyncio
@pytest.mark.parametrize("source,external,url,body", [
    ("nmc-weather", "Wqsps", NMC + "/rest/real/Wqsps", {"station": "", "publish_time": "", "weather": "", "wind": ""}),
    ("nmc-weather", "Wqsps", NMC + "/rest/real/Wqsps", weather("Wrong")),
    ("cenc-earthquake", "current-catalog", CENC_URL, [{"id": "metadata", "title": "catalog"}]),
])
async def test_facade_rejects_empty_success_and_wrong_city_during_probe_and_full_download(source, external, url, body):
    facade = OpenDataSources(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)))
    option = {"source_id": source, "external_id": external, "url": url, "size": None, "checksum": None,
              "filename": "observations.json", "content_type": "application/json"}
    assert await facade._probe_file(option) is None
    with pytest.raises(SourceConnectorError, match="no_observations"):
        await facade._stream_file({"source_id": source, "external_id": external}, option, None)


@pytest.mark.asyncio
async def test_facade_stops_an_empty_snapshot_changed_after_successful_probe():
    responses = [weather(), {"station": "", "publish_time": "", "weather": "", "wind": ""}]
    def handle(request):
        return httpx.Response(200, json=responses.pop(0))
    facade = OpenDataSources(transport=httpx.MockTransport(handle))
    option = {"source_id": "nmc-weather", "external_id": "Wqsps", "url": NMC + "/rest/real/Wqsps",
              "size": None, "checksum": None, "filename": "observations.json", "content_type": "application/json"}
    assert await facade._probe_file(option) is not None
    with pytest.raises(SourceConnectorError, match="no_observations"):
        await facade._stream_file({"source_id": "nmc-weather", "external_id": "Wqsps"}, option, None)
