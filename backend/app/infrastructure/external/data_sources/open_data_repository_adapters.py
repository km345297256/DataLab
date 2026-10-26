"""Anonymous repository APIs with original-file and fixed-version resolution.

Contracts: https://docs.figshare.com/v2/
https://guides.dataverse.org/en/latest/api/search.html
https://guides.dataverse.org/en/latest/api/dataaccess.html

Only the registered Harvard installation is contacted. File locations are
re-created from fresh repository manifests, never from a candidate's URL.
"""
from __future__ import annotations

import mimetypes
import re
from urllib.parse import parse_qsl, unquote, urlsplit

from app.domain.external.data_source import SourceConnectorError
from app.infrastructure.external.data_sources.open_data_query_syntax import all_terms_query as _dataverse_query

PAGE_SIZE = 10
FIGSHARE_API = "https://api.figshare.com/v2"
DATAVERSE = "https://dataverse.harvard.edu"
SOURCES = frozenset({"figshare", "harvard-dataverse"})


def safe_file_url(source_id: str, url: str) -> bool:
    """Allow stable provider endpoints and their documented storage buckets.

    Signed query strings are accepted only on the two providers' named buckets;
    no S3-wide or arbitrary-subdomain rule is used.
    """
    if not isinstance(url, str) or len(url) > 16000 or re.search(r"[\x00-\x20\\]", url):
        return False
    try:
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.username or parts.password or parts.fragment
                or parts.port not in (None, 443)):
            return False
        path = unquote(parts.path)
        if (re.search(r"[\x00-\x1f\\]", path) or re.search(r"%2f|%5c|%2e", parts.path, re.I)
                or any(piece in {".", ".."} for piece in path.split("/"))):
            return False
        if source_id == "figshare":
            if parts.hostname == "ndownloader.figshare.com":
                return not parts.query and bool(re.fullmatch(r"/files/[1-9][0-9]{0,19}", path))
            if parts.hostname == "figshare.com":
                return not parts.query and bool(re.fullmatch(r"/ndownloader/files/[1-9][0-9]{0,19}", path))
            buckets = r"(?:pfigshare-u-files|pstorage-digitalscience-874864551)"
            if parts.hostname in {"s3-eu-west-1.amazonaws.com", "s3.eu-west-1.amazonaws.com"}:
                return bool(re.fullmatch(r"/" + buckets + r"/[1-9][0-9]{0,19}/[^/]+", path))
            if parts.hostname in {
                "pfigshare-u-files.s3.eu-west-1.amazonaws.com",
                "pfigshare-u-files.s3-eu-west-1.amazonaws.com",
                "pstorage-digitalscience-874864551.s3.eu-west-1.amazonaws.com",
            }:
                return bool(re.fullmatch(r"/[1-9][0-9]{0,19}/[^/]+", path))
        elif source_id == "harvard-dataverse":
            if parts.hostname == "dataverse.harvard.edu":
                return (bool(re.fullmatch(r"/api/access/datafile/[1-9][0-9]{0,19}", path))
                        and parse_qsl(parts.query, keep_blank_values=True) == [("format", "original")])
            if parts.hostname in {"dvn-cloud-iqss.s3.amazonaws.com", "dvn-cloud-iqss.s3.us-east-1.amazonaws.com"}:
                return bool(re.fullmatch(r"/10\.7910/DVN/[A-Za-z0-9]{4,20}/[A-Za-z0-9._-]+", path))
        return False
    except (ValueError, TypeError):
        return False


def _numeric(value) -> str:
    if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]{0,19}", str(value)):
        raise SourceConnectorError("invalid_response")
    return str(value)


def _doi(value) -> str | None:
    if value in (None, ""):
        return None
    from app.infrastructure.external.data_sources.open_data_sources import _doi as validate_doi
    return validate_doi(value)


def _harvard_pid(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"doi:10\.7910/DVN/[A-Za-z0-9]{4,20}", value, re.I):
        raise SourceConnectorError("invalid_candidate")
    return "doi:10.7910/DVN/" + value.rsplit("/", 1)[1].upper()


def _version(value) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,8}\.[0-9]{1,8}", value):
        raise SourceConnectorError("invalid_candidate")
    return value


def _checksum(kind, value):
    if value in (None, ""):
        return None
    normalized = str(kind).lower().replace("-", "")
    lengths = {"md5": 32, "sha1": 40, "sha256": 64, "sha512": 128}
    if normalized not in lengths or not isinstance(value, str) or not re.fullmatch(r"[a-fA-F0-9]{%d}" % lengths[normalized], value):
        raise SourceConnectorError("invalid_response")
    return normalized + ":" + value.lower()


