"""Offline contracts for official complete FAOSTAT and BIS archives."""
import asyncio
import io
import zipfile

import httpx
import pytest

from app.domain.external.data_source import SourceConnectorError
from app.infrastructure.external.data_sources.open_data_sources import OpenDataSources
from app.infrastructure.external.data_sources.open_data_international_oct22_adapters import (
    BIS_CATALOG, FAO_CATALOG, SOURCES, InternationalOct22Adapters, safe_file_url,
)

FAO_URL = "https://bulks-faostat.fao.org/production/ASTI_Researchers_E_All_Data_(Normalized).zip"
BIS_URL = "https://data.bis.org/static/bulk/WS_CBPOL_csv_flat.zip"


def fao(code="AF", **changes):
    return {"DatasetCode": code, "DatasetName": "Agricultural Science and Technology: ASTI - Researchers",
            "DatasetDescription": "Researchers working in agriculture", "Topic": "All available countries and years",
            "DateUpdate": "2026-04-30T00:00:00", "CompressionFormat": "zip", "FileType": "csv",
            "FileSize": "33KB", "FileRows": 3800, "FileLocation": FAO_URL, **changes}


def bis(code="WS_CBPOL", name="Central bank policy rates", date="Thu Oct 22 2026"):
    return f'<a href="/static/bulk/{code}_csv_flat.zip"><h4>{name} (CSV, flat)</h4><time datetime="{date}">22 Oct 2026</time></a>'


def adapter(handle):
    s = OpenDataSources(transport=httpx.MockTransport(handle))
    return s, InternationalOct22Adapters(s._json, s._text_metadata)


@pytest.mark.parametrize("source,url", [("faostat", FAO_URL), ("bis", BIS_URL)])
def test_entity_routes_only_and_no_arbitrary_external_or_scope_urls(source, url):
    assert safe_file_url(source, url)
    assert not safe_file_url("unknown", url)
    assert not safe_file_url(next(s for s in SOURCES if s != source), url)
    for bad in [url.replace("https://", "http://"), url.replace("https://", "https://evil.test/"),
                url.replace("https://", "https://@"), url.replace("https://", "https://user:pass@"),
                url + "?limit=1", url + "#part", url.replace("/production/", "/production/%2e%2e/"),
                url.replace("/static/", "/static/%252e%252e/"), url.replace(".zip", ".csv"),
                url.replace(".zip", "\\x.zip"), url.replace(".zip", "\n.zip")]:
        if bad != url:
            assert not safe_file_url(source, bad), bad


@pytest.mark.parametrize("source,url", [("faostat", FAO_CATALOG), ("bis", BIS_CATALOG),
    ("bis", "https://data.bis.org/static/bulk/WS_CBPOL_sdmx-generic-2.1.zip"),
    ("faostat", "https://bulks-faostat.fao.org/production/ASTI_Researchers_E_AreaCodes.csv")])
def test_catalogs_code_lists_and_non_selected_bulk_representations_are_not_download_options(source, url):
    assert not safe_file_url(source, url)


@pytest.mark.asyncio
@pytest.mark.parametrize("page", [True, 0, 101, -1, "2"])
async def test_invalid_page_never_accesses_network(page):
    _, a = adapter(lambda _: pytest.fail("no network expected"))
    with pytest.raises(SourceConnectorError, match="invalid_cursor"):
        await a.search("faostat", "agriculture", page)


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["", "   ", '""', "“”", None, "x" * 1001])
async def test_invalid_query_never_accesses_network(query):
    _, a = adapter(lambda _: pytest.fail("no network expected"))
    with pytest.raises(SourceConnectorError, match="invalid_query"):
        await a.search("faostat", query, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("external", ["../private", "http://evil.test", "AF?limit=1", None, 1])
async def test_candidate_identifier_injection_rejected_before_request(source, external):
    _, a = adapter(lambda _: pytest.fail("no network expected"))
    with pytest.raises(SourceConnectorError, match="invalid_candidate"):
        await a.resolve({"source_id": source, "external_id": external})


@pytest.mark.asyncio
async def test_fao_search_uses_full_dynamic_catalog_and_exact_code_over_substring():
    rows = [fao("A" + str(i)) for i in range(23)] + [fao("AF"), fao("XX", FileRows=0), fao("YY", FileType="pdf")]
    calls = []
    def handle(request):
        calls.append(str(request.url))
        assert str(request.url) == FAO_CATALOG
        return httpx.Response(200, json={"Datasets": {"Dataset": rows}})
    _, a = adapter(handle)
    result = await a.search("faostat", '"researchers"', 2)
    assert result["total_count"] == 24
    assert [r["external_id"] for r in result["results"]] == ["A" + str(i) for i in range(10, 20)]
    assert (await a.search("faostat", "‘af’", 1))["results"][0]["external_id"] == "AF"
    assert len(calls) == 1
    assert (await a.search("faostat", "unknown topic", 1))["results"] == []
    assert all(r["access"] == "needs_resolution" for r in result["results"])


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [dict(FileRows=True), dict(FileRows=-1), dict(FileRows="1"), dict(FileRows=None),
    dict(CompressionFormat=None), dict(FileType=1), dict(DatasetCode="../private"), dict(FileLocation="https://evil.test/a.zip"),
    dict(DatasetName=""), dict(DatasetName=None)])
async def test_fao_rejects_non_observation_and_malformed_metadata(changes):
    _, a = adapter(lambda _: httpx.Response(200, json={"Datasets": {"Dataset": [fao(**changes)]}}))
    assert (await a.search("faostat", "researchers", 1))["results"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, {"Datasets": []}, {"Datasets": {}}, {"Datasets": {"Dataset": [None]}}])
