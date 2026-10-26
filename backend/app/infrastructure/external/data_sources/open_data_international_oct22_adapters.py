"""Discover official full-data archives without confusing catalogs with observations.

FAOSTAT's English bulk catalog identifies original normalised CSV archives. BIS's
public bulk page is the authoritative list of available flat CSV archives; no
download locations are guessed from dataflow names or accepted from callers.
"""
from __future__ import annotations

import asyncio
from html.parser import HTMLParser
import re
import time
from urllib.parse import unquote, urljoin, urlsplit

from app.domain.external.data_source import SourceConnectorError
from app.domain.models.open_data_international_oct22_definitions import DEFINITIONS

SOURCES = tuple(row["id"] for row in DEFINITIONS)
FAO_CATALOG = "https://bulks-faostat.fao.org/production/datasets_E.json"
BIS_CATALOG = "https://data.bis.org/bulkdownload"
_PATTERNS = {"faostat": r"[A-Z][A-Z0-9]{0,15}", "bis": r"WS_[A-Z0-9_]{1,80}"}
_LICENSES = {"faostat": "https://www.fao.org/contact-us/terms/en/",
             "bis": "https://data.bis.org/help/legal"}
_FAO_ARCHIVE = r"/production/[A-Za-z0-9_()\-]+_E_All_Data_\(Normalized\)\.zip"
_BIS_ARCHIVE = r"/static/bulk/(WS_[A-Z0-9_]{1,80})_csv_flat\.zip"


def safe_file_url(source, url):
    try:
        if not isinstance(url, str) or len(url) > 4096 or re.search(r"[\x00-\x20\\]", url):
            return False
        p = urlsplit(url)
        path = unquote(p.path)
        if (p.scheme != "https" or p.port not in (None, 443) or p.username is not None or p.password is not None
                or p.query or p.fragment or "%" in path or re.search(r"%2f|%5c|%2e", p.path, re.I)
                or any(piece in {".", ".."} for piece in path.split("/"))):
            return False
        if source == "faostat":
            return p.hostname == "bulks-faostat.fao.org" and bool(re.fullmatch(_FAO_ARCHIVE, path))
        if source == "bis":
            return p.hostname == "data.bis.org" and bool(re.fullmatch(_BIS_ARCHIVE, path))
    except (ValueError, TypeError):
        pass
    return False


def _identifier(source, external):
    if not isinstance(external, str) or not re.fullmatch(_PATTERNS[source], external):
        raise SourceConnectorError("invalid_candidate")
    return external


