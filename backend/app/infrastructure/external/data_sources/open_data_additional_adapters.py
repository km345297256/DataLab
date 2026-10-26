"""Public-file adapters for OpenML and Dryad.

Contracts: https://docs.openml.org/ecosystem/Rest/
https://github.com/openml/openml.org/blob/develop/server/src/client/app/src/pages/search/api.js
https://github.com/datadryad/dryad-app/tree/main/documentation/apis

Dryad is kept out of the enabled registry until its download authentication
has been configured and verified. Its API's public metadata does not imply
anonymous file access.
"""
from __future__ import annotations

import mimetypes
import re
from urllib.parse import parse_qsl, quote, unquote, urlsplit

from app.domain.external.data_source import SourceConnectorError


OPENML_SEARCH = "https://www.openml.org/es/data/data/_search"
OPENML_API = "https://www.openml.org/api/v1/json/data"
DRYAD_API = "https://datadryad.org/api/v2"
PAGE_SIZE = 10
FILE_PAGE_SIZE = 100


def _number(value, *, code="invalid_response"):
    if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]{0,19}", str(value)):
        raise SourceConnectorError(code)
    return str(value)


def _dryad_doi(value, *, code="invalid_response"):
    if not isinstance(value, str):
        raise SourceConnectorError(code)
    value = value.removeprefix("doi:")
    if not re.fullmatch(r'10\.\d{4,9}/[^\s<>"?#]{1,180}', value):
        raise SourceConnectorError(code)
    return value


def safe_file_url(source_id, url) -> bool:
    """Download-only allowlist; no arbitrary repository or object-store hosts."""
    if not isinstance(url, str) or len(url) > 4096 or any(ord(c) < 32 for c in url):
        return False
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.port not in (None, 443) or parsed.fragment or parsed.query):
            return False
        path = unquote(parsed.path)
        if ("\\" in path or any(ord(c) < 32 for c in path)
                or any(part in (".", "..") for part in path.split("/"))):
            return False
        if source_id == "openml":
            return parsed.hostname in {"openml.org", "www.openml.org"} and bool(
                re.fullmatch(r"/data/(?:v1/)?download/[1-9][0-9]{0,19}/[^/]+", path))
        if source_id == "dryad":
            return parsed.hostname == "datadryad.org" and bool(
                re.fullmatch(r"/api/v2/files/[1-9][0-9]{0,19}/download", path))
    except ValueError:
        return False
    return False


