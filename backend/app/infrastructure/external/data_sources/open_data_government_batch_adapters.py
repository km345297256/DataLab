"""Read-only CKAN catalogs backed by verified official file publishers.

Protocol: https://docs.ckan.org/en/2.11/api/
https://open.canada.ca/en/access-our-application-programming-interface-api
https://handbook.opendata.swiss/en/content/nutzen/api-nutzen.html
https://www.govdata.de/sparql-assistent

Catalog search hits are not data downloads. Only supported entity-file links
on individually registered official publishers proceed to the shared content
verifier. Downloads are reconstructed from fresh package_show manifests.
"""
from __future__ import annotations

import mimetypes
import re
from urllib.parse import unquote, urlsplit

from app.domain.external.data_source import SourceConnectorError
from app.infrastructure.external.data_sources.open_data_query_syntax import all_terms_query

PAGE_SIZE = 10
_IDENTIFIER = r"[A-Za-z0-9][A-Za-z0-9_-]{0,199}"
_UUID = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
_DOWNLOAD = rf"/dataset/{_IDENTIFIER}/resource/{_UUID}/download/[^/]+"

# No arbitrary host supplied by a search result is ever trusted. Each publisher
# below was verified with a complete original-file download, not a landing page.
_CONFIG = {
    "canada-open-data": ("https://open.canada.ca/data/en", "https://open.canada.ca/data/en/dataset/", {
        "www150.statcan.gc.ca": r"/n1/tbl/csv/[0-9]{8}-(?:eng|fra)\.zip"}),
    "uk-data-gov": ("https://ckan.publishing.service.gov.uk", "https://www.data.gov.uk/dataset/", {
        "dataworks.calderdale.gov.uk": r"/download/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/[^/]+"}),
    "switzerland-open-data": ("https://opendata.swiss", "https://opendata.swiss/en/dataset/", {
        "dam-api.bfs.admin.ch": r"/hub/api/dam/assets/[0-9]+/(?:master|appendix)"}),
    "germany-govdata": ("https://www.govdata.de/ckan", "https://www.govdata.de/ckan/dataset/", {
        "opendata.ruhr": _DOWNLOAD,
        "www.berlin.de": r"/sen/frauen/gleichstellung/gender-daten/daten/[A-Za-z0-9_.-]+\.(?:xlsx|csv|xls)"}),
    "ireland-data-gov": ("https://data.gov.ie", "https://data.gov.ie/dataset/", {
        "ws.cso.ie": r"/public/api\.restful/PxStat\.Data\.Cube_API\.ReadDataset/[A-Za-z0-9_-]+/(?:CSV/1\.0|XLSX/2007|JSON-stat/2\.0)/[a-z]{2}"}),
    "japan-e-gov": ("https://data.e-gov.go.jp/data", "https://data.e-gov.go.jp/data/dataset/", {
        "www.soumu.go.jp": r"/johotsusintokei/whitepaper/ja/[A-Za-z0-9_-]+/csv/[A-Za-z0-9_.-]+\.csv"}),
    "argentina-data-gov": ("https://datos.gob.ar", "https://datos.gob.ar/dataset/", {
        "infra.datos.gob.ar": r"/catalog/[A-Za-z0-9_-]+/dataset/[A-Za-z0-9_.-]+/distribution/[A-Za-z0-9_.-]+/download/[^/]+"}),
    "uruguay-open-data": ("https://catalogodatos.gub.uy", "https://catalogodatos.gub.uy/dataset/", {
        "catalogodatos.gub.uy": _DOWNLOAD}),
    "italy-dati": ("https://www.dati.gov.it/opendata", "https://www.dati.gov.it/opendata/dataset/", {
        "dati.comune.milano.it": _DOWNLOAD}),
    "canada-bc": ("https://catalogue.data.gov.bc.ca", "https://catalogue.data.gov.bc.ca/dataset/", {
        "catalogue.data.gov.bc.ca": _DOWNLOAD}),
    "canada-ontario": ("https://data.ontario.ca", "https://data.ontario.ca/dataset/", {
        "data.ontario.ca": _DOWNLOAD}),
    "canada-quebec": ("https://www.donneesquebec.ca/recherche", "https://www.donneesquebec.ca/recherche/dataset/", {
        "www.donneesquebec.ca": "/recherche" + _DOWNLOAD}),
    "australia-nsw": ("https://data.nsw.gov.au/data", "https://data.nsw.gov.au/data/dataset/", {
        "opendata.transport.nsw.gov.au": "/data" + _DOWNLOAD}),
    "australia-sa": ("https://data.sa.gov.au/data", "https://data.sa.gov.au/data/dataset/", {
        "data.sa.gov.au": "/data" + _DOWNLOAD}),
    "australia-vic": ("https://discover.data.vic.gov.au", "https://discover.data.vic.gov.au/dataset/", {
        "www.health.vic.gov.au": r"/sites/default/files/(?:[^/]+/)*[^/]+"}),
    "berlin-open-data": ("https://datenregister.berlin.de", "https://datenregister.berlin.de/dataset/", {
        "www.berlin.de": r"/sen/frauen/gleichstellung/gender-daten/daten/[A-Za-z0-9_.-]+\.(?:xlsx|csv|xls)"}),
}
SOURCES = frozenset(_CONFIG)
_FORMATS = frozenset({"CSV", "TSV", "TXT", "XLS", "XLSX", "ODS", "JSON", "GEOJSON", "JSONL", "NDJSON",
                      "JSON-STAT", "ZIP", "GZ", "BZ2", "7Z", "TAR", "PARQUET", "NC", "NETCDF", "H5", "HDF5",
                      "GPKG", "SHP", "SAV", "DTA", "RDS", "RDATA", "TIF", "TIFF", "ARFF"})
