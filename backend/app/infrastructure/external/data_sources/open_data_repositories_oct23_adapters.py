"""Public original-file adapters for independently verified research repositories.

Dataverse contracts: https://guides.dataverse.org/en/latest/api/search.html
https://guides.dataverse.org/en/latest/api/dataaccess.html
Source identities and exact storage paths are declared independently of user
input in open_data_repositories_oct23_definitions.py.
"""
from __future__ import annotations

import mimetypes
import re
from urllib.parse import parse_qsl, quote, unquote, urlsplit

from app.domain.external.data_source import SourceConnectorError
from app.domain.models.open_data_repositories_oct23_definitions import REPOSITORY_CONFIG
from app.infrastructure.external.data_sources.open_data_repository_adapters import _dataverse_query, _numeric, _version
from app.infrastructure.external.data_sources.open_data_catalog_adapters import _checksum

PAGE_SIZE = 10
DATAVERSE_SOURCES = frozenset(REPOSITORY_CONFIG)
SOURCES = DATAVERSE_SOURCES | {'muenster-datastore'}
MUENSTER = 'https://datastore.uni-muenster.de'
INSTALLATIONS = {key: (value["base_url"], value["pid_pattern"], value["prefix"])
                 for key, value in REPOSITORY_CONFIG.items()}


def _pid(source_id, value):
    if (not isinstance(value, str) or not value.lower().startswith("doi:")
            or not re.fullmatch(INSTALLATIONS[source_id][1], value[4:], re.I)):
        raise SourceConnectorError("invalid_candidate")
    normalized = value[4:].upper()
    return "doi:" + normalized


def safe_file_url(source_id, url, external=None):
    if source_id not in SOURCES or not isinstance(url, str) or len(url) > 16000 or re.search(r"[\x00-\x20\x7f\\]", url):
        return False
    try:
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.username or parts.password or parts.fragment
                or parts.port not in (None, 443)):
            return False
        path = unquote(parts.path)
        if ('%' in path or re.search(r"[\x00-\x1f\x7f\\]", path) or re.search(r"%2f|%5c|%2e", parts.path, re.I)
                or any(piece in {".", ".."} for piece in path.split("/"))):
            return False
        if source_id == 'muenster-datastore':
            if parts.hostname == 's3.uni-muenster.de':
                return bool(re.fullmatch(r'/datastore-data/[a-f0-9]{2}/[a-f0-9]{2}/[a-f0-9]{4}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}/data', path))
            match = re.fullmatch(r'/api/records/([a-z0-9]{5}-[a-z0-9]{5})/files/([^/]+)/content', path)
            return (parts.hostname == 'datastore.uni-muenster.de' and not parts.query and bool(match)
                    and (external is None or external == match[1]))
        if parts.hostname == urlsplit(INSTALLATIONS[source_id][0]).hostname:
            if source_id == 'bonndata' and not parts.query and re.fullmatch(r'/static/datafile/[1-9][0-9]{0,19}/[^/]+', path):
                return True
            return (bool(re.fullmatch(r"/api/access/datafile/[1-9][0-9]{0,19}", path))
                    and parse_qsl(parts.query, keep_blank_values=True) in ([], [("format", "original")]))
        return any(parts.hostname == host and re.fullmatch(pattern, path, re.I)
                   for host, pattern in REPOSITORY_CONFIG[source_id]["storage"])
    except (ValueError, TypeError):
        return False


