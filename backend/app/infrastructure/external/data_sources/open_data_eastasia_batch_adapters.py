"""Read-only portal queries and fresh original-file manifests.

Hong Kong: https://data.gov.hk/en/help/ckan-api-development-guide
Taiwan: https://data.gov.tw/about/doc?chapter=27&doc=8
Taiwan keyword query follows the official public search page's request schema.
"""
from __future__ import annotations

import asyncio
import mimetypes
import re
import time
from urllib.parse import parse_qsl, unquote, urlsplit

from app.domain.external.data_source import SourceConnectorError

SOURCES = frozenset(("hongkong-data", "taiwan-data"))
PAGE_SIZE = 10
FORMATS = frozenset(("csv", "tsv", "txt", "json", "xml", "xlsx", "xls", "zip", "geojson", "kml", "kmz", "shp", "parquet", "nc"))
UUID = r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}"
FILE = r"[^/]+\.(?:csv|tsv|txt|json|xml|xlsx|xls|zip|geojson|kml|kmz|shp|parquet|nc)"
ROUTES = {
    "hongkong-data": {
        "data.weather.gov.hk": rf"/weatherAPI/hko_data/(?:[^/]+/)*{FILE}",
        "static.data.gov.hk": rf"/(?:[^/]+/)*{FILE}",
        "data.gov.hk": rf"/filestore/(?:[^/]+/)*{FILE}",
        "www.statistics.gov.hk": rf"/pub/(?:[^/]+/)*{FILE}",
    },
    "taiwan-data": {
        "opendata.penghu.gov.tw": rf"/dataset/{UUID}/resource/{UUID}/download/{FILE}",
        "ws.kinmen.gov.tw": rf"/[0-9]+/Upload/(?:[0-9A-Za-z_-]+/)+{FILE}",
        "www.ris.gov.tw": rf"/info-popudata/opendata/(?:[^/]+/)*{FILE}",
        "data.ntpc.gov.tw": rf"/api/datasets/{UUID}/(?:csv|json|xml)/file",
        "datacenter.taichung.gov.tw": rf"/swagger/OpenData/{UUID}",
    },
}


def safe_file_url(source, url):
    try:
        if source not in SOURCES or not isinstance(url, str) or len(url) > 4096:
            return False
        p = urlsplit(url)
        path = unquote(p.path)
        if (p.scheme != "https" or p.username or p.password or p.fragment or p.port not in (None, 443)
                or re.search(r"[\x00-\x20\\]", url) or re.search(r"%2e|%2f|%5c", p.path, re.I)
                or re.search(r"[\x00-\x1f\\]", path) or any(x in {".", ".."} for x in path.split("/"))):
            return False
        if source == "taiwan-data" and p.hostname == "data.moi.gov.tw":
            pairs = parse_qsl(p.query, keep_blank_values=True)
            return (path == "/MoiOD/System/DownloadFile.aspx" and len(pairs) == 1 and pairs[0][0] == "DATA"
                    and bool(re.fullmatch(UUID, pairs[0][1])))
        pattern = ROUTES[source].get(p.hostname)
        return bool(pattern and not p.query and re.fullmatch(pattern, path, re.I))
    except (ValueError, TypeError):
        return False


def _id(source, value):
    pattern = r"[a-z0-9][a-z0-9_-]{1,199}" if source == "hongkong-data" else r"[1-9][0-9]{0,9}"
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise SourceConnectorError("invalid_candidate")
    return value


def _public(payload):
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise SourceConnectorError("invalid_response")
    return payload["result"]


