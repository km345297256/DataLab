"""Offline provider contracts: pagination, real bytes, receipts and safe origins."""
import asyncio
import csv
import gzip
import hashlib
import io
import json

import httpx
import pytest

from app.domain.external.data_source import SourceConnectorError
from app.infrastructure.external.data_sources import open_data_sources as module
from app.infrastructure.external.data_sources.open_data_sources import OpenDataSources
from app.infrastructure.external.data_sources.open_data_transfer import Transfer


@pytest.fixture(autouse=True)
def isolated_transfers(tmp_path, monkeypatch):
    monkeypatch.setenv("OPEN_DATA_DOWNLOAD_ROOT", str(tmp_path / "transfers"))


DATA = b"country,value\nChina,12\n"
FILE_URL = "https://zenodo.org/api/records/123/files/data.csv/content"


def zenodo_record(*, data=DATA, url=FILE_URL, size=None, checksum=None, restricted=False):
    return {"id": 123, "doi": "10.5281/zenodo.123", "metadata": {
        "title": "Climate data", "description": "<p>Observed <b>climate</b> values</p>",
        "license": {"id": "cc-by-4.0"}, "access_right": "restricted" if restricted else "open"},
        "files": [{"key": "data.csv", "size": len(data) if size is None else size,
                   "checksum": checksum or "md5:" + hashlib.md5(data).hexdigest(),
                   "links": {"self": url}}]}


def zenodo_candidate():
    return {"source_id": "zenodo", "external_id": "123", "source_url": "https://zenodo.org/records/123"}


def provider(handler):
    return OpenDataSources(transport=httpx.MockTransport(handler))


def wb_indicator(code="SP.POP.TOTL", name="Population, total"):
    return {"id": code, "name": name, "source": {"id": "2", "value": "World Development Indicators"},
            "sourceNote": name + " observed values", "unit": "", "sourceOrganization": "World Bank"}


def wb_page(rows, *, page=1, total=None, per_page=2000, lastupdated="2026-01-01"):
    total = len(rows) if total is None else total
    return [{"page": page, "pages": (total + per_page - 1) // per_page, "total": total,
             "per_page": per_page, "lastupdated": lastupdated}, rows]


def observation(country="CN", *, year="2024", value=12):
    return {"indicator": {"id": "SP.POP.TOTL", "value": "Population, total"},
            "country": {"id": country, "value": "China" if country == "CN" else "India"},
            "countryiso3code": "CHN" if country == "CN" else "IND", "date": year,
            "value": value, "unit": "", "decimal": 0, "obs_status": ""}


def wb_candidate():
    return {"source_id": "worldbank", "external_id": "SP.POP.TOTL", "source_url": "https://data.worldbank.org/indicator/SP.POP.TOTL"}


class Chunks(httpx.AsyncByteStream):
    def __init__(self, *chunks):
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


def test_catalog_has_real_cross_discipline_sources_and_owned_list():
    sources = OpenDataSources()
    catalog = sources.catalog()
    from app.domain.models.open_data_registry import CATALOG
    assert catalog == list(CATALOG)
    assert {entry["id"] for entry in catalog} >= {"zenodo", "datacite", "worldbank", "ncbi-geo", "ncbi-sra"}
    catalog[0]["id"] = "invented"
    assert sources.catalog()[0]["id"] == "zenodo"


@pytest.mark.asyncio
async def test_filtered_provider_page_preserves_cursor_to_later_entity_data():
    class FilteredAdapter:
        async def search(self, source, query, page):
            assert source == "canada-open-data" and query == "population"
            return {"results": [] if page == 1 else [{"external_id": "public-data"}],
                    "total_count": 11}

    source = OpenDataSources()
    source._adapters["canada-open-data"] = FilteredAdapter()
    first = await source.search("canada-open-data", "population")
    assert first["results"] == [] and first["next_cursor"]
    second = await source.search("canada-open-data", "population", first["next_cursor"])
    assert second["results"] == [{"external_id": "public-data"}]
    assert second["next_cursor"] == ""
    with pytest.raises(SourceConnectorError, match="invalid_cursor"):
        await source.search("canada-open-data", "different topic", first["next_cursor"])


