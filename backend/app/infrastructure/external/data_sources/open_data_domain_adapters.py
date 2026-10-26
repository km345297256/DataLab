"""Published original files from ScienceDB and supported GBIF hosting nodes.

ScienceDB's documented OpenAPI exposes metadata but its ``search`` method is a
publication-date listing. Topic discovery therefore uses the DataCite index;
access, version and files are re-read from ScienceDB's public website APIs.
https://www.scidb.cn/api/sdb-openapi-service/sdk/README.md

GBIF discovery uses its Registry API. Only DWC_ARCHIVE endpoints on explicitly
supported official hosting nodes are accepted; occurrence download jobs that
require a GBIF user account are not impersonated or submitted anonymously.
https://techdocs.gbif.org/en/openapi/v1/registry-principal-methods
"""
from __future__ import annotations

from collections import deque
import mimetypes
import re
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

from app.domain.external.data_source import SourceConnectorError


PAGE_SIZE = 10
SOURCES = ("sciencedb", "gbif")
SCIENCE_API = "https://www.scidb.cn/api"
GBIF_API = "https://api.gbif.org/v1"
_SCIENCE_DOI = re.compile(r"10\.(?:57760|11922)/sciencedb\.[a-z0-9._-]{1,120}", re.I)
_UUID = re.compile(r"[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}", re.I)
# These are official hosting nodes, not arbitrary URLs from publisher metadata.
_IPT_PATHS = {
    "ipt.gbif.org": {"/archive.do", "/ipt/archive.do"},
    "ipt.gbif.fr": {"/archive.do"},
    "ipt.gbif.no": {"/archive.do"},
    "ipt.gbif.es": {"/archive.do"},
    "ipt.gbif.pt": {"/ipt/archive.do"},
}


def _valid(value, pattern, code="invalid_response"):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise SourceConnectorError(code)
    return value


def safe_file_url(source_id, url) -> bool:
    """Exact download origins and paths only; query duplicates are rejected."""
    if not isinstance(url, str) or len(url) > 4096 or any(ord(c) < 32 for c in url):
        return False
    try:
        parsed = urlsplit(url)
        path = unquote(parsed.path)
        if (parsed.scheme != "https" or parsed.username or parsed.password
                or parsed.port not in (None, 443) or parsed.fragment
                or "\\" in path or any(ord(c) < 32 for c in path)
                or any(p in {".", ".."} for p in path.split("/"))):
            return False
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        params = dict(pairs)
        if len(params) != len(pairs):
            return False
        if source_id == "sciencedb":
            return (parsed.hostname in {"china.scidb.cn", "download.scidb.cn"}
                    and path == "/download" and set(params) == {"fileId"}
                    and bool(re.fullmatch(r"(?:[a-f0-9]{24}|[a-f0-9]{32})", params["fileId"])))
        if source_id == "gbif":
            if parsed.hostname in _IPT_PATHS and path in _IPT_PATHS[parsed.hostname]:
                return (set(params) <= {"r", "v"} and bool(re.fullmatch(r"[A-Za-z0-9_.-]{1,200}", params.get("r", "")))
                        and ("v" not in params or bool(re.fullmatch(r"[0-9]{1,8}(?:\.[0-9]{1,8}){0,3}", params["v"]))))
            return (parsed.hostname == "hosted-datasets.gbif.org" and not params
                    and bool(re.fullmatch(r"/(?:[A-Za-z0-9_-][A-Za-z0-9_.-]*/)*[A-Za-z0-9_-][A-Za-z0-9_.-]*\.(?:zip|csv|tsv|txt|gz)", path)))
    except ValueError:
        return False
    return False


