"""NASA OSDR, BioStudies and RODARE original-file adapters.

Only provider-owned public records decide download URLs. File manifests are
traversed completely; unsupported/metadata files are filtered, never truncated.
The facade performs content probes and streams complete files with checksums.
"""
from __future__ import annotations

import math
import json
import mimetypes
import re
from urllib.parse import parse_qsl, quote, unquote, urlsplit

from app.domain.external.data_source import SourceConnectorError
from app.infrastructure.external.data_sources.open_data_query_syntax import all_terms_query

SOURCES = frozenset(("nasa-osdr", "ebi-biostudies", "rodare"))
PAGE_SIZE = 10
NASA = "https://osdr.nasa.gov"
BIO = "https://www.ebi.ac.uk/biostudies"
RODARE = "https://rodare.hzdr.de"
_BIO_ID = r"[SE]-[A-Z][A-Z0-9]{1,20}-?[0-9]{1,14}"
_UUID = r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}"
_DATA = re.compile(r"\.(?:csv|tsv|txt|dat|tab|xlsx?|ods|json|jsonl|parquet|arff|sav|dta|rds|rdata|h5|hdf5|h5ad|nc|netcdf|tiff?|czi|nd2|lif|fcs|imzml|mzml|mzxml|raw|fasta|fastq|fa|fq|bam|sam|vcf|bed|seg|cel|gpr|zip|tar|7z)(?:\.(?:gz|bz2|xz))?$", re.I)
_DOCUMENT = re.compile(r"(?:^|[\s/_.-])(?:readme|metadata|manifest|license|md5sum|sha256sum|checksum|codebook|dictionary|software_versions|sampletable|runsheet|processing_info)(?:[\s/_.-]|$)", re.I)


def _identifier(source, value):
    pattern = {"nasa-osdr": r"OSD-[1-9][0-9]{0,9}", "ebi-biostudies": _BIO_ID, "rodare": r"[1-9][0-9]{0,14}"}.get(source)
    if not pattern or not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise SourceConnectorError("invalid_candidate")
    return value


def _relative(value):
    return (isinstance(value, str) and 0 < len(value) < 2000 and not value.startswith("/")
            and not re.search(r"[\x00-\x1f\\%?#]", value)
            and all(part and part not in {".", ".."} for part in value.split("/")))


def safe_file_url(source, url, external=None):
    try:
        if source not in SOURCES or not isinstance(url, str) or len(url) > 8192 or re.search(r"[\x00-\x20\\]", url):
            return False
        p = urlsplit(url)
        if p.scheme != "https" or p.username or p.password or p.port not in (None, 443) or p.fragment:
            return False
        if re.search(r"%2f|%5c|%2e|%25", p.path, re.I):
            return False
        path = unquote(p.path)
        if not _relative(path.removeprefix("/")):
            return False
        if source == "nasa-osdr":
            pairs = parse_qsl(p.query, keep_blank_values=True, strict_parsing=True)
            params = dict(pairs)
            if p.hostname == "genelab-repo-prod.s3.amazonaws.com":
                match = re.fullmatch(r"/genelab-data/(?:GLDS|OSD)-([1-9][0-9]{0,9})/(?:[A-Za-z0-9_.-]+/)*[^/]+", path)
                allowed = {"versionId", "response-content-disposition", "AWSAccessKeyId", "Signature", "x-amz-security-token", "Expires"}
                return (bool(match) and (external is None or external == "OSD-" + match[1]) and bool(_DATA.search(path))
                        and len(pairs) == len(params) and set(params) <= allowed and {"AWSAccessKeyId", "Signature", "Expires"} <= set(params)
                        and all(value and len(value) < 4096 and not re.search(r"[\x00-\x1f]", value) for value in params.values())
                        and bool(re.fullmatch(r"[0-9]{1,15}", params["Expires"])))
            return (p.hostname == "osdr.nasa.gov" and bool(re.fullmatch(r"/geode-py/ws/studies/OSD-[1-9][0-9]{0,9}/download", path))
                    and (external is None or path == "/geode-py/ws/studies/" + external + "/download")
                    and len(pairs) == 2 and set(params) == {"source", "file"} and params["source"] == "datamanager"
                    and _relative(params["file"]) and "/" not in params["file"] and bool(_DATA.search(params["file"])))
        if p.query:
            return False
        if source == "ebi-biostudies":
            return (p.hostname == "ftp.ebi.ac.uk" and bool(re.fullmatch(r"/biostudies/(?:fire|nfs)/[SE]-[A-Z0-9-]+/[A-Za-z0-9-]+/" + _BIO_ID + r"/Files/.+", path))
                    and bool(_DATA.search(path)))
        return p.hostname == "rodare.hzdr.de" and bool(re.fullmatch(r"/api/files/" + _UUID + r"/[^/]+", path)) and bool(_DATA.search(path))
    except (TypeError, ValueError):
        return False


def _data_file(name):
    return _relative(name) and bool(_DATA.search(name)) and not _DOCUMENT.search(name)