_DOCUMENT = re.compile(r"(?:metadata|metadatos|metadonnees|métadonnées|data[ _-]?dictionary|dictionnaire|"
                       r"readme|codebook|schema|メタデータ|データセット一覧)", re.I)


def safe_file_url(source_id: str, url: str) -> bool:
    if not isinstance(source_id, str) or source_id not in SOURCES or not isinstance(url, str) or len(url) > 16000 or re.search(r"[\x00-\x20\\]", url):
        return False
    try:
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.username or parts.password or parts.fragment or parts.query
                or parts.port not in (None, 443)):
            return False
        path = unquote(parts.path)
        if (re.search(r"[\x00-\x1f\\]", path) or re.search(r"%2f|%5c|%2e|%25", parts.path, re.I)
                or any(piece in {".", ".."} for piece in path.split("/"))):
            return False
        pattern = _CONFIG[source_id][2].get(parts.hostname)
        return bool(pattern and re.fullmatch(pattern, path))
    except (ValueError, TypeError):
        return False


def _identifier(value, reason="invalid_response"):
    if not isinstance(value, str) or not re.fullmatch(_IDENTIFIER, value):
        raise SourceConnectorError(reason)
    return value


def _localized(value):
    if isinstance(value, dict):
        value = next((value[k] for k in ("en", "fr", "de", "it", "ja", "es")
                      if isinstance(value.get(k), str) and value[k].strip()), "")
    return value if isinstance(value, str) else ""


def _license(record, resource=None):
    from app.infrastructure.external.data_sources.open_data_sources import _text
    for row in (resource or {}, record):
        for key in ("license_title", "license", "license_id"):
            value = _localized(row.get(key))
            if value and value.lower() not in {"notspecified", "not specified", "unknown"}:
                return _text(value, 300)
    return None


def _format(resource):
    declared = _localized(resource.get("format")).upper().rsplit("/", 1)[-1].strip()
    path = unquote(urlsplit(resource.get("url", "")).path)
    ext = path.rsplit("/", 1)[-1].rsplit(".", 1)[-1].upper()
    # Statistics Canada labels compressed CSV as CSV. Keep the actual archive
    # extension rather than renaming an original ZIP to .csv.
    if declared in {"HTML", "HTM", "PDF", "XML", "DOC", "DOCX", "API"}:
        return None
    return ext if ext in _FORMATS else declared if declared in _FORMATS else None


def _resources(source, record):
    rows = record.get("resources", [])
    if not isinstance(rows, list):
        raise SourceConnectorError("invalid_response")
    for row in rows:
        if not isinstance(row, dict) or row.get("state") not in (None, "active") or row.get("restricted") is True:
            continue
        url = row.get("url")
        if not safe_file_url(source, url):
            continue
        name = _localized(row.get("name"))
        if _DOCUMENT.search(name + " " + unquote(urlsplit(url).path)):
            continue
        fmt = _format(row)
        if fmt:
            yield row, fmt


def _public(record):
    # Missing private is normal on older CKAN installations; any explicit
    # non-false value is treated conservatively as non-public.
    return (isinstance(record, dict) and record.get("private", False) is False
            and record.get("state") in (None, "active"))


def _result(payload):
    if not isinstance(payload, dict) or payload.get("success") is not True or not isinstance(payload.get("result"), dict):
        raise SourceConnectorError("invalid_response")
    return payload["result"]