@pytest.mark.asyncio
async def test_zenodo_search_pagination_query_binding_and_normalization():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host == "zenodo.org"
        assert request.url.params["type"] == "dataset"
        assert request.url.params["size"] == "10"
        return httpx.Response(200, json={"hits": {"hits": [zenodo_record()], "total": {"value": 11}}})

    source = provider(handler)
    first = await source.search("zenodo", "climate")
    candidate = first["results"][0]
    assert candidate["description"] == "Observed climate values"
    assert candidate["license"] == "cc-by-4.0" and candidate["formats"] == ["csv"]
    assert candidate["size"] == len(DATA) and candidate["access"] == "needs_resolution"
    second = await source.search("zenodo", "climate", first["next_cursor"])
    assert requests[-1].url.params["page"] == "2"
    assert second["results"][0]["id"] == candidate["id"]
    for source_id, query in (("zenodo", "population"), ("datacite", "climate")):
        with pytest.raises(SourceConnectorError, match="invalid_cursor"):
            await source.search(source_id, query, first["next_cursor"])
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_verified_zenodo_file_uses_refetched_manifest_not_injected_url():
    requests, progress = [], []

    def handler(request):
        requests.append(request)
        if request.url.path == "/api/records/123":
            return httpx.Response(200, json=zenodo_record())
        assert str(request.url) == FILE_URL
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(200, content=DATA)

    async def on_progress(received, total):
        progress.append((received, total))

    source = provider(handler)
    option = (await source.resolve(zenodo_candidate()))[0]
    option.update(url="http://127.0.0.1/private", size=1, filename="../evil.sh", checksum="md5:" + "0" * 32)
    stream, filename, mime, metadata = await source.download(zenodo_candidate(), option, on_progress)
    try:
        assert stream.read() == DATA
        assert filename.startswith("zenodo_123_") and filename.endswith("data.csv")
        assert mime == "text/csv"
        assert metadata["sha256"] == hashlib.sha256(DATA).hexdigest()
        assert metadata["provider_checksum_verified"] is True
        assert metadata["checksum_origin"] == "computed_locally"
        assert metadata["source_url"] == "https://zenodo.org/records/123"
        assert progress[-1] == (len(DATA), len(DATA))
        assert len([r for r in requests if r.url.path == "/api/records/123"]) == 2
    finally:
        stream.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://evil.example/data.csv", "https://zenodo.org.evil.example/data.csv",
    "https://user:password@zenodo.org/api/records/123/files/data.csv/content",
    "https://zenodo.org:444/api/records/123/files/data.csv/content",
    "http://zenodo.org/api/records/123/files/data.csv/content",
    "https://zenodo.org/api/records/999/files/data.csv/content",
    "https://zenodo.org/api/records/123/files/%2e%2e/content",
    "https://zenodo.org/api/records/123/files/data.csv/content?access_token=secret",
])
async def test_manifest_download_urls_are_origin_and_record_scoped(url):
    source = provider(lambda request: httpx.Response(200, json=zenodo_record(url=url)))
    with pytest.raises(SourceConnectorError, match="unsafe_download_url"):
        await source.resolve(zenodo_candidate())


@pytest.mark.asyncio
async def test_changed_manifest_rejects_old_file_choice():
    record = zenodo_record()
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=record)

    source = provider(handler)
    option = (await source.resolve(zenodo_candidate()))[0]
    record["files"][0]["checksum"] = "md5:" + "0" * 32
    with pytest.raises(SourceConnectorError, match="invalid_file_selection"):
        await source.download(zenodo_candidate(), option)
    assert all(request.url.path == "/api/records/123" for request in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("response,reason", [
    (lambda: httpx.Response(302, headers={"location": "http://127.0.0.1/private"}), "unsafe_download_redirect"),
    (lambda: httpx.Response(200, content=DATA[:-1]), "download_integrity_failed"),
    (lambda: httpx.Response(200, content=b"x" * len(DATA)), "download_integrity_failed"),
    (lambda: httpx.Response(429, headers={"retry-after": "30"}), "rate_limited"),
])
async def test_file_failure_closes_temp_stream_and_never_follows_redirect(monkeypatch, response, reason):
    streams, requests = [], []
    original = Transfer.acquire

    async def temporary(self):
        transfer = await original(self)
        streams.append(transfer)
        return transfer

    monkeypatch.setattr(Transfer, "acquire", temporary)

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=zenodo_record()) if request.url.path == "/api/records/123" else response()

    source = provider(handler)
    option = (await source.resolve(zenodo_candidate()))[0]
    with pytest.raises(SourceConnectorError, match=reason):
        await source.download(zenodo_candidate(), option)
    assert streams and all(stream.file is None and stream.lock is None for stream in streams)
    assert len(requests) == 3 and all(request.url.host == "zenodo.org" for request in requests)


@pytest.mark.asyncio
async def test_chunked_download_accepts_unknown_content_length():
    record = zenodo_record()
    record["files"][0].update(size=None, checksum=None)
    source = provider(lambda request: httpx.Response(200, json=record) if request.url.path == "/api/records/123"
                      else httpx.Response(200, stream=Chunks(b"1234", b"56789")))
    option = (await source.resolve(zenodo_candidate()))[0]
    stream, *_ = await source.download(zenodo_candidate(), option)
    try:
        assert stream.read() == b"123456789"
    finally:
        stream.close()


@pytest.mark.asyncio
async def test_restricted_files_are_not_downloaded():
    for record, reason in [(zenodo_record(restricted=True), "restricted_access")]:
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=record)

        source = provider(handler)
        option = (await source.resolve(zenodo_candidate()))[0]
        assert not option["available"] and option["reason"]
        with pytest.raises(SourceConnectorError, match=reason):
            await source.download(zenodo_candidate(), option)
        assert all(request.url.path == "/api/records/123" for request in requests)


@pytest.mark.asyncio
async def test_download_cancellation_closes_stream(monkeypatch):
    streams = []
    original = Transfer.acquire

    async def temporary(self):
        transfer = await original(self)
        streams.append(transfer)
        return transfer

    monkeypatch.setattr(Transfer, "acquire", temporary)
    source = provider(lambda request: httpx.Response(200, json=zenodo_record()) if request.url.path == "/api/records/123" else httpx.Response(200, content=DATA))

    async def cancel(received, total):
        raise asyncio.CancelledError()

    option = (await source.resolve(zenodo_candidate()))[0]
    with pytest.raises(asyncio.CancelledError):
        await source.download(zenodo_candidate(), option, cancel)
    assert all(stream.file is None and stream.lock is None for stream in streams)