def _total(value):
    if isinstance(value, dict):
        value = value.get("value")
    if type(value) is not int or value < 0:
        raise SourceConnectorError("invalid_response")
    return value


def _version(value):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool) or (isinstance(value, float) and not math.isfinite(value)):
        raise SourceConnectorError("invalid_response")
    return str(value)


def _attrs(attributes):
    if not isinstance(attributes, list):
        raise SourceConnectorError("invalid_response")
    return {str(row.get("name", "")).lower(): row.get("value") for row in attributes if isinstance(row, dict)}


def _option(source, external, name, url, *, size, version, license=None, checksum=None):
    from app.infrastructure.external.data_sources.open_data_sources import _filename, _identity, _size
    _size(size)
    if not safe_file_url(source, url):
        raise SourceConnectorError("unsafe_download_url")
    record = _record(source, external)
    return dict(id=_identity(source, external, name, url, version, checksum), source_id=source, external_id=external,
                filename=_filename(source + "_" + external + "_" + name.rsplit("/", 1)[-1]), source_filename=name,
                url=url, record_url=record, size=size, version=version, license=license, checksum=checksum,
                content_type=mimetypes.guess_type(name)[0] or "application/octet-stream", available=size != 0,
                reason="", kind="源站文件")


def _record(source, external):
    return {"nasa-osdr": NASA + "/bio/repo/data/studies/" + external,
            "ebi-biostudies": BIO + "/studies/" + external,
            "rodare": RODARE + "/record/" + external}[source]


