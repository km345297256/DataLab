from copy import deepcopy

import pytest

from app.domain.external.data_source import SourceConnectorError
from app.domain.models.open_data_government_batch_definitions import DEFINITIONS
from app.infrastructure.external.data_sources.open_data_government_batch_adapters import (
    GovernmentBatchAdapters, SOURCES, safe_file_url,
)

DATASET = "c8e0d421-a4c7-43d4-aac9-aa13ca634c16"
RESOURCE = "25d7fb36-003d-4d0d-a8b3-18ada3e8a987"
PATH = f"/dataset/{DATASET}/resource/{RESOURCE}/download/observations.csv"
URLS = {
    "canada-open-data": "https://www150.statcan.gc.ca/n1/tbl/csv/32100397-eng.zip",
    "uk-data-gov": "https://dataworks.calderdale.gov.uk/download/233x7/j3n/population%20single%20year.csv",
    "switzerland-open-data": "https://dam-api.bfs.admin.ch/hub/api/dam/assets/18084208/master",
    "germany-govdata": "https://opendata.ruhr" + PATH,
    "ireland-data-gov": "https://ws.cso.ie/public/api.restful/PxStat.Data.Cube_API.ReadDataset/FY108/CSV/1.0/en",
    "japan-e-gov": "https://www.soumu.go.jp/johotsusintokei/whitepaper/ja/h28/csv/n5205011.csv",
    "argentina-data-gov": "https://infra.datos.gob.ar/catalog/sspm/dataset/49/distribution/49.1/download/population.csv",
    "uruguay-open-data": "https://catalogodatos.gub.uy" + PATH,
    "italy-dati": "https://dati.comune.milano.it" + PATH,
    "canada-bc": "https://catalogue.data.gov.bc.ca" + PATH,
    "canada-ontario": "https://data.ontario.ca" + PATH,
    "canada-quebec": "https://www.donneesquebec.ca/recherche" + PATH,
    "australia-nsw": "https://opendata.transport.nsw.gov.au/data" + PATH,
    "australia-sa": "https://data.sa.gov.au/data" + PATH,
    "australia-vic": "https://www.health.vic.gov.au/sites/default/files/2024-01/observations.xlsx",
    "berlin-open-data": "https://www.berlin.de/sen/frauen/gleichstellung/gender-daten/daten/2023_1_1_bevoelkerung_final.xlsx",
}


def record(source="canada-bc"):
    return {"id": DATASET, "name": "population-observations", "title": {"fr": "Population", "en": "Population observations"},
            "notes": {"en": "<p>Observed population</p>"}, "state": "active", "private": False,
            "metadata_modified": "2026-10-19T12:00:00", "license_title": "Source-issued open government licence",
            "resources": [{"id": RESOURCE, "name": "Population observations", "format": "CSV",
                           "size": 2**40, "url": URLS[source]}]}


def service(row, *, search_rows=None):
    calls = []

    async def fetch(url, params=None):
        calls.append((url, deepcopy(params)))
        if url.endswith("package_search"):
            if params.get("fq"):
                return {"success": True, "result": {"results": [deepcopy(row)], "count": 1}}
            return {"success": True, "result": {"results": search_rows if search_rows is not None else [deepcopy(row)], "count": 21}}
        return {"success": True, "result": deepcopy(row)}
    return GovernmentBatchAdapters(fetch), calls


@pytest.mark.asyncio
async def test_large_resource_manifest_keeps_data_after_document_entries():
    row = record()
    row['resources'] = [{**row['resources'][0], 'name': 'Data dictionary'}] * 10000 + row['resources']
    adapter, _ = service(row)
    options = await adapter.resolve({'source_id': 'canada-bc', 'external_id': DATASET})
    assert len(options) == 1 and options[0]['url'] == URLS['canada-bc']