class AdditionalAdapters:
    def __init__(self, fetch_json):
        self._json = fetch_json

    async def search(self, source_id, query, page):
        try:
            if source_id == "openml":
                return await self._search_openml(query, page)
            if source_id == "dryad":
                return await self._search_dryad(query, page)
            raise SourceConnectorError("unknown_source")
        except (KeyError, ValueError, TypeError, AttributeError):
            raise SourceConnectorError("invalid_response") from None

    async def resolve(self, candidate):
        try:
            if candidate["source_id"] == "openml":
                return await self._resolve_openml(candidate)
            if candidate["source_id"] == "dryad":
                return await self._resolve_dryad(candidate)
            raise SourceConnectorError("unknown_source")
        except (KeyError, ValueError, TypeError, AttributeError):
            raise SourceConnectorError("invalid_response") from None

    async def _search_openml(self, query, page):
        from app.infrastructure.external.data_sources.open_data_sources import _candidate, _text

        # This is the public website's read-only search endpoint. The classic
        # REST data_name filter only supports exact names, not research topics.
        payload = await self._json(OPENML_SEARCH, {"type": "data"}, method="POST", json={
            "from": (page - 1) * PAGE_SIZE, "size": PAGE_SIZE,
            "query": {"bool": {
                "must": {"multi_match": {"query": query, "fields": ["name^3", "description"], "operator": "and"}},
                "filter": [{"term": {"visibility": "public"}}, {"term": {"status": "active"}}]}},
            "sort": [{"_score": "desc"}, {"data_id": "asc"}],
            "_source": ["data_id", "name", "description", "format", "version", "licence", "status", "visibility", "qualities.NumberOfInstances", "qualities.NumberOfFeatures"],
        })
        if payload.get("timed_out") or payload.get("_shards", {}).get("failed", 0):
            raise SourceConnectorError("upstream_unavailable")
        hits, total = payload["hits"]["hits"], payload["hits"]["total"]
        if isinstance(total, dict):
            total = total["value"]
        if not isinstance(hits, list) or len(hits) > PAGE_SIZE or type(total) is not int or total < 0:
            raise SourceConnectorError("invalid_response")
        results = []
        for hit in hits:
            item = hit["_source"]
            if item.get("visibility") != "public" or item.get("status") != "active":
                continue
            external = _number(item["data_id"])
            quality = item.get("qualities") or {}
            results.append(_candidate("openml", external, item["name"], item.get("description"),
                f"https://www.openml.org/d/{external}", formats=[_text(item.get("format"), 20).lower()],
                license=_text(item.get("licence"), 200) or None,
                coverage={key: _text(quality[key], 100) for key in ("NumberOfInstances", "NumberOfFeatures") if quality.get(key) is not None},
                metadata={"version": item.get("version")}))
        return {"results": results, "total_count": total}

    async def _resolve_openml(self, candidate):
        from app.infrastructure.external.data_sources.open_data_sources import _filename, _identity, _text

        external = _number(candidate["external_id"], code="invalid_candidate")
        item = (await self._json(f"{OPENML_API}/{external}"))["data_set_description"]
        if _number(item["id"]) != external:
            raise SourceConnectorError("invalid_candidate")
        if item.get("visibility") != "public" or item.get("status") != "active":
            return []
        url = item["url"]
        if not safe_file_url("openml", url):
            raise SourceConnectorError("unsafe_download_url")
        file_id = _number(item["file_id"])
        if urlsplit(url).path.split("/")[-2] != file_id:
            raise SourceConnectorError("invalid_response")
        # Keep the originally published ARFF (including sparse ARFF), rather
        # than silently switching to the generated parquet derivative.
        filename = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
        checksum = item.get("md5_checksum")
        if checksum and not re.fullmatch(r"[a-fA-F0-9]{32}", checksum):
            raise SourceConnectorError("invalid_response")
        checksum = "md5:" + checksum.lower() if checksum else None
        return [{"id": _identity("openml", external, file_id, item.get("version"), checksum),
                 "source_id": "openml", "external_id": external,
                 "filename": _filename(f"openml_{external}_{filename}"), "source_filename": filename,
                 "size": None, "content_type": "text/plain" if filename.lower().endswith(".arff") else mimetypes.guess_type(filename)[0] or "application/octet-stream",
                 "url": url, "record_url": f"https://www.openml.org/d/{external}",
                 "checksum": checksum, "available": True, "reason": "", "kind": "源站文件",
                 "license": _text(item.get("licence"), 200) or None}]

    async def _search_dryad(self, query, page):
        from app.infrastructure.external.data_sources.open_data_sources import _candidate, _size, _text

        payload = await self._json(f"{DRYAD_API}/search", {"q": query, "page": page, "per_page": PAGE_SIZE})
        rows, total = payload["_embedded"]["stash:datasets"], payload["total"]
        if not isinstance(rows, list) or len(rows) > PAGE_SIZE or type(total) is not int or total < 0:
            raise SourceConnectorError("invalid_response")
        results = []
        for row in rows:
            if row.get("visibility") != "public" or row.get("curationStatus") != "Published":
                continue
            doi = _dryad_doi(row["identifier"])
            results.append(_candidate("dryad", doi, row["title"], row.get("abstract"),
                f"https://datadryad.org/dataset/doi:{quote(doi, safe='/')}", size=_size(row.get("storageSize")),
                license=_text(row.get("license"), 200) or None,
                metadata={"doi": doi, "version": row.get("versionNumber"), "published": row.get("publicationDate")}))
        return {"results": results, "total_count": total}

    async def _dryad_files(self, version):
        """Read the entire pinned-version manifest without following arbitrary links."""
        path = f"/api/v2/versions/{version}/files"
        page, expected_total = 1, None
        files, seen = [], set()
        while True:
            payload = await self._json("https://datadryad.org" + path,
                                       {"page": page, "per_page": FILE_PAGE_SIZE})
            rows = payload["_embedded"]["stash:files"]
            if not isinstance(rows, list) or len(rows) > FILE_PAGE_SIZE:
                raise SourceConnectorError("invalid_response")
            total = payload.get("total")
            if total is not None:
                if type(total) is not int or total < 0:
                    raise SourceConnectorError("invalid_response")
                if expected_total is not None and total != expected_total:
                    raise SourceConnectorError("inconsistent_source_snapshot")
                expected_total = total
            if "count" in payload and (type(payload["count"]) is not int or payload["count"] != len(rows)):
                raise SourceConnectorError("invalid_response")
            for row in rows:
                href = row["_links"]["stash:download"]["href"]
                if not isinstance(href, str) or not re.fullmatch(r"/api/v2/files/[1-9][0-9]{0,19}/download", href):
                    raise SourceConnectorError("invalid_response")
                if href in seen:
                    raise SourceConnectorError("inconsistent_source_snapshot")
                seen.add(href)
                files.append(row)
            next_link = payload.get("_links", {}).get("next")
            if next_link:
                parsed = urlsplit(next_link["href"])
                params = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
                query = dict(params)
                if (parsed.scheme not in {"", "https"} or parsed.netloc not in {"", "datadryad.org"}
                        or parsed.path != path or parsed.fragment or len(params) != len(query)
                        or set(query) - {"page", "per_page"} or query.get("page") != str(page + 1)
                        or query.get("per_page", str(FILE_PAGE_SIZE)) != str(FILE_PAGE_SIZE)):
                    raise SourceConnectorError("invalid_response")
            if expected_total is not None:
                if len(files) > expected_total or (not rows and len(files) < expected_total):
                    raise SourceConnectorError("inconsistent_source_snapshot")
                if len(files) == expected_total:
                    if next_link:
                        raise SourceConnectorError("inconsistent_source_snapshot")
                    return files
            if not next_link and len(rows) < FILE_PAGE_SIZE:
                if expected_total is not None and len(files) != expected_total:
                    raise SourceConnectorError("inconsistent_source_snapshot")
                return files
            if not rows:
                raise SourceConnectorError("inconsistent_source_snapshot")
            page += 1

    async def _resolve_dryad(self, candidate):
        from app.infrastructure.external.data_sources.open_data_sources import _filename, _identity, _size, _text

        doi = _dryad_doi(candidate["external_id"], code="invalid_candidate")
        record = await self._json(f"{DRYAD_API}/datasets/{quote('doi:' + doi, safe='')}")
        if _dryad_doi(record["identifier"]).lower() != doi.lower():
            raise SourceConnectorError("invalid_candidate")
        if record.get("visibility") != "public" or record.get("curationStatus") != "Published":
            return []
        version = record["_links"]["stash:version"]["href"]
        match = re.fullmatch(r"/api/v2/versions/([1-9][0-9]{0,19})", version)
        if not match:
            raise SourceConnectorError("invalid_response")
        rows = await self._dryad_files(match[1])
        results = []
        for row in rows:
            href = row["_links"]["stash:download"]["href"]
            if not isinstance(href, str) or not re.fullmatch(r"/api/v2/files/[1-9][0-9]{0,19}/download", href):
                raise SourceConnectorError("invalid_response")
            filename = row["path"]
            if not isinstance(filename, str) or not filename:
                raise SourceConnectorError("invalid_response")
            size = _size(row.get("size"))
            digest_type = {"sha-256": "sha256", "sha256": "sha256", "md5": "md5"}.get(row.get("digestType"))
            digest = row.get("digest")
            checksum = None
            if digest and digest_type:
                if not re.fullmatch(r"[a-fA-F0-9]{%d}" % (64 if digest_type == "sha256" else 32), digest):
                    raise SourceConnectorError("invalid_response")
                checksum = digest_type + ":" + digest.lower()
            # The metadata endpoint is anonymous; the file API requires a
            # bearer token. Do not pretend these files are directly available.
            results.append({"id": _identity("dryad", doi, match[1], href, size, checksum),
                "source_id": "dryad", "external_id": doi,
                "filename": _filename(f"dryad_{_identity(doi)[:8]}_{filename}"), "source_filename": filename,
                "size": size, "content_type": row.get("mimeType") or mimetypes.guess_type(filename)[0] or "application/octet-stream",
                "url": "https://datadryad.org" + href,
                "record_url": f"https://datadryad.org/dataset/doi:{quote(doi, safe='/')}",
                "checksum": checksum, "available": False, "reason": "此来源的文件 API 需要授权", "kind": "源站文件",
                "license": _text(record.get("license"), 200) or None})
        return results
