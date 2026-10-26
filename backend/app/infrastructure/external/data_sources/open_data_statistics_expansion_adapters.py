"""Discover complete official catalogs and resolve actual statistical observations.

Catalogs are metadata only. File options point exclusively to original observation
exports, and every resolution reloads authoritative metadata rather than caller URLs.
"""
from __future__ import annotations

import asyncio
import csv
import io
import re
import time
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit
from xml.etree import ElementTree

from app.domain.external.data_source import SourceConnectorError
from app.domain.models.open_data_statistics_expansion_definitions import DEFINITIONS

SOURCES = tuple(row["id"] for row in DEFINITIONS)
PAGE_SIZE = 10
CATALOG_TTL_SECONDS = 60
EUROSTAT = "https://ec.europa.eu/eurostat/api/dissemination"
ECB = "https://data-api.ecb.europa.eu/service"
ILO = "https://rplumber.ilo.org"
UIS = "https://api.uis.unesco.org/api/public"
_CODE = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}"
_FLOW = r"[A-Z][A-Z0-9_]{1,30}"
_SERIES = r"[A-Z0-9_]+(?:\.[A-Z0-9_]+){1,29}"
_VERSION = r"[0-9]{8}-[a-f0-9]{8}"
_PATTERNS = {"eurostat": r"[a-z][a-z0-9_]{0,79}", "ecb": _FLOW + "(?:/" + _SERIES + ")?",
             "ilostat": r"[A-Z][A-Z0-9_]{2,119}_[AQM]", "unesco-uis": _CODE}
_LICENSES = {"eurostat": "https://ec.europa.eu/eurostat/about-us/policies/copyright",
             "ecb": "https://www.ecb.europa.eu/services/disclaimer/html/index.en.html",
             "ilostat": "https://www.ilo.org/global/copyright/lang--en/index.htm",
             "unesco-uis": "https://databrowser.uis.unesco.org/terms-and-conditions"}


def _identifier(source, value):
    if not isinstance(value, str) or not re.fullmatch(_PATTERNS[source], value):
        raise SourceConnectorError("invalid_candidate")
    return value


def safe_file_url(source, url):
    try:
        if not isinstance(url, str) or len(url) > 4096 or any(ord(c) < 32 for c in url):
            return False
        p = urlsplit(url)
        path = unquote(p.path)
        pairs = parse_qsl(p.query, keep_blank_values=True, strict_parsing=True)
        q = dict(pairs)
        if (p.scheme != "https" or p.username is not None or p.password is not None or p.port not in (None, 443)
                or p.fragment or len(pairs) != len(q) or "\\" in path or "%" in path
                or re.search(r"%2f|%5c|%2e", p.path, re.I) or any(c in {".", ".."} for c in path.split("/"))):
            return False
        if source == "eurostat":
            return p.hostname == "ec.europa.eu" and bool(re.fullmatch(r"/eurostat/api/dissemination/sdmx/2\.1/data/[A-Z][A-Z0-9_]{0,79}", path)) and q == {"format": "tsv", "compressed": "true"}
        if source == "ecb":
            return p.hostname == "data-api.ecb.europa.eu" and bool(re.fullmatch(r"/service/data/" + _FLOW + "(?:/" + _SERIES + ")?", path)) and q == {"format": "csvdata"}
        if source == "ilostat":
            return p.hostname == "rplumber.ilo.org" and path == "/data/indicator" and set(q) == {"format", "id"} and q["format"] == ".csv.gz" and bool(re.fullmatch(_PATTERNS[source], q["id"]))
        if source == "unesco-uis":
            return p.hostname == "api.uis.unesco.org" and path == "/api/public/data/indicators/export" and set(q) == {"format", "indicator", "version"} and q["format"] == "csv" and bool(re.fullmatch(_CODE, q["indicator"])) and bool(re.fullmatch(_VERSION, q["version"]))
    except (ValueError, TypeError, KeyError):
        pass
    return False


def _xml(text):
    # ElementTree does not retrieve external entities; reject all declarations
    # as well, including internal entity expansion before parsing.
    if not isinstance(text, str) or re.search(r"<!\s*(?:DOCTYPE|ENTITY)", text, re.I):
        raise SourceConnectorError("invalid_response")
    try:
        return ElementTree.fromstring(text)
    except ElementTree.ParseError:
        raise SourceConnectorError("invalid_response") from None


def _table(text, required, delimiter=","):
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")), delimiter=delimiter)
    if not required.issubset(reader.fieldnames or []):
        raise SourceConnectorError("invalid_response")
    return list(reader)


def _record_url(source, external):
    if source == "eurostat":
        return "https://ec.europa.eu/eurostat/databrowser/view/" + external + "/default/table?lang=en"
    if source == "ecb":
        flow, _, key = external.partition("/")
        return "https://data.ecb.europa.eu/data/datasets/" + flow + ("/" + flow + "." + key if key else "")
    if source == "ilostat":
        return "https://rplumber.ilo.org/files/website/bulk/indicator.html"
    # The documented API provides a stable per-indicator record URL; the web
    # browser's share-link fragment syntax is not part of the API contract.
    return UIS + "/data/indicators?" + urlencode({"indicator": external})


