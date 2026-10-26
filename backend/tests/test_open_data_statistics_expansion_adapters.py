"""Offline contracts for full official catalogs and original observation files."""
import asyncio
import csv
import gzip
import hashlib
import io
import zipfile

import httpx
import pytest

from app.domain.external.data_source import SourceConnectorError
from app.infrastructure.external.data_sources.open_data_sources import OpenDataSources
from app.infrastructure.external.data_sources.open_data_statistics_expansion_adapters import (
    ECB, EUROSTAT, ILO, UIS, SOURCES, StatisticsExpansionAdapters, safe_file_url,
)

URLS = {
    "eurostat": EUROSTAT + "/sdmx/2.1/data/TPS00001?format=tsv&compressed=true",
    "ecb": ECB + "/data/EXR/M.USD.EUR.SP00.A?format=csvdata",
    "ilostat": ILO + "/data/indicator?format=.csv.gz&id=SDG_0552_NOC_RT_A",
    "unesco-uis": UIS + "/data/indicators/export?format=csv&indicator=CR.1&version=20260507-91260335",
}


def table(fields, rows, delimiter=","):
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=fields, delimiter=delimiter)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def eurostat(rows=None):
    fields = ["title", "code", "type", "last update of data", "last table structure change", "data start", "data end", "values"]
    return table(fields, rows or [dict(zip(fields, ["Population", "tps00001", "table", "01.10.2026", "02.10.2026", "2000", "2025", "20"]))], "\t")


def ecb(code="EXR", agency="ECB"):
    return f'''<mes:Structure xmlns:mes="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message" xmlns:str="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/structure" xmlns:com="http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common"><mes:Structures><str:Dataflows><str:Dataflow agencyID="{agency}" id="{code}" version="1.0"><com:Name xml:lang="en">Exchange Rates</com:Name></str:Dataflow></str:Dataflows></mes:Structures></mes:Structure>'''


def ilo(updated="2026-10-01", count="2"):
    fields = ["id", "indicator.label", "freq.label", "last.update", "n.records"]
    return "\ufeff" + table(fields, [dict(zip(fields, ["SDG_0552_NOC_RT_A", "Women in management", "Annual", updated, count]))])


def uis(count=2):
    return [{"indicatorCode": "CR.1", "name": "Primary education completion rate", "theme": "EDUCATION",
             "dataAvailability": {"totalRecordCount": count, "timeLine": {"min": 2020, "max": 2021}}}]


def adapter(handle):
    source = OpenDataSources(transport=httpx.MockTransport(handle))
    return source, StatisticsExpansionAdapters(source._json, source._text_metadata)


@pytest.mark.parametrize("source", SOURCES)
def test_download_whitelist_rejects_metadata_and_scope_injection(source):
    url = URLS[source]
    assert safe_file_url(source, url)
    for other in SOURCES:
        if other != source:
            assert not safe_file_url(other, url)
    for bad in [url.replace("https://", "http://"), url.replace("https://", "https://user@"),
                url.replace("https://", "https://@"), url + "#fragment", url + "&limit=1", url + "&format=json",
                url.replace("https://", "https://evil.test/"), url.replace("/data/", "/data/%2e%2e/"),
                url.replace("/data/", "/data/%252e%252e/"), url.replace("/data/", "/data/%2f")]:
        assert not safe_file_url(source, bad), bad


@pytest.mark.parametrize("source,url", [
    ("eurostat", EUROSTAT + "/catalogue/toc/txt?lang=en"),
    ("ecb", ECB + "/dataflow/ECB/all/latest?references=none"),
    ("ecb", ECB + "/data/EXR/M.USD.EUR.SP00.A?format=csvdata&detail=serieskeysonly"),
    ("ilostat", ILO + "/metadata/toc/indicator?format=.csv&lang=en"),
    ("unesco-uis", UIS + "/definitions/indicators/export?format=csv"),
    ("unesco-uis", URLS["unesco-uis"].replace("CR.1", "%2e%2e%2fprivate")),
])
def test_catalogs_dictionaries_and_metadata_never_become_entity_downloads(source, url):
    assert not safe_file_url(source, url)