@pytest.mark.asyncio
async def test_datacite_leads_do_not_fetch_arbitrary_landing_pages():
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host == "api.datacite.org"
        assert request.url.params["resource-type-id"] == "dataset"
        prefixes = request.url.params['prefix'].split(',')
        assert {'10.5281', '10.7910', '10.1594', '10.5683', '10.34894', '10.57745'} <= set(prefixes)
        assert '10.5061' not in prefixes  # Disabled Dryad is not queried.
        return httpx.Response(200, json={"data": [{"id": "10.1234/example", "attributes": {
            "doi": "10.1234/example", "titles": [{"title": "Public climate data"}],
            "url": "http://localhost/secret", "descriptions": [{"description": "Real metadata"}],
            "rightsList": [{"rightsIdentifier": "cc-by-4.0"}]}}], "meta": {"total": 1}})

    source = provider(handler)
    candidate = (await source.search("datacite", "climate"))["results"][0]
    assert candidate["access"] == "lead" and candidate["source_url"] == "https://doi.org/10.1234/example"
    assert await source.resolve(candidate) == []
    with pytest.raises(SourceConnectorError, match="invalid_file_selection"):
        await source.download(candidate, {"id": "fake", "url": "https://evil.example/file"})
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_datacite_supported_doi_resolves_exact_zenodo_record():
    record = zenodo_record()
    source = provider(lambda request: httpx.Response(200, json=record))
    candidate = {"source_id": "datacite", "external_id": "10.5281/zenodo.123"}
    assert (await source.resolve(candidate))[0]["source_id"] == "zenodo"
    record["doi"] = "10.5281/zenodo.999"
    with pytest.raises(SourceConnectorError, match="invalid_candidate"):
        await source.resolve(candidate)


@pytest.mark.asyncio
async def test_datacite_routes_harvard_to_real_manifest_and_download_ignoring_index_urls_and_version():
    pid = 'doi:10.7910/DVN/ABC123'
    record = {'status': 'OK', 'data': {'protocol': 'doi', 'authority': '10.7910', 'identifier': 'DVN/ABC123',
        'latestVersion': {'datasetPersistentId': pid, 'versionNumber': 1, 'versionMinorNumber': 0,
            'versionState': 'RELEASED', 'license': {'name': 'CC0 1.0'}, 'files': [{'restricted': False, 'dataFile': {
                'id': 321, 'filename': 'observations.csv', 'filesize': len(DATA), 'contentType': 'text/csv',
                'checksum': {'type': 'MD5', 'value': hashlib.md5(DATA).hexdigest()}}}]}}}
    requests = []
    def handler(request):
        requests.append(request)
        assert request.url.host == 'dataverse.harvard.edu'
        if request.url.path == '/api/datasets/:persistentId/':
            assert request.url.params['persistentId'] == pid
            return httpx.Response(200, json=record)
        assert request.url.path == '/api/access/datafile/321'
        assert request.url.params['format'] == 'original'
        return httpx.Response(200, content=DATA, headers={'content-type': 'text/csv'})
    source = provider(handler)
    candidate = {'source_id': 'datacite', 'external_id': '10.7910/dvn/abc123',
        'source_url': 'http://127.0.0.1/private', 'metadata': {'version': '999.0', 'url': 'https://evil.example/file'}}
    option = (await source.verify(candidate))[0]
    assert option['source_id'] == 'harvard-dataverse' and option['version'] == '1.0'
    stream, _, _, receipt = await source.download(candidate, option)
    try:
        assert stream.read() == DATA
        assert receipt['source_version'] == '1.0' and receipt['provider_checksum_verified']
        assert receipt['source_url'] == 'https://doi.org/10.7910/DVN/ABC123'
    finally:
        stream.close()
    assert len(requests) == 4


@pytest.mark.asyncio
async def test_worldbank_or_keyword_search_cache_and_result_pagination():
    rows = [wb_indicator(f"EDU.{index}", "Education attainment") for index in range(11)]
    rows += [wb_indicator("EMP.1", "Employment rate"), wb_indicator("POP.1", "Population total")]
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.host == "api.worldbank.org"
        assert request.url.params["source"] == "2"
        return httpx.Response(200, json=wb_page(rows))

    source = provider(handler)
    first = await source.search("worldbank", "education employment")
    second = await source.search("worldbank", "education employment", first["next_cursor"])
    combined = first["results"] + second["results"]
    assert len(combined) == 12 and first["total_count"] == 12
    assert {row["external_id"] for row in combined} == {row["id"] for row in rows[:-1]}
    assert all(row["size"] is None and "未据此过滤" in row["match_notes"][0] for row in combined)
    assert len(requests) == 1
    with pytest.raises(SourceConnectorError, match="english_query_required"):
        await source.search("worldbank", "教育")


@pytest.mark.asyncio
async def test_worldbank_download_is_complete_csv_observations_not_catalog():
    requests, progress = [], []

    def handler(request):
        requests.append(request)
        if request.url.path == "/v2/indicator/SP.POP.TOTL":
            return httpx.Response(200, json=wb_page([wb_indicator()], per_page=50))
        assert request.url.path == "/v2/country/all/indicator/SP.POP.TOTL"
        page = int(request.url.params["page"])
        row = observation("CN", value=-0.2) if page == 1 else observation("IN", value=None)
        return httpx.Response(200, json=wb_page([row], page=page, total=2, per_page=1))

    async def on_progress(received, total):
        progress.append((received, total))

    source = provider(handler)
    option = (await source.resolve(wb_candidate()))[0]
    stream, filename, mime, metadata = await source.download(wb_candidate(), option, on_progress)
    try:
        content = stream.read()
        rows = list(csv.DictReader(io.StringIO(content.decode())))
        assert filename == "worldbank_SP.POP.TOTL.csv" and mime == "text/csv"
        assert len(rows) == 2 and rows[0]["country_iso3"] == "CHN" and rows[1]["country_iso3"] == "IND"
        assert rows[0]["year"] == "2024" and rows[0]["value"] == "-0.2" and rows[1]["value"] == ""
        assert rows[0]["retrieved_at"] and rows[0]["source_last_updated"] == "2026-01-01"
        assert metadata["snapshot_rows"] == 2 and metadata["snapshot_page_count"] == 2
        assert metadata["acquisition_method"] == "api_csv_snapshot"
        assert metadata["sha256"] == hashlib.sha256(content).hexdigest()
        assert metadata["provider_checksum_verified"] is False
        assert progress[-1] == (len(content), None)
    finally:
        stream.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing_page_rows", "changed_total", "changed_date", "duplicate_row", "wrong_indicator"])
