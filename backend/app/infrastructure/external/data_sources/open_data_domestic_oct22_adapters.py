"""Official current earthquake catalog and individual city weather snapshots.

The endpoints are used by the providers' own public pages. These adapters never
claim historical query support, generate observations, or strip missing values.
Downloads preserve the complete provider JSON rather than reconstructed rows.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import json
import math
import re
import time
from app.domain.external.data_source import SourceConnectorError

SOURCES = frozenset(("cenc-earthquake", "nmc-weather"))
CENC_URL = "https://www.ceic.ac.cn/data/data.json"
NMC = "https://www.nmc.cn"
NMC_LICENSE = NMC + "/publish/cms/view/722665831f0a4e98800c41e691444963.html"
PAGE_SIZE = 10


def safe_file_url(source, url):
    if source not in SOURCES or not isinstance(url, str) or len(url) > 1024:
        return False
    if source == "cenc-earthquake":
        return url == CENC_URL
    return bool(re.fullmatch(r"https://www\.nmc\.cn/rest/real/[A-Za-z0-9]{5,12}", url))


def _number(value, minimum, maximum):
    return type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum


def _date(value, pattern):
    if not isinstance(value, str):
        return False
    try:
        return datetime.strptime(value, pattern).strftime(pattern) == value
    except ValueError:
        return False


def _earthquake(row):
    return (isinstance(row, dict) and isinstance(row.get("id"), str)
            and bool(re.fullmatch(r"[A-Z]{2}\.[0-9]{14}\.[0-9]+", row["id"]))
            and _date(row.get("time"), "%Y-%m-%d %H:%M:%S")
            and _number(row.get("latitude"), -90, 90) and _number(row.get("longitude"), -180, 180)
            and _number(row.get("depth"), 0, 1000) and _number(row.get("magnitude"), -2, 10)
            and isinstance(row.get("location"), str) and bool(row["location"].strip()))


def _weather(row, station):
    if (not isinstance(row, dict) or not isinstance(row.get("station"), dict)
            or row["station"].get("code") != station
            or not _date(row.get("publish_time"), "%Y-%m-%d %H:%M")
            or not isinstance(row.get("weather"), dict) or not isinstance(row.get("wind"), dict)):
        return False
    bounds = {"temperature": (-100, 70), "humidity": (0, 100), "rain": (0, 3000), "airpressure": (100, 1200)}
    # Missing sentinels alone never constitute an acquired observation.
    return (any(_number(row["weather"].get(key), *bound) for key, bound in bounds.items())
            or _number(row["wind"].get("speed"), 0, 150))


def is_domestic_observation_sample(source, content, external):
    """Source-aware probe: reject empty successful JSON, metadata and wrong cities.

    CENC's full catalog is larger than a probe. Decode its complete first event
    from a prefix. NMC snapshots are small and must decode as a full object.
    """
    try:
        value = content.decode("utf-8-sig").lstrip()
        if source == "cenc-earthquake":
            if external != "current-catalog" or not value.startswith("["):
                return False
            first, _ = json.JSONDecoder().raw_decode(value[1:].lstrip())
            return _earthquake(first)
        if source == "nmc-weather":
            return _weather(json.loads(value), external)
    except (UnicodeError, ValueError, TypeError):
        return False
    return False


def _query(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise SourceConnectorError("invalid_query")
    value = value.strip()
    if len(value) >= 2 and (value[0], value[-1]) in (("\"", "\""), ("'", "'"), ("“", "”")):
        value = value[1:-1].strip()
    return value


def _province_name(value):
    return re.sub(r"(?:壮族自治区|回族自治区|维吾尔自治区|自治区|特别行政区|省|市)$", "", value)


def _option(source, external, name, url, record_url, version=None, license=None):
    from app.infrastructure.external.data_sources.open_data_sources import _identity
    return {"id": _identity(source, external, version, url), "source_id": source,
            "external_id": external, "version": version, "filename": name, "source_filename": name,
            "url": url, "size": None, "content_type": "application/json", "checksum": None,
            "available": True, "reason": "", "kind": "源站完整 JSON 快照",
            "record_url": record_url, "license": license}


class DomesticOct22Adapters:
    def __init__(self, fetch_json):
        self._json = fetch_json
        self._stations = None
        self._stations_at = 0.0
        self._lock = asyncio.Lock()

    async def _cenc(self):
        data = await self._json(CENC_URL)
        if (not isinstance(data, list) or not data or not all(_earthquake(row) for row in data)
                or len({row["id"] for row in data}) != len(data)):
            raise SourceConnectorError("no_observations")
        return data

    async def _catalog(self):
        async with self._lock:
            if self._stations is not None and time.monotonic() - self._stations_at < 3600:
                return self._stations
            provinces = await self._json(NMC + "/rest/province/all")
            if not isinstance(provinces, list) or not provinces:
                raise SourceConnectorError("invalid_response")
            codes = []
            for row in provinces:
                if (not isinstance(row, dict) or not isinstance(row.get("code"), str)
                        or not re.fullmatch(r"A[A-Z]{2}", row["code"])):
                    raise SourceConnectorError("invalid_response")
                codes.append(row["code"])
            if len(codes) != len(set(codes)):
                raise SourceConnectorError("invalid_response")
            slots = asyncio.Semaphore(4)
            async def read(code):
                async with slots:
                    rows = await self._json(NMC + "/rest/province/" + code)
                if not isinstance(rows, list):
                    raise SourceConnectorError("invalid_response")
                for row in rows:
                    if (not isinstance(row, dict) or not isinstance(row.get("code"), str)
                            or not re.fullmatch(r"[A-Za-z0-9]{5,12}", row["code"])
                            or not all(isinstance(row.get(k), str) and row[k].strip() for k in ("province", "city"))
                            or not isinstance(row.get("url"), str)
                            or not re.fullmatch(r"/publish/forecast/" + code + r"/[a-z0-9_-]+\.html", row["url"])):
                        raise SourceConnectorError("invalid_response")
                return rows
            groups = await asyncio.gather(*(read(code) for code in codes))
            stations = [row for group in groups for row in group]
            if len({row["code"] for row in stations}) != len(stations):
                raise SourceConnectorError("invalid_response")
            self._stations, self._stations_at = stations, time.monotonic()
            return stations

    async def search(self, source, query, page):
        from app.infrastructure.external.data_sources.open_data_sources import _candidate
        if source not in SOURCES:
            raise SourceConnectorError("unknown_source")
        query = _query(query)
        if type(page) is not int or not 1 <= page <= 100:
            raise SourceConnectorError("invalid_query")
        if source == "cenc-earthquake":
            rows = await self._cenc()
            start, end = min(r["time"] for r in rows), max(r["time"] for r in rows)
            generic = re.search(r"地震|震级|震中|earthquake|seismic|seismology|\bcenc\b|\bceic\b", query, re.I)
            if not generic and not any(query in r["location"] or query == r["id"] for r in rows):
                return {"results": [], "total_count": 0}
            years = re.findall(r"(?<![0-9])(?:19|20)[0-9]{2}(?![0-9])", query)
            if years and not all(start[:4] <= year <= end[:4] for year in years):
                return {"results": [], "total_count": 0}
            candidate = _candidate(source, "current-catalog", f"中国地震台网当前目录 · {start[:10]} 至 {end[:10]}",
                f"官网当前公布的完整地震目录，共 {len(rows)} 个事件。包含北京时间、震级、经纬度、深度及参考位置；获取整个目录，关键词不裁剪观测事件。", "https://www.ceic.ac.cn/",
                formats=["JSON"], coverage={"start": start, "end": end, "timezone": "Asia/Shanghai", "records": len(rows)},
                metadata={"snapshot": True}, limitations=["当前公开目录会随源站更新；不代表全部历史地震记录。"])
            return {"results": [candidate] if page == 1 else [], "total_count": 1}
        # This source has no historical date-range endpoint. Never turn a past
        # period request into today's observation under the same label.
        if re.search(r"历史|历年|多年|去年|前年|今年|全年|昨天|昨日|上个月|上月|本月|本周|过去|最近|年均|月均|日均|气候|趋势|"
                     r"近\s*[0-9一二三四五六七八九十]+\s*[天月年]|[0-9]{4}[-年/]|history|historical|climate|trend|past|previous|last\s", query, re.I):
            return {"results": [], "total_count": 0}
        rows = await self._catalog()
        exact = [row for row in rows if row["code"] == query.removeprefix("station=")]
        cities = [row for row in rows if row["city"] in query]
        if cities:
            # Where the query names a province as well, disambiguate repeated
            # city names using the same fresh provider directory.
            provinces = {row["province"] for row in rows if _province_name(row["province"]) in query}
            if provinces:
                cities = [row for row in cities if row["province"] in provinces]
        matches = exact or cities or [row for row in rows if _province_name(row["province"]) in query]
        results = []
        for row in matches[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]:
            results.append(_candidate(source, row["code"], f"{row['province']} · {row['city']}最新天气实况",
                "该城市最近一次气象观测快照，含气温、湿度、降水和风；保留源站缺测标记 9999。不是历史气候序列或天气预报。",
                NMC + row["url"], formats=["JSON"], license=NMC_LICENSE,
                coverage={"province": row["province"], "city": row["city"], "temporal": "最新一次实况"},
                limitations=["商业使用须事先取得中央气象台书面授权；遵守官方网站声明。"], metadata={"snapshot": True}))
        return {"results": results, "total_count": len(matches)}

    async def resolve(self, candidate):
        source, external = candidate.get("source_id"), candidate.get("external_id")
        if source not in SOURCES:
            raise SourceConnectorError("unknown_source")
        if source == "cenc-earthquake":
            if external != "current-catalog":
                raise SourceConnectorError("invalid_candidate")
            await self._cenc()
            return [_option(source, external, "cenc_current_catalog.json", CENC_URL, "https://www.ceic.ac.cn/")]
        if not isinstance(external, str) or not re.fullmatch(r"[A-Za-z0-9]{5,12}", external):
            raise SourceConnectorError("invalid_candidate")
        row = next((r for r in await self._catalog() if r["code"] == external), None)
        if row is None:
            raise SourceConnectorError("invalid_candidate")
        url = NMC + "/rest/real/" + external
        data = await self._json(url)
        if not _weather(data, external):
            raise SourceConnectorError("no_observations")
        return [_option(source, external, f"nmc_{external}_latest_observation.json", url, NMC + row["url"], license=NMC_LICENSE)]
