"""Bounded public-data adapters, independent of chat and sandbox lifecycle.

API contracts: https://developers.zenodo.org/#records
https://support.datacite.org/docs/api-queries
https://support.datacite.org/docs/pagination
https://datahelpdesk.worldbank.org/knowledgebase/articles/898599
https://datahelpdesk.worldbank.org/knowledgebase/articles/898581

Download URLs are obtained again from the registered source, never from a
caller-provided URL. Returned streams belong to the caller and must be closed.
"""
from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import html
import io
import json
from json import loads as json_loads
import math
import mimetypes
import re
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from functools import lru_cache
from urllib.parse import quote, urljoin, urlsplit

import httpx

from app.domain.external.data_source import SourceConnectorError
from app.domain.models.open_data_registry import CATALOG, doi_repository, source_definitions
from app.infrastructure.external.data_sources.open_data_query_syntax import all_terms_query
from app.infrastructure.external.data_sources.open_data_transfer import Transfer, checksum_hasher, cleanup_transfer, stream_download, storage_error
from app.infrastructure.external.data_sources.open_data_research_batch_adapters import (
    ResearchBatchAdapters, SOURCES as RESEARCH_SOURCES, safe_file_url as safe_research_file_url,
)
from app.infrastructure.external.data_sources.open_data_eastasia_batch_adapters import (
    EastAsiaBatchAdapters, SOURCES as EASTASIA_SOURCES, safe_file_url as safe_eastasia_file_url,
)
from app.infrastructure.external.data_sources.open_data_scientific_batch_adapters import (
    ScientificBatchAdapters, SOURCES as SCIENTIFIC_SOURCES, safe_file_url as safe_scientific_file_url,
)
from app.infrastructure.external.data_sources.open_data_government_batch_adapters import (
    GovernmentBatchAdapters, SOURCES as GOVERNMENT_SOURCES, safe_file_url as safe_government_file_url,
)

MAX_METADATA_BYTES = 8 * 1024 * 1024
PAGE_SIZE = 10
MAX_SEARCH_PAGES = 100
VERIFY_TIMEOUT_SECONDS = 45
VERIFY_FILE_LIMIT = 6
VERIFY_FILE_CONCURRENCY = 2
VERIFY_SAMPLE_BYTES = 16 * 1024


def _identity(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=True, sort_keys=True).encode()).hexdigest()


def _text(value, limit=6000) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]*>", " ", str(value or "")))).strip()[:limit]


def _size(value) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SourceConnectorError("invalid_response")
    return value


def _filename(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]", "_", value).strip("._") or "data.bin"
    return name[:60] + "_" + name[-120:] if len(name) > 190 else name


def _cursor(source, query, page) -> str:
    return base64.urlsafe_b64encode(json.dumps([source, _identity(query), page]).encode()).decode().rstrip("=")


def _page(source, query, cursor):
    if not cursor:
        return 1
    try:
        if not isinstance(cursor, str) or len(cursor) > 500:
            raise ValueError()
        saved_source, query_hash, page = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if saved_source != source or query_hash != _identity(query) or type(page) is not int or not 1 <= page <= MAX_SEARCH_PAGES:
            raise ValueError()
        return page
    except (ValueError, TypeError, UnicodeError):
        raise SourceConnectorError("invalid_cursor") from None


def _candidate(source, external, title, description, source_url, **fields):
    return {"id": _identity(source, external), "source_id": source, "external_id": external,
            "title": _text(title, 400), "description": _text(description), "source_url": source_url,
            "access": "needs_resolution", "formats": [], "size": None, "license": None,
            "coverage": {}, "match_notes": [], "limitations": [], "system_notes": [], "metadata": {}, **fields}


def _doi(value) -> str:
    value = str(value or "").lower()
    if not re.fullmatch(r'10\.\d{4,9}/[^\s<>"?#]{1,180}', value):
        raise SourceConnectorError("invalid_response")
    return value


def _zenodo_id(doi: str) -> str | None:
    match = re.fullmatch(r"10\.5281/zenodo\.([0-9]+)", doi, re.I)
    return match.group(1) if match else None


