"""Official event/point/indicator queries and CERN original-file manifests.

API contracts are recorded in open_data_earth_expansion_definitions.SOURCE_INFO.
No source accepts caller supplied download URLs. Observation exports never add
record limits or transform values; the source's CSV bytes are downloaded intact.
"""
from __future__ import annotations

import csv
from datetime import date, timedelta
import io
import math
import mimetypes
import re
from urllib.parse import parse_qsl, urlencode, urlsplit

from app.domain.external.data_source import SourceConnectorError

SOURCES = frozenset(("usgs-earthquake", "nasa-power", "noaa-gml", "cern-open-data"))
PAGE_SIZE = 10
USGS = "https://earthquake.usgs.gov/fdsnws/event/1/"
POWER = "https://power.larc.nasa.gov/api/temporal/daily/point"
CERN = "https://opendata.cern.ch"
PARAMETERS = frozenset(("T2M", "T2M_MIN", "T2M_MAX", "PRECTOTCORR", "RH2M", "WS10M", "PS", "ALLSKY_SFC_SW_DWN"))
NOAA = {
    "co2-mlo-monthly": ("CO2 二氧化碳 · Mauna Loa 月均观测", "co2/co2_mm_mlo.csv", ("co2", "carbon dioxide", "二氧化碳"), "https://gml.noaa.gov/ccgg/trends/data.html"),
    "ch4-global-monthly": ("CH4 甲烷 · 全球海洋地表月均观测", "ch4/ch4_mm_gl.csv", ("ch4", "methane", "甲烷"), "https://gml.noaa.gov/ccgg/trends_ch4/"),
    "n2o-global-monthly": ("N2O 氧化亚氮 · 全球海洋地表月均观测", "n2o/n2o_mm_gl.csv", ("n2o", "nitrous oxide", "氧化亚氮", "一氧化二氮"), "https://gml.noaa.gov/ccgg/trends_n2o/"),
    "sf6-global-monthly": ("SF6 六氟化硫 · 全球海洋地表月均观测", "sf6/sf6_mm_gl.csv", ("sf6", "sulfur hexafluoride", "六氟化硫"), "https://gml.noaa.gov/ccgg/trends_sf6/"),
}
NOAA_BASE = "https://gml.noaa.gov/webdata/ccgg/trends/"
NOAA_URLS = frozenset(NOAA_BASE + row[1] for row in NOAA.values())
CERN_ID = r"(?:[1-9][0-9]{0,11}|atlas-[1-9][0-9]{0,11})"
UUID = r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}"
CERN_PATH = r"/eos/opendata/(?:cms|atlas|alice|lhcb|opera|delphi|jade|totem)/(?:[A-Za-z0-9_-][A-Za-z0-9._-]*/)*[A-Za-z0-9_-][A-Za-z0-9._-]*"
DATA_FILE = re.compile(r"\.(?:csv|tsv|root|parquet|h5|hdf5|hepmc|lhe|dat|json|zip|ig)(?:\.(?:gz|bz2|xz))?$", re.I)


def _pairs(query, *, external=False):
    if not isinstance(query, str) or not query or len(query) > 1500:
        raise SourceConnectorError("invalid_query")
    if external:
        pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True)
    else:
        pieces = re.split(r"[\s&]+", query.strip())
        if any(piece.count("=") != 1 for piece in pieces):
            raise SourceConnectorError("invalid_query")
        pairs = [piece.split("=", 1) for piece in pieces]
    if len(dict(pairs)) != len(pairs):
        raise SourceConnectorError("invalid_query")
    return dict(pairs)


def _number(value, minimum, maximum):
    if not isinstance(value, str) or not re.fullmatch(r"-?[0-9]{1,6}(?:\.[0-9]{1,8})?", value):
        raise SourceConnectorError("invalid_query")
    number = float(value)
    if not minimum <= number <= maximum:
        raise SourceConnectorError("invalid_query")
    return format(number, ".8f").rstrip("0").rstrip(".") or "0"


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-?[0-9]{2}-?[0-9]{2}", value):
        raise SourceConnectorError("invalid_query")
    parsed = date.fromisoformat(value)
    return parsed