async def test_worldbank_incomplete_or_changed_pages_never_return_success(failure):
    def handler(request):
        if "/country/all/" not in request.url.path:
            return httpx.Response(200, json=wb_page([wb_indicator()], per_page=50))
        page = int(request.url.params["page"])
        row = observation("CN" if page == 1 or failure == "duplicate_row" else "IN")
        if page == 2 and failure == "wrong_indicator":
            row["indicator"]["id"] = "OTHER"
        payload = wb_page([] if page == 2 and failure == "missing_page_rows" else [row],
                          page=page, total=3 if page == 2 and failure == "changed_total" else 2, per_page=1,
                          lastupdated="2026-01-02" if page == 2 and failure == "changed_date" else "2026-01-01")
        return httpx.Response(200, json=payload)

    source = provider(handler)
    option = (await source.resolve(wb_candidate()))[0]
    with pytest.raises(SourceConnectorError) as error:
        await source.download(wb_candidate(), option)
    assert error.value.reason in {"invalid_response", "inconsistent_source_snapshot"}


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{}, {"hits": {"hits": None, "total": 0}}, {"hits": {"hits": [], "total": -1}}])
async def test_malformed_metadata_is_not_an_empty_success(payload):
    source = provider(lambda request: httpx.Response(200, json=payload))
    with pytest.raises(SourceConnectorError, match="invalid_response"):
        await source.search("zenodo", "climate")


@pytest.mark.asyncio
async def test_metadata_limit_and_unknown_source(monkeypatch):
    monkeypatch.setattr(module, "MAX_METADATA_BYTES", 16)
    source = provider(lambda request: httpx.Response(200, content=b" " * 17))
    with pytest.raises(SourceConnectorError, match="response_too_large"):
        await source.search("zenodo", "climate")
    with pytest.raises(SourceConnectorError, match="unknown_source"):
        await source.search("arbitrary-http", "climate")


@pytest.mark.asyncio
async def test_text_metadata_bom_accept_and_bound(monkeypatch):
    def handler(request):
        assert request.headers['accept'] == 'text/csv'
        return httpx.Response(200, content=b'\xef\xbb\xbfid,name\n1,test\n')
    source = provider(handler)
    assert await source._text_metadata('https://example.org/catalog', accept='text/csv') == 'id,name\n1,test\n'
    monkeypatch.setattr(module, 'MAX_METADATA_BYTES', 8)
    with pytest.raises(SourceConnectorError, match='response_too_large'):
        await source._text_metadata('https://example.org/catalog', accept='text/csv')


@pytest.mark.asyncio
@pytest.mark.parametrize('status,body,error', [(200, b'\xff', 'invalid_response'), (302, b'', 'unsafe_download_redirect'), (403, b'', 'restricted_access')])
async def test_text_metadata_rejects_invalid_encoding_and_status(status, body, error):
    source = provider(lambda _: httpx.Response(status, content=body))
    with pytest.raises(SourceConnectorError, match=error):
        await source._text_metadata('https://example.org/catalog')


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
async def test_worldbank_nonfinite_observations_are_not_saved(value):
    def handler(request):
        payload = wb_page([observation(value=value)]) if "/country/all/" in request.url.path else wb_page([wb_indicator()])
        return httpx.Response(200, content=json.dumps(payload).encode())

    source = provider(handler)
    option = (await source.resolve(wb_candidate()))[0]
    with pytest.raises(SourceConnectorError, match="invalid_response"):
        await source.download(wb_candidate(), option)


@pytest.mark.asyncio
@pytest.mark.parametrize("source_id", ["ncbi-geo", "ncbi-sra"])
async def test_ncbi_enumeration_uses_registered_origins_and_preserves_compressed_bytes(source_id):
    data = gzip.compress(b"@sample\nACGT\n+\nIIII\n", mtime=0)
    accession = "GSE123" if source_id == "ncbi-geo" else "SRX123"
    candidate = {"source_id": source_id, "external_id": accession}
    requests = []
    file_url = ("https://ftp.ncbi.nlm.nih.gov/geo/series/GSEnnn/GSE123/suppl/counts.txt.gz"
                if source_id == "ncbi-geo" else "https://ftp.sra.ebi.ac.uk/vol1/fastq/SRR123/SRR123.fastq.gz")

    def handler(request):
        requests.append(request)
        if str(request.url) == file_url:
            return (httpx.Response(200, headers={"content-length": str(len(data))}) if request.method == "HEAD"
                    else httpx.Response(200, content=data))
        if source_id == "ncbi-geo":
            assert request.url.host == "ftp.ncbi.nlm.nih.gov"
            if request.url.path.endswith("/suppl/"):
                return httpx.Response(200, text='<a href="counts.txt.gz">counts</a><a href="https://evil.example/file">ignore</a>')
            return httpx.Response(404)
        assert request.url.host == "www.ebi.ac.uk"
        assert request.url.params["accession"] == accession
        return httpx.Response(200, json=[{"experiment_accession": accession, "run_accession": "SRR123",
            "fastq_ftp": file_url.removeprefix("https://"), "fastq_bytes": str(len(data)),
            "fastq_md5": hashlib.md5(data).hexdigest()}])

    source = provider(handler)
    options = await source.resolve(candidate)
    assert len(options) == 1 and options[0]["available"]
    stream, filename, mime, metadata = await source.download(candidate, options[0])
    try:
        assert stream.read() == data and filename.endswith(".gz")
        assert metadata["connector_id"] == source_id
        assert metadata["sha256"] == hashlib.sha256(data).hexdigest()
        assert metadata["provider_checksum_verified"] is (source_id == "ncbi-sra")
        assert all(request.url.host in {"ftp.ncbi.nlm.nih.gov", "www.ebi.ac.uk", "ftp.sra.ebi.ac.uk"} for request in requests)
        assert sum(request.method == "GET" and str(request.url) == file_url for request in requests) == 1
    finally:
        stream.close()