@pytest.mark.asyncio
@pytest.mark.parametrize("page", [True, 0, -1, 101, "2"])
async def test_invalid_page_never_calls_provider(page):
    _, a = adapter(lambda request: pytest.fail("no network expected"))
    with pytest.raises(SourceConnectorError, match="invalid_cursor"):
        await a.search("eurostat", "population", page)


@pytest.mark.asyncio
async def test_eurostat_complete_catalog_paging_deduplicates_tables_and_filters_folders():
    fields = ["title", "code", "type", "last update of data", "last table structure change", "values"]
    rows = [dict(zip(fields, ["Population " + str(i), "pop_" + str(i), "table", "2026", "2026", "2"])) for i in range(23)]
    rows += [rows[0], {**rows[0], "type": "folder", "code": "folder"}, {**rows[0], "code": "empty", "values": "0"}]
    def handle(request):
        assert request.url.path.endswith("/catalogue/toc/txt") and request.url.params["lang"] == "en"
        return httpx.Response(200, text=table(fields, rows, "\t"))
    _, a = adapter(handle)
    found = await a.search("eurostat", '"population"', 2)
    assert found["total_count"] == 23
    assert [r["external_id"] for r in found["results"]] == ["pop_" + str(i) for i in range(10, 20)]
    assert all(r["access"] == "needs_resolution" and not r["formats"] for r in found["results"])
    assert (await a.search("eurostat", "missing", 1))["results"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("xml", ["<!DOCTYPE x [<!ENTITY x 'expansion'>]><x>&x;</x>", "<!entity x SYSTEM 'file:///etc/passwd'><x/>", "<broken>"])
async def test_xml_declarations_and_invalid_xml_are_rejected(xml):
    _, a = adapter(lambda request: httpx.Response(200, text=xml))
    with pytest.raises(SourceConnectorError, match="invalid_response"):
        await a.search("ecb", "exchange", 1)


@pytest.mark.asyncio
async def test_ecb_distinguishes_full_dataflow_and_exact_series_all_periods():
    def handle(request):
        if "/dataflow/" in request.url.path:
            assert dict(request.url.params) == {"references": "none"}
            return httpx.Response(200, text=ecb())
        assert request.url.path.endswith("/EXR/M.USD.EUR.SP00.A")
        assert dict(request.url.params) == {"format": "csvdata", "detail": "serieskeysonly"}
        return httpx.Response(200, text="KEY,FREQ\r\nEXR.M.USD.EUR.SP00.A,M\r\n")
    _, a = adapter(handle)
    full = (await a.search("ecb", "exchange rates", 1))["results"][0]
    exact = (await a.search("ecb", "EXR/M.USD.EUR.SP00.A", 1))["results"][0]
    assert full["external_id"] == "EXR" and full["coverage"]["scope"] == "完整数据流"
    assert exact["external_id"] == "EXR/M.USD.EUR.SP00.A"
    full_option, = await a.resolve(full)
    series_option, = await a.resolve(exact)
    assert full_option["url"] == ECB + "/data/EXR?format=csvdata"
    assert series_option["url"] == URLS["ecb"]
    assert full_option["version"] is None and series_option["version"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [ecb(agency="FOREIGN"), ecb(code="OTHER")])
async def test_ecb_resolver_requires_matching_authoritative_agency_and_record(payload):
    _, a = adapter(lambda request: httpx.Response(200, text=payload))
    assert await a.resolve({"source_id": "ecb", "external_id": "EXR"}) == []


@pytest.mark.asyncio
async def test_ecb_series_metadata_must_match_requested_series():
    _, a = adapter(lambda request: httpx.Response(200, text=ecb() if "/dataflow/" in request.url.path else "KEY\nEXR.M.GBP.EUR.SP00.A\n"))
    assert await a.resolve({"source_id": "ecb", "external_id": "EXR/M.USD.EUR.SP00.A"}) == []


@pytest.mark.asyncio
async def test_ilostat_refreshes_catalog_identity_and_keeps_original_file():
    updated = ["2026-10-01"]
    def handle(request):
        assert request.url.path == "/metadata/toc/indicator"
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, text=ilo(updated[0]))
    _, a = adapter(handle)
    candidate = (await a.search("ilostat", "women management", 1))["results"][0]
    first, = await a.resolve({**candidate, "url": "https://evil.test/file", "license": "invented"})
    assert first["url"] == URLS["ilostat"] and first["size"] is None
    assert first["license"].startswith("https://www.ilo.org/")
    updated[0] = "2026-10-02"
    a._cache.clear()
    second, = await a.resolve(candidate)
    assert first["id"] != second["id"]


