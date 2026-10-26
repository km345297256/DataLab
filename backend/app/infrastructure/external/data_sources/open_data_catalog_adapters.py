"""Public original-file access for three Dataverse installations and PANGAEA.

The installations and storage paths are deliberately enumerated. A search
index hit never supplies a trusted download URL: manifests are re-read from
the relevant repository, then the common acquisition layer probes each file.

Contracts:
https://guides.dataverse.org/en/latest/api/search.html
https://guides.dataverse.org/en/latest/api/dataaccess.html
https://wiki.pangaea.de/wiki/Data_Access_and_Reuse
"""
from __future__ import annotations

import mimetypes
import re
from urllib.parse import parse_qsl, unquote, urlsplit

from app.domain.external.data_source import SourceConnectorError
from app.infrastructure.external.data_sources.open_data_repository_adapters import (
    _dataverse_query, _numeric, _version,
)

PAGE_SIZE = 10
INSTALLATIONS = {
    "borealis": ("https://borealisdata.ca", r"10\.5683/SP[23]?/[A-Za-z0-9]{4,20}", "10.5683/"),
    "dataverse-nl": ("https://dataverse.nl", r"10\.34894/[A-Za-z0-9]{4,20}", "10.34894/"),
    "recherche-data-gouv": ("https://entrepot.recherche.data.gouv.fr", r"10\.57745/[A-Za-z0-9]{4,20}", "10.57745/"),
}
SOURCES = frozenset((*INSTALLATIONS, "pangaea"))


def _pid(source_id, value):
    if (not isinstance(value, str) or not value.lower().startswith("doi:")
            or not re.fullmatch(INSTALLATIONS[source_id][1], value[4:], re.I)):
        raise SourceConnectorError("invalid_candidate")
    return "doi:" + value[4:].upper()


def _pangaea_doi(value):
    if not isinstance(value, str) or not re.fullmatch(r"10\.1594/PANGAEA\.[1-9][0-9]{0,11}", value, re.I):
        raise SourceConnectorError("invalid_candidate")
    return value.upper()


def _checksum(kind, value):
    if value in (None, ""):
        return None
    kind = str(kind).lower().replace("-", "")
    lengths = {"md5": 32, "sha1": 40, "sha256": 64, "sha512": 128}
    if kind not in lengths or not isinstance(value, str) or not re.fullmatch(r"[a-fA-F0-9]{%d}" % lengths[kind], value):
        raise SourceConnectorError("invalid_response")
    return kind + ":" + value.lower()


def safe_file_url(source_id, url):
    """Only the observed repository endpoint and named object-storage paths."""
    if not isinstance(url, str) or len(url) > 16000 or re.search(r"[\x00-\x20\\]", url):
        return False
    try:
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.username or parts.password or parts.fragment
                or parts.port not in (None, 443)):
            return False
        path = unquote(parts.path)
        if (re.search(r"[\x00-\x1f\\]", path) or re.search(r"%2f|%5c|%2e", parts.path, re.I)
                or any(piece in {".", ".."} for piece in path.split("/"))):
            return False
        if source_id == "pangaea":
            return (parts.hostname == "doi.pangaea.de"
                    and bool(re.fullmatch(r"/10\.1594/PANGAEA\.[1-9][0-9]{0,11}", path))
                    and parse_qsl(parts.query, keep_blank_values=True) == [("format", "textfile")])
        if source_id not in INSTALLATIONS:
            return False
        if parts.hostname == urlsplit(INSTALLATIONS[source_id][0]).hostname:
            return (bool(re.fullmatch(r"/api/access/datafile/[1-9][0-9]{0,19}", path))
                    and parse_qsl(parts.query, keep_blank_values=True) == [("format", "original")])
        storage = {
            "borealis": ("olrc2.scholarsportal.info", r"/dataverse/10\.5683/SP[23]?/[A-Za-z0-9]{4,20}/[A-Za-z0-9._-]+"),
            "dataverse-nl": ("objectstore.surf.nl", r"/store/10\.34894/[A-Za-z0-9]{4,20}/[A-Za-z0-9._-]+"),
            "recherche-data-gouv": ("prod-datainra.opens3r-tls.stockage.inrae.fr", r"/10\.57745/[A-Za-z0-9]{4,20}/[A-Za-z0-9._-]+"),
        }
        host, pattern = storage[source_id]
        return parts.hostname == host and bool(re.fullmatch(pattern, path))
    except (ValueError, TypeError):
        return False