@pytest.mark.asyncio
async def test_verify_reads_real_partial_content_and_keeps_full_download_validation():
    requests = []
    data = DATA * 1000

    def handler(request):
        requests.append(request)
        if request.url.path == "/api/records/123":
            return httpx.Response(200, json=zenodo_record(data=data))
        if "range" in request.headers:
            assert request.headers["range"] == f"bytes=0-{module.VERIFY_SAMPLE_BYTES - 1}"
            sample = data[:module.VERIFY_SAMPLE_BYTES]
            return httpx.Response(206, content=sample, headers={"content-range": f"bytes 0-{len(sample)-1}/{len(data)}", "content-type": "text/csv"})
        return httpx.Response(200, content=b"x" * len(data))

    source = provider(handler)
    options = await source.verify(zenodo_candidate())
    assert len(options) == 1 and options[0]["available"]
    assert options[0]["size"] == len(data)
    assert options[0]["verification"]["sample_bytes"] == module.VERIFY_SAMPLE_BYTES
    assert "完整性" in options[0]["system_notes"][1]
    # A readable prefix never waives the original full checksum check.
    with pytest.raises(SourceConnectorError, match="download_integrity_failed"):
        await source.download(zenodo_candidate(), options[0])
    assert len([request for request in requests if request.url.path == "/api/records/123"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status,headers,body", [
    (206, {"content-range": "bytes 1-4/5"}, b"1234"),
        (206, {"content-range": "bytes 0-3/2"}, b"1234"),
    (206, {"content-range": "bytes 0-3/8", "content-length": "3"}, b"1234"),
    (206, {"content-range": "bytes 0-3/8"}, b"123"),
    (206, {"content-range": "bytes 0-3/8"}, b"12345"),
    (200, {"content-range": "bytes 0-3/8"}, b"1234"),
    (200, {"content-encoding": "gzip"}, b"1234"),
    (200, {"content-length": "nonsense"}, b"1234"),
    (200, {}, b""),
])
async def test_verify_rejects_invalid_ranges_encoding_and_empty_bodies(status, headers, body):
    record = zenodo_record()
    record["files"][0].update(size=None, checksum=None)
    source = provider(lambda request: httpx.Response(200, json=record) if request.url.path == "/api/records/123"
                      else httpx.Response(status, headers=headers, stream=Chunks(body)))
    assert await source.verify(zenodo_candidate()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("body,mime", [
    (b"<!DOCTYPE html><html><body>Login required</body></html>", "application/octet-stream"),
    (DATA, "text/html"),
    (b"  \n\t", "text/plain"),
    (b"Access denied", "text/plain"),
    (b'{"error":"Too many requests","status":429}', "application/json"),
    (b'{"doi":"10.1234/example","title":"A dataset","creators":[]}', "application/json"),
    (b"{}", "application/json"),
    (b"[]", "application/json"),
    (b"country,value\n", "text/csv"),
])
async def test_verify_excludes_html_errors_empty_and_metadata_only_content(body, mime):
    record = zenodo_record(data=body)
    source = provider(lambda request: httpx.Response(200, json=record) if request.url.path == "/api/records/123"
                      else httpx.Response(200, content=body, headers={"content-type": mime}))
    assert await source.verify(zenodo_candidate()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("name,body,mime", [
    ("observations.json", b'[{"year":2024,"value":3}]', "application/json"),
    ("measurements.json", b'{"value":0,"unit":"kg"}', "application/json"),
    ("interviews.txt", b"Participant 1: daily journeys by bicycle.", "text/plain"),
    ("measurements.unknown", b"\x01\x02\x03\xff", "application/octet-stream"),
    ("archival-scans.pdf", b"%PDF-1.7\n1 0 obj<< /Type /Catalog >>endobj\n%%EOF", "application/pdf"),
    ("interviews.md", b"# Interview 1\nThe participant travels by bicycle.", "text/markdown"),
    ("letters.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1archived letters", "application/msword"),
])
async def test_verify_preserves_real_json_text_and_unknown_cross_discipline_data(name, body, mime):
    record = zenodo_record(data=body)
    record["files"][0]["key"] = name
    source = provider(lambda request: httpx.Response(200, json=record) if request.url.path == "/api/records/123"
                      else httpx.Response(200, content=body, headers={"content-type": mime}))
    options = await source.verify(zenodo_candidate())
    assert len(options) == 1 and options[0]["size"] == len(body)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["README", "README.txt", "README.txt.gz", "README.pdf", "LICENSE.txt", "metadata.json", "ro-crate-metadata.json", "CITATION.cff"])
async def test_verify_excludes_explicit_documentation_and_metadata_before_probe(name):
    record = zenodo_record()
    record["files"][0]["key"] = name
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.path == "/api/records/123"
        return httpx.Response(200, json=record)

    assert await provider(handler).verify(zenodo_candidate()) == []
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("size,headers,expected", [
    (None, {"content-range": f"bytes 0-{len(DATA)-1}/{len(DATA)}"}, True),
    (None, {"content-range": f"bytes 0-{len(DATA)-1}/{(64 * 1024 * 1024)+1}"}, True),
    (len(DATA) + 1, {"content-range": f"bytes 0-{len(DATA)-1}/{len(DATA)}"}, False),
])
async def test_verify_range_proves_total_size_not_just_partial_response_length(size, headers, expected):
    record = zenodo_record()
    record["files"][0].update(size=size, checksum=None)
    source = provider(lambda request: httpx.Response(200, json=record) if request.url.path == "/api/records/123"
                      else httpx.Response(206, content=DATA, headers=headers))
    assert bool(await source.verify(zenodo_candidate())) is expected


@pytest.mark.asyncio
async def test_verify_accepts_unknown_size_and_rejects_restricted_redirected_files():
    for size, response, accepted in [
        (None, lambda: httpx.Response(200, stream=Chunks(DATA)), True),
        (None, lambda: httpx.Response(206, headers={"content-range": f"bytes 0-{len(DATA)-1}/*"}, stream=Chunks(DATA)), True),
        (len(DATA), lambda: httpx.Response(403), False),
        (len(DATA), lambda: httpx.Response(302, headers={"location": "http://127.0.0.1/secret"}), False),
    ]:
        record = zenodo_record()
        record["files"][0].update(size=size, checksum=None)
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=record) if request.url.path == "/api/records/123" else response()

        assert bool(await provider(handler).verify(zenodo_candidate())) is accepted
        assert len(requests) == 2 and all(request.url.host == "zenodo.org" for request in requests)