class ResearchOct22Adapters:
    def __init__(self, fetch_json, fetch_text):
        self._json, self._text = fetch_json, fetch_text

    async def _rodare_json(self, url, params=None):
        # application/json selects RODARE's legacy export serializer, which
        # omits pagination and renames file fields. Select the native API.
        text = await self._text(url, params, accept="application/vnd.zenodo.v1+json")
        return json.loads(text)

    async def search(self, source, query, page):
        if source not in SOURCES:
            raise SourceConnectorError("unknown_source")
        if not isinstance(query, str) or not query.strip() or len(query) > 500 or type(page) is not int or not 1 <= page <= 100:
            raise SourceConnectorError("invalid_query")
        try:
            return await getattr(self, "_search_" + source.replace("-", "_"))(query, page)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise SourceConnectorError("invalid_response") from None

    async def resolve(self, candidate):
        source = candidate.get("source_id")
        if source not in SOURCES:
            raise SourceConnectorError("unknown_source")
        external = _identifier(source, candidate.get("external_id"))
        try:
            return await getattr(self, "_resolve_" + source.replace("-", "_"))(external, candidate.get("metadata") or {})
        except (KeyError, TypeError, ValueError, AttributeError):
            raise SourceConnectorError("invalid_response") from None

    async def _search_nasa_osdr(self, query, page):
        from app.infrastructure.external.data_sources.open_data_sources import _candidate
        payload = await self._json(NASA + "/osdr/data/search", {"term": query, "type": "cgene", "from": (page - 1) * PAGE_SIZE, "size": PAGE_SIZE})
        if payload.get("timed_out") is True or payload.get("_shards", {}).get("failed", 0):
            raise SourceConnectorError("upstream_unavailable")
        rows = payload["hits"]["hits"]
        if not isinstance(rows, list) or len(rows) > PAGE_SIZE:
            raise ValueError()
        results = []
        for item in rows:
            row = item["_source"]
            external = _identifier("nasa-osdr", row["Accession"])
            results.append(_candidate("nasa-osdr", external, row["Study Title"], row.get("Study Description"), _record("nasa-osdr", external)))
        return {"results": results, "total_count": _total(payload["hits"]["total"])}

    async def _resolve_nasa_osdr(self, external, metadata):
        payload = await self._json(NASA + "/osdr/data/osd/files/" + external[4:])
        if payload.get("success") is not True or payload.get("valid_input") != [external[4:]]:
            raise ValueError()
        study = payload["studies"].get(external)
        if not isinstance(study, dict):
            raise SourceConnectorError("invalid_candidate")
        rows = study["study_files"]
        if not isinstance(rows, list) or _total(study["file_count"]) != len(rows):
            raise SourceConnectorError("incomplete_file_manifest")
        result = []
        for row in rows:
            name = row.get("file_name")
            if (row.get("restricted") is not False or row.get("visible") is not True or not _data_file(name)
                    or "metadata" in str(row.get("category", "")).lower()
                    or row.get("subcategory") in {"Processing Info", "MD5 Checksums"}):
                continue
            remote = row.get("remote_url")
            if not isinstance(remote, str) or not remote.startswith("/"):
                continue
            url = NASA + remote
            if not safe_file_url("nasa-osdr", url):
                continue
            parsed = urlsplit(url)
            if parsed.path != "/geode-py/ws/studies/" + external + "/download" or dict(parse_qsl(parsed.query)).get("file") != name:
                continue
            result.append(_option("nasa-osdr", external, name, url, size=row.get("file_size"), version=_version(row["date_updated"])))
        return result

    async def _search_ebi_biostudies(self, query, page):
        from app.infrastructure.external.data_sources.open_data_sources import _candidate
        payload = await self._json(BIO + "/api/v1/search", {"query": all_terms_query(query), "page": page, "pageSize": PAGE_SIZE, "type": "study", "files": "[1 TO *]"})
        rows = payload["hits"]
        if not isinstance(rows, list) or len(rows) > PAGE_SIZE:
            raise ValueError()
        result = []
        for row in rows:
            if row.get("isPublic") is not True or row.get("files") == 0:
                continue
            external = _identifier("ebi-biostudies", row["accession"])
            result.append(_candidate("ebi-biostudies", external, row["title"], row.get("content"), _record("ebi-biostudies", external)))
        # Search totals describe matching source records, not verified data files.
        return {"results": result, "total_count": _total(payload["totalHits"])}

    async def _resolve_ebi_biostudies(self, external, metadata):
        info = await self._json(BIO + "/api/v1/studies/" + external + "/info")
        if info.get("isPublic") is not True:
            return []
        base = info.get("httpLink")
        if not isinstance(base, str):
            return []
        p = urlsplit(base)
        if (p.scheme != "https" or p.hostname != "ftp.ebi.ac.uk" or p.username or p.password or p.port not in (None, 443)
                or p.query or p.fragment or not re.fullmatch(r"/biostudies/(?:fire|nfs)/[SE]-[A-Z0-9-]+/[A-Za-z0-9-]+/" + re.escape(external), p.path)):
            return []
        record = await self._json(BIO + "/api/v1/studies/" + external)
        if record.get("accno") != external:
            raise SourceConnectorError("invalid_candidate")
        attrs = {**_attrs(record.get("attributes", [])), **_attrs(record.get("section", {}).get("attributes", []))}
        license = attrs.get("license")
        if license is not None and not isinstance(license, str):
            raise ValueError()
        version = _version(info["modified"])
        result, names = [], set()
        stack = [record["section"]]
        while stack:
            section = stack.pop()
            if isinstance(section, list):
                stack.extend(reversed(section))
                continue
            if not isinstance(section, dict):
                raise ValueError()
            children = section.get("subsections", [])
            files = section.get("files", [])
            if not isinstance(children, list) or not isinstance(files, list):
                raise ValueError()
            stack.extend(reversed(children))
            pending = list(reversed(files))
            while pending:
                file = pending.pop()
                if isinstance(file, list):
                    pending.extend(reversed(file))
                    continue
                if not isinstance(file, dict):
                    raise ValueError()
                name = file.get("path")
                if file.get("type") != "file" or not _data_file(name) or name in names:
                    continue
                names.add(name)
                url = base + "/Files/" + quote(name, safe="/")
                result.append(_option("ebi-biostudies", external, name, url, size=file.get("size"), version=version, license=license))
        return result

    @staticmethod
    def _rodare_public(record):
        meta = record.get("metadata", {})
        return meta.get("access_right") == "open" and meta.get("resource_type", {}).get("type") == "dataset"

    async def _search_rodare(self, query, page):
        from app.infrastructure.external.data_sources.open_data_sources import _candidate
        payload = await self._rodare_json(RODARE + "/api/records/", {"q": "(" + all_terms_query(query) + ") AND access_right:open AND resource_type.type:dataset", "size": PAGE_SIZE, "page": page})
        rows = payload["hits"]["hits"]
        if not isinstance(rows, list) or len(rows) > PAGE_SIZE:
            raise ValueError()
        results = []
        for row in rows:
            if not self._rodare_public(row):
                continue
            external = _identifier("rodare", str(row["id"]))
            results.append(_candidate("rodare", external, row["metadata"]["title"], row["metadata"].get("description"), _record("rodare", external),
                                      metadata={"version": _version(row["updated"]), "doi": row.get("doi")}))
        return {"results": results, "total_count": _total(payload["hits"]["total"])}

    async def _resolve_rodare(self, external, metadata):
        record = await self._rodare_json(RODARE + "/api/records/" + external)
        if str(record.get("id")) != external:
            raise SourceConnectorError("invalid_candidate")
        if not self._rodare_public(record):
            return []
        version = _version(record["updated"])
        if metadata.get("version") and metadata["version"] != version:
            raise SourceConnectorError("inconsistent_source_snapshot")
        rows = record["files"]
        if not isinstance(rows, list):
            raise ValueError()
        license = record["metadata"].get("license", {}).get("id")
        result = []
        for row in rows:
            name, url = row.get("key"), row.get("links", {}).get("self")
            if not _data_file(name) or "/" in name or not safe_file_url("rodare", url):
                continue
            if unquote(urlsplit(url).path).rsplit("/", 1)[-1] != name or not re.fullmatch(_UUID, str(row.get("bucket", ""))):
                continue
            if urlsplit(url).path.split("/")[3] != row["bucket"]:
                continue
            checksum = row.get("checksum")
            if not isinstance(checksum, str) or not re.fullmatch(r"md5:[a-f0-9]{32}", checksum):
                raise ValueError()
            result.append(_option("rodare", external, name, url, size=row.get("size"), version=version, license=license, checksum=checksum))
        return result