@pytest.mark.asyncio
async def test_uis_pins_live_publication_version_and_exports_observations_only():
    version = ["20260507-91260335"]
    def handle(request):
        if request.url.path.endswith("/versions/default"):
            return httpx.Response(200, json={"version": version[0]})
        assert request.url.path.endswith("/definitions/indicators")
        assert request.url.params["version"] == version[0]
        return httpx.Response(200, json=uis())
    _, a = adapter(handle)
    row = (await a.search("unesco-uis", "primary education", 1))["results"][0]
    first, = await a.resolve({**row, "metadata": {"version": "untrusted"}, "url": "https://evil.test/data"})
    assert first["url"] == URLS["unesco-uis"]
    assert first["version"] == "20260507-91260335"
    version[0] = "20261021-abcdef12"
    a._cache.clear()
    second, = await a.resolve(row)
    assert first["id"] != second["id"] and "version=20261021-abcdef12" in second["url"]


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["ilostat", "unesco-uis"])
async def test_metadata_with_zero_observations_is_not_a_data_candidate(source):
    def handle(request):
        if source == "ilostat":
            return httpx.Response(200, text=ilo(count="0"))
        return httpx.Response(200, json={"version": "20260507-91260335"} if request.url.path.endswith("/default") else uis(0))
    _, a = adapter(handle)
    assert (await a.search(source, "education" if source == "unesco-uis" else "women", 1))["results"] == []


@pytest.mark.asyncio
async def test_concurrent_resolution_reuses_trusted_catalog_until_short_ttl_expires(monkeypatch):
    now, calls = [100.0], []
    monkeypatch.setattr("app.infrastructure.external.data_sources.open_data_statistics_expansion_adapters.time.monotonic", lambda: now[0])
    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(200, text=ilo())
    _, a = adapter(handle)
    candidate = {"source_id": "ilostat", "external_id": "SDG_0552_NOC_RT_A"}
    await asyncio.gather(*(a.resolve(candidate) for _ in range(10)))
    assert len(calls) == 1
    now[0] = 161.0
    await a.resolve(candidate)
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("source", SOURCES)
async def test_facade_verify_and_complete_download_reads_entity_and_ignores_caller_url(source, tmp_path, monkeypatch):
    monkeypatch.setenv("OPEN_DATA_DOWNLOAD_ROOT", str(tmp_path))
    body = b"geo,time,obs_value\nAAA,2025,25.2\n"
    if source in {"eurostat", "ilostat"}:
        body = gzip.compress(body, mtime=0)
    elif source == "unesco-uis":
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("data.csv", body)
        body = stream.getvalue()
    mime = "application/zip" if source == "unesco-uis" else "application/gzip" if source in {"eurostat", "ilostat"} else "text/csv"
    calls = []
    def handle(request):
        calls.append(str(request.url))
        if "/catalogue/" in request.url.path:
            return httpx.Response(200, text=eurostat())
        if "/dataflow/" in request.url.path:
            return httpx.Response(200, text=ecb())
        if "/metadata/" in request.url.path:
            return httpx.Response(200, text=ilo())
        if request.url.path.endswith("/versions/default"):
            return httpx.Response(200, json={"version": "20260507-91260335"})
        if "/definitions/" in request.url.path:
            return httpx.Response(200, json=uis())
        assert safe_file_url(source, str(request.url))
        return httpx.Response(200, content=body, headers={"content-type": mime})
    service, a = adapter(handle)
    query = {"eurostat": "population", "ecb": "exchange", "ilostat": "women", "unesco-uis": "primary"}[source]
    row = (await a.search(source, query, 1))["results"][0]
    option, = await service.verify(row)
    stream, filename, content_type, receipt = await service.download(row, {**option, "url": "https://evil.test/forged"})
    try:
        actual = stream.read()
    finally:
        stream.close()
    assert actual == body
    assert receipt["sha256"] == hashlib.sha256(body).hexdigest()
    assert all("evil.test" not in url for url in calls)
    assert receipt["license"].startswith("https://")