@pytest.mark.asyncio
async def test_verify_server_ignoring_range_reads_only_bounded_prefix_and_closes():
    class CountingStream(httpx.AsyncByteStream):
        read = 0
        closed = False

        async def __aiter__(self):
            for _ in range(10000):
                self.read += 1024
                yield b"x" * 1024

        async def aclose(self):
            self.closed = True

    stream = CountingStream()
    record = zenodo_record(size=10_240_000)
    record["files"][0]["key"] = "measurements.bin"
    source = provider(lambda request: httpx.Response(200, json=record) if request.url.path == "/api/records/123"
                      else httpx.Response(200, headers={"content-length": "10240000"}, stream=stream))
    assert len(await source.verify(zenodo_candidate())) == 1
    assert stream.read == module.VERIFY_SAMPLE_BYTES and stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("source_id, opt_out, expected_range", [
    ("cuhk-data", True, None),
    ("cuhk-data", "true", "bytes=0-16383"),
    ("zenodo", True, "bytes=0-16383"),
])
async def test_source_owned_get_probe_stays_bounded_and_cannot_affect_other_sources(source_id, opt_out, expected_range):
    class CountingStream(httpx.AsyncByteStream):
        read = 0
        closed = False

        async def __aiter__(self):
            for _ in range(10000):
                self.read += 1024
                yield b"x" * 1024

        async def aclose(self):
            self.closed = True

    stream = CountingStream()

    def handler(request):
        assert request.headers.get("range") == expected_range
        return httpx.Response(200, headers={"content-length": "10240000"}, stream=stream)

    option = {"source_id": source_id, "external_id": "123" if source_id == "zenodo" else "doi:10.48668/ABC123",
              "url": FILE_URL if source_id == "zenodo" else "https://researchdata.cuhk.edu.hk/api/access/datafile/123?format=original",
              "filename": "measurements.bin", "size": 10_240_000, "probe_without_range": opt_out}
    result = await provider(handler)._probe_file(option)
    assert result["verification"]["sample_bytes"] == module.VERIFY_SAMPLE_BYTES
    assert stream.read == module.VERIFY_SAMPLE_BYTES and stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["429", "503", "timeout"])
async def test_verify_transient_failure_is_not_a_false_no_data(failure):
    def handler(request):
        if request.url.path == "/api/records/123":
            return httpx.Response(200, json=zenodo_record())
        if failure == "timeout":
            raise httpx.ReadTimeout("network detail must not be exposed")
        return httpx.Response(int(failure), headers={"retry-after": "20"})

    with pytest.raises(SourceConnectorError) as error:
        await provider(handler).verify(zenodo_candidate())
    assert error.value.reason == ("rate_limited" if failure == "429" else "upstream_unavailable")


@pytest.mark.asyncio
async def test_verify_file_count_and_concurrency_are_bounded_and_subset_disclosed():
    record = zenodo_record()
    record["files"] = [{**record["files"][0], "key": f"data-{index}.csv"} for index in range(20)]
    active = maximum = probes = 0

    async def handler(request):
        nonlocal active, maximum, probes
        if request.url.path == "/api/records/123":
            return httpx.Response(200, json=record)
        active += 1
        probes += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.001)
        active -= 1
        return httpx.Response(200, content=DATA)

    options = await provider(handler).verify(zenodo_candidate())
    assert len(options) == probes == module.VERIFY_FILE_LIMIT
    assert maximum == module.VERIFY_FILE_CONCURRENCY
    assert "14 个可选文件未检查" in options[0]["system_notes"][0]


@pytest.mark.asyncio
async def test_complete_file_verification_includes_past_100_without_total_batch_timeout(monkeypatch):
    count = 103
    record = zenodo_record()
    record["files"] = [{**record["files"][0], "key": f"data-{index}.csv",
        "links": {"self": f"https://zenodo.org/api/records/123/files/data-{index}.csv/content"}}
        for index in range(count)]
    active = maximum = probes = 0
    async def handler(request):
        nonlocal active, maximum, probes
        if request.url.path == "/api/records/123":
            return httpx.Response(200, json=record)
        active += 1
        probes += 1
        maximum = max(maximum, active)
        try:
            await asyncio.sleep(0.005)
            return httpx.Response(200, content=DATA)
        finally:
            active -= 1
    monkeypatch.setattr(module, "VERIFY_TIMEOUT_SECONDS", 0.1)
    source = provider(handler)
    options = await source.verify_all(zenodo_candidate())
    assert len(options) == probes == count
    assert maximum == module.VERIFY_FILE_CONCURRENCY and active == 0
    assert options[-1]["source_filename"] == "data-102.csv"
    assert "103 个数据文件" in options[0]["system_notes"][0]
    assert all("未检查" not in note for note in options[0]["system_notes"])