class RepositoriesOct23Adapters:
    def __init__(self, fetch_json):
        self._json = fetch_json

    async def search(self, source_id, query, page):
        if source_id not in SOURCES:
            raise SourceConnectorError("unknown_source")
        if not isinstance(query, str) or not query.strip() or len(query) > 500 or type(page) is not int or not 1 <= page <= 100:
            raise SourceConnectorError("invalid_query")
        try:
            return await self._search_muenster(query, page) if source_id == 'muenster-datastore' else await self._search_dataverse(source_id, query, page)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise SourceConnectorError("invalid_response") from None

    async def resolve(self, candidate):
        source_id = candidate.get("source_id")
        if source_id not in SOURCES:
            raise SourceConnectorError("unknown_source")
        try:
            return await self._resolve_muenster(candidate) if source_id == 'muenster-datastore' else await self._resolve_dataverse(source_id, candidate)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise SourceConnectorError("invalid_response") from None

    async def _search_dataverse(self, source_id, query, page):
        from app.infrastructure.external.data_sources.open_data_sources import _candidate
        base, _, prefix = INSTALLATIONS[source_id]
        payload = await self._json(base + "/api/search", {
            "q": _dataverse_query(query), "type": "dataset",
            "fq": ["publicationStatus:Published", f'dsPersistentId:"doi:{prefix}"'],
            "per_page": PAGE_SIZE, "start": (page - 1) * PAGE_SIZE, "sort": "score",
        })
        if payload.get("status") != "OK":
            raise ValueError()
        rows, total = payload["data"]["items"], payload["data"]["total_count"]
        if not isinstance(rows, list) or len(rows) > PAGE_SIZE or type(total) is not int or total < 0:
            raise ValueError()
        results = []
        for row in rows:
            # RepOD's documented Fairchive fork omits version fields in search;
            # resolve checks its published manifest before offering any file.
            if (row.get("type") != "dataset" or row.get("fileCount") == 0
                    or row.get("versionState") != "RELEASED" and not (source_id == 'repod' and row.get('versionState') is None and row.get('published_at'))):
                continue
            try:
                external = _pid(source_id, row.get("global_id"))
            except SourceConnectorError:
                # Federated/harvested records require their own resolver.
                continue
            version = None
            if source_id == 'repod' and row.get('majorVersion') is None and row.get('minorVersion') is None:
                pass
            elif type(row.get("majorVersion")) is not int or type(row.get("minorVersion")) is not int:
                raise ValueError()
            else:
                version = _version(f"{row['majorVersion']}.{row['minorVersion']}")
            results.append(_candidate(source_id, external, row["name"], row.get("description"),
                "https://doi.org/" + external[4:], metadata={"doi": external[4:].lower(), "version": version,
                                                          "published": row.get("published_at")}))
        return {"results": results, "total_count": total}

    async def _resolve_dataverse(self, source_id, candidate):
        from app.infrastructure.external.data_sources.open_data_sources import _filename, _identity, _size, _text
        base = INSTALLATIONS[source_id][0]
        external = _pid(source_id, candidate["external_id"])
        payload = await self._json(base + "/api/datasets/:persistentId/", {"persistentId": external})
        if payload.get("status") != "OK":
            raise ValueError()
        dataset = payload["data"]
        actual = str(dataset.get("protocol")) + ":" + str(dataset.get("authority")) + "/" + str(dataset.get("identifier"))
        if _pid(source_id, actual) != external:
            raise SourceConnectorError("invalid_candidate")
        latest = dataset["latestVersion"]
        requested = (candidate.get("metadata") or {}).get("version")
        requested = _version(requested) if requested is not None else None
        selected = latest
        if requested is not None and requested != f"{latest['versionNumber']}.{latest['versionMinorNumber']}":
            previous = await self._json(base + "/api/datasets/:persistentId/versions/" + requested, {"persistentId": external})
            if previous.get("status") != "OK":
                raise ValueError()
            selected = previous["data"]
        version = _version(f"{selected['versionNumber']}.{selected['versionMinorNumber']}")
        if requested is not None and requested != version:
            raise SourceConnectorError("invalid_candidate")
        selected_pid = selected.get('datasetPersistentId')
        if selected_pid is None and source_id == 'repod':
            selected_pid = actual
        if _pid(source_id, selected_pid) != external:
            raise SourceConnectorError("invalid_candidate")
        if selected.get("versionState") != "RELEASED":
            return []
        if any(part.get("guestbookId") not in (None, 0, -1) or part.get("guestbook") for part in (dataset, latest, selected)):
            return []
        if source_id == 'repod' and (dataset.get('hasActiveGuestbook') is not False or dataset.get('embargoActive') is not False):
            return []
        files = selected["files"]
        if not isinstance(files, list):
            raise ValueError()
        rights = selected.get("license") or {}
        license_text = _text(rights.get("name") or selected.get("termsOfUse"), 500) if isinstance(rights, dict) else _text(rights, 500)
        results = []
        for item in files:
            data = item["dataFile"]
            if item.get("restricted") is not False or item.get("embargo") or data.get("embargo"):
                continue
            file_id = _numeric(data["id"])
            original = bool(data.get("originalFileFormat"))
            name = data.get("originalFileName") if original else data["filename"]
            if original and not name and source_id == 'repod':
                # Fairchive retains the original bytes/checksum but only names
                # the converted TAB. Restore its original-format extension.
                extension = _ORIGINAL_EXTENSIONS.get(data['originalFileFormat'])
                if not extension or not isinstance(data.get('filename'), str):
                    continue
                name = data['filename'].rsplit('.', 1)[0] + extension
            if not isinstance(name, str) or not name or len(name) > 2000:
                raise ValueError()
            size = _size(data.get("originalFileSize") if original else data.get("filesize"))
            if not _entity_file(name):
                continue
            if size == 0:
                continue
            digest = data.get("checksum") or {"type": "MD5", "value": data.get("md5")}
            checksum = _checksum(digest.get("type"), digest.get("value"))
            mime = data.get("originalFileFormat") if original else data.get("contentType")
            results.append({"id": _identity(source_id, external, version, file_id, name, size, checksum),
                "source_id": source_id, "external_id": external, "version": version,
                "filename": _filename(f"{source_id}_{file_id}_{name}"), "source_filename": name,
                "size": size, "content_type": mime or mimetypes.guess_type(name)[0] or "application/octet-stream",
                # Older Dataverse deployments reject format=original on files
                # that were never converted. Their plain endpoint is original.
                "url": base + "/api/access/datafile/" + file_id + ("?format=original" if original else ""),
                "checksum": checksum, "available": True, "reason": "", "kind": "源站文件",
                "record_url": "https://doi.org/" + external[4:],
                "license": (_text(item.get('licenseUrl') or item.get('licenseName'), 500) if source_id == 'repod' else license_text) or None})
        return results


    async def _search_muenster(self, query, page):
        from app.infrastructure.external.data_sources.open_data_sources import _candidate
        payload = await self._json(MUENSTER + '/api/records', {
            'q': 'metadata.resource_type.id:dataset AND (' + _dataverse_query(query) + ')',
            'size': PAGE_SIZE, 'page': page, 'sort': 'bestmatch',
        })
        hits = payload['hits']
        rows, total = hits['hits'], hits['total']
        if isinstance(total, dict):
            total = total['value']
        if not isinstance(rows, list) or len(rows) > PAGE_SIZE or type(total) is not int or total < 0:
            raise ValueError()
        results = []
        for row in rows:
            if not _open_muenster_record(row):
                continue
            external = _muenster_id(row['id'])
            results.append(_candidate('muenster-datastore', external, row['metadata']['title'],
                row['metadata'].get('description'), MUENSTER + '/records/' + external,
                license=_muenster_license(row), metadata={'version': str(row['versions']['index']),
                    'doi': row.get('pids', {}).get('doi', {}).get('identifier'),
                    'published': row['metadata'].get('publication_date')}))
        return {'results': results, 'total_count': total}

    async def _resolve_muenster(self, candidate):
        from app.infrastructure.external.data_sources.open_data_sources import _filename, _identity, _size
        external = _muenster_id(candidate['external_id'])
        row = await self._json(MUENSTER + '/api/records/' + external)
        if row.get('id') != external:
            raise SourceConnectorError('invalid_candidate')
        if not _open_muenster_record(row):
            return []
        version = str(row['versions']['index'])
        requested = (candidate.get('metadata') or {}).get('version')
        if requested is not None and str(requested) != version:
            raise SourceConnectorError('invalid_candidate')
        files = row['files']
        entries = files['entries']
        # Never present a truncated manifest as the complete file list.
        if not isinstance(entries, dict) or type(files.get('count')) is not int or files['count'] != len(entries):
            raise SourceConnectorError('invalid_response')
        result = []
        for name, data in entries.items():
            if (not isinstance(name, str) or name != data.get('key') or '/' in name or '\\' in name
                    or name in {'.', '..'} or not _entity_file(name) or data.get('access', {}).get('hidden') is not False):
                continue
            url = MUENSTER + '/api/records/' + external + '/files/' + quote(name, safe='') + '/content'
            if not safe_file_url('muenster-datastore', url, external):
                raise SourceConnectorError('unsafe_download_url')
            size = _size(data.get('size'))
            if size == 0:
                continue
            algorithm, _, digest = str(data.get('checksum', '')).partition(':')
            checksum = _checksum(algorithm, digest)
            if not checksum:
                raise SourceConnectorError('invalid_response')
            result.append(dict(id=_identity('muenster-datastore', external, version, name, size, checksum),
                source_id='muenster-datastore', external_id=external, version=version,
                filename=_filename('muenster_' + external + '_' + name), source_filename=name,
                size=size, content_type=data.get('mimetype') or mimetypes.guess_type(name)[0] or 'application/octet-stream',
                url=url, checksum=checksum, available=True, reason='', kind='源站原始文件',
                record_url=MUENSTER + '/records/' + external, license=_muenster_license(row)))
        return result