class DomainAdapters:
    def __init__(self, fetch_json):
        self._json = fetch_json

    async def search(self, source_id, query, page):
        try:
            if source_id == "sciencedb":
                return await self._search_sciencedb(query, page)
            if source_id == "gbif":
                return await self._search_gbif(query, page)
            raise SourceConnectorError("unknown_source")
        except (KeyError, ValueError, TypeError, AttributeError):
            raise SourceConnectorError("invalid_response") from None

    async def resolve(self, candidate):
        try:
            if candidate["source_id"] == "sciencedb":
                return await self._resolve_sciencedb(candidate)
            if candidate["source_id"] == "gbif":
                return await self._resolve_gbif(candidate)
            raise SourceConnectorError("unknown_source")
        except (KeyError, ValueError, TypeError, AttributeError):
            raise SourceConnectorError("invalid_response") from None

    async def _search_sciencedb(self, query, page):
        from app.infrastructure.external.data_sources.open_data_sources import _candidate, _text

        payload = await self._json("https://api.datacite.org/dois", {
            "query": f"prefix:(10.57760 OR 10.11922) AND types.resourceTypeGeneral:Dataset AND ({query})",
            "page[size]": PAGE_SIZE, "page[number]": page})
        rows, total = payload["data"], payload["meta"]["total"]
        if not isinstance(rows, list) or len(rows) > PAGE_SIZE or type(total) is not int or total < 0:
            raise SourceConnectorError("invalid_response")
        results = []
        for row in rows:
            item = row["attributes"]
            doi = item["doi"]
            if not isinstance(doi, str) or not _SCIENCE_DOI.fullmatch(doi):
                continue
            if any(r.get("rights", "").lower() in {"restricted access", "closed access", "embargoed access"}
                   for r in item.get("rightsList", [])):
                continue
            results.append(_candidate("sciencedb", doi.lower(), item["titles"][0]["title"],
                " ".join(d.get("description", "") for d in item.get("descriptions", [])),
                f"https://doi.org/{quote(doi, safe='/')}",
                license=next((_text(r.get("rightsUri") or r.get("rights"), 300) for r in item.get("rightsList", [])
                              if r.get("rightsIdentifier")), None),
                metadata={"doi": doi.lower(), "published": item.get("publicationYear")}))
        return {"results": results, "total_count": total}

    async def _resolve_sciencedb(self, candidate):
        from app.infrastructure.external.data_sources.open_data_sources import _filename, _identity, _size, _text

        doi = _valid(candidate["external_id"], _SCIENCE_DOI, "invalid_candidate").lower()
        index = (await self._json("https://api.datacite.org/dois/" + quote(doi, safe="")))["data"]["attributes"]
        if str(index["doi"]).lower() != doi:
            raise SourceConnectorError("invalid_candidate")
        # DataCite carries the ScienceDB dataset identifier, never a file URL.
        parsed = urlsplit(index["url"])
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        params = dict(pairs)
        if (parsed.scheme != "https" or parsed.hostname not in {"www.scidb.cn", "scidb.cn"}
                or parsed.username or parsed.password or parsed.port not in (None, 443) or parsed.fragment
                or parsed.path not in {"/detail", "/en/detail"} or len(pairs) != len(params)
                or set(params) != {"dataSetId"}):
            raise SourceConnectorError("invalid_response")
        dataset = _valid(params["dataSetId"], r"[a-f0-9]{32}")
        record = await self._json(SCIENCE_API + "/sdb-openapi-service/json", {"doi": doi})
        if record.get("@type") != "Dataset" or str(record.get("identifier", "")).lower() != "https://doi.org/" + doi:
            raise SourceConnectorError("invalid_candidate")
        if record.get("conditionsOfAccess", "").lower() != "unrestricted" or record.get("isAccessibleForFree") is not True:
            return []
        version = _valid(record["version"], r"V[1-9][0-9]{0,7}")
        pending, visited, seen_files = deque([(f"/{version}", 0)]), set(), set()
        directory_counts = {}
        results = []
        while pending:
            path, start = pending.popleft()
            if (path, start) in visited:
                raise SourceConnectorError("invalid_response")
            visited.add((path, start))
            payload = await self._json(SCIENCE_API + "/gin-sdb-filetree/public/file/childrenFileListByPath",
                method="POST", json={"dataSetId": dataset, "version": version, "path": path,
                                    "lastIndex": start, "pageSize": 100})
            if payload.get("code") != 20000:
                raise SourceConnectorError("restricted_access" if payload.get("code") in {401, 403, 10001} else "upstream_unavailable")
            rows = payload["data"]
            if not isinstance(rows, list) or len(rows) > 100:
                raise SourceConnectorError("invalid_response")
            if not rows and start:
                raise SourceConnectorError("inconsistent_source_snapshot")
            for row in rows:
                if row.get("datasetId") != dataset or row.get("version") != version:
                    raise SourceConnectorError("invalid_candidate")
                child_path = row["path"]
                if (not isinstance(child_path, str) or not child_path.startswith(path + "/")
                        or any(p in {".", ".."} for p in child_path.split("/"))
                        or "\\" in child_path or any(ord(c) < 32 for c in child_path)
                        or "/" in child_path[len(path) + 1:]):
                    raise SourceConnectorError("invalid_response")
                if row.get("dir") is True:
                    pending.append((child_path, 0))
                elif row.get("dir") is False and not row.get("link"):
                    file_id = _valid(row["id"], r"(?:[a-f0-9]{24}|[a-f0-9]{32})")
                    if file_id in seen_files:
                        raise SourceConnectorError("invalid_response")
                    seen_files.add(file_id)
                    filename = _valid(row["fileName"], r"[^/\\\x00-\x1f]{1,500}")
                    checksum = row.get("md5") or None
                    if checksum:
                        checksum = "md5:" + _valid(checksum, r"[a-fA-F0-9]{32}").lower()
                    size = _size(row.get("size"))
                    results.append({"id": _identity("sciencedb", doi, dataset, version, file_id, size, checksum),
                        "source_id": "sciencedb", "external_id": doi, "version": version,
                        "filename": _filename(f"sciencedb_{dataset[:8]}_{file_id[:8]}_{filename}"),
                        "source_filename": filename, "size": size,
                        "content_type": mimetypes.guess_type(filename)[0] or "application/octet-stream",
                        "url": f"https://china.scidb.cn/download?fileId={file_id}",
                        "record_url": f"https://doi.org/{quote(doi, safe='/')}", "checksum": checksum,
                        "available": size != 0, "reason": "文件为空" if size == 0 else "", "kind": "源站文件",
                        "license": _text(record.get("license"), 300) or None})
            if rows:
                last = rows[-1]
                index, count = last.get("index"), last.get("parentFileCount")
                if (type(index) is not int or type(count) is not int
                        or index != start + len(rows) - 1 or count <= index):
                    raise SourceConnectorError("invalid_response")
                if path in directory_counts and directory_counts[path] != count:
                    raise SourceConnectorError("inconsistent_source_snapshot")
                directory_counts[path] = count
                if index + 1 < count:
                    pending.appendleft((path, index + 1))
        return results

    async def _search_gbif(self, query, page):
        from app.infrastructure.external.data_sources.open_data_sources import _candidate, _text

        payload = await self._json(GBIF_API + "/dataset/search", {
            "q": query, "limit": PAGE_SIZE, "offset": (page - 1) * PAGE_SIZE})
        rows, total = payload["results"], payload["count"]
        if not isinstance(rows, list) or len(rows) > PAGE_SIZE or type(total) is not int or total < 0:
            raise SourceConnectorError("invalid_response")
        results = []
        for row in rows:
            if row.get("deleted") or row.get("type") not in {"OCCURRENCE", "CHECKLIST", "SAMPLING_EVENT"}:
                continue
            key = _valid(row["key"], _UUID).lower()
            results.append(_candidate("gbif", key, row["title"], row.get("description"),
                f"https://www.gbif.org/dataset/{key}", formats=["zip"], license=_text(row.get("license"), 300) or None,
                coverage={"records": row.get("recordCount") or row.get("nameUsagesCount")},
                metadata={"doi": row.get("doi"), "published": row.get("publicationDate"), "type": row["type"]}))
        return {"results": results, "total_count": total}

    async def _resolve_gbif(self, candidate):
        from app.infrastructure.external.data_sources.open_data_sources import _filename, _identity, _text

        key = _valid(candidate["external_id"], _UUID, "invalid_candidate").lower()
        record = await self._json(GBIF_API + "/dataset/" + key)
        if record.get("key") != key:
            raise SourceConnectorError("invalid_candidate")
        if record.get("deleted") or record.get("type") not in {"OCCURRENCE", "CHECKLIST", "SAMPLING_EVENT"}:
            return []
        version = record.get("version")
        results, seen = [], set()
        for endpoint in record.get("endpoints", []):
            if endpoint.get("type") != "DWC_ARCHIVE":
                continue
            url = endpoint.get("url")
            if not isinstance(url, str):
                continue
            # Legacy registry endpoints often still use HTTP; only upgrade
            # known HTTPS-capable hosts, then apply the same strict validator.
            parsed = urlsplit(url)
            if (parsed.scheme == "http" and parsed.hostname in _IPT_PATHS and parsed.port in (None, 80)
                    and not parsed.username and not parsed.password):
                url = urlunsplit(("https", parsed.hostname, parsed.path, parsed.query, parsed.fragment))
            if not safe_file_url("gbif", url):
                continue
            parsed = urlsplit(url)
            if parsed.hostname in _IPT_PATHS:
                if not isinstance(version, str) or not re.fullmatch(r"[0-9]{1,8}(?:\.[0-9]{1,8}){0,3}", version):
                    continue
                params = dict(parse_qsl(parsed.query))
                if "v" in params and params["v"] != version:
                    continue
                params["v"] = version
                url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(params), ""))
            if url in seen:
                continue
            seen.add(url)
            filename = f"gbif_{key}_{version or 'published'}.zip"
            # Hosted endpoints can preserve a different original extension.
            if parsed.hostname == "hosted-datasets.gbif.org":
                filename = f"gbif_{key}_" + unquote(parsed.path.rsplit("/", 1)[-1])
            results.append({"id": _identity("gbif", key, version, url), "source_id": "gbif", "external_id": key,
                "version": version, "filename": _filename(filename), "source_filename": filename,
                "size": None, "content_type": mimetypes.guess_type(filename)[0] or "application/zip",
                "url": url, "record_url": f"https://www.gbif.org/dataset/{key}", "checksum": None,
                "available": True, "reason": "", "kind": "Darwin Core 源数据归档",
                "license": _text(record.get("license"), 300) or None})
        return results