@pytest.mark.asyncio
async def test_complete_verification_keeps_usable_subset_and_discloses_transient_failure():
    record = zenodo_record()
    record["files"] = [{**record["files"][0], "key": f"data-{index}.csv",
        "links": {"self": f"https://zenodo.org/api/records/123/files/data-{index}.csv/content"}}
        for index in range(43)]
    probes = []
    def handler(request):
        if request.url.path == "/api/records/123":
            return httpx.Response(200, json=record)
        probes.append(request.url.path)
        if "/data-41.csv/" in request.url.path:
            return httpx.Response(403)
        return httpx.Response(429 if "/data-42.csv/" in request.url.path else 200, content=DATA)
    options = await provider(handler).verify_all(zenodo_candidate())
    assert len(options) == 41 and len(probes) == 43
    assert options[0]["_verification_summary"] == {
        "verification_pending_count": 1, "unavailable_file_count": 1}
    assert "1 个文件核验失败" in options[0]["system_notes"][0]


@pytest.mark.asyncio
async def test_complete_verification_cancellation_joins_fixed_workers():
    record = zenodo_record()
    record["files"] = [{**record["files"][0], "key": f"data-{index}.csv"} for index in range(503)]
    started = asyncio.Event()
    active = probes = 0
    async def handler(request):
        nonlocal active, probes
        if request.url.path == "/api/records/123":
            return httpx.Response(200, json=record)
        active += 1
        probes += 1
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1
    task = asyncio.create_task(provider(handler).verify_all(zenodo_candidate()))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert active == 0 and probes <= module.VERIFY_FILE_CONCURRENCY


@pytest.mark.asyncio
async def test_verify_total_timeout_includes_manifest_and_cancels_workers(monkeypatch):
    monkeypatch.setattr(module, "VERIFY_TIMEOUT_SECONDS", 0.01)
    cancelled = []

    async def handler(request):
        try:
            await asyncio.sleep(100)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    with pytest.raises(SourceConnectorError, match="upstream_unavailable"):
        await provider(handler).verify(zenodo_candidate())
    assert cancelled


@pytest.mark.asyncio
async def test_verify_keeps_readable_subset_when_another_file_is_rate_limited():
    record = zenodo_record()
    record["files"].append({**record["files"][0], "key": "other.csv",
                            "links": {"self": FILE_URL.replace("data.csv", "other.csv")}})

    def handler(request):
        if request.url.path == "/api/records/123":
            return httpx.Response(200, json=record)
        if "other.csv" in request.url.path:
            return httpx.Response(429, headers={"retry-after": "30"})
        return httpx.Response(200, content=DATA)

    options = await provider(handler).verify(zenodo_candidate())
    assert len(options) == 1
    assert "另有 1 个文件核验失败" in options[0]["system_notes"][0]


@pytest.mark.asyncio
async def test_verify_does_not_convert_external_cancellation_into_partial_success():
    record = zenodo_record()
    record["files"].append({**record["files"][0], "key": "other.csv",
                            "links": {"self": FILE_URL.replace("data.csv", "other.csv")}})
    waiting = asyncio.Event()
    cancelled = []

    async def handler(request):
        if request.url.path == "/api/records/123":
            return httpx.Response(200, json=record)
        if "other.csv" in request.url.path:
            waiting.set()
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
        return httpx.Response(200, content=DATA)

    task = asyncio.create_task(provider(handler).verify(zenodo_candidate()))
    await waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled


@pytest.mark.asyncio
async def test_verify_own_time_budget_keeps_previously_confirmed_files(monkeypatch):
    monkeypatch.setattr(module, "VERIFY_TIMEOUT_SECONDS", 0.01)
    record = zenodo_record()
    record["files"].append({**record["files"][0], "key": "other.csv",
                            "links": {"self": FILE_URL.replace("data.csv", "other.csv")}})
    cancelled = []

    async def handler(request):
        if request.url.path == "/api/records/123":
            return httpx.Response(200, json=record)
        if "other.csv" in request.url.path:
            try:
                await asyncio.sleep(100)
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
        return httpx.Response(200, content=DATA)

    options = await provider(handler).verify(zenodo_candidate())
    assert len(options) == 1 and cancelled
    assert "另有 1 个文件核验失败" in options[0]["system_notes"][0]