def _muenster_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-z0-9]{5}-[a-z0-9]{5}', value):
        raise SourceConnectorError('invalid_candidate')
    return value


def _open_muenster_record(row):
    access = row.get('access', {})
    return (row.get('is_published') is True and row.get('is_draft') is False
            and row.get('status') == 'published' and row.get('deletion_status', {}).get('is_deleted') is False
            and row.get('metadata', {}).get('resource_type', {}).get('id') == 'dataset'
            and access.get('record') == 'public' and access.get('files') == 'public'
            and access.get('embargo', {}).get('active') is False
            and row.get('files', {}).get('enabled') is True)


def _muenster_license(row):
    from app.infrastructure.external.data_sources.open_data_sources import _text
    rights = row.get('metadata', {}).get('rights', [])
    values = [r.get('props', {}).get('url') or r.get('id') or r.get('title', {}).get('en') for r in rights if isinstance(r, dict)]
    return _text('; '.join(v for v in values if isinstance(v, str)), 500) or None


_ENTITY_EXTENSIONS = ('.csv', '.tsv', '.tab', '.txt', '.json', '.jsonl', '.xml', '.xlsx', '.xls', '.ods',
    '.dta', '.sav', '.por', '.sas7bdat', '.rds', '.rdata', '.parquet', '.arrow', '.feather', '.sqlite', '.db',
    '.zip', '.tar', '.gz', '.bz2', '.xz', '.7z', '.zst', '.nc', '.netcdf', '.h5', '.hdf5', '.hdf', '.mat',
    '.npy', '.npz', '.fits', '.fit', '.root', '.grib', '.grb', '.grb2', '.cdf', '.dat', '.bin', '.raw', '.spe',
    '.tif', '.tiff', '.geotiff', '.shp', '.gpkg', '.geojson', '.kml',
    '.las', '.laz', '.xyz', '.cif', '.sdf', '.mol', '.pdb', '.vtk', '.ply', '.obj', '.fcs', '.edf', '.nii',
    '.fasta', '.fa', '.fastq', '.fq', '.bam', '.sam', '.bed', '.vcf', '.mzml', '.mzxml', '.mrc',
    '.png', '.jpg', '.jpeg', '.wav', '.flac', '.ogg', '.mp3', '.mp4', '.webm', '.vtt', '.srt')

_ORIGINAL_EXTENSIONS = {
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
    'application/vnd.ms-excel': '.xls', 'text/csv': '.csv', 'text/comma-separated-values': '.csv',
    'text/tab-separated-values': '.tsv', 'application/x-stata': '.dta', 'application/x-stata-14': '.dta',
    'application/x-spss-sav': '.sav', 'application/x-rlang-transport': '.RData',
}


def _entity_file(name):
    if not isinstance(name, str) or any(ord(c) < 32 or ord(c) == 127 for c in name):
        return False
    return (name.lower().endswith(_ENTITY_EXTENSIONS)
        and not re.search(r'(?:^|[._ -])(?:readme|metadata|codebook|dictionary|headers?|license|licence|ethogram)(?:[._ -]|$)|_vi_', name, re.I))