class RepositoryAdapters:
    def __init__(self, fetch_json):
        self._json = fetch_json

    async def search(self, source_id, query, page):
        if source_id not in SOURCES:
            raise SourceConnectorError("unknown_source")
        if not isinstance(query, str) or not query.strip() or len(query) > 500 or type(page) is not int or not 1 <= page <= 100:
            raise SourceConnectorError("invalid_query")
        try:
            if source_id == "figshare":
                rows = await self._json(FIGSHARE_API + "/articles/search", method="POST", json={
                    "search_for": query, "item_type": 3, "page": page, "page_size": PAGE_SIZE,
                })
                if not isinstance(rows, list) or len(rows) > PAGE_SIZE:
                    raise ValueError()
                results = [self._figshare_candidate(row) for row in rows if row.get("defined_type") in (None, 3)]
                # The public Figshare API supplies no total. A full page is a
                # continuation hint, not a claim about the number of datasets.
                total = (page - 1) * PAGE_SIZE + len(rows) + (len(rows) == PAGE_SIZE)
            else:
                payload = await self._json(DATAVERSE + "/api/search", {
                    "q": _dataverse_query(query), "type": "dataset",
                    "fq": ["publicationStatus:Published", 'dsPersistentId:"doi:10.7910/DVN/"'],
                    "per_page": PAGE_SIZE, "start": (page - 1) * PAGE_SIZE, "sort": "score",
                })
                if payload.get("status") != "OK":
                    raise ValueError()
                data = payload["data"]
                rows, total = data["items"], data["total_count"]
                if not isinstance(rows, list) or len(rows) > PAGE_SIZE or type(total) is not int or total < 0:
                    raise ValueError()
                results = []
                for row in rows:
                    if row.get("type") != "dataset" or row.get("versionState") != "RELEASED":
                        continue
                    # Harvard also indexes harvested metadata from other
                    # repositories; those files need their own registered
                    # resolver and must not invalidate this whole page.
                    try:
                        _harvard_pid(row.get("global_id"))
                    except SourceConnectorError:
                        continue
                    results.append(self._dataverse_candidate(row))
            return {"results": results, "total_count": total}
        except (KeyError, TypeError, ValueError, AttributeError):
            raise SourceConnectorError("invalid_response") from None

    @staticmethod
    def _figshare_candidate(row):
        from app.infrastructure.external.data_sources.open_data_sources import _candidate
        external = _numeric(row["id"])
        version = row.get("version")
        if version is not None:
            _numeric(version)
        doi = _doi(row.get("doi"))
        record_url = ("https://doi.org/" + doi if doi else
                      f"https://figshare.com/articles/dataset/_/{external}" + (f"/{version}" if version else ""))
        return _candidate("figshare", external, row["title"], row.get("description"),
                          record_url,
                          metadata={"doi": doi, "version": version,
                                    "published": row.get("published_date")})

    @staticmethod
    def _dataverse_candidate(row):
        from app.infrastructure.external.data_sources.open_data_sources import _candidate
        external = _harvard_pid(row["global_id"])
        major, minor = row.get("majorVersion"), row.get("minorVersion")
        if type(major) is not int or type(minor) is not int:
            raise ValueError()
        version = _version(f"{major}.{minor}")
        return _candidate("harvard-dataverse", external, row["name"], row.get("description"),
                          "https://doi.org/" + external[4:],
                          metadata={"doi": external[4:].lower(), "version": version,
                                    "published": row.get("published_at")})

    async def resolve(self, candidate):
        source_id = candidate.get("source_id")
        if source_id not in SOURCES:
            raise SourceConnectorError("unknown_source")
        try:
            return (await self._figshare_options(candidate) if source_id == "figshare"
                    else await self._dataverse_options(candidate))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise SourceConnectorError("invalid_response") from None

    async def _figshare_options(self, candidate):
        from app.infrastructure.external.data_sources.open_data_sources import _filename, _identity, _size, _text
        external = _numeric(candidate["external_id"])
        requested_version = (candidate.get("metadata") or {}).get("version")
        url = FIGSHARE_API + "/articles/" + external
        if requested_version is not None:
            url += "/versions/" + _numeric(requested_version)
        record = await self._json(url)
        if _numeric(record["id"]) != external:
            raise SourceConnectorError("invalid_candidate")
        version = _numeric(record["version"])
        if requested_version is not None and version != str(requested_version):
            raise SourceConnectorError("invalid_candidate")
        if (record.get("is_confidential") or record.get("is_embargoed") or record.get("is_metadata_record")
                or record.get("defined_type") not in (None, 3)):
            return []
        files = record["files"]
        if not isinstance(files, list):
            raise ValueError()
        results = []
        rights = record.get("license") or {}
        license_text = _text(rights.get("name") or rights.get("value"), 500) if isinstance(rights, dict) else _text(rights, 500)
        doi = _doi(record.get("doi"))
        record_url = "https://doi.org/" + doi if doi else f"https://figshare.com/articles/dataset/_/{external}/{version}"
        for item in files:
            if item.get("is_link_only") is not False:
                continue
            file_id = _numeric(item["id"])
            declared_url = item["download_url"]
            if (not safe_file_url("figshare", declared_url)
                    or urlsplit(declared_url).hostname not in {"figshare.com", "ndownloader.figshare.com"}
                    or urlsplit(declared_url).path.rsplit("/", 1)[1] != file_id):
                raise SourceConnectorError("unsafe_download_url")
            name, size = item["name"], _size(item["size"])
            if not isinstance(name, str) or not name or len(name) > 2000:
                raise ValueError()
            if size == 0:
                continue
            checksum = _checksum("md5", item.get("computed_md5") or item.get("supplied_md5"))
            stable_url = "https://ndownloader.figshare.com/files/" + file_id
            results.append({"id": _identity("figshare", external, version, file_id, name, size, checksum),
                            "source_id": "figshare", "external_id": external, "version": version,
                            "filename": _filename(f"figshare_{external}_{file_id}_{name}"), "source_filename": name,
                            "size": size, "content_type": mimetypes.guess_type(name)[0] or "application/octet-stream",
                            "url": stable_url, "checksum": checksum, "available": True, "reason": "", "kind": "源站文件",
                            "license": license_text or None,
                            "record_url": record_url})
        return results

    async def _dataverse_options(self, candidate):
        from app.infrastructure.external.data_sources.open_data_sources import _filename, _identity, _size, _text
        external = _harvard_pid(candidate["external_id"])
        payload = await self._json(DATAVERSE + "/api/datasets/:persistentId/", {"persistentId": external})
        if payload.get("status") != "OK":
            raise ValueError()
        dataset = payload["data"]
        actual = str(dataset.get("protocol")) + ":" + str(dataset.get("authority")) + "/" + str(dataset.get("identifier"))
        if _harvard_pid(actual) != external:
            raise SourceConnectorError("invalid_candidate")
        latest = dataset["latestVersion"]
        requested_version = (candidate.get("metadata") or {}).get("version")
        if requested_version is not None:
            requested_version = _version(requested_version)
        latest_version = f"{latest['versionNumber']}.{latest['versionMinorNumber']}"
        version_data = latest
        if requested_version is not None and requested_version != latest_version:
            previous = await self._json(DATAVERSE + "/api/datasets/:persistentId/versions/" + requested_version,
                                        {"persistentId": external})
            if previous.get("status") != "OK":
                raise ValueError()
            version_data = previous["data"]
        version = _version(f"{version_data['versionNumber']}.{version_data['versionMinorNumber']}")
        if requested_version is not None and requested_version != version:
            raise SourceConnectorError("invalid_candidate")
        if version_data.get("versionState") != "RELEASED":
            return []
        # A configured guestbook can require user-supplied answers/consent.
        # Anonymous discovery never invents or submits those answers.
        if any(part.get("guestbookId") not in (None, 0, -1) or part.get("guestbook")
               for part in (dataset, latest, version_data)):
            return []
        if _harvard_pid(version_data.get("datasetPersistentId")) != external:
            raise SourceConnectorError("invalid_candidate")
        files = version_data["files"]
        if not isinstance(files, list):
            raise ValueError()
        results = []
        rights = version_data.get("license") or {}
        license_text = _text(rights.get("name") or version_data.get("termsOfUse"), 500) if isinstance(rights, dict) else _text(rights, 500)
        for item in files:
            data = item["dataFile"]
            if item.get("restricted") is not False or item.get("embargo") or data.get("embargo"):
                continue
            file_id = _numeric(data["id"])
            original = bool(data.get("originalFileFormat"))
            name = data.get("originalFileName") if original else data["filename"]
            if not isinstance(name, str) or not name or len(name) > 2000:
                raise ValueError()
            size = _size(data.get("originalFileSize") if original else data.get("filesize"))
            if size == 0:
                continue
            checksum_data = data.get("checksum") or {"type": "MD5", "value": data.get("md5")}
            checksum = _checksum(checksum_data.get("type"), checksum_data.get("value"))
            mime = data.get("originalFileFormat") if original else data.get("contentType")
            record_url = "https://doi.org/" + external[4:]
            results.append({"id": _identity("harvard-dataverse", external, version, file_id, name, size, checksum),
                            "source_id": "harvard-dataverse", "external_id": external, "version": version,
                            "filename": _filename(f"harvard_{file_id}_{name}"), "source_filename": name,
                            "size": size, "content_type": mime or mimetypes.guess_type(name)[0] or "application/octet-stream",
                            "url": DATAVERSE + "/api/access/datafile/" + file_id + "?format=original",
                            "checksum": checksum, "available": True, "reason": "", "kind": "源站文件", "record_url": record_url,
                            "license": license_text or None})
        return results