def test_definitions_cover_only_verified_protocol_sources():
    assert SOURCES == URLS.keys() == {row["id"] for row in DEFINITIONS}
    assert len(SOURCES) == 16
    unavailable = {"argentina-data-gov", "australia-nsw", "australia-sa", "canada-ontario", "italy-dati", "uruguay-open-data"}
    assert {row["id"] for row in DEFINITIONS if not row["enabled"]} == unavailable
    assert all(row["connector_enabled"] == row["enabled"] and row["authentication"] == "none" for row in DEFINITIONS)
    assert all("当前运行环境" in row["limitation"] for row in DEFINITIONS if not row["enabled"])
    assert all("省" in row["title"] or "州" in row["title"] for row in DEFINITIONS
               if row["id"] in {"canada-bc", "canada-ontario", "canada-quebec", "australia-nsw", "australia-sa", "australia-vic"})


@pytest.mark.asyncio
@pytest.mark.parametrize("source", sorted(SOURCES))
async def test_catalog_to_fresh_manifest_for_each_verified_origin(source):
    row = record(source)
    adapter, calls = service(row)
    page = await adapter.search(source, " population ", 2)
    assert calls[0][0].endswith("/api/3/action/package_search")
    assert calls[0][1] == {"q": "population", "rows": 10, "start": 10}
    assert page["total_count"] == 21
    candidate = page["results"][0]
    assert candidate["title"] == "Population observations"
    assert candidate["description"] == "Observed population"
    assert candidate["license"] == row["license_title"]
    candidate["url"] = "https://attacker.example/file.csv"
    candidate["metadata"]["download_url"] = "https://attacker.example/file.csv"
    files = await adapter.resolve(candidate)
    if source == "italy-dati":
        assert calls[-1][0].endswith("/api/3/action/package_search")
        assert calls[-1][1] == {"fq": "id:" + DATASET, "rows": 1}
    else:
        assert calls[-1][0].endswith("/api/3/action/package_show")
        assert calls[-1][1] == {"id": DATASET}
    assert files[0]["url"] == URLS[source]
    assert files[0]["size"] == 2**40  # No file size ceiling introduced.
    assert files[0]["license"] == row["license_title"]
    assert files[0]["version"] == row["metadata_modified"]
    assert files[0]["available"] is True
    assert safe_file_url(source, files[0]["url"])
    if source == "canada-open-data":
        assert files[0]["filename"].endswith(".zip")
        assert candidate["formats"] == ["ZIP"]


@pytest.mark.parametrize("source", sorted(SOURCES))
def test_download_routes_reject_host_confusion_and_redirection_parameters(source):
    url = URLS[source]
    assert safe_file_url(source, url)
    host, path = url.removeprefix("https://").split("/", 1)
    for malicious in (url.replace("https://", "http://"), url + "?url=https://attacker.example/data.csv",
                      url + "#fragment", f"https://{host}.attacker.example/{path}",
                      f"https://user@{host}/{path}", f"https://{host}:8443/{path}",
                      f"https://{host}/../{path}", f"https://{host}/%2e%2e/{path}",
                      f"https://{host}/%252e%252e/{path}", f"https://{host}/%2f{path}",
                      f"https://{host}/%5c{path}", f"https://{host}/%00{path}",
                      "https://127.0.0.1/file.csv", "https://s3.amazonaws.com/arbitrary-bucket/file.csv",
                      "https://opendata.arcgis.com/datasets/arbitrary-tenant.csv"):
        assert not safe_file_url(source, malicious), malicious
    assert not safe_file_url(source, f"https://{host}/api/3/action/package_show")
    assert not safe_file_url("unknown-source", url)
    assert not safe_file_url([], url)


@pytest.mark.asyncio
async def test_private_inactive_documentation_and_unsafe_resources_hidden():
    row = record()
    docs = deepcopy(row)
    docs["resources"][0]["name"] = "Data dictionary"
    unsafe = deepcopy(row)
    unsafe["resources"][0]["url"] = "https://attacker.example/data.csv"
    pdf = deepcopy(row)
    pdf["resources"][0]["format"] = "PDF"
    empty = deepcopy(row)
    empty["resources"] = []
    private = {**row, "private": True}
    deleted = {**row, "state": "deleted"}
    adapter, _ = service(row, search_rows=[docs, unsafe, pdf, empty, private, deleted, row])
    page = await adapter.search("canada-bc", "population", 1)
    assert len(page["results"]) == 1
    for changed in (private, deleted):
        adapter, _ = service(changed)
        assert await adapter.resolve(page["results"][0]) == []