def _candidate(source, external, title, description, *, coverage=None, metadata=None):
    from app.infrastructure.external.data_sources.open_data_sources import _candidate as candidate
    return candidate(source, external, title, description, _record_url(source, external),
                     license=_LICENSES[source], coverage=coverage or {}, metadata=metadata or {},
                     limitations=["检索名称和主题用于发现；观测范围以结果说明为准。"])


def _option(source, external, url, filename, version, identity, content_type):
    from app.infrastructure.external.data_sources.open_data_sources import _filename, _identity
    if not safe_file_url(source, url):
        raise SourceConnectorError("unsafe_download_url")
    return {"id": _identity(source, external, url, version, identity), "source_id": source, "external_id": external,
            "filename": _filename(filename), "source_filename": filename, "size": None, "url": url,
            "content_type": content_type, "record_url": _record_url(source, external), "version": version,
            "license": _LICENSES[source], "checksum": None, "available": True, "reason": "", "kind": "源站观测数据"}


def _matches(query, *values):
    words = query.strip('"').casefold().split()
    text = " ".join(str(v) for v in values).casefold()
    return all(word in text for word in words)


def _page(rows, page):
    return {"results": rows[(page - 1) * PAGE_SIZE:page * PAGE_SIZE], "total_count": len(rows)}