@pytest.mark.asyncio
async def test_verify_small_complete_file_checks_known_checksum():
    source = provider(lambda request: httpx.Response(200, json=zenodo_record(checksum="md5:" + "0" * 32))
                      if request.url.path == "/api/records/123" else httpx.Response(200, content=DATA))
    assert await source.verify(zenodo_candidate()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("values,expected", [([], False), ([None], False), ([None, 0], True), ([12], True)])
async def test_worldbank_verify_requires_actual_non_null_observations(values, expected):
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/v2/indicator/SP.POP.TOTL":
            return httpx.Response(200, json=wb_page([wb_indicator()]))
        assert request.url.path == "/v2/country/all/indicator/SP.POP.TOTL"
        assert request.url.params["mrnev"] == "1" and request.url.params["per_page"] == "1000"
        return httpx.Response(200, json=wb_page([observation(year=str(2024-index), value=value) for index, value in enumerate(values)]))

    options = await provider(handler).verify(wb_candidate())
    assert bool(options) is expected
    assert len(requests) == 2
    if options:
        assert options[0]["verification"]["non_null_observations"] == sum(value is not None for value in values)
        assert "完整获取另行分页" in options[0]["system_notes"][0]


@pytest.mark.asyncio
async def test_worldbank_all_null_full_snapshot_is_not_saved_as_real_data():
    source = provider(lambda request: httpx.Response(200, json=wb_page([observation(value=None)]) if "/country/all/" in request.url.path else wb_page([wb_indicator()])))
    option = (await source.resolve(wb_candidate()))[0]
    with pytest.raises(SourceConnectorError, match="no_observations"):
        await source.download(wb_candidate(), option)


@pytest.mark.asyncio
async def test_unsupported_datacite_doi_never_becomes_verified_from_metadata():
    def handler(request):
        pytest.fail("An unsupported DOI must not probe arbitrary landing URLs")

    assert await provider(handler).verify({"source_id": "datacite", "external_id": "10.1234/example"}) == []


@pytest.mark.asyncio
async def test_large_zenodo_records_remain_resolvable_and_probeable():
    size = 5 * 1024**3
    record = zenodo_record(size=size)
    record["files"][0]["checksum"] = None
    source = provider(lambda request: httpx.Response(200, json=record) if request.url.path == "/api/records/123"
                      else httpx.Response(206, content=DATA, headers={"content-range": f"bytes 0-{len(DATA)-1}/{size}"}))
    assert (await source.resolve(zenodo_candidate()))[0]["available"]
    verified = await source.verify(zenodo_candidate())
    assert verified[0]["size"] == size and verified[0]["available"]
    assert verified[0]["system_notes"] and "limitations" not in verified[0]


@pytest.mark.asyncio
async def test_probe_rejects_premature_eof_despite_large_manifest_size():
    record = zenodo_record(size=5 * 1024**3)
    record["files"][0]["checksum"] = None
    source = provider(lambda request: httpx.Response(200, json=record) if request.url.path == "/api/records/123"
                      else httpx.Response(200, stream=Chunks(DATA)))
    assert await source.verify(zenodo_candidate()) == []


@pytest.mark.asyncio
async def test_ncbi_new_flow_can_enumerate_large_files_while_legacy_default_stays_bounded():
    from app.infrastructure.external.data_sources.study_download import StudyDownloadClient
    size = 5 * 1024**3
    def handler(request):
        return httpx.Response(200, json=[{"experiment_accession": "SRX123", "run_accession": "SRR123",
            "fastq_ftp": "ftp.sra.ebi.ac.uk/vol1/fastq/SRR123/SRR123.fastq.gz", "fastq_bytes": str(size), "fastq_md5": "a" * 32}])
    transport = httpx.MockTransport(handler)
    legacy = await StudyDownloadClient(transport).list_files("ncbi-sra", "SRX123")
    assert not legacy[0]["available"]
    options = await OpenDataSources(transport=transport).resolve({"source_id": "ncbi-sra", "external_id": "SRX123"})
    assert options[0]["available"] and options[0]["size"] == size


@pytest.mark.asyncio
async def test_ncbi_new_flow_enumerates_all_runs_beyond_legacy_20_files():
    from app.infrastructure.external.data_sources.study_download import StudyDownloadClient
    rows = [{"experiment_accession": "SRX123", "run_accession": f"SRR{1000+i}",
        "fastq_ftp": f"ftp.sra.ebi.ac.uk/vol1/fastq/SRR100/SRR{1000+i}.fastq.gz",
        "fastq_bytes": "123", "fastq_md5": "a" * 32} for i in range(43)]
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=rows))
    assert len(await StudyDownloadClient(transport).list_files("ncbi-sra", "SRX123")) == 20
    options = await OpenDataSources(transport=transport).resolve({"source_id": "ncbi-sra", "external_id": "SRX123"})
    assert len(options) == 43 and options[-1]["source_filename"] == "SRR1042.fastq.gz"


@pytest.mark.asyncio
async def test_ncbi_new_flow_enumerates_all_geo_directory_files():
    from app.infrastructure.external.data_sources.study_download import StudyDownloadClient
    def handler(request):
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": "123"})
        if request.url.path.endswith("/suppl/"):
            listing = "".join(f'<a href="data-{i}.csv">data</a>' for i in range(43))
            return httpx.Response(200, text=listing)
        return httpx.Response(404)
    transport = httpx.MockTransport(handler)
    assert len(await StudyDownloadClient(transport).list_files("ncbi-geo", "GSE123")) == 20
    options = await OpenDataSources(transport=transport).resolve({"source_id": "ncbi-geo", "external_id": "GSE123"})
    assert len(options) == 43 and options[-1]["source_filename"] == "data-42.csv"


@pytest.mark.asyncio
async def test_worldbank_snapshot_is_not_cut_off_at_old_page_cap():
    def handler(request):
        if "/country/all/" not in request.url.path:
            return httpx.Response(200, json=wb_page([wb_indicator()]))
        page = int(request.url.params["page"])
        return httpx.Response(200, json=wb_page([observation(year=str(1900+page))], page=page, total=101, per_page=1))
    source = provider(handler)
    option = (await source.resolve(wb_candidate()))[0]
    stream, _, _, metadata = await source.download(wb_candidate(), option)
    try:
        assert len(list(csv.DictReader(io.StringIO(stream.read().decode())))) == 101
        assert metadata["snapshot_page_count"] == 101
    finally: stream.close()