class OpenDataSources:
    def __init__(self, *, transport=None):
        self.transport = transport
        self._wb_catalog = None
        self._wb_catalog_at = 0.0
        self._wb_lock = asyncio.Lock()
        from app.infrastructure.external.data_sources.open_data_repository_adapters import RepositoryAdapters
        from app.infrastructure.external.data_sources.open_data_additional_adapters import AdditionalAdapters
        from app.infrastructure.external.data_sources.open_data_catalog_adapters import CatalogAdapters, SOURCES as catalog_sources
        from app.infrastructure.external.data_sources.open_data_domain_adapters import DomainAdapters, SOURCES as domain_sources
        repositories, additional = RepositoryAdapters(self._json), AdditionalAdapters(self._json)
        catalog = CatalogAdapters(self._json)
        domains = DomainAdapters(self._json)
        research = ResearchBatchAdapters(self._json)
        eastasia, scientific = EastAsiaBatchAdapters(self._json), ScientificBatchAdapters(self._json)
        government = GovernmentBatchAdapters(self._json)
        from app.infrastructure.external.data_sources.open_data_ml_expansion_adapters import MLExpansionAdapters, SOURCES as ml_sources
        ml = MLExpansionAdapters(self._json)
        from app.infrastructure.external.data_sources.open_data_earth_expansion_adapters import EarthExpansionAdapters, SOURCES as earth_sources
        from app.infrastructure.external.data_sources.open_data_statistics_expansion_adapters import StatisticsExpansionAdapters, SOURCES as statistics_sources
        earth = EarthExpansionAdapters(self._json, self._text_metadata)
        statistics = StatisticsExpansionAdapters(self._json, self._text_metadata)
        from app.infrastructure.external.data_sources.open_data_regional_expansion_adapters import RegionalExpansionAdapters, SOURCES as regional_sources
        regional = RegionalExpansionAdapters(self._json, self._text_metadata)
        from app.infrastructure.external.data_sources.open_data_research_oct22_adapters import ResearchOct22Adapters, SOURCES as research_oct22_sources
        from app.infrastructure.external.data_sources.open_data_international_oct22_adapters import InternationalOct22Adapters, SOURCES as international_oct22_sources
        from app.infrastructure.external.data_sources.open_data_france_oct22_adapters import FranceOct22Adapters, SOURCES as france_oct22_sources
        research_oct22 = ResearchOct22Adapters(self._json, self._text_metadata)
        international_oct22 = InternationalOct22Adapters(self._json, self._text_metadata)
        france_oct22 = FranceOct22Adapters(self._json)
        from app.infrastructure.external.data_sources.open_data_domestic_oct22_adapters import DomesticOct22Adapters, SOURCES as domestic_oct22_sources
        domestic_oct22 = DomesticOct22Adapters(self._json)
        from app.infrastructure.external.data_sources.open_data_statistics_oct23_adapters import StatisticsOct23Adapters, SOURCES as statistics_oct23_sources
        statistics_oct23 = StatisticsOct23Adapters(self._json, self._text_metadata)
        from app.infrastructure.external.data_sources.open_data_repositories_oct23_adapters import RepositoriesOct23Adapters, SOURCES as repositories_oct23_sources
        repositories_oct23 = RepositoriesOct23Adapters(self._json)
        from app.infrastructure.external.data_sources.open_data_earth_oct23_adapters import EarthOct23Adapters, SOURCES as earth_oct23_sources
        earth_oct23 = EarthOct23Adapters(self._json, self._text_metadata)
        from app.infrastructure.external.data_sources.open_data_math_foundation_adapters import MathFoundationAdapters, SOURCES as math_foundation_sources
        math_foundation = MathFoundationAdapters(self._json, self._text_metadata)
        from app.infrastructure.external.data_sources.open_data_physics_space_foundation_adapters import PhysicsSpaceFoundationAdapters, SOURCES as physics_space_foundation_sources
        physics_space_foundation = PhysicsSpaceFoundationAdapters(self._json, self._text_metadata)
        from app.infrastructure.external.data_sources.open_data_chem_materials_foundation_adapters import ChemMaterialsFoundationAdapters, SOURCES as chem_materials_foundation_sources
        chem_materials_foundation = ChemMaterialsFoundationAdapters(self._json, self._text_metadata)
        from app.infrastructure.external.data_sources.open_data_astronomy_foundation_adapters import AstronomyFoundationAdapters, SOURCES as astronomy_foundation_sources
        astronomy_foundation = AstronomyFoundationAdapters(self._json, self._text_metadata)
        self._adapters = {"figshare": repositories, "harvard-dataverse": repositories,
                          "dryad": additional, "openml": additional,
                          **dict.fromkeys(catalog_sources, catalog), **dict.fromkeys(domain_sources, domains),
                          **dict.fromkeys(RESEARCH_SOURCES, research), **dict.fromkeys(EASTASIA_SOURCES, eastasia),
                          **dict.fromkeys(SCIENTIFIC_SOURCES, scientific), **dict.fromkeys(GOVERNMENT_SOURCES, government),
                          **dict.fromkeys(ml_sources, ml), **dict.fromkeys(earth_sources, earth),
                          **dict.fromkeys(statistics_sources, statistics), **dict.fromkeys(regional_sources, regional),
                          **dict.fromkeys(research_oct22_sources, research_oct22),
                          **dict.fromkeys(international_oct22_sources, international_oct22),
                          **dict.fromkeys(france_oct22_sources, france_oct22),
                          **dict.fromkeys(domestic_oct22_sources, domestic_oct22),
                          **dict.fromkeys(statistics_oct23_sources, statistics_oct23),
                          **dict.fromkeys(math_foundation_sources, math_foundation),
                          **dict.fromkeys(physics_space_foundation_sources, physics_space_foundation),
                          **dict.fromkeys(chem_materials_foundation_sources, chem_materials_foundation),
                          **dict.fromkeys(astronomy_foundation_sources, astronomy_foundation),
                          **dict.fromkeys(repositories_oct23_sources, repositories_oct23),
                          **dict.fromkeys(earth_oct23_sources, earth_oct23)}

    def catalog(self):
        return [dict(source) for source in CATALOG]

    def _client(self):
        from app.infrastructure.external.data_sources.open_data_proxy import huggingface_proxy_mounts
        mounts = huggingface_proxy_mounts() if self.transport is None else {}
        return httpx.AsyncClient(transport=self.transport, mounts=mounts, timeout=httpx.Timeout(25, connect=10),
                                 follow_redirects=False, trust_env=False,
                                 headers={"User-Agent": "DataLab-public-data/1.0", "Accept-Encoding": "identity"})

    @staticmethod
    def _status(response):
        if response.status_code == 429:
            retry = response.headers.get("retry-after", "")
            raise SourceConnectorError("rate_limited", retry_after_seconds=min(int(retry), 3600) if retry.isdigit() else None)
        if response.status_code in (401, 403):
            raise SourceConnectorError("restricted_access")
        if response.status_code == 404:
            raise SourceConnectorError("query_not_found_or_invalid")
        if 300 <= response.status_code < 400:
            raise SourceConnectorError("unsafe_download_redirect")
        if response.status_code != 200:
            raise SourceConnectorError("upstream_unavailable")

    async def _metadata_bytes(self, url, params=None, *, accept, method="GET", json=None, max_bytes=None):
        limit = MAX_METADATA_BYTES if max_bytes is None else max_bytes
        if type(limit) is not int or not 0 < limit <= 32 * 1024 * 1024:
            raise SourceConnectorError("invalid_response")
        # This larger allowance is for one official station catalogue, not
        # acquired data files (which use the unlimited streaming transfer).
        if limit > MAX_METADATA_BYTES and (url != "https://www.ncei.noaa.gov/pub/data/ghcn/daily/ghcnd-stations.txt" or params):
            raise SourceConnectorError("invalid_response")
        try:
            async with asyncio.timeout(35):
                async with self._client() as client:
                    async with client.stream(method, url, params=params, json=json, headers={"Accept": accept}) as response:
                        self._status(response)
                        data = bytearray()
                        async for chunk in response.aiter_bytes(65536):
                            if len(data) + len(chunk) > limit:
                                raise SourceConnectorError("response_too_large")
                            data.extend(chunk)
            return bytes(data)
        except (httpx.HTTPError, TimeoutError):
            raise SourceConnectorError("upstream_unavailable") from None

    async def _json(self, url, params=None, *, method="GET", json=None):
        try:
            data = await self._metadata_bytes(url, params, accept="application/json", method=method, json=json)
            return json_loads(data)
        except (ValueError, UnicodeError):
            raise SourceConnectorError("invalid_response") from None

    async def _text_metadata(self, url, params=None, *, accept="text/plain", method="GET", json=None, max_bytes=None):
        """Read a bounded official CSV/XML catalog; dataset transfers use stream_download."""
        data = await self._metadata_bytes(url, params, accept=accept, method=method, json=json, max_bytes=max_bytes)
        try:
            return data.decode("utf-8-sig")
        except UnicodeError:
            raise SourceConnectorError("invalid_response") from None

    async def search(self, source_id, query, cursor=""):
        if source_id not in {entry["id"] for entry in CATALOG}:
            raise SourceConnectorError("unknown_source")
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise SourceConnectorError("invalid_query")
        query = query.strip()
        page = _page(source_id, query, cursor)
        try:
            if source_id in self._adapters:
                result = await self._adapters[source_id].search(source_id, query, page)
                results, total = result["results"], result["total_count"]
            elif source_id == "zenodo":
                data = await self._json("https://zenodo.org/api/records", {
                    "q": all_terms_query(query), "type": "dataset", "page": page, "size": PAGE_SIZE, "sort": "bestmatch"})
                hits = data["hits"]["hits"]
                total = data["hits"]["total"]
                total = total["value"] if isinstance(total, dict) else total
                results = [self._zenodo_candidate(item) for item in hits]
            elif source_id == "datacite":
                data = await self._json("https://api.datacite.org/dois", {
                    "query": query, "resource-type-id": "dataset", "page[size]": PAGE_SIZE,
                    "prefix": ",".join(sorted({prefix.split("/", 1)[0] for source in source_definitions()
                                             for prefix in source.doi_prefixes})),
                    "page[number]": page, "sort": "relevance"})
                results = [self._datacite_candidate(item) for item in data["data"]]
                total = data["meta"]["total"]
            elif source_id == "worldbank":
                return await self._search_worldbank(query, page)
            else:
                from app.infrastructure.external.data_sources.entrez import EntrezTopicConnector, TopicSearch
                database = "gds" if source_id == "ncbi-geo" else "sra"
                result = await EntrezTopicConnector(transport=self.transport).search(database,
                    TopicSearch(query=query, page_size=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE))
                results = [_candidate(source_id, item.external_id, item.title, item.metadata.get("summary", item.metadata.get("experiment_title", "")),
                    item.source_url, coverage={key: str(item.metadata[key]) for key in ("organism", "sample_count", "library_strategy") if item.metadata.get(key) is not None},
                    limitations=["GEO 补充文件可能是处理后数据；SRA 按实验返回，文件来自 ENA 镜像。", "双端测序需保留配对文件。"],
                    metadata=item.metadata) for item in result.resources]
                total = result.total_count
            if not isinstance(results, list) or len(results) > PAGE_SIZE or type(total) is not int or total < 0:
                raise ValueError()
            return {"results": results, "total_count": total,
                    # An adapter may filter every row on one provider page
                    # (restricted files, documents, unsupported publishers).
                    # The provider total still determines whether more pages exist.
                    "next_cursor": _cursor(source_id, query, page + 1) if page * PAGE_SIZE < total and page < MAX_SEARCH_PAGES else ""}
        except (KeyError, TypeError, ValueError, AttributeError):
            raise SourceConnectorError("invalid_response") from None
        except TimeoutError:
            raise SourceConnectorError("upstream_unavailable") from None

    @staticmethod
    def _zenodo_files(record):
        files = record.get("files", [])
        if isinstance(files, dict):
            files = list(files.get("entries", {}).values())
        if not isinstance(files, list) or len(files) > 10000:
            raise SourceConnectorError("invalid_response")
        return files

    def _zenodo_candidate(self, record):
        external = str(record["id"])
        if not re.fullmatch(r"[0-9]{1,20}", external):
            raise ValueError()
        meta = record["metadata"]
        files = self._zenodo_files(record)
        access = meta.get("access_right") or (record.get("access") or {}).get("files")
        sizes = [_size(item.get("size")) for item in files]
        license_value = meta.get("license") or {}
        if not license_value and meta.get("rights"):
            license_value = meta["rights"][0]
        license_text = _text(license_value.get("id") or license_value.get("title") or license_value.get("rights"), 200) if isinstance(license_value, dict) else _text(license_value, 200)
        return _candidate("zenodo", external, meta["title"], meta.get("description"), f"https://zenodo.org/records/{external}",
            access="restricted" if access in {"restricted", "closed", "embargoed"} else "needs_resolution",
            formats=list(dict.fromkeys(str(item.get("key", "")).rsplit(".", 1)[-1].lower()[:20] for item in files if "." in str(item.get("key", ""))))[:20],
            size=sum(sizes) if sizes and all(value is not None for value in sizes) else None,
            license=license_text or None, system_notes=["候选记录不代表文件已获取；完整获取保留源文件格式。", "文件解析最多返回记录中的前 100 个文件。"],
            metadata={"doi": meta.get("doi") or record.get("doi"), "published": meta.get("publication_date"), "version": meta.get("version")})

    @staticmethod
    def _datacite_candidate(item):
        attributes = item["attributes"]
        doi = _doi(attributes.get("doi", item.get("id")))
        descriptions = attributes.get("descriptions") or []
        rights = attributes.get("rightsList") or []
        return _candidate("datacite", doi, attributes["titles"][0]["title"],
            " ".join(_text(entry.get("description"), 3000) for entry in descriptions[:3]), "https://doi.org/" + quote(doi, safe="/"),
            access="needs_resolution" if doi_repository(doi) else "lead", formats=[_text(x, 80) for x in (attributes.get("formats") or [])[:20]],
            license=_text(rights[0].get("rightsIdentifier") or rights[0].get("rights"), 200) or None if rights else None,
            system_notes=["DataCite 提供 DOI 索引；仅已接入仓库中核验可获取的实体数据文件会展示。"],
            metadata={"doi": doi, "publisher": _text(attributes.get("publisher"), 200), "publication_year": attributes.get("publicationYear")})

    @staticmethod
    def _wb_page(payload, expected_page):
        if not isinstance(payload, list) or len(payload) != 2 or not isinstance(payload[0], dict):
            raise SourceConnectorError("invalid_response")
        meta, rows = payload
        try:
            page, pages, total = int(meta["page"]), int(meta["pages"]), int(meta["total"])
            per_page = int(meta["per_page"])
            if page != expected_page or pages < 0 or total < 0 or per_page <= 0 or (total and pages < page):
                raise ValueError()
            if rows is None and total == 0:
                rows = []
            if not isinstance(rows, list) or len(rows) != min(per_page, max(0, total - (page - 1) * per_page)):
                raise ValueError()
            if pages != ((total + per_page - 1) // per_page) and not (total == 0 and pages == 1):
                raise ValueError()
            return meta, rows, pages, total
        except (KeyError, TypeError, ValueError):
            raise SourceConnectorError("invalid_response") from None

    async def _worldbank_catalog(self):
        async with self._wb_lock:
            if self._wb_catalog is not None and time.monotonic() - self._wb_catalog_at < 3600:
                return self._wb_catalog
            rows = []
            expected_total = None
            async with asyncio.timeout(90):
                for page in range(1, 11):
                    payload = await self._json("https://api.worldbank.org/v2/indicator", {
                        "format": "json", "source": "2", "per_page": 2000, "page": page})
                    _, items, pages, total = self._wb_page(payload, page)
                    if pages > 10 or total > 20000:
                        raise SourceConnectorError("response_too_large")
                    if expected_total is not None and total != expected_total:
                        raise SourceConnectorError("inconsistent_source_snapshot")
                    expected_total = total
                    rows.extend(items)
                    if page >= pages:
                        break
            if len({item["id"] for item in rows}) != len(rows):
                raise SourceConnectorError("invalid_response")
            self._wb_catalog, self._wb_catalog_at = rows, time.monotonic()
            return rows

    async def _search_worldbank(self, query, page):
        if re.search(r"[^\x00-\x7f]", query) or not re.search(r"[A-Za-z]", query):
            raise SourceConnectorError("english_query_required")
        words = re.findall(r"[a-z0-9_.]+", query.lower())
        words = [word for word in words if word not in {"data", "dataset", "indicator", "indicators", "the", "and", "of", "for"}]
        if not words:
            raise SourceConnectorError("invalid_query")
        rows = await self._worldbank_catalog()
        matched = []
        for row in rows:
            code, name = row["id"], row["name"]
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", code) or str(row.get("source", {}).get("id")) != "2":
                raise SourceConnectorError("invalid_response")
            haystack = (code + " " + name + " " + str(row.get("sourceNote", ""))).lower()
            if any(word in haystack for word in words):
                matched.append(row)
        matched.sort(key=lambda row: (-sum(word in row["name"].lower() for word in words), row["id"]))
        results = [_candidate("worldbank", row["id"], row["name"], row.get("sourceNote"),
            "https://data.worldbank.org/indicator/" + quote(row["id"], safe=""), access="needs_resolution", formats=["csv"],
            coverage={"geography": "全部经济体及源站汇总地区", "time": "该指标全部源站可用年份", "granularity": "国家/地区 × 年；覆盖在获取后确认"},
            match_notes=["按英文关键词分别匹配指标，可选择多个指标组合分析；未据此过滤国家或年份。"],
            limitations=["范围仅 World Development Indicators（source=2），并非世界银行全部数据库。"],
            system_notes=["获取后由官方 API 观测值生成 CSV 快照，保留缺失值；不是源站原始 CSV 文件。"],
            metadata={"source": "2", "unit": _text(row.get("unit"), 200), "source_organization": _text(row.get("sourceOrganization"), 1000)})
            for row in matched[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]]
        return {"results": results, "total_count": len(matched),
                "next_cursor": _cursor("worldbank", query, page + 1) if page * PAGE_SIZE < len(matched) and page < MAX_SEARCH_PAGES else ""}

    async def _zenodo_record(self, external):
        if not isinstance(external, str) or not re.fullmatch(r"[0-9]{1,20}", external):
            raise SourceConnectorError("invalid_candidate")
        record = await self._json(f"https://zenodo.org/api/records/{external}")
        if not isinstance(record, dict) or str(record.get("id")) != external:
            raise SourceConnectorError("invalid_response")
        return record

    @staticmethod
    def _safe_file_url(url, source, external):
        try:
            parsed = urlsplit(url)
            if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
                    or parsed.port not in (None, 443) or parsed.fragment or "\\" in url
                    or any(ord(char) < 33 for char in url) or re.search(r"%2f|%5c|%2e", parsed.path, re.I)
                    or any(part in (".", "..") for part in parsed.path.split("/"))):
                raise ValueError()
            if source == "zenodo":
                valid = parsed.hostname == "zenodo.org" and parsed.query in ("", "download=1") and bool(re.fullmatch(
                    rf"/(?:api/records/{external}/files/[^/]+/content|records?/{external}/files/[^/]+|api/files/[a-fA-F0-9-]{{36}}/[^/]+)", parsed.path))
            elif source == "ncbi-geo":
                valid = parsed.hostname == "ftp.ncbi.nlm.nih.gov" and not parsed.query and parsed.path.startswith("/geo/") and bool(re.fullmatch(r"[A-Za-z0-9/_.-]+", parsed.path))
            elif source == "ncbi-sra":
                valid = parsed.hostname == "ftp.sra.ebi.ac.uk" and not parsed.query and bool(re.fullmatch(r"/vol1/fastq/[A-Za-z0-9/_-]+\.fastq\.gz", parsed.path))
            elif source in {"figshare", "harvard-dataverse"}:
                from app.infrastructure.external.data_sources.open_data_repository_adapters import safe_file_url
                valid = safe_file_url(source, url)
            elif source in {"dryad", "openml"}:
                from app.infrastructure.external.data_sources.open_data_additional_adapters import safe_file_url
                valid = safe_file_url(source, url)
            elif source in {"borealis", "dataverse-nl", "recherche-data-gouv", "pangaea"}:
                from app.infrastructure.external.data_sources.open_data_catalog_adapters import safe_file_url
                valid = safe_file_url(source, url)
            elif source in {"sciencedb", "gbif"}:
                from app.infrastructure.external.data_sources.open_data_domain_adapters import safe_file_url
                valid = safe_file_url(source, url)
            elif source in RESEARCH_SOURCES:
                valid = safe_research_file_url(source, url)
            elif source in EASTASIA_SOURCES:
                valid = safe_eastasia_file_url(source, url)
            elif source in SCIENTIFIC_SOURCES:
                valid = safe_scientific_file_url(source, url)
            elif source in GOVERNMENT_SOURCES:
                valid = safe_government_file_url(source, url)
            elif source in {'uci-ml', 'huggingface-datasets'}:
                from app.infrastructure.external.data_sources.open_data_ml_expansion_adapters import safe_file_url
                valid = safe_file_url(source, url, external)
            elif source in {'usgs-earthquake', 'nasa-power', 'noaa-gml', 'cern-open-data'}:
                from app.infrastructure.external.data_sources.open_data_earth_expansion_adapters import safe_file_url
                valid = safe_file_url(source, url)
            elif source in {'eurostat', 'ecb', 'ilostat', 'unesco-uis'}:
                from app.infrastructure.external.data_sources.open_data_statistics_expansion_adapters import safe_file_url
                valid = safe_file_url(source, url)
            elif source in {'macau-open-data', 'un-sdg'}:
                from app.infrastructure.external.data_sources.open_data_regional_expansion_adapters import safe_file_url
                valid = safe_file_url(source, url)
            elif source in {'nasa-osdr', 'ebi-biostudies', 'rodare'}:
                from app.infrastructure.external.data_sources.open_data_research_oct22_adapters import safe_file_url
                valid = safe_file_url(source, url, external)
            elif source in {'faostat', 'bis'}:
                from app.infrastructure.external.data_sources.open_data_international_oct22_adapters import safe_file_url
                valid = safe_file_url(source, url)
            elif source == 'france-data-gouv':
                from app.infrastructure.external.data_sources.open_data_france_oct22_adapters import safe_file_url
                valid = safe_file_url(source, url)
            elif source in {'malaysia-data-gov', 'norway-ssb', 'who-gho'}:
                from app.infrastructure.external.data_sources.open_data_statistics_oct23_adapters import safe_file_url
                valid = safe_file_url(source, url, external)
            elif source in {'asu-research-data', 'juelich-data', 'repod', 'deic-dataverse', 'bonndata', 'tudo-data', 'muenster-datastore'}:
                from app.infrastructure.external.data_sources.open_data_repositories_oct23_adapters import safe_file_url
                valid = safe_file_url(source, url, external)
            elif source in {'heasarc', 'earthscope', 'natural-earth', 'worldclim', 'noaa-ghcn-daily'}:
                from app.infrastructure.external.data_sources.open_data_earth_oct23_adapters import safe_file_url
                valid = safe_file_url(source, url, external)
            elif source in {'cenc-earthquake', 'nmc-weather'}:
                from app.infrastructure.external.data_sources.open_data_domestic_oct22_adapters import safe_file_url
                valid = safe_file_url(source, url)
            elif source in {'oeis', 'miplib', 'lmfdb'}:
                from app.infrastructure.external.data_sources.open_data_math_foundation_adapters import safe_file_url
                valid = safe_file_url(source, url, external)
            elif source in {'gwosc', 'nasa-cdaweb'}:
                from app.infrastructure.external.data_sources.open_data_physics_space_foundation_adapters import safe_file_url
                valid = safe_file_url(source, url, external)
            elif source in {'crystallography-open-database', 'nist-thermoml', 'nomad'}:
                from app.infrastructure.external.data_sources.open_data_chem_materials_foundation_adapters import safe_file_url
                valid = safe_file_url(source, url, external)
            elif source in {'cds-vizier', 'mast'}:
                from app.infrastructure.external.data_sources.open_data_astronomy_foundation_adapters import safe_file_url
                valid = safe_file_url(source, url, external)
            else:
                valid = False
            if not valid:
                raise ValueError()
            return url
        except (ValueError, TypeError, AttributeError):
            raise SourceConnectorError("unsafe_download_url") from None

    @asynccontextmanager
    async def _file_response(self, client, option, *, headers):
        """Follow only registered provider file routes, validating every hop."""
        url = self._safe_file_url(option["url"], option["source_id"], option["external_id"])
        if option['source_id'] == 'nist-thermoml':
            # This origin advertises compressed Range offsets for decoded XML.
            # A 200 response makes the shared transfer safely restart, then
            # verify the complete file against NIST's authoritative MD5.
            headers = {key: value for key, value in headers.items() if key.lower() not in {'range', 'if-range'}}
        for hop in range(6):
            async with client.stream("GET", url, headers=headers) as response:
                if response.status_code not in {301, 302, 303, 307, 308}:
                    yield response
                    return
                if option["source_id"] not in self._adapters or hop == 5:
                    raise SourceConnectorError("unsafe_download_redirect")
                location = response.headers.get("location")
                if not location:
                    raise SourceConnectorError("unsafe_download_redirect")
                try:
                    url = self._safe_file_url(urljoin(url, location), option["source_id"], option["external_id"])
                except SourceConnectorError:
                    raise SourceConnectorError("unsafe_download_redirect") from None

    async def resolve(self, candidate):
        try:
            return await self._resolve(candidate)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise SourceConnectorError("invalid_response") from None
        except TimeoutError:
            raise SourceConnectorError("upstream_unavailable") from None

    async def verify(self, candidate):
        """Read source bytes before advertising any downloadable data.

        This is bounded evidence of present readability, not a full download or
        a promise of scientific relevance. Full acquisition still re-resolves
        the manifest and checks every byte, length and provider checksum.
        """
        return await self._verify_candidate(candidate, complete=False)

    async def verify_all(self, candidate):
        """Check every manifest member before the user selects files.

        Each read is still bounded. The entire collection has no file-count or
        elapsed-time cutoff, and only a fixed number of probes run at once.
        """
        return await self._verify_candidate(candidate, complete=True)

    async def _verify_candidate(self, candidate, *, complete):
        checked, eligible, selected = [], [], []
        try:
            async with asyncio.timeout(None if complete else VERIFY_TIMEOUT_SECONDS):
                options = await self.resolve(candidate)
                if candidate["source_id"] == "worldbank":
                    return await self._verify_worldbank(options)
                if candidate["source_id"] == "who-gho":
                    return await self._adapters["who-gho"].verify_who(options)
                eligible = [item for item in options if item.get("available") is True
                            and not self._document_option(item)]
                selected = eligible if complete else eligible[:VERIFY_FILE_LIMIT]
                semaphore = asyncio.Semaphore(VERIFY_FILE_CONCURRENCY)

                async def check(option):
                    try:
                        async with semaphore:
                            async with asyncio.timeout(VERIFY_TIMEOUT_SECONDS):
                                result = await self._probe_file(option)
                    except TimeoutError:
                        result = SourceConnectorError("upstream_unavailable")
                    except Exception as error:
                        result = error
                    checked.append(result)
                    return result

                # Fixed workers avoid allocating a task for every file in large
                # manifests. Preserve the source order and cancel/join all work
                # before returning when the caller disconnects.
                results = [None] * len(selected)
                pending = iter(enumerate(selected))

                async def worker():
                    for index, option in pending:
                        results[index] = await check(option)

                workers = [asyncio.create_task(worker())
                           for _ in range(min(VERIFY_FILE_CONCURRENCY, len(selected)))]
                try:
                    await asyncio.gather(*workers)
                finally:
                    for task in workers:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*workers, return_exceptions=True)
                # Preserve usable files even when a sibling probe is throttled.
                # Cancellation must still terminate the operation immediately.
                for result in results:
                    if isinstance(result, BaseException) and not isinstance(result, Exception):
                        raise result
                # Keep authoritative file order despite concurrent probes.
                return self._verified_subset(results, len(eligible), len(selected), complete=complete)
        except SourceConnectorError as error:
            if error.reason in {"restricted_access", "query_not_found_or_invalid", "no_observations",
                                "unsafe_download_url", "unsafe_download_redirect"}:
                return []
            raise
        except TimeoutError:
            # The overall budget may expire after some files passed. Its own
            # timeout is recoverable; external cancellation is never caught.
            if any(isinstance(result, dict) for result in checked):
                pending = len(selected) - len(checked)
                checked.extend(SourceConnectorError("upstream_unavailable") for _ in range(pending))
                return self._verified_subset(checked, len(eligible), len(selected))
            raise SourceConnectorError("upstream_unavailable") from None
        except httpx.HTTPError:
            raise SourceConnectorError("upstream_unavailable") from None
        except (KeyError, TypeError, ValueError, AttributeError):
            raise SourceConnectorError("invalid_response") from None

    @staticmethod
    def _verified_subset(checked, eligible_count, selected_count, *, complete=False):
        errors = [result for result in checked if isinstance(result, Exception)]
        verified = [result for result in checked if isinstance(result, dict)]
        # A transient failure is not evidence that no data exists.
        if not verified and errors:
            raise errors[0]
        note = (f"已检查本次来源清单中的 {selected_count} 个数据文件，确认 {len(verified)} 个当前可读取；"
                if complete else f"搜索阶段已读取 {len(verified)} 个文件的内容；每条记录先核验 {VERIFY_FILE_LIMIT} 个文件，")
        note += f"每文件最多读取 {VERIFY_SAMPLE_BYTES // 1024} KiB。"
        if eligible_count > selected_count:
            note += f"另有 {eligible_count - selected_count} 个可选文件未检查，不代表来源全量。"
        if errors:
            note += f"另有 {len(errors)} 个文件核验失败，未展示；这不代表这些文件没有数据，可稍后重试。"
        for result in verified:
            result["system_notes"] = [note, "读取样本仅确认当前可读；完整性、校验和及实际分析适用性仍需获取后确认。"]
            if complete:
                result["_verification_summary"] = {
                    "verification_pending_count": len(errors),
                    "unavailable_file_count": selected_count - len(verified) - len(errors),
                }
        return verified

    @staticmethod
    def _document_option(option):
        name = str(option.get("source_filename") or option.get("filename", "")).lower()
        # A negative list keeps unknown scientific formats, JSON and plain text
        # eligible. Explicit documentation/metadata attachments are not data.
        stem = name.rsplit("/", 1)[-1]
        # Repositories commonly sort documentation first as 00_README or
        # 01-license. The numeric prefix does not turn documentation into data.
        stem = re.sub(r"^(?:[0-9]{1,4}[._ -]+)+", "", stem)
        if re.fullmatch(r"(?:readme(?:[._-].*)?|licen[cs]e(?:[._-].*)?|copying(?:[._-].*)?|"
                        r"citation(?:[._-].*)?|changelog(?:[._-].*)?|authors(?:[._-].*)?|"
                        r"(?:metadata|datacite|codemeta|ro-crate-metadata|manifest|schema|description)\.(?:json|xml|txt|yaml|yml))", stem):
            return True
        # PDF scans, Word interviews and Markdown corpora can themselves be
        # cross-disciplinary data. Format alone is not a documentation signal.
        return False

    @staticmethod
    def _data_sample(data, mime, *, complete):
        stripped = data.lstrip(b"\xef\xbb\xbf \t\r\n\x00")
        if not stripped:
            return False
        lower = stripped[:2048].lower()
        if re.search(br"<(?:!doctype\s+html|html\b|head\b|body\b)", lower):
            return False
        if lower.startswith(b"<?xml") and b"<error" in lower:
            return False
        if mime in {"text/html", "application/xhtml+xml"}:
            return False
        if stripped.strip().lower() in {b"access denied", b"forbidden", b"not found", b"unauthorized"}:
            return False
        if complete and ("json" in mime or stripped[:1] in (b"{", b"[")):
            try:
                value = json.loads(stripped)
            except (ValueError, UnicodeError):
                return False
            if value is None or value == [] or value == {}:
                return False
            if isinstance(value, dict):
                keys = {str(key).lower() for key in value}
                error_keys = {"error", "errors", "message", "detail", "status", "code", "request_id", "timestamp"}
                metadata_keys = {"doi", "title", "titles", "description", "descriptions", "creators", "authors", "license",
                                 "licenses", "rights", "rightslist", "publisher", "publicationyear", "publication_date",
                                 "version", "url", "identifier", "identifiers", "@context", "@type", "schema", "schema_version"}
                if keys <= error_keys or keys <= metadata_keys:
                    return False
        if complete and mime in {"text/csv", "text/tab-separated-values"}:
            if len([line for line in stripped.splitlines() if line.strip()]) < 2:
                return False
        return True

    async def _probe_file(self, option):
        url = self._safe_file_url(option["url"], option["source_id"], option["external_id"])
        expected = _size(option.get("size"))
        if expected == 0:
            return None
        # CUHK's public file endpoint returns an inconsistent Content-Range.
        # Its trusted adapter opts into a normal GET; the existing bounded
        # 200-response reader still stops at the sample limit and closes it.
        # Never allow an arbitrary option to weaken another provider's probe.
        # These official servers misreport Range over encoded representations;
        # a normal GET still reads only the bounded prefix and then closes.
        without_range = option["source_id"] in {"cuhk-data", "nist-thermoml"} and option.get("probe_without_range") is True
        probe_headers = {} if without_range else {"Range": f"bytes=0-{VERIFY_SAMPLE_BYTES - 1}"}
        async with self._client() as client:
            async with self._file_response(client, {**option, "url": url}, headers=probe_headers) as response:
                if response.status_code in {401, 403, 404, 410, 416} or 300 <= response.status_code < 400:
                    return None
                if response.status_code != 206:
                    self._status(response)
                length = response.headers.get("content-length")
                if length is not None and not length.isdigit():
                    return None
                declared = int(length) if length is not None else None
                if response.headers.get("content-encoding", "identity").lower() not in ("", "identity"):
                    return None
                if response.status_code == 206:
                    match = re.fullmatch(r"bytes 0-([0-9]+)/([0-9]+|\*)", response.headers.get("content-range", ""))
                    if not match:
                        return None
                    end = int(match[1])
                    total = int(match[2]) if match[2] != "*" else expected
                    if not 0 <= end < VERIFY_SAMPLE_BYTES or total is not None and end >= total or declared is not None and declared != end + 1:
                        return None
                    if expected is not None and total is not None and expected != total:
                        return None
                    body_size = end + 1
                else:
                    if response.headers.get("content-range") or declared is not None and expected is not None and declared != expected:
                        return None
                    total = declared if declared is not None else expected
                    body_size = total
                if total == 0:
                    return None
                mime = response.headers.get("content-type", option.get("content_type", "application/octet-stream")).lower().split(";", 1)[0].strip()
                if self._document_option({**option, "content_type": mime}):
                    return None
                sample = bytearray()
                complete = True
                async for chunk in response.aiter_bytes(1024):
                    if body_size is not None and len(sample) + len(chunk) > body_size:
                        return None
                    sample.extend(chunk[:max(0, VERIFY_SAMPLE_BYTES - len(sample))])
                    # A 200 server may ignore Range. Stop reading/close the
                    # response after the prefix; never buffer the full file.
                    if response.status_code == 200 and (total is None or total > VERIFY_SAMPLE_BYTES) and len(sample) == VERIFY_SAMPLE_BYTES:
                        complete = False
                        break
                if not sample or body_size is not None and body_size <= VERIFY_SAMPLE_BYTES and len(sample) != body_size:
                    return None
                if complete and response.status_code == 200 and total is not None and len(sample) != total:
                    return None
                complete = complete and response.status_code == 200 or total is not None and len(sample) == total
                if not self._data_sample(bytes(sample), mime, complete=complete):
                    return None
                if not self._foundation_sample(option, bytes(sample), complete=complete):
                    return None
                if option['source_id'] == 'un-sdg':
                    from app.infrastructure.external.data_sources.open_data_regional_expansion_adapters import is_sdg_observation_sample
                    if not is_sdg_observation_sample(bytes(sample), option['external_id'], complete=complete):
                        return None
                if option['source_id'] in {'cenc-earthquake', 'nmc-weather'}:
                    from app.infrastructure.external.data_sources.open_data_domestic_oct22_adapters import is_domestic_observation_sample
                    if not is_domestic_observation_sample(option['source_id'], bytes(sample), option['external_id']):
                        return None
                if option['source_id'] in SCIENTIFIC_SOURCES:
                    from app.infrastructure.external.data_sources.open_data_scientific_batch_adapters import is_scientific_observation_sample
                    if not is_scientific_observation_sample(option['source_id'], bytes(sample), option['external_id'], complete=complete):
                        return None
                if option['source_id'] == 'earthscope':
                    from app.infrastructure.external.data_sources.open_data_earth_oct23_adapters import is_earthscope_sample
                    if not is_earthscope_sample(bytes(sample), option['external_id']):
                        return None
                if option['source_id'] == 'noaa-ghcn-daily':
                    from app.infrastructure.external.data_sources.open_data_earth_oct23_adapters import is_ghcn_sample
                    if not is_ghcn_sample(bytes(sample), option['external_id']):
                        return None
                checksum = option.get("checksum")
                if complete and checksum:
                    digest = checksum_hasher(checksum)
                    digest.update(sample)
                    if digest.hexdigest() != checksum.split(":", 1)[1]:
                        return None
                return {**option, "size": total, "content_type": mime, "available": True,
                        "reason": "已确认来源文件当前可读取。",
                        "verification": {"method": "http_content_probe", "sample_bytes": len(sample), "total_bytes": total,
                                         "checked_at": datetime.now(timezone.utc).isoformat()}}

    async def _verify_worldbank(self, options):
        if not options:
            return []
        option = options[0]
        # MRNEV is the provider's most-recent non-empty observations endpoint;
        # do not mistake an indicator definition or a header-only CSV for data.
        payload = await self._json(option["url"], {"format": "json", "source": "2", "mrnev": 1, "per_page": 1000, "page": 1})
        _, rows, pages, _ = self._wb_page(payload, 1)
        count = 0
        for row in rows:
            if (row["indicator"]["id"] != option["external_id"] or not re.fullmatch(r"\d{4}", row["date"])
                    or not isinstance(row["country"]["id"], str) or not row["country"]["id"]):
                raise SourceConnectorError("invalid_response")
            value = row.get("value")
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, (int, float)) or isinstance(value, float) and not math.isfinite(value):
                    raise SourceConnectorError("invalid_response")
                count += 1
        if not count:
            return []
        return [{**option, "available": True, "reason": "已确认来源存在真实观测值，可获取 CSV 快照。",
                 "system_notes": ["可读性核验只读取最近非空观测的第一页（最多 1000 条）；完整获取另行分页并保留缺失值。",
                                 "核验不代表所有国家、年份都有数据，也未将观测样本当作完整下载。"],
                 "verification": {"method": "api_observation_probe", "non_null_observations": count, "sample_pages": 1,
                                  "source_pages": pages, "checked_at": datetime.now(timezone.utc).isoformat()}}]

    async def _resolve(self, candidate):
        source, external = candidate["source_id"], candidate["external_id"]
        if source in self._adapters:
            options = await self._adapters[source].resolve(candidate)
            for option in options:
                self._safe_file_url(option["url"], option["source_id"], option["external_id"])
            return options
        if source == "datacite":
            doi = _doi(external)
            target = doi_repository(doi)
            if target and target[0] in self._adapters:
                # Index metadata cannot pin a provider version or override the
                # resolved origin. Re-read the registered repository manifest.
                return await self._resolve({**candidate, "source_id": target[0],
                                            "external_id": target[1], "metadata": {"doi": doi}})
            record_id = _zenodo_id(doi)
            if not record_id:
                return []
            record = await self._zenodo_record(record_id)
            metadata = record.get("metadata", {})
            record_dois = {str(record.get("doi", "")).lower(), str(metadata.get("doi", "")).lower(), str(record.get("conceptdoi", "")).lower()}
            if doi not in record_dois:
                raise SourceConnectorError("invalid_candidate")
            return self._zenodo_options(record, record_id)
        if source == "zenodo":
            return self._zenodo_options(await self._zenodo_record(external), external)
        if source == "worldbank":
            if not isinstance(external, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", external):
                raise SourceConnectorError("invalid_candidate")
            payload = await self._json(f"https://api.worldbank.org/v2/indicator/{external}", {"format": "json", "source": "2"})
            _, rows, _, _ = self._wb_page(payload, 1)
            if len(rows) != 1 or rows[0].get("id") != external or str(rows[0].get("source", {}).get("id")) != "2":
                raise SourceConnectorError("invalid_candidate")
            return [{"id": _identity("worldbank", external, "csv-snapshot-v1"), "filename": _filename(f"worldbank_{external}.csv"),
                     "size": None, "content_type": "text/csv", "available": True, "reason": "官方 API 观测值生成的 CSV 快照；覆盖全部可用经济体和年份。",
                     "kind": "API 观测值快照", "source_id": source, "external_id": external,
                     "url": f"https://api.worldbank.org/v2/country/all/indicator/{external}", "checksum": None}]
        if source in {"ncbi-geo", "ncbi-sra"}:
            from app.infrastructure.external.data_sources.study_download import StudyDownloadClient
            records = await StudyDownloadClient(transport=self.transport).list_files(
                source, external, ignore_legacy_size_limit=True, file_limit=None)
            return [{"id": row["id"], "filename": _filename(external + "_" + _identity(row["url"])[:8] + "_" + row["filename"]),
                     "size": row["size"], "content_type": mimetypes.guess_type(row["filename"])[0] or "application/octet-stream",
                     "available": row["available"], "reason": row["unavailable_reason"], "kind": row["kind"],
                     "url": self._safe_file_url(row["url"], source, external), "checksum": "md5:" + row["md5"] if row.get("md5") else None,
                     "source_id": source, "external_id": external, "source_filename": row["filename"]} for row in records]
        raise SourceConnectorError("unknown_source")

    def _zenodo_options(self, record, external):
        meta = record.get("metadata", {})
        restricted = (meta.get("access_right") or (record.get("access") or {}).get("files")) in {"restricted", "closed", "embargoed"}
        results = []
        for item in self._zenodo_files(record):
            key = item.get("key") or item.get("filename")
            if not isinstance(key, str) or not key:
                raise SourceConnectorError("invalid_response")
            url = (item.get("links") or {}).get("content") or (item.get("links") or {}).get("self")
            size = _size(item.get("size"))
            checksum = item.get("checksum")
            if checksum is not None and not re.fullmatch(r"(?:md5:[a-fA-F0-9]{32}|sha256:[a-fA-F0-9]{64})", checksum):
                raise SourceConnectorError("invalid_response")
            safe_url = self._safe_file_url(url, "zenodo", external)
            available = not restricted and (size is None or size > 0)
            reason = "来源文件受访问限制" if restricted else "文件为空" if size == 0 else ""
            results.append({"id": _identity("zenodo", external, key, safe_url, size, checksum),
                "filename": _filename(f"zenodo_{external}_{_identity(key)[:8]}_{key}"), "size": size,
                "content_type": item.get("mimetype") or mimetypes.guess_type(key)[0] or "application/octet-stream",
                "available": available, "reason": reason, "kind": "源站文件", "url": safe_url,
                "checksum": checksum.lower() if checksum else None, "source_id": "zenodo", "external_id": external,
                "source_filename": key})
        return results

    async def download(self, candidate, option, progress=None, *, transfer_key=None):
        # Re-resolve the authoritative file list. Ignore the caller's URL, size,
        # checksum and filename, and reject choices whose source changed.
        options = await self.resolve(candidate)
        chosen = next((entry for entry in options if entry["id"] == option.get("id")), None)
        if chosen is None:
            raise SourceConnectorError("invalid_file_selection")
        if not chosen["available"]:
            raise SourceConnectorError("restricted_access")
        if chosen["source_id"] == "worldbank":
            return await self._worldbank_csv(candidate, chosen, progress, transfer_key=transfer_key)
        if chosen["source_id"] == "who-gho":
            return await self._adapters["who-gho"].download_who(candidate, chosen, progress, self._receipt, transfer_key=transfer_key)
        return await self._stream_file(candidate, chosen, progress, transfer_key=transfer_key)

    async def cleanup_transfer(self, key):
        await cleanup_transfer(key)

    @staticmethod
    def _receipt(candidate, chosen, digest, verified=False):
        source_urls = {"zenodo": f"https://zenodo.org/records/{chosen['external_id']}",
                       "worldbank": "https://data.worldbank.org/indicator/" + quote(chosen["external_id"], safe=""),
                       "ncbi-geo": "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=" + chosen["external_id"],
                       "ncbi-sra": "https://www.ncbi.nlm.nih.gov/sra/" + chosen["external_id"]}
        return {"source": "data_connector", "connector_id": chosen["source_id"], "external_id": chosen["external_id"],
                "discovery_source_id": candidate["source_id"], "source_url": source_urls.get(chosen["source_id"], chosen.get("record_url", candidate.get("source_url", ""))),
                "sha256": digest, "checksum_origin": "computed_locally", "provider_checksum": chosen.get("checksum"),
                "license": chosen.get("license") or candidate.get("license"),
                "source_version": chosen.get("version") or (candidate.get("metadata") or {}).get("version"),
                "provider_checksum_verified": verified, "acquired_at": datetime.now(timezone.utc).isoformat()}

    @staticmethod
    def _foundation_sample(option, sample, *, complete):
        if option['source_id'] in {'oeis', 'miplib', 'lmfdb'}:
            from app.infrastructure.external.data_sources.open_data_math_foundation_adapters import is_math_sample
            return is_math_sample(option['source_id'], sample, option['external_id'], complete=complete)
        if option['source_id'] in {'gwosc', 'nasa-cdaweb'}:
            from app.infrastructure.external.data_sources.open_data_physics_space_foundation_adapters import is_physics_space_sample
            return is_physics_space_sample(option['source_id'], sample)
        if option['source_id'] in {'crystallography-open-database', 'nist-thermoml', 'nomad'}:
            from app.infrastructure.external.data_sources.open_data_chem_materials_foundation_adapters import is_chem_materials_sample
            return is_chem_materials_sample(option['source_id'], sample, option['external_id'], complete=complete)
        if option['source_id'] in {'cds-vizier', 'mast'}:
            from app.infrastructure.external.data_sources.open_data_astronomy_foundation_adapters import is_astronomy_sample
            return is_astronomy_sample(sample, option)
        return True

    async def _stream_file(self, candidate, chosen, progress, *, transfer_key=None):
        # The URL is revalidated even though resolve already constrained it.
        self._safe_file_url(chosen["url"], chosen["source_id"], chosen["external_id"])
        stream, digest = await stream_download(self._client, self._status, chosen, progress, transfer_key=transfer_key,
                                               response_opener=self._file_response)
        try:
            prefix = stream.read(VERIFY_SAMPLE_BYTES + 1)
            if not self._foundation_sample(chosen, prefix[:VERIFY_SAMPLE_BYTES], complete=len(prefix) <= VERIFY_SAMPLE_BYTES):
                raise SourceConnectorError('no_observations')
            stream.seek(0)
            if chosen['source_id'] in {'crystallography-open-database', 'nist-thermoml', 'nomad', 'miplib', 'cds-vizier', 'mast'}:
                if chosen['source_id'] == 'miplib':
                    from app.infrastructure.external.data_sources.open_data_math_foundation_adapters import validate_math_stream as validate_stream
                elif chosen['source_id'] in {'cds-vizier', 'mast'}:
                    from app.infrastructure.external.data_sources.open_data_astronomy_foundation_adapters import validate_astronomy_stream as validate_stream
                else:
                    from app.infrastructure.external.data_sources.open_data_chem_materials_foundation_adapters import validate_chem_materials_stream as validate_stream
                cancelled = threading.Event()
                validation = asyncio.create_task(asyncio.to_thread(
                    validate_stream, chosen['source_id'], stream, chosen, cancelled=cancelled))
                try:
                    await asyncio.shield(validation)
                except asyncio.CancelledError:
                    # Stop the bounded validator before the transfer stream is closed.
                    cancelled.set()
                    await asyncio.gather(validation, return_exceptions=True)
                    raise
        except BaseException:
            stream.close()
            raise
        if chosen['source_id'] in SCIENTIFIC_SOURCES:
            from app.infrastructure.external.data_sources.open_data_scientific_batch_adapters import is_scientific_observation_sample
            try:
                prefix = stream.read(VERIFY_SAMPLE_BYTES + 1)
                if not is_scientific_observation_sample(chosen['source_id'], prefix[:VERIFY_SAMPLE_BYTES], chosen['external_id'], complete=len(prefix) <= VERIFY_SAMPLE_BYTES):
                    raise SourceConnectorError('no_observations')
                stream.seek(0)
            except BaseException:
                stream.close()
                raise
        if chosen['source_id'] == 'earthscope':
            from app.infrastructure.external.data_sources.open_data_earth_oct23_adapters import is_earthscope_sample
            try:
                if not is_earthscope_sample(stream.read(VERIFY_SAMPLE_BYTES), chosen['external_id']):
                    raise SourceConnectorError('no_observations')
                stream.seek(0)
            except BaseException:
                stream.close()
                raise
        if chosen['source_id'] == 'noaa-ghcn-daily':
            from app.infrastructure.external.data_sources.open_data_earth_oct23_adapters import is_ghcn_sample
            try:
                if not is_ghcn_sample(stream.read(VERIFY_SAMPLE_BYTES), chosen['external_id']):
                    raise SourceConnectorError('no_observations')
                stream.seek(0)
            except BaseException:
                stream.close()
                raise
        if chosen['source_id'] == 'un-sdg':
            from app.infrastructure.external.data_sources.open_data_regional_expansion_adapters import is_sdg_observation_sample
            try:
                prefix = stream.read(VERIFY_SAMPLE_BYTES + 1)
                if not is_sdg_observation_sample(prefix[:VERIFY_SAMPLE_BYTES], chosen['external_id'], complete=len(prefix) <= VERIFY_SAMPLE_BYTES):
                    raise SourceConnectorError('no_observations')
                stream.seek(0)
            except BaseException:
                stream.close()
                raise
        if chosen['source_id'] in {'cenc-earthquake', 'nmc-weather'}:
            from app.infrastructure.external.data_sources.open_data_domestic_oct22_adapters import is_domestic_observation_sample
            try:
                prefix = stream.read(VERIFY_SAMPLE_BYTES)
                if not is_domestic_observation_sample(chosen['source_id'], prefix, chosen['external_id']):
                    raise SourceConnectorError('no_observations')
                stream.seek(0)
            except BaseException:
                stream.close()
                raise
        return stream, chosen["filename"], chosen["content_type"], self._receipt(candidate, chosen, digest, bool(chosen.get("checksum")))

    async def _worldbank_csv(self, candidate, chosen, progress, *, transfer_key=None):
        transfer = await Transfer(transfer_key).acquire()
        stream = transfer.file
        # Generated snapshots restart from page one after interruption: mixing
        # pages across provider revisions would misrepresent a complete CSV.
        transfer.reset()
        handed_off = False
        digest = hashlib.sha256()
        retrieved = datetime.now(timezone.utc).isoformat()
        fields = ["indicator_id", "indicator_name", "country_id", "country_iso3", "country_name", "year", "value", "unit", "decimal", "obs_status", "source_id", "source_last_updated", "retrieved_at"]
        count, non_null_count, total, pages, last_updated = 0, 0, None, 0, None
        seen = set()

        def write(values):
            buffer = io.StringIO(newline="")
            # Escape spreadsheet formulas in source text, retaining numeric values.
            safe = ["'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@")) else value for value in values]
            csv.writer(buffer).writerow(safe)
            data = buffer.getvalue().encode("utf-8")
            transfer.write(data)
            digest.update(data)

        try:
            write(fields)
            page = 1
            while True:
                payload = await self._json(chosen["url"], {"format": "json", "source": "2", "per_page": 1000, "page": page})
                meta, rows, source_pages, source_total = self._wb_page(payload, page)
                if total is not None and (total != source_total or source_pages != pages or meta.get("lastupdated") != last_updated):
                    raise SourceConnectorError("inconsistent_source_snapshot")
                total, pages, last_updated = source_total, source_pages, meta.get("lastupdated")
                for row in rows:
                    if row["indicator"]["id"] != chosen["external_id"] or not re.fullmatch(r"\d{4}", row["date"]):
                        raise SourceConnectorError("invalid_response")
                    key = (row["country"]["id"], row["date"])
                    if key in seen:
                        raise SourceConnectorError("inconsistent_source_snapshot")
                    seen.add(key)
                    value = row.get("value")
                    if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                              or isinstance(value, float) and not math.isfinite(value)):
                        raise SourceConnectorError("invalid_response")
                    if value is not None:
                        non_null_count += 1
                    write([row["indicator"]["id"], row["indicator"]["value"], row["country"]["id"], row.get("countryiso3code", ""), row["country"]["value"],
                           row["date"], value, row.get("unit", ""), row.get("decimal", ""), row.get("obs_status", ""), "2", last_updated or "", retrieved])
                    count += 1
                if progress:
                    await progress(stream.tell(), None)
                if page >= pages:
                    break
                page += 1
            if not count or not non_null_count:
                raise SourceConnectorError("no_observations")
            if count != total:
                raise SourceConnectorError("inconsistent_source_snapshot")
            metadata = self._receipt(candidate, chosen, digest.hexdigest())
            metadata.update(snapshot_rows=count, snapshot_page_count=pages, source_last_updated=last_updated,
                            acquisition_method="api_csv_snapshot", source_api_url=chosen["url"],
                            source_checksum_note="CSV 由官方 API 数据生成；SHA-256 为本地计算，源站未提供该快照的校验和。")
            transfer.save({"source": {"url": chosen["url"], "kind": "api_csv_snapshot"}, "complete": True,
                           "bytes_saved": stream.tell(), "prefix_sha256": digest.hexdigest()})
            handed_off = True
            return transfer.reader(), chosen["filename"], "text/csv", metadata
        except BaseException as error:
            if isinstance(error, (KeyError, TypeError, ValueError, AttributeError)):
                raise SourceConnectorError("invalid_response") from None
            if isinstance(error, TimeoutError):
                raise SourceConnectorError("download_timeout_or_unavailable") from None
            if isinstance(error, OSError):
                raise storage_error(error) from None
            raise
        finally:
            if not handed_off:
                transfer.close()


@lru_cache()
def get_open_data_sources() -> OpenDataSources:
    return OpenDataSources()