async def test_fao_malformed_catalog_fails(payload):
    _, a = adapter(lambda _: httpx.Response(200, json=payload))
    with pytest.raises(SourceConnectorError, match="invalid_response"):
        await a.search("faostat", "agriculture", 1)


@pytest.mark.asyncio
async def test_bis_only_actual_official_flat_csv_anchors_and_complete_catalog():
    body = "".join(bis("WS_TEST_" + str(i)) for i in range(23)) + bis()
    body += '<script>' + bis("WS_SCRIPT") + '</script>'
    body += bis("WS_EVIL").replace("/static/bulk/", "https://evil.test/static/bulk/")
    body += bis("WS_DICTIONARY").replace("_csv_flat.zip", "_dictionary.zip")
    body += '<a href="/static/bulk/WS_NOTITLE_csv_flat.zip">missing expected title</a>'
    _, a = adapter(lambda r: httpx.Response(200, text=body))
    result = await a.search("bis", "policy rates", 3)
    assert result["total_count"] == 24
    assert len(result["results"]) == 4
    assert result["results"][-1]["external_id"] == "WS_CBPOL"
    assert result["results"][-1]["metadata"]["last_update"] == "Thu Oct 22 2026"
    assert result["results"][-1]["title"] == "Central bank policy rates"
    assert (await a.search("bis", "WS_CBPOL", 1))["total_count"] == 1


@pytest.mark.asyncio
async def test_bis_catalog_markup_changes_fail_instead_of_inventing_downloads():
    _, a = adapter(lambda _: httpx.Response(200, text="<html><h1>data page moved</h1></html>"))
    with pytest.raises(SourceConnectorError, match="invalid_response"):
        await a.search("bis", "interest", 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("source,external", [("faostat", "AF"), ("bis", "WS_CBPOL")])
async def test_resolve_reloads_authoritative_archive_ignores_caller_overrides_and_retains_format(source, external):
    updated = ["Thu Oct 22 2026" if source == "bis" else "2026-04-30T00:00:00"]
    def handle(request):
        return httpx.Response(200, text=bis(date=updated[0])) if source == "bis" else httpx.Response(200, json={"Datasets": {"Dataset": [fao(DateUpdate=updated[0])]}})
    _, a = adapter(handle)
    candidate = {"source_id": source, "external_id": external, "url": "https://evil.test/evil.zip", "size": 3, "license": "invented"}
    first, = await a.resolve(candidate)
    assert first["url"] == (BIS_URL if source == "bis" else FAO_URL)
    assert first["filename"].endswith(".zip") and first["content_type"] == "application/zip"
    assert first["source_filename"].endswith(".zip") and first["size"] is None
    assert first["version"] == updated[0] and first["license"] != "invented"
    updated[0] = "Fri Oct 23 2026" if source == "bis" else "2026-05-01T00:00:00"
    a._cache.clear()
    second, = await a.resolve(candidate)
    assert first["id"] != second["id"]
    assert await a.resolve({"source_id": source, "external_id": "WS_MISSING" if source == "bis" else "ZZ"}) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("source", SOURCES)
async def test_conflicting_duplicate_catalog_identifier_fails(source):
    def handle(request):
        if source == "faostat":
            return httpx.Response(200, json={"Datasets": {"Dataset": [fao(), fao(DateUpdate="different")]}})
        return httpx.Response(200, text=bis() + bis(date="Fri Oct 23 2026"))
    _, a = adapter(handle)
    with pytest.raises(SourceConnectorError, match="inconsistent_source_snapshot"):
        await a.search(source, "rates", 1)


@pytest.mark.asyncio
async def test_concurrent_resolution_shares_one_catalog_read():
    count = 0
    async def read(*a, **kw):
        nonlocal count
        count += 1
        await asyncio.sleep(0)
        return {"Datasets": {"Dataset": [fao()]}}
    a = InternationalOct22Adapters(read, None)
    result = await asyncio.gather(*(a.resolve({"source_id": "faostat", "external_id": "AF"}) for _ in range(12)))
    assert count == 1 and all(len(rows) == 1 for rows in result)


@pytest.mark.asyncio
@pytest.mark.parametrize("source,external,url", [("faostat", "AF", FAO_URL), ("bis", "WS_CBPOL", BIS_URL)])
async def test_native_complete_archive_download_and_receipt(source, external, url, monkeypatch):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("observations.csv", "country,year,value\nCN,2025,3\nFR,2025,8\n")
    body = buffer.getvalue()
    def handle(request):
        if str(request.url) == url:
            return httpx.Response(200, content=body, headers={"content-type": "application/zip", "content-length": str(len(body))})
        if source == "faostat":
            return httpx.Response(200, json={"Datasets": {"Dataset": [fao()]}})
        return httpx.Response(200, text=bis())
    s, a = adapter(handle)
    s._adapters[source] = a
    def validate(url, sid, eid):
        if not safe_file_url(sid, url):
            raise SourceConnectorError("unsafe_download_url")
        return url
    monkeypatch.setattr(s, "_safe_file_url", validate)
    candidate = (await a.search(source, external, 1))["results"][0]
    option, = await s.resolve(candidate)
    verified = await s.verify(candidate)
    assert len(verified) == 1
    stream, name, content_type, receipt = await s.download(candidate, option)
    try:
        assert stream.read() == body and name.endswith(".zip") and content_type == "application/zip"
        assert receipt["connector_id"] == source and receipt["external_id"] == external
        assert receipt["source_version"] == option["version"] and receipt["license"] == option["license"]
    finally:
        stream.close()