def _query(query):
    if not isinstance(query, str) or not query.strip() or len(query) > 1000:
        raise SourceConnectorError("invalid_query")
    query = query.strip()
    for left, right in [('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’")]:
        if query.startswith(left) and query.endswith(right):
            query = query[1:-1].strip()
            break
    if not query:
        raise SourceConnectorError("invalid_query")
    return query


def _record_url(source, external):
    if source == "faostat":
        return "https://www.fao.org/faostat/en/#data/" + external
    return BIS_CATALOG


class _BulkLinks(HTMLParser):
    """Read only actual HTML anchors, ignoring Next.js scripts and other links."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.link = [], None
        self.title, self.in_title = [], False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            url = urljoin(BIS_CATALOG, attrs.get("href", ""))
            self.link = {"url": url, "updated": None} if safe_file_url("bis", url) else None
            self.title, self.in_title = [], False
        elif self.link is not None:
            if tag == "h4":
                self.in_title = True
            elif tag == "time":
                value = attrs.get("datetime")
                if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9 :+.,T\-]{4,80}", value):
                    self.link["updated"] = value

    def handle_data(self, data):
        if self.link is not None and self.in_title:
            self.title.append(data)

    def handle_endtag(self, tag):
        if tag == "h4":
            self.in_title = False
        elif tag == "a":
            if self.link is not None:
                title = " ".join("".join(self.title).split())
                if title:
                    self.rows.append({**self.link, "name": re.sub(r"\s*\(CSV, flat\)\s*$", "", title)})
            self.link, self.title, self.in_title = None, [], False


class InternationalOct22Adapters:
    def __init__(self, fetch_json, fetch_text):
        self._json, self._text = fetch_json, fetch_text
        self._cache, self._locks = {}, {}

    async def _catalog(self, source):
        async with self._locks.setdefault(source, asyncio.Lock()):
            saved = self._cache.get(source)
            if saved and time.monotonic() - saved[0] < 60:
                return saved[1]
            rows = await self._load_fao() if source == "faostat" else await self._load_bis()
            self._cache[source] = (time.monotonic(), rows)
            return rows

    async def _load_fao(self):
        payload = await self._json(FAO_CATALOG)
        if not isinstance(payload, dict) or not isinstance(payload.get("Datasets"), dict):
            raise SourceConnectorError("invalid_response")
        rows = payload["Datasets"].get("Dataset")
        if not isinstance(rows, list):
            raise SourceConnectorError("invalid_response")
        unique = {}
        for row in rows:
            if not isinstance(row, dict):
                raise SourceConnectorError("invalid_response")
            code, url, count = row.get("DatasetCode"), row.get("FileLocation"), row.get("FileRows")
            if (not isinstance(code, str) or not re.fullmatch(_PATTERNS["faostat"], code)
                    or not isinstance(row.get("FileType"), str) or row["FileType"].lower() != "csv"
                    or not isinstance(row.get("CompressionFormat"), str) or row["CompressionFormat"].lower() != "zip"
                    or not safe_file_url("faostat", url) or type(count) is not int or count <= 0
                    or not isinstance(row.get("DatasetName"), str) or not row["DatasetName"].strip()):
                continue
            record = {"id": code, "name": row["DatasetName"], "description": str(row.get("DatasetDescription") or ""),
                      "topic": str(row.get("Topic") or ""), "url": url, "rows": count,
                      "updated": row.get("DateUpdate") if isinstance(row.get("DateUpdate"), str) else None}
            if code in unique and unique[code] != record:
                raise SourceConnectorError("inconsistent_source_snapshot")
            unique[code] = record
        return list(unique.values())

    async def _load_bis(self):
        parser = _BulkLinks()
        text = await self._text(BIS_CATALOG, accept="text/html")
        if not isinstance(text, str):
            raise SourceConnectorError("invalid_response")
        parser.feed(text)
        parser.close()
        unique = {}
        for row in parser.rows:
            code = re.fullmatch(_BIS_ARCHIVE, unquote(urlsplit(row["url"]).path)).group(1)
            record = {"id": code, "description": "完整主题的所有公开序列和时期。", **row}
            if code in unique and unique[code] != record:
                raise SourceConnectorError("inconsistent_source_snapshot")
            unique[code] = record
        if not unique:
            raise SourceConnectorError("invalid_response")
        return list(unique.values())

    async def search(self, source, query, page):
        if source not in SOURCES:
            raise SourceConnectorError("unknown_source")
        if type(page) is not int or not 1 <= page <= 100:
            raise SourceConnectorError("invalid_cursor")
        query = _query(query)
        from app.infrastructure.external.data_sources.open_data_sources import _candidate
        rows = await self._catalog(source)
        exact = [r for r in rows if r["id"].casefold() == query.casefold()]
        matched = exact or [r for r in rows if all(word in " ".join((r["id"], r["name"], r["description"], r.get("topic", ""))).casefold()
                                                for word in query.casefold().split())]
        results = [_candidate(source, r["id"], r["name"], r["description"], _record_url(source, r["id"]),
                              license=_LICENSES[source],
                              coverage={"scope": "完整主题数据包；所有源站可用地区和时期"},
                              metadata={"last_update": r["updated"], **({"observations": r["rows"]} if "rows" in r else {})},
                              limitations=["关键词用于发现主题，不过滤文件内的国家、年份或观测值。"])
                   for r in matched[(page - 1) * 10:page * 10]]
        return {"results": results, "total_count": len(matched)}

    async def resolve(self, candidate):
        if not isinstance(candidate, dict):
            raise SourceConnectorError("invalid_candidate")
        source = candidate.get("source_id")
        if source not in SOURCES:
            raise SourceConnectorError("unknown_source")
        external = _identifier(source, candidate.get("external_id"))
        row = next((r for r in await self._catalog(source) if r["id"] == external), None)
        if row is None:
            return []
        from app.infrastructure.external.data_sources.open_data_sources import _filename, _identity
        url = row["url"]
        if not safe_file_url(source, url):
            raise SourceConnectorError("unsafe_download_url")
        filename = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
        return [{"id": _identity(source, external, row), "source_id": source, "external_id": external,
                 "url": url, "filename": _filename(filename), "source_filename": filename,
                 "size": None, "content_type": "application/zip", "record_url": _record_url(source, external),
                 "version": row["updated"], "license": _LICENSES[source], "checksum": None,
                 "available": True, "reason": "", "kind": "源站完整数据包"}]