class StatisticsExpansionAdapters:
    def __init__(self, fetch_json, fetch_text):
        self._json, self._text = fetch_json, fetch_text
        self._cache, self._locks = {}, {}

    async def _catalog(self, key, loader):
        # One shared, short-lived official catalog per adapter prevents each
        # concurrently verified result from fetching the same multi-MB index.
        async with self._locks.setdefault(key, asyncio.Lock()):
            saved = self._cache.get(key)
            if saved and time.monotonic() - saved[0] < CATALOG_TTL_SECONDS:
                return saved[1]
            value = await loader()
            self._cache[key] = (time.monotonic(), value)
            return value

    async def search(self, source, query, page):
        if source not in SOURCES:
            raise SourceConnectorError("unknown_source")
        if type(page) is not int or not 1 <= page <= 100:
            raise SourceConnectorError("invalid_cursor")
        if not isinstance(query, str) or not query.strip() or len(query) > 1000:
            raise SourceConnectorError("invalid_query")
        try:
            return await getattr(self, "_search_" + source.replace("-", "_"))(query.strip().strip('"'), page)
        except (KeyError, ValueError, TypeError, AttributeError, IndexError, csv.Error):
            raise SourceConnectorError("invalid_response") from None

    async def resolve(self, candidate):
        source = candidate.get("source_id")
        if source not in SOURCES:
            raise SourceConnectorError("unknown_source")
        external = _identifier(source, candidate.get("external_id"))
        try:
            return await getattr(self, "_resolve_" + source.replace("-", "_"))(external)
        except (KeyError, ValueError, TypeError, AttributeError, IndexError, csv.Error):
            raise SourceConnectorError("invalid_response") from None

    async def _eurostat_catalog(self):
        return await self._catalog("eurostat", self._load_eurostat_catalog)

    async def _load_eurostat_catalog(self):
        text = await self._text(EUROSTAT + "/catalogue/toc/txt", {"lang": "en"})
        rows = _table(text, {"title", "code", "type", "last update of data", "last table structure change", "values"}, "\t")
        unique = {}
        for row in rows:
            code = row["code"]
            if row["type"] in {"dataset", "table"} and re.fullmatch(_PATTERNS["eurostat"], code) and int(row["values"] or 0) > 0:
                unique.setdefault(code, row)
        return list(unique.values())

    async def _search_eurostat(self, query, page):
        rows = [_candidate("eurostat", r["code"], r["title"].strip(), "完整统计表；全部地区与时期。",
                           coverage={"scope": "完整统计表", "start": r.get("data start"), "end": r.get("data end")},
                           metadata={"last_update": r["last update of data"], "observations": int(r["values"])})
                for r in await self._eurostat_catalog() if _matches(query, r["code"], r["title"])]
        return _page(rows, page)

    async def _resolve_eurostat(self, external):
        row = next((r for r in await self._eurostat_catalog() if r["code"] == external), None)
        if not row:
            return []
        version = ":".join((row["last update of data"], row["last table structure change"]))
        return [_option("eurostat", external, EUROSTAT + "/sdmx/2.1/data/" + external.upper() + "?format=tsv&compressed=true",
                        external + ".tsv.gz", version, row, "application/gzip")]

    async def _ecb_catalog(self, flow="all"):
        return await self._catalog("ecb:" + flow, lambda: self._load_ecb_catalog(flow))

    async def _load_ecb_catalog(self, flow):
        tree = _xml(await self._text(ECB + "/dataflow/ECB/" + flow + "/latest", {"references": "none"}, accept="application/vnd.sdmx.structure+xml;version=2.1"))
        ns = {"s": "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/structure", "c": "http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common"}
        rows = []
        for item in tree.findall(".//s:Dataflow", ns):
            code = item.get("id", "")
            if item.get("agencyID") != "ECB" or not re.fullmatch(_FLOW, code):
                continue
            names = item.findall("c:Name", ns)
            name = next((n.text for n in names if n.get("{http://www.w3.org/XML/1998/namespace}lang") == "en"), code)
            rows.append({"id": code, "name": name, "structure": ElementTree.tostring(item, encoding="unicode")})
        return rows

    async def _ecb_series(self, external):
        text = await self._text(ECB + "/data/" + external, {"format": "csvdata", "detail": "serieskeysonly"}, accept="text/csv")
        rows = _table(text, {"KEY"})
        return any(row["KEY"] == external.replace("/", ".", 1) for row in rows)

    async def _search_ecb(self, query, page):
        if re.fullmatch(_FLOW + "/" + _SERIES, query):
            flow, key = query.split("/")
            rows = await self._ecb_catalog(flow)
            row = next((r for r in rows if r["id"] == flow), None)
            if not row or not await self._ecb_series(query):
                return _page([], page)
            return _page([_candidate("ecb", query, row["name"] + " — " + key, "明确的单一序列；所有可用时期。",
                                    coverage={"scope": "单一序列的全部时期", "series_key": key})], page)
        return _page([_candidate("ecb", r["id"], r["name"], "完整数据流的全部序列与时期；动态快照。",
                                 coverage={"scope": "完整数据流"}) for r in await self._ecb_catalog() if _matches(query, r["id"], r["name"])], page)

    async def _resolve_ecb(self, external):
        flow = external.split("/")[0]
        row = next((r for r in await self._ecb_catalog(flow) if r["id"] == flow), None)
        if not row or "/" in external and not await self._ecb_series(external):
            return []
        # The SDMX dataflow version describes structure, not observation revisions.
        return [_option("ecb", external, ECB + "/data/" + external + "?format=csvdata", external.replace("/", "_") + ".csv",
                        None, row, "text/csv")]

    async def _ilo_catalog(self):
        return await self._catalog("ilostat", self._load_ilo_catalog)

    async def _load_ilo_catalog(self):
        text = await self._text(ILO + "/metadata/toc/indicator", {"format": ".csv", "lang": "en"}, accept="text/csv")
        rows = _table(text, {"id", "indicator.label", "freq.label", "last.update", "n.records"})
        return [r for r in rows if re.fullmatch(_PATTERNS["ilostat"], r["id"]) and int(r["n.records"] or 0) > 0]

    async def _search_ilostat(self, query, page):
        return _page([_candidate("ilostat", r["id"], r["indicator.label"] + " — " + r["freq.label"], "指标与频率对应的完整观测文件；全部地区与时期。",
                                 coverage={"scope": "完整指标文件", "start": r.get("data.start"), "end": r.get("data.end")},
                                 metadata={"last_update": r["last.update"], "observations": int(r["n.records"])})
                      for r in await self._ilo_catalog() if _matches(query, r["id"], r["indicator.label"], r.get("subject.label", ""))], page)

    async def _resolve_ilostat(self, external):
        row = next((r for r in await self._ilo_catalog() if r["id"] == external), None)
        if not row:
            return []
        return [_option("ilostat", external, ILO + "/data/indicator?" + urlencode({"format": ".csv.gz", "id": external}),
                        external + ".csv.gz", row["last.update"], row, "application/gzip")]

    async def _uis_catalog(self):
        return await self._catalog("unesco-uis", self._load_uis_catalog)

    async def _load_uis_catalog(self):
        version = (await self._json(UIS + "/versions/default"))["version"]
        if not re.fullmatch(_VERSION, version):
            raise SourceConnectorError("invalid_response")
        rows = await self._json(UIS + "/definitions/indicators", {"version": version})
        if not isinstance(rows, list):
            raise SourceConnectorError("invalid_response")
        return version, [r for r in rows if re.fullmatch(_CODE, r["indicatorCode"]) and r.get("dataAvailability", {}).get("totalRecordCount", 0) > 0]

    async def _search_unesco_uis(self, query, page):
        version, rows = await self._uis_catalog()
        return _page([_candidate("unesco-uis", r["indicatorCode"], r["name"], "完整指标；该发布版本的全部地区与年份。",
                                 coverage={"scope": "完整指标", **r.get("dataAvailability", {})}, metadata={"version": version})
                      for r in rows if _matches(query, r["indicatorCode"], r["name"], r.get("theme", ""))], page)

    async def _resolve_unesco_uis(self, external):
        version, rows = await self._uis_catalog()
        row = next((r for r in rows if r["indicatorCode"] == external), None)
        if not row:
            return []
        return [_option("unesco-uis", external, UIS + "/data/indicators/export?" + urlencode({"format": "csv", "indicator": external, "version": version}),
                        external + "_" + version + ".zip", version, row, "application/zip")]