class EastAsiaBatchAdapters:
    def __init__(self, fetch_json):
        self._json = fetch_json
        self._hk_catalog = None
        self._hk_at = 0.0
        self._hk_lock = asyncio.Lock()

    async def search(self, source, query, page):
        from app.infrastructure.external.data_sources.open_data_sources import _candidate
        if source not in SOURCES:
            raise SourceConnectorError("unknown_source")
        if not isinstance(query, str) or not query.strip() or len(query) > 500 or type(page) is not int or not 1 <= page <= 100:
            raise SourceConnectorError("invalid_query")
        query = query.strip().strip('"')
        if source == "hongkong-data":
            words = re.findall(r"[a-z0-9]+", query.lower())
            if not words or re.search(r"[^\x00-\x7f]", query):
                raise SourceConnectorError("english_query_required")
            async with self._hk_lock:
                if self._hk_catalog is None or time.monotonic() - self._hk_at > 3600:
                    ids = _public(await self._json("https://data.gov.hk/en-data/api/3/action/package_list"))
                    if not isinstance(ids, list) or len(ids) > 50000 or len(set(ids)) != len(ids):
                        raise SourceConnectorError("invalid_response")
                    self._hk_catalog = [_id(source, item) for item in ids]
                    self._hk_at = time.monotonic()
            matches = [value for value in self._hk_catalog if all(word in value for word in words)]
            slots = asyncio.Semaphore(4)
            async def get(value):
                async with slots:
                    row = await self._hk_record(value)
                    if row.get("private") is not False or row.get("state") != "active":
                        return None
                    return _candidate(source, value, row["title"], row.get("notes"), "https://data.gov.hk/en-data/dataset/" + value,
                                      metadata={"version": row.get("metadata_modified")})
            rows = await asyncio.gather(*(get(value) for value in matches[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]))
            return {"results": [row for row in rows if row], "total_count": len(matches)}
        payload = await self._json("https://data.gov.tw/api/front/dataset/list", method="POST", json={
            "page_num": page, "page_limit": PAGE_SIZE, "bool": [{"fulltext": {"value": query, "operator": "AND"}}]})
        if not isinstance(payload, dict) or payload.get("success") is not True or payload.get("code") != 200:
            raise SourceConnectorError("invalid_response")
        data = payload["payload"]
        rows, total = data["search_result"], data["search_count"]
        if not isinstance(rows, list) or len(rows) > PAGE_SIZE or type(total) is not int or total < 0:
            raise SourceConnectorError("invalid_response")
        result = []
        for row in rows:
            external = _id(source, str(row["nid"]))
            if row.get("status") != 1 or row.get("dataset_status") != "c01" or row.get("charge") not in {"免費", "免费", "free"}:
                continue
            result.append(_candidate(source, external, row["title"], row.get("content"), "https://data.gov.tw/dataset/" + external,
                                     license=row.get("license_name")))
        return {"results": result, "total_count": total}

    async def _hk_record(self, external):
        row = _public(await self._json("https://data.gov.hk/en-data/api/3/action/package_show", {"id": external}))
        if not isinstance(row, dict) or row.get("name") != external:
            raise SourceConnectorError("invalid_candidate")
        return row

    async def resolve(self, candidate):
        from app.infrastructure.external.data_sources.open_data_sources import _filename, _identity, _text
        source = candidate.get("source_id")
        if source not in SOURCES:
            raise SourceConnectorError("unknown_source")
        external = _id(source, candidate["external_id"])
        if source == "hongkong-data":
            row = await self._hk_record(external)
            if row.get("private") is not False or row.get("state") != "active":
                return []
            version = row.get("metadata_modified")
            expected = (candidate.get("metadata") or {}).get("version")
            if expected and version != expected:
                raise SourceConnectorError("inconsistent_source_snapshot")
            resources = row.get("resources", [])
            license_name = row.get("license_title") or "https://data.gov.hk/en/terms-and-conditions"
        else:
            row = _public(await self._json("https://data.gov.tw/api/v2/rest/dataset/" + external))
            if not isinstance(row, dict) or str(row.get("datasetId")) != external:
                raise SourceConnectorError("invalid_candidate")
            if row.get("cost") not in {"free", "免費", "免费"} or not row.get("license"):
                return []
            version, resources = row.get("modifiedDate"), row.get("distribution", [])
            license_name = "https://data.gov.tw/license" if str(row["license"]) == "1" else _text(row["license"], 300)
        if not isinstance(resources, list):
            raise SourceConnectorError("invalid_response")
        result = []
        for index, item in enumerate(resources):
            if not isinstance(item, dict):
                raise SourceConnectorError("invalid_response")
            if source == "hongkong-data" and (item.get("state") != "active" or item.get("is_api") == "Y"):
                continue
            url, fmt = (item.get("url"), item.get("format")) if source == "hongkong-data" else (item.get("resourceDownloadUrl"), item.get("resourceFormat"))
            fmt = str(fmt or "").lower()
            if fmt not in FORMATS or not safe_file_url(source, url):
                continue
            original = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
            if original.rsplit(".", 1)[-1].lower() not in FORMATS:
                original = f"{external}_{index + 1}.{fmt}"
            if re.search(r"(?:^|[._ -])(?:readme|metadata|dictionary|schema|codebook)(?:[._ -]|$)", original, re.I):
                continue
            result.append({"id": _identity(source, external, version, url, fmt), "source_id": source,
                           "external_id": external, "version": version, "url": url,
                           "filename": _filename(source + "_" + external + "_" + original), "source_filename": original,
                           "size": None, "content_type": mimetypes.guess_type(original)[0] or "application/octet-stream",
                           "checksum": None, "available": True, "reason": "", "kind": "源站文件",
                           "record_url": ("https://data.gov.hk/en-data/dataset/" if source == "hongkong-data" else "https://data.gov.tw/dataset/") + external,
                           "license": license_name})
        return result