class CatalogAdapters:
    def __init__(self, fetch_json):
        self._json = fetch_json

    async def search(self, source_id, query, page):
        if source_id not in SOURCES:
            raise SourceConnectorError("unknown_source")
        if not isinstance(query, str) or not query.strip() or len(query) > 500 or type(page) is not int or not 1 <= page <= 100:
            raise SourceConnectorError("invalid_query")
        try:
            return (await self._search_pangaea(query, page) if source_id == "pangaea"
                    else await self._search_dataverse(source_id, query, page))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise SourceConnectorError("invalid_response") from None

    async def resolve(self, candidate):
        source_id = candidate.get("source_id")
        if source_id not in SOURCES:
            raise SourceConnectorError("unknown_source")
        try:
            return (await self._resolve_pangaea(candidate) if source_id == "pangaea"
                    else await self._resolve_dataverse(source_id, candidate))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise SourceConnectorError("invalid_response") from None

    async def _search_dataverse(self, source_id, query, page):
        from app.infrastructure.external.data_sources.open_data_sources import _candidate
        base, _, prefix = INSTALLATIONS[source_id]
        payload = await self._json(base + "/api/search", {
            "q": _dataverse_query(query), "type": "dataset",
            "fq": ["publicationStatus:Published", f'dsPersistentId:"doi:{prefix}"'],
            "per_page": PAGE_SIZE, "start": (page - 1) * PAGE_SIZE, "sort": "score",
        })
        if payload.get("status") != "OK":
            raise ValueError()
        rows, total = payload["data"]["items"], payload["data"]["total_count"]
        if not isinstance(rows, list) or len(rows) > PAGE_SIZE or type(total) is not int or total < 0:
            raise ValueError()
        results = []
        for row in rows:
            if row.get("type") != "dataset" or row.get("versionState") != "RELEASED" or row.get("fileCount") == 0:
                continue
            try:
                external = _pid(source_id, row.get("global_id"))
            except SourceConnectorError:
                # Federated/harvested records require their own resolver.
                continue
            if type(row.get("majorVersion")) is not int or type(row.get("minorVersion")) is not int:
                raise ValueError()
            version = _version(f"{row['majorVersion']}.{row['minorVersion']}")
            results.append(_candidate(source_id, external, row["name"], row.get("description"),
                "https://doi.org/" + external[4:], metadata={"doi": external[4:].lower(), "version": version,
                                                          "published": row.get("published_at")}))
        return {"results": results, "total_count": total}

    async def _resolve_dataverse(self, source_id, candidate):
        from app.infrastructure.external.data_sources.open_data_sources import _filename, _identity, _size, _text
        base = INSTALLATIONS[source_id][0]
        external = _pid(source_id, candidate["external_id"])
        payload = await self._json(base + "/api/datasets/:persistentId/", {"persistentId": external})
        if payload.get("status") != "OK":
            raise ValueError()
        dataset = payload["data"]
        actual = str(dataset.get("protocol")) + ":" + str(dataset.get("authority")) + "/" + str(dataset.get("identifier"))
        if _pid(source_id, actual) != external:
            raise SourceConnectorError("invalid_candidate")
        latest = dataset["latestVersion"]
        requested = (candidate.get("metadata") or {}).get("version")
        requested = _version(requested) if requested is not None else None
        selected = latest
        if requested is not None and requested != f"{latest['versionNumber']}.{latest['versionMinorNumber']}":
            previous = await self._json(base + "/api/datasets/:persistentId/versions/" + requested, {"persistentId": external})
            if previous.get("status") != "OK":
                raise ValueError()
            selected = previous["data"]
        version = _version(f"{selected['versionNumber']}.{selected['versionMinorNumber']}")
        if requested is not None and requested != version:
            raise SourceConnectorError("invalid_candidate")
        if _pid(source_id, selected.get("datasetPersistentId")) != external:
            raise SourceConnectorError("invalid_candidate")
        if selected.get("versionState") != "RELEASED":
            return []
        if any(part.get("guestbookId") not in (None, 0, -1) or part.get("guestbook") for part in (dataset, latest, selected)):
            return []
        files = selected["files"]
        if not isinstance(files, list):
            raise ValueError()
        rights = selected.get("license") or {}
        license_text = _text(rights.get("name") or selected.get("termsOfUse"), 500) if isinstance(rights, dict) else _text(rights, 500)
        results = []
        for item in files:
            data = item["dataFile"]
            if item.get("restricted") is not False or item.get("embargo") or data.get("embargo"):
                continue
            file_id = _numeric(data["id"])
            original = bool(data.get("originalFileFormat"))
            name = data.get("originalFileName") if original else data["filename"]
            if not isinstance(name, str) or not name or len(name) > 2000:
                raise ValueError()
            size = _size(data.get("originalFileSize") if original else data.get("filesize"))
            if size == 0:
                continue
            digest = data.get("checksum") or {"type": "MD5", "value": data.get("md5")}
            checksum = _checksum(digest.get("type"), digest.get("value"))
            mime = data.get("originalFileFormat") if original else data.get("contentType")
            results.append({"id": _identity(source_id, external, version, file_id, name, size, checksum),
                "source_id": source_id, "external_id": external, "version": version,
                "filename": _filename(f"{source_id}_{file_id}_{name}"), "source_filename": name,
                "size": size, "content_type": mime or mimetypes.guess_type(name)[0] or "application/octet-stream",
                "url": base + "/api/access/datafile/" + file_id + "?format=original",
                "checksum": checksum, "available": True, "reason": "", "kind": "源站文件",
                "record_url": "https://doi.org/" + external[4:], "license": license_text or None})
        return results

    async def _search_pangaea(self, query, page):
        from app.infrastructure.external.data_sources.open_data_sources import _candidate
        # PANGAEA explicitly recommends DataCite for stable public discovery.
        # Its internal Elasticsearch API is not a published API contract.
        payload = await self._json("https://api.datacite.org/dois", {
            "query": "prefix:10.1594 AND (" + _dataverse_query(query) + ")",
            "page[number]": page, "page[size]": PAGE_SIZE,
        })
        rows, total = payload["data"], payload["meta"]["total"]
        if not isinstance(rows, list) or len(rows) > PAGE_SIZE or type(total) is not int or total < 0:
            raise ValueError()
        results = []
        for row in rows:
            item = row["attributes"]
            try:
                external = _pangaea_doi(item["doi"])
            except SourceConnectorError:
                continue
            types = item.get("types") or {}
            if types.get("resourceTypeGeneral") not in (None, "Dataset"):
                continue
            results.append(_candidate("pangaea", external, item["titles"][0]["title"],
                "\n".join(str(d.get("description", "")) for d in item.get("descriptions", [])),
                "https://doi.pangaea.de/" + external, formats=["tab"],
                metadata={"doi": external.lower(), "published": item.get("publicationYear")}))
        return {"results": results, "total_count": total}

    async def _resolve_pangaea(self, candidate):
        from app.infrastructure.external.data_sources.open_data_sources import _identity, _text
        external = _pangaea_doi(candidate["external_id"])
        base = "https://doi.pangaea.de/" + external
        item = await self._json(base, {"format": "metadata_jsonld"})
        if item.get("identifier") != "https://doi.org/" + external or item.get("@type") != "Dataset":
            raise SourceConnectorError("invalid_candidate")
        if item.get("isAccessibleForFree") is not True:
            return []
        distributions = item.get("distribution", [])
        if not isinstance(distributions, list):
            raise ValueError()
        # Collection pages and binary external attachments are not silently
        # represented by an empty table. Accept only the declared table file.
        url = base + "?format=textfile"
        if not any(d.get("contentUrl") == url and d.get("encodingFormat") == "text/tab-separated-values" for d in distributions):
            return []
        filename = "pangaea_" + external.rsplit(".", 1)[1] + ".tab"
        return [{"id": _identity("pangaea", external, "textfile"), "source_id": "pangaea", "external_id": external,
            "version": _text(item.get("version"), 100) or external,
            "filename": filename, "source_filename": "data_matrix.tab", "size": None,
            "content_type": "text/tab-separated-values", "url": url, "checksum": None,
            "available": True, "reason": "", "kind": "源站文件", "record_url": base,
            "license": _text(item.get("license"), 500) or None}]