def _usgs_params(values):
    if set(values) == {"eventid"}:
        if not re.fullmatch(r"[a-z0-9]{4,30}", values["eventid"]):
            raise SourceConnectorError("invalid_query")
        return values
    allowed = {"start", "end", "starttime", "endtime", "minmagnitude", "maxmagnitude",
               "minlatitude", "maxlatitude", "minlongitude", "maxlongitude"}
    if set(values) - allowed or ("start" in values and "starttime" in values) or ("end" in values and "endtime" in values):
        raise SourceConnectorError("invalid_query")
    start, end = _date(values.get("start", values.get("starttime"))), _date(values.get("end", values.get("endtime")))
    if end <= start or end > date.today() + timedelta(days=1):
        raise SourceConnectorError("invalid_query")
    result = {"starttime": start.isoformat(), "endtime": end.isoformat()}
    for key in ("minmagnitude", "maxmagnitude", "minlatitude", "maxlatitude", "minlongitude", "maxlongitude"):
        if key in values:
            bound = 90 if "latitude" in key else 180 if "longitude" in key else 10
            result[key] = _number(values[key], -bound, bound)
    for kind in ("magnitude", "latitude", "longitude"):
        low, high = "min" + kind, "max" + kind
        if low in result and high in result and float(result[low]) > float(result[high]):
            raise SourceConnectorError("invalid_query")
    return result


def _power_params(values):
    if set(values) - {"latitude", "longitude", "start", "end", "parameters"}:
        raise SourceConnectorError("invalid_query")
    start, end = _date(values.get("start")), _date(values.get("end"))
    if start < date(1981, 1, 1) or end < start or end > date.today():
        raise SourceConnectorError("invalid_query")
    parameters = values.get("parameters", "T2M").upper().split(",")
    if not parameters or len(parameters) != len(set(parameters)) or set(parameters) - PARAMETERS:
        raise SourceConnectorError("invalid_query")
    return {"latitude": _number(values.get("latitude"), -90, 90),
            "longitude": _number(values.get("longitude"), -180, 180),
            "start": start.strftime("%Y%m%d"), "end": end.strftime("%Y%m%d"),
            "parameters": ",".join(parameters)}


def _encoded(values):
    return urlencode(sorted(values.items()))


def _external_params(source, value):
    result = (_usgs_params if source == "usgs-earthquake" else _power_params)(_pairs(value, external=True))
    if _encoded(result) != value:
        raise SourceConnectorError("invalid_candidate")
    return result


def safe_file_url(source, url):
    try:
        if source not in SOURCES or not isinstance(url, str) or len(url) > 4096 or re.search(r"[\x00-\x20\\]", url):
            return False
        p = urlsplit(url)
        if p.scheme != "https" or p.username or p.password or p.port not in (None, 443) or p.fragment:
            return False
        if re.search(r"%2f|%5c|%2e", p.path, re.I) or any(s in (".", "..") for s in p.path.split("/")):
            return False
        if source == "noaa-gml":
            return url in NOAA_URLS
        if source == "cern-open-data":
            return p.hostname == "opendata.cern.ch" and not p.query and bool(re.fullmatch(CERN_PATH, p.path)) and bool(DATA_FILE.search(p.path))
        pairs = parse_qsl(p.query, keep_blank_values=True, strict_parsing=True)
        params = dict(pairs)
        if len(params) != len(pairs):
            return False
        if source == "usgs-earthquake":
            if p.hostname != "earthquake.usgs.gov" or p.path != "/fdsnws/event/1/query" or params.pop("format", None) != "csv":
                return False
            return _usgs_params(params) == params
        if p.hostname != "power.larc.nasa.gov" or p.path != "/api/temporal/daily/point":
            return False
        if params.pop("format", None) != "CSV" or params.pop("community", None) != "RE" or params.pop("time-standard", None) != "UTC":
            return False
        return _power_params(params) == params
    except (ValueError, TypeError, SourceConnectorError):
        return False


