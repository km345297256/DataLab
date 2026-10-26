"""Anonymous official regional datasets, resolved from fresh public manifests.

Macao's official website calls /datadir/search and /datadir/detail/{id}; its
terms explicitly distinguish completely open data from registration-only data:
https://data.gov.mo/UseClause . Only the former is exposed here.
UN SDG contract: https://unstats.un.org/sdgapi/swagger/ . DataSlice is a
complete, unpaginated series for one geography; Series/Data is paginated and
is deliberately not used as a downloadable whole dataset.
"""
from __future__ import annotations

import mimetypes
import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit

from app.domain.external.data_source import SourceConnectorError

SOURCES = frozenset({"macau-open-data", "un-sdg"})
PAGE_SIZE = 10
_ID = r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}"
_MACAU = "https://api.data.gov.mo"
_TERMS = "https://data.gov.mo/UseClause"
_SDG = "https://unstats.un.org/sdgapi/v1/sdg"
_SERIES = r"[A-Z][A-Z0-9_]{1,79}"
_FORMATS = frozenset({"CSV", "TSV", "XLS", "XLSX", "ODS", "JSON", "GEOJSON", "XML", "ZIP", "GZ", "SHP", "GPKG"})
_DOCUMENT = re.compile(r"readme|metadata|data[ _-]?dictionary|codebook|数据字典|數據字典|使用说明|使用說明", re.I)


def safe_file_url(source_id: str, url: str) -> bool:
    if not isinstance(source_id, str) or source_id not in SOURCES or not isinstance(url, str) or len(url) > 2000 or re.search(r"[\x00-\x20\\%]", url):
        return False
    try:
        p = urlsplit(url)
        if p.scheme != "https" or p.username or p.password or p.fragment or p.port not in (None, 443):
            return False
        if source_id == "un-sdg":
            return (p.hostname == "unstats.un.org" and not p.query
                    and bool(re.fullmatch(rf"/sdgapi/v1/sdg/Series/{_SERIES}/GeoArea/[1-9][0-9]{{0,5}}/DataSlice", p.path)))
        if p.hostname != "api.data.gov.mo" or p.path != "/datadir/downloadSingleFile":
            return False
        pairs = parse_qsl(p.query, keep_blank_values=True, strict_parsing=True)
        values = dict(pairs)
        return (len(pairs) == 2 and set(values) == {"fileId", "dataDirId"}
                and bool(re.fullmatch(r"[1-9][0-9]{0,17}", values["fileId"]))
                and bool(re.fullmatch(_ID, values["dataDirId"])))
    except (ValueError, TypeError):
        return False


def _localized(value):
    # The official portal stores Traditional/Simplified Chinese, Portuguese,
    # English in one field separated by this literal character.
    return value.split("☯", 1)[0].strip() if isinstance(value, str) else ""


def _payload(data):
    if not isinstance(data, dict) or type(data.get("code")) is not int or data["code"] != 0 or not isinstance(data.get("data"), dict):
        raise SourceConnectorError("invalid_response")
    return data["data"]