class GovernmentBatchAdapters:
    def __init__(self, fetch_json):
        self._json = fetch_json

    async def search(self, source_id, query, page):
        if not isinstance(source_id, str) or source_id not in SOURCES:
            raise SourceConnectorError("unknown_source")
        if not isinstance(query, str) or not query.strip() or len(query) > 500 or type(page) is not int or not 1 <= page <= 100:
            raise SourceConnectorError("invalid_query")
        from app.infrastructure.external.data_sources.open_data_sources import _candidate
        payload = await self._json(_CONFIG[source_id][0] + "/api/3/action/package_search", {
            "q": all_terms_query(query), "rows": PAGE_SIZE, "start": (page - 1) * PAGE_SIZE,
        })
        result = _result(payload)
        rows, total = result.get("results"), result.get("count")
        if not isinstance(rows, list) or len(rows) > PAGE_SIZE or type(total) is not int or total < 0:
            raise SourceConnectorError("invalid_response")
        candidates = []
        for row in rows:
            if not _public(row):
                continue
            external = _identifier(row.get("id"))
            resources = list(_resources(source_id, row))
            if not resources:
                continue
            title = _localized(row.get("title")) or _localized(row.get("name")) or external
            candidates.append(_candidate(
                source_id, external, title, _localized(row.get("notes")) or _localized(row.get("description")),
                _CONFIG[source_id][1] + external,
                formats=sorted({fmt for _, fmt in resources}), license=_license(row),
                metadata={"ckan_modified": row.get("metadata_modified") if isinstance(row.get("metadata_modified"), str) else None},
            ))
        return {"results": candidates, "total_count": total}

    async def resolve(self, candidate):
        if not isinstance(candidate, dict):
            raise SourceConnectorError("invalid_candidate")
        source = candidate.get("source_id")
        if not isinstance(source, str) or source not in SOURCES:
            raise SourceConnectorError("unknown_source")
        external = _identifier(candidate.get("external_id"), "invalid_candidate")
        metadata = candidate.get("metadata", {})
        if not isinstance(metadata, dict):
            raise SourceConnectorError("invalid_candidate")
        if source == "italy-dati":
            # dati.gov.it exposes complete package manifests through its read
            # search API but returns 403 for package_show. The exact ID filter
            # is validated below; never fall back to a fuzzy title search.
            result = _result(await self._json(_CONFIG[source][0] + "/api/3/action/package_search", {
                "fq": "id:" + external, "rows": 1,
            }))
            rows = result.get("results")
            if type(result.get("count")) is not int or result["count"] != 1 or not isinstance(rows, list) or len(rows) != 1:
                raise SourceConnectorError("invalid_response")
            row = rows[0]
            if not isinstance(row, dict):
                raise SourceConnectorError("invalid_response")
        else:
            row = _result(await self._json(_CONFIG[source][0] + "/api/3/action/package_show", {"id": external}))
        if row.get("id") != external:
            raise SourceConnectorError("invalid_response")
        if not _public(row):
            return []
        modified = row.get("metadata_modified") if isinstance(row.get("metadata_modified"), str) else None
        # CKAN's search index may lag package_show or render its timestamp in a
        # different timezone/precision. A catalog timestamp is not a pinned
        # dataset version. Use the fresh manifest for the receipt and file IDs;
        # the shared acquisition layer revalidates those before downloading.
        from app.infrastructure.external.data_sources.open_data_sources import _filename, _identity, _text
        options = []
        seen = set()
        for resource, fmt in _resources(source, row):
            resource_id = _identifier(resource.get("id"))
            url = resource["url"]
            if url in seen:
                continue
            seen.add(url)
            basename = unquote(urlsplit(url).path).rsplit("/", 1)[-1]
            if basename.rsplit(".", 1)[-1].upper() not in _FORMATS:
                basename = (_localized(resource.get("name")) or resource_id) + "." + fmt.lower()
            name = _filename(basename)
            size = resource.get("size")
            # Human-readable/rounded catalog sizes cannot be used to validate
            # a byte stream. Unknown remains unknown; there is no byte ceiling.
            # Some harvested catalogs encode missing file lengths as zero.
            # Actual empty files are rejected by the shared content verifier.
            size = size if type(size) is int and size > 0 else None
            version = modified or _localized(resource.get("last_modified")) or None
            mime = mimetypes.guess_type(name)[0]
            options.append({
                "id": _identity(source, external, version, resource_id, url, size),
                "source_id": source, "external_id": external, "version": version,
                "filename": _filename(f"{source}_{external}_{resource_id}_{name}"),
                "source_filename": _text(basename, 400), "size": size,
                "content_type": mime, "url": url, "checksum": None,
                "available": True, "reason": "", "kind": "源站文件",
                "license": _license(row, resource), "record_url": _CONFIG[source][1] + external,
            })
        return options