def _option(source, external, name, url, record_url, *, size=None, checksum=None, version=None, license=None):
    from app.infrastructure.external.data_sources.open_data_sources import _filename, _identity
    return dict(id=_identity(source, external, name, url, checksum, version), source_id=source, external_id=external,
                filename=_filename(source + "_" + name), source_filename=name, url=url, record_url=record_url,
                size=size, checksum=checksum, version=version, license=license,
                content_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
                available=size != 0, reason="", kind="源站文件")


class EarthExpansionAdapters:
    def __init__(self, fetch_json, fetch_text):
        self._json, self._text = fetch_json, fetch_text

    async def search(self, source, query, page):
        from app.infrastructure.external.data_sources.open_data_sources import _candidate
        if source not in SOURCES:
            raise SourceConnectorError("unknown_source")
        if not isinstance(query, str) or not query.strip() or len(query) > 500 or type(page) is not int or not 1 <= page <= 100:
            raise SourceConnectorError("invalid_query")
        try:
            if source == "cern-open-data":
                return await self._cern_search(query, page)
            query = query.strip()
            # The shared planner may quote a complete query as one search term.
            # Parameter services interpret the content, not Lucene phrase syntax.
            if len(query) >= 2 and (query[0], query[-1]) in {(chr(34), chr(34)), ("'", "'"), ("“", "”")}:
                query = query[1:-1].strip()
            if page != 1:
                return {"results": [], "total_count": 0}
            if source == "noaa-gml":
                result = []
                lower = query.strip().lower().replace("₂", "2").replace("₄", "4").replace("₆", "6")
                for external, (title, _, aliases, record) in NOAA.items():
                    if lower not in {"greenhouse gases", "温室气体", "noaa gml"} and not any(alias in lower for alias in aliases):
                        continue
                    version, count = await self._noaa_record(external)
                    result.append(_candidate(source, external, title, f"源站完整月平均序列，当前含{count}个观测月。保留源文件引用和质量说明。", record,
                                             metadata={"version": version, "observation_months": count}))
                return {"results": result, "total_count": len(result)}
            if source == "usgs-earthquake" and query.strip().lower() in {"earthquake", "earthquakes", "地震", "usgs"}:
                end = date.today()
                values = {"start": (end - timedelta(days=30)).isoformat(), "end": end.isoformat(), "minmagnitude": "4.5"}
            else:
                values = _pairs(query)
            params = (_usgs_params if source == "usgs-earthquake" else _power_params)(values)
            external = _encoded(params)
            if source == "usgs-earthquake":
                count = await self._usgs_count(params)
                title = "USGS 地震观测 · " + (params.get("eventid") or params["starttime"] + " 至 " + params["endtime"])
                record = "https://earthquake.usgs.gov/earthquakes/search/"
                description = f"查询条件：{external}；当前{count}条事件观测，CSV导出全部匹配事件，不设置limit。"
            else:
                count = await self._power_count(params)
                title = f"NASA POWER · {params['latitude']}, {params['longitude']} · {params['start']}—{params['end']}"
                record = "https://power.larc.nasa.gov/"
                description = f"逐日UTC数据；参数{params['parameters']}；{count}个有有效测量值的日期。完整CSV保留缺失值与源分辨率。"
            rows = [_candidate(source, external, title, description, record, metadata={"observations": count})] if count else []
            return {"results": rows, "total_count": len(rows)}
        except (ValueError, KeyError, TypeError, AttributeError):
            raise SourceConnectorError("invalid_response") from None

    async def resolve(self, candidate):
        try:
            source, external = candidate["source_id"], candidate["external_id"]
            if source == "cern-open-data":
                return await self._cern_resolve(candidate)
            if source == "noaa-gml":
                if external not in NOAA:
                    raise SourceConnectorError("invalid_candidate")
                version, _ = await self._noaa_record(external)
                requested = candidate.get("metadata", {}).get("version")
                if requested and requested != version:
                    raise SourceConnectorError("inconsistent_source_snapshot")
                row = NOAA[external]
                return [_option(source, external, row[1].split("/")[-1], NOAA_BASE + row[1], row[3], version=version,
                                license="NOAA GML data use and citation policy: see original file header")]
            if source not in {"usgs-earthquake", "nasa-power"}:
                raise SourceConnectorError("unknown_source")
            params = _external_params(source, external)
            if source == "usgs-earthquake":
                if not await self._usgs_count(params):
                    return []
                url = USGS + "query?" + urlencode({**params, "format": "csv"})
                name = "events_" + (params.get("eventid") or params["starttime"] + "_" + params["endtime"]) + ".csv"
                record = "https://earthquake.usgs.gov/earthquakes/search/"
            else:
                if not await self._power_count(params):
                    return []
                url = POWER + "?" + urlencode({**params, "community": "RE", "format": "CSV", "time-standard": "UTC"})
                name = f"daily_{params['latitude']}_{params['longitude']}_{params['start']}_{params['end']}.csv"
                record = "https://power.larc.nasa.gov/"
            return [_option(source, external, name, url, record)]
        except (KeyError, ValueError, TypeError, AttributeError):
            raise SourceConnectorError("invalid_response") from None

    async def _usgs_count(self, params):
        if "eventid" in params:
            item = await self._json(USGS + "query", {**params, "format": "geojson"})
            if item.get("type") != "Feature" or item.get("id") != params["eventid"] or not isinstance(item.get("geometry", {}).get("coordinates"), list):
                raise SourceConnectorError("invalid_response")
            return 1
        item = await self._json(USGS + "count", {**params, "format": "geojson"})
        count, maximum = item.get("count"), item.get("maxAllowed")
        if type(count) is not int or count < 0 or type(maximum) is not int or maximum < 1:
            raise SourceConnectorError("invalid_response")
        if count > maximum:
            raise SourceConnectorError("query_not_found_or_invalid")
        return count

    async def _power_count(self, params):
        item = await self._json(POWER, {**params, "community": "RE", "format": "JSON", "time-standard": "UTC"})
        header, fields = item["header"], item["properties"]["parameter"]
        if (item.get("type") != "Feature" or header.get("start") != params["start"] or header.get("end") != params["end"]
                or header.get("time_standard") != "UTC" or set(fields) != set(params["parameters"].split(","))):
            raise SourceConnectorError("invalid_response")
        coords = item["geometry"]["coordinates"]
        if len(coords) < 2 or any(type(coords[i]) not in (int, float) or not math.isfinite(coords[i]) or abs(coords[i] - float(params[key])) > .00001 for i, key in enumerate(("longitude", "latitude"))):
            raise SourceConnectorError("invalid_response")
        valid_dates = set()
        for values in fields.values():
            if not isinstance(values, dict):
                raise SourceConnectorError("invalid_response")
            for day, value in values.items():
                if not params["start"] <= day <= params["end"] or not re.fullmatch(r"[0-9]{8}", day):
                    raise SourceConnectorError("invalid_response")
                _date(day)
                if type(value) in (int, float) and math.isfinite(value) and value != header.get("fill_value", -999):
                    valid_dates.add(day)
        return len(valid_dates)

    async def _noaa_record(self, external):
        text = await self._text(NOAA_BASE + NOAA[external][1], accept="text/csv")
        if not isinstance(text, str) or re.search(r"<html|<!doctype", text[:1000], re.I):
            raise SourceConnectorError("invalid_response")
        rows = list(csv.reader(line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")))
        if len(rows) < 2 or rows[0][:2] != ["year", "month"] or not any(len(row) >= 4 and row[0].isdigit() and row[1].isdigit() and math.isfinite(float(row[3])) for row in rows[1:]):
            raise SourceConnectorError("no_observations")
        stamp = re.search(r"^#\s*File Creation:\s*(.+)$", text, re.M)
        return (stamp.group(1).strip()[:100] if stamp else None), len(rows) - 1

    async def _cern_search(self, query, page):
        from app.infrastructure.external.data_sources.open_data_sources import _candidate
        payload = await self._json(CERN + "/api/records/", {"q": query, "type": "Dataset", "size": PAGE_SIZE, "page": page})
        rows, total = payload["hits"]["hits"], payload["hits"]["total"]
        total = total.get("value") if isinstance(total, dict) else total
        if not isinstance(rows, list) or len(rows) > PAGE_SIZE or type(total) is not int or total < 0:
            raise SourceConnectorError("invalid_response")
        results = []
        for row in rows:
            data = row["metadata"]
            external = str(row["id"])
            if not re.fullmatch(CERN_ID, external) or data.get("type", {}).get("primary") != "Dataset" or not self._cern_files(data, external):
                continue
            results.append(_candidate("cern-open-data", external, data["title"], data.get("abstract", {}).get("description"), CERN + "/record/" + external,
                                      metadata={"version": row.get("updated")}))
        return {"results": results, "total_count": total}

    async def _cern_resolve(self, candidate):
        external = candidate["external_id"]
        if not isinstance(external, str) or not re.fullmatch(CERN_ID, external):
            raise SourceConnectorError("invalid_candidate")
        row = await self._json(CERN + "/api/records/" + external)
        data = row["metadata"]
        if str(row["id"]) != external or str(data.get("recid")) != external:
            raise SourceConnectorError("invalid_candidate")
        if candidate.get("metadata", {}).get("version") not in (None, row.get("updated")):
            raise SourceConnectorError("inconsistent_source_snapshot")
        if data.get("type", {}).get("primary") != "Dataset":
            return []
        return self._cern_files(data, external)

    @staticmethod
    def _cern_files(data, external):
        from app.infrastructure.external.data_sources.open_data_sources import _text, _size
        rows = data.get("_files", [])
        if not isinstance(rows, list):
            raise SourceConnectorError("invalid_response")
        results = []
        for item in rows:
            if item.get("availability") != "online":
                continue
            name, uri = item.get("key"), item.get("uri")
            if not isinstance(name, str) or not DATA_FILE.search(name) or re.search(r"readme|metadata|indexfile|software|tutorial|codebook", name, re.I):
                continue
            parsed = urlsplit(uri or "")
            if parsed.scheme != "root" or parsed.netloc != "eospublic.cern.ch" or parsed.query or parsed.fragment or not parsed.path.startswith("//eos/opendata/"):
                continue
            url = CERN + parsed.path[1:]
            if not safe_file_url("cern-open-data", url) or urlsplit(url).path.rsplit("/", 1)[-1] != name:
                continue
            checksum = item.get("checksum")
            if checksum is not None and not re.fullmatch(r"(?:adler32:[a-f0-9]{8}|md5:[a-f0-9]{32}|sha256:[a-f0-9]{64})", checksum):
                raise SourceConnectorError("invalid_response")
            version = item.get("version_id")
            if not isinstance(version, str) or not re.fullmatch(UUID, version):
                raise SourceConnectorError("invalid_response")
            license = data.get("license", {})
            license = _text(license.get("attribution") or license.get("name") or license.get("url"), 500) if isinstance(license, dict) else _text(license, 500)
            size = _size(item.get("size"))
            if size == 0:
                continue
            results.append(_option("cern-open-data", external, name, url, CERN + "/record/" + external,
                                   size=size, checksum=checksum, version=version, license=license or None))
        return results