@pytest.mark.asyncio
async def test_stale_catalog_index_uses_fresh_manifest_but_wrong_dataset_is_rejected():
    row = record()
    adapter, _ = service(row)
    candidate = (await adapter.search("canada-bc", "population", 1))["results"][0]
    row["metadata_modified"] = "2026-10-20T12:00:00"
    files = await adapter.resolve(candidate)
    assert files[0]["version"] == "2026-10-20T12:00:00"
    row["id"] = "unrelated-dataset"
    with pytest.raises(SourceConnectorError, match="invalid_response"):
        await adapter.resolve(candidate)


@pytest.mark.asyncio
async def test_provider_format_uri_rounded_size_and_duplicate_links():
    row = record()
    row["resources"][0].update(format="http://publications.europa.eu/resource/authority/file-type/CSV", size="17.7 KiB")
    row["resources"].append(deepcopy(row["resources"][0]))
    adapter, _ = service(row)
    candidate = (await adapter.search("canada-bc", "population", 1))["results"][0]
    files = await adapter.resolve(candidate)
    assert candidate["formats"] == ["CSV"]
    assert len(files) == 1
    assert files[0]["size"] is None


@pytest.mark.asyncio
async def test_missing_and_resource_specific_licences_are_not_relabelled_as_open():
    row = record()
    row["resources"][0]["license"] = "http://dcat-ap.de/def/licenses/other-closed"
    adapter, _ = service(row)
    candidate = (await adapter.search("canada-bc", "population", 1))["results"][0]
    assert (await adapter.resolve(candidate))[0]["license"].endswith("other-closed")
    del row["resources"][0]["license"]
    del row["license_title"]
    assert (await adapter.resolve(candidate))[0]["license"] is None


@pytest.mark.asyncio
async def test_catalog_zero_size_means_unreported_length_until_content_check():
    row = record("germany-govdata")
    row["resources"][0].update(size=0, url=URLS["berlin-open-data"], format="XLSX")
    adapter, _ = service(row)
    candidate = (await adapter.search("germany-govdata", "population", 1))["results"][0]
    assert (await adapter.resolve(candidate))[0]["size"] is None


@pytest.mark.asyncio
async def test_query_and_candidate_identifiers_are_not_urls():
    adapter, calls = service(record())
    for query, page in (("", 1), ("topic", True), ("topic", 101), ("x" * 501, 1)):
        with pytest.raises(SourceConnectorError, match="invalid_query"):
            await adapter.search("canada-bc", query, page)
    for candidate in ({"source_id": "canada-bc", "external_id": "https://attacker.example"},
                      {"source_id": "canada-bc", "external_id": "../secret"},
                      {"source_id": "canada-bc", "external_id": DATASET, "metadata": []}):
        with pytest.raises(SourceConnectorError, match="invalid_candidate"):
            await adapter.resolve(candidate)
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"success": False, "result": {}}, {"success": True, "result": []},
                                    {"success": True, "result": {"results": [], "count": True}}])
async def test_ckan_http_200_is_not_necessarily_success(payload):
    async def fetch(*args, **kwargs):
        return payload
    with pytest.raises(SourceConnectorError, match="invalid_response"):
        await GovernmentBatchAdapters(fetch).search("canada-bc", "population", 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("rows,count", [([], 0), ([record("italy-dati"), record("italy-dati")], 2),
                                        ([{**record("italy-dati"), "id": "wrong-dataset"}], 1)])
async def test_italian_exact_id_manifest_never_accepts_missing_or_fuzzy_results(rows, count):
    async def fetch(url, params=None):
        assert params == {"fq": "id:" + DATASET, "rows": 1}
        return {"success": True, "result": {"results": rows, "count": count}}
    with pytest.raises(SourceConnectorError, match="invalid_response"):
        await GovernmentBatchAdapters(fetch).resolve({"source_id": "italy-dati", "external_id": DATASET})
