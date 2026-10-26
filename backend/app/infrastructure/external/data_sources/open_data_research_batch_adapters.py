"""Public original-file adapters for independently verified research repositories.

Dataverse contracts: https://guides.dataverse.org/en/latest/api/search.html
https://guides.dataverse.org/en/latest/api/dataaccess.html
Source identities and exact storage paths are declared independently of user
input in open_data_research_batch_definitions.py.
"""
from __future__ import annotations

import mimetypes
import re
from urllib.parse import parse_qsl, unquote, urlsplit

from app.domain.external.data_source import SourceConnectorError
from app.domain.models.open_data_research_batch_definitions import REPOSITORY_CONFIG
from app.infrastructure.external.data_sources.open_data_repository_adapters import _dataverse_query, _numeric, _version
from app.infrastructure.external.data_sources.open_data_catalog_adapters import _checksum

PAGE_SIZE = 10
SOURCES = frozenset(REPOSITORY_CONFIG)
INSTALLATIONS = {key: (value["base_url"], value["pid_pattern"], value["prefix"])
                 for key, value in REPOSITORY_CONFIG.items()}


def _pid(source_id, value):
    if (not isinstance(value, str) or not value.lower().startswith("doi:")
            or not re.fullmatch(INSTALLATIONS[source_id][1], value[4:], re.I)):
        raise SourceConnectorError("invalid_candidate")
    normalized = value[4:].upper()
    # HKUST's public API treats its registered "dataset/" namespace as
    # case-sensitive even though DOI matching itself is case-insensitive.
    canonical_prefix = REPOSITORY_CONFIG[source_id].get("canonical_prefix")
    if canonical_prefix:
        normalized = canonical_prefix + normalized[len(canonical_prefix):]
    return "doi:" + normalized


def safe_file_url(source_id, url):
    if source_id not in SOURCES or not isinstance(url, str) or len(url) > 16000 or re.search(r"[\x00-\x20\\]", url):
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
        if parts.hostname == urlsplit(INSTALLATIONS[source_id][0]).hostname:
            return (bool(re.fullmatch(r"/api/access/datafile/[1-9][0-9]{0,19}", path))
                    and parse_qsl(parts.query, keep_blank_values=True) == [("format", "original")])
        return any(parts.hostname == host and re.fullmatch(pattern, path, re.I)
                   for host, pattern in REPOSITORY_CONFIG[source_id]["storage"])
    except (ValueError, TypeError):
        return False


class ResearchBatchAdapters:
    def __init__(self, fetch_json):
        self._json = fetch_json

    async def search(self, source_id, query, page):
        if source_id not in SOURCES:
            raise SourceConnectorError("unknown_source")
        if not isinstance(query, str) or not query.strip() or len(query) > 500 or type(page) is not int or not 1 <= page <= 100:
            raise SourceConnectorError("invalid_query")
        try:
            return await self._search_dataverse(source_id, query, page)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise SourceConnectorError("invalid_response") from None

    async def resolve(self, candidate):
        source_id = candidate.get("source_id")
        if source_id not in SOURCES:
            raise SourceConnectorError("unknown_source")
        try:
            return await self._resolve_dataverse(source_id, candidate)
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
            if re.search(r"(?:^|[._ -])(?:readme|metadata|codebook|dictionary|headers?)(?:[._ -]|$)|_vi_", name, re.I):
                continue
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
            # CUHK returns an incorrect total in Content-Range for partial
            # requests. The facade uses a bounded ordinary GET for this
            # explicitly identified source; full-transfer integrity is unchanged.
            if source_id == "cuhk-data":
                results[-1]["probe_without_range"] = True
        return results