def is_sdg_observation_sample(sample: bytes, external_id: str, *, complete: bool) -> bool:
    """Validate only the already-read prefix; never fetch or cap a whole file.

The API returns HTTP 200 even for missing series/areas, with dimensions=[].
For truncated JSON, decode individual *complete* observation objects after the
matching series/area header. Incomplete objects are not evidence of data.
    """
    match = re.fullmatch(rf"({_SERIES}):([1-9][0-9]{{0,5}})", external_id) if isinstance(external_id, str) else None
    if not match or not isinstance(sample, bytes):
        return False
    series, geo = match.groups()

    def identity(obj):
        return obj.get("series") == series and str(obj.get("geoAreaCode")) == geo

    def observation(obj):
        if not isinstance(obj, dict):
            return False
        period, value = obj.get("timePeriodStart"), obj.get("value")
        return (not isinstance(period, bool) and isinstance(period, (str, int, float))
                and bool(re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", str(period)))
                and not isinstance(value, bool) and isinstance(value, (str, int, float))
                and bool(str(value).strip()))

    try:
        text = sample.decode("utf-8-sig", errors="strict" if complete else "ignore")
        if complete:
            obj = json.loads(text)
            return (isinstance(obj, dict) and identity(obj) and isinstance(obj.get("dimensions"), list)
                    and any(observation(row) for row in obj["dimensions"]))
        # Prefix decoder walks the JSON structure instead of matching field
        # names inside string values. Header fields precede dimensions in the
        # official response; unknown or incomplete layouts are not advertised.
        decoder, pos, header = json.JSONDecoder(), 0, {}
        def whitespace(index):
            while index < len(text) and text[index].isspace():
                index += 1
            return index
        pos = whitespace(pos)
        if text[pos] != "{":
            return False
        pos += 1
        while True:
            key, pos = decoder.raw_decode(text, whitespace(pos))
            pos = whitespace(pos)
            if not isinstance(key, str) or key in header or text[pos] != ":":
                return False
            pos = whitespace(pos + 1)
            if key == "dimensions":
                if not identity(header) or text[pos] != "[":
                    return False
                pos += 1
                while True:
                    row, pos = decoder.raw_decode(text, whitespace(pos))
                    if observation(row):
                        return True
                    pos = whitespace(pos)
                    if text[pos] != ",":
                        return False
                    pos += 1
            header[key], pos = decoder.raw_decode(text, pos)
            pos = whitespace(pos)
            if text[pos] != ",":
                return False
            pos += 1
    except (ValueError, UnicodeError, IndexError, TypeError):
        return False


class RegionalExpansionAdapters:
    def __init__(self, fetch_json, fetch_text=None):
        self._json = fetch_json

    async def search(self, source_id, query, page):
        if not isinstance(source_id, str) or source_id not in SOURCES:
            raise SourceConnectorError("unknown_source")
        if not isinstance(query, str) or not query.strip() or len(query) > 500 or type(page) is not int or not 1 <= page <= 100:
            raise SourceConnectorError("invalid_query")
        query = query.strip()
        if len(query) >= 2 and (query[0], query[-1]) in {(chr(34), chr(34)), ("'", "'"), ("“", "”"), ("‘", "’")}:
            query = query[1:-1].strip()
        if not query:
            raise SourceConnectorError("invalid_query")
        if source_id == "un-sdg":
            return await self._sdg_search(query, page)
        data = _payload(await self._json(_MACAU + "/datadir/search", method="POST", json={
            "api": False, "categoryId": "", "deptId": "", "file": True, "fileType": "",
            "key": query.strip(), "orderBy": "asc", "page": page, "rows": PAGE_SIZE,
            "sortBy": "name", "url": False,
        }))
        rows, total = data.get("items"), data.get("total")
        if not isinstance(rows, list) or type(total) is not int or total < 0:
            raise SourceConnectorError("invalid_response")
        from app.infrastructure.external.data_sources.open_data_sources import _candidate
        results = []
        for row in rows[:PAGE_SIZE]:
            if not isinstance(row, dict) or type(row.get("openType")) is not int or row["openType"] != 0 or row.get("isFile") != 1:
                continue
            external, title = row.get("id"), _localized(row.get("name"))
            if not isinstance(external, str) or not re.fullmatch(_ID, external) or not title:
                continue
            formats = sorted(set(str(row.get("dataFormat", "")).upper().split(",")) & _FORMATS)
            if not formats:
                continue
            results.append(_candidate(source_id, external, title, _localized(row.get("description")),
                                      "https://data.gov.mo/Detail?id=" + external, formats=formats,
                                      license=_TERMS))
        return {"results": results, "total_count": total}

    async def resolve(self, candidate):
        if not isinstance(candidate, dict) or not isinstance(candidate.get("source_id"), str) or candidate["source_id"] not in SOURCES:
            raise SourceConnectorError("invalid_candidate")
        source, external = candidate["source_id"], candidate.get("external_id")
        if source == "un-sdg":
            return await self._sdg_resolve(external)
        if not isinstance(external, str) or not re.fullmatch(_ID, external):
            raise SourceConnectorError("invalid_candidate")
        data = _payload(await self._json(_MACAU + "/datadir/detail/" + external))
        info, files = data.get("info"), data.get("file")
        if not isinstance(info, dict) or not isinstance(files, list):
            raise SourceConnectorError("invalid_response")
        # status=1 means an approved logged-in application. It is intentionally
        # excluded: this adapter carries no login or registration credentials.
        if type(info.get("openType")) is not int or info["openType"] != 0 or data.get("status") is not None:
            return []
        from app.infrastructure.external.data_sources.open_data_sources import _filename, _identity
        options, seen = [], set()
        version = info.get("updateTime") if isinstance(info.get("updateTime"), str) else None
        for file in files:
            if not isinstance(file, dict):
                continue
            file_id, name = file.get("fileId"), file.get("fileName")
            if type(file_id) is not int or not 0 < file_id < 10**18 or file_id in seen or not isinstance(name, str):
                continue
            suffix = name.rsplit(".", 1)[-1].upper()
            if suffix not in _FORMATS or _DOCUMENT.search(name) or "/" in name or "\\" in name:
                continue
            url = _MACAU + "/datadir/downloadSingleFile?" + urlencode({"fileId": file_id, "dataDirId": external})
            if not safe_file_url(source, url):
                continue
            seen.add(file_id)
            options.append({
                "id": _identity(source, external, version, file_id, name),
                "source_id": source, "external_id": external, "version": version,
                "filename": _filename(f"{source}_{external}_{file_id}_{name}"), "source_filename": name,
                "size": None, "content_type": mimetypes.guess_type(name)[0], "url": url, "checksum": None,
                "available": True, "reason": "", "kind": "源站文件", "license": _TERMS,
                "record_url": "https://data.gov.mo/Detail?id=" + external,
            })
        return options

    async def _sdg_catalog(self):
        # The single-series /List route currently returns [] for existing
        # series. The documented complete current-release catalog is reliable.
        rows = await self._json(_SDG + "/Series/List", {"allreleases": "false"})
        if not isinstance(rows, list):
            raise SourceConnectorError("invalid_response")
        return [row for row in rows if isinstance(row, dict)
                and isinstance(row.get("code"), str) and re.fullmatch(_SERIES, row["code"])
                and isinstance(row.get("description"), str) and row["description"].strip()]

    async def _sdg_search(self, query, page):
        from app.infrastructure.external.data_sources.open_data_sources import _candidate
        geography = re.findall(r"(?:^|\s)geo=([1-9][0-9]{0,5})(?=\s|$)", query, re.I)
        if "geo=" in query.casefold() and len(geography) != 1:
            raise SourceConnectorError("invalid_query")
        geo = geography[0] if geography else "1"
        query = re.sub(r"(?:^|\s)geo=[1-9][0-9]{0,5}(?=\s|$)", " ", query, flags=re.I)
        terms = re.findall(r"[\w.]+", query.casefold())
        if not terms:
            raise SourceConnectorError("invalid_query")
        matches, seen = [], set()
        for row in await self._sdg_catalog():
            external = row["code"]
            indicators = row.get("indicator", [])
            indicators = indicators if isinstance(indicators, list) else []
            haystack = (external + " " + row["description"] + " " + " ".join(x for x in indicators if isinstance(x, str))).casefold()
            if external in seen or not all(term in haystack for term in terms):
                continue
            seen.add(external)
            matches.append(row)
        results = []
        for row in matches[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]:
            code = row["code"]
            name = (await self._sdg_areas(code)).get(geo)
            if name is None:
                continue
            scope = name + ("（全球汇总）" if geo == "1" else "")
            results.append(_candidate("un-sdg", f"{code}:{geo}", row["description"] + " · " + scope,
                                      f"{scope}（M49={geo}）该指标的完整官方 JSON 时间序列，保留全部可用观测与维度。未指定地区时提供 World 汇总；检索其他地区可使用序列代码与 geo=M49编码，例如 EG_ACS_ELEC geo=156。",
                                      "https://unstats.un.org/sdgs/dataportal/database",
                                      formats=["JSON"], coverage={"geography": name, "m49": geo},
                                      metadata={"series_code": code, "geo_area_code": geo}))
        return {"results": results, "total_count": len(matches)}

    async def _sdg_areas(self, series):
        areas = await self._json(_SDG + f"/Series/{series}/GeoAreas")
        if not isinstance(areas, list):
            raise SourceConnectorError("invalid_response")
        names = {}
        for area in areas:
            if not isinstance(area, dict):
                continue
            code, name = area.get("geoAreaCode"), area.get("geoAreaName")
            if isinstance(code, str) and re.fullmatch(r"[1-9][0-9]{0,5}", code) and isinstance(name, str) and name.strip():
                names.setdefault(code, name)
        return names

    async def _sdg_resolve(self, external):
        match = re.fullmatch(rf"({_SERIES}):([1-9][0-9]{{0,5}})", external) if isinstance(external, str) else None
        if not match:
            raise SourceConnectorError("invalid_candidate")
        series, geo = match.groups()
        record = next((row for row in await self._sdg_catalog() if row["code"] == series), None)
        if record is None:
            return []
        name = (await self._sdg_areas(series)).get(geo)
        if name is None:
            return []
        from app.infrastructure.external.data_sources.open_data_sources import _filename, _identity
        version = record.get("release") if isinstance(record.get("release"), str) else None
        url = _SDG + f"/Series/{series}/GeoArea/{geo}/DataSlice"
        filename = _filename(f"un-sdg_{series}_{geo}_{name}.json")
        return [{
                "id": _identity("un-sdg", external, version), "source_id": "un-sdg",
                "external_id": external, "version": version, "filename": filename,
                "source_filename": filename, "size": None, "content_type": "application/json",
                "url": url, "checksum": None, "available": True, "reason": "",
                "kind": name + " · 完整时间序列", "license": None,
                "record_url": "https://unstats.un.org/sdgs/dataportal/database",
            }]
