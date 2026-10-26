"""data.gouv.fr's public catalog and original hosted resources.

Contract: https://www.data.gouv.fr/api/1/swagger.json . The public catalog also
indexes third-party servers; those are deliberately not arbitrary download URLs.
"""
from __future__ import annotations

import mimetypes
import re
from urllib.parse import unquote, urlsplit

from app.domain.external.data_source import SourceConnectorError

SOURCES = ('france-data-gouv',)
API = 'https://www.data.gouv.fr/api/1/datasets/'
ID = r'[a-f0-9]{24}'
UUID = r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}'
FORMATS = frozenset('csv tsv txt xls xlsx ods json geojson jsonl ndjson parquet zip gz bz2 xz 7z tar nc netcdf h5 hdf5 gpkg shp sav dta rds rdata tif tiff arff'.split())
DOCUMENT = re.compile(r'(?:readme|metadata|metadonn[ée]es|dictionnaire|data[ _-]?dictionary|codebook|schema|documentation)', re.I)


def safe_file_url(source, url):
    if source not in SOURCES or not isinstance(url, str) or len(url) > 4000 or re.search(r'[\x00-\x20\\]', url):
        return False
    try:
        p = urlsplit(url)
        path = unquote(p.path)
        if (p.scheme != 'https' or p.port not in (None, 443) or p.username is not None or p.password is not None
                or p.query or p.fragment or '%' in path or re.search(r'[\x00-\x1f\x7f\\]', path) or re.search(r'%2f|%5c|%2e', p.path, re.I)
                or any(piece in {'', '.', '..'} for piece in path.lstrip('/').split('/'))):
            return False
        return ((p.hostname == 'static.data.gouv.fr' and bool(re.fullmatch(r'/resources/[^/]+/[0-9]{8}-[0-9]{6}/[^/]+', path)))
                or (p.hostname == 'www.data.gouv.fr' and bool(re.fullmatch(r'/storage/f/[0-9T:\-]+/[^/]+', path))))
    except (ValueError, TypeError):
        return False


def _public(row):
    return (isinstance(row, dict) and row.get('private') is False and not row.get('deleted')
            and not row.get('archived') and row.get('access_type') in (None, 'open'))


def _resources(row):
    resources = row.get('resources')
    if not isinstance(resources, list):
        raise SourceConnectorError('invalid_response')
    seen = set()
    for resource in resources:
        if (not isinstance(resource, dict) or resource.get('filetype') != 'file'
                or resource.get('type') in {'documentation', 'api', 'code'}):
            continue
        identifier, url = resource.get('id'), resource.get('url')
        if not isinstance(identifier, str) or not re.fullmatch(UUID, identifier) or identifier in seen or not safe_file_url(SOURCES[0], url):
            continue
        name = unquote(urlsplit(url).path).rsplit('/', 1)[-1]
        ext = name.rsplit('.', 1)[-1].lower()
        if ext not in FORMATS or DOCUMENT.search(name + ' ' + str(resource.get('title') or '')):
            continue
        size = resource.get('filesize')
        if size is not None and (type(size) is not int or size <= 0):
            continue
        seen.add(identifier)
        yield resource, name, ext


class FranceOct22Adapters:
    def __init__(self, fetch_json):
        self._json = fetch_json

    async def search(self, source, query, page):
        if source not in SOURCES:
            raise SourceConnectorError('unknown_source')
        if type(page) is not int or not 1 <= page <= 100:
            raise SourceConnectorError('invalid_cursor')
        if not isinstance(query, str) or not query.strip():
            raise SourceConnectorError('invalid_query')
        query = query.strip()
        if len(query) > 1 and (query[0], query[-1]) in {('"', '"'), ('“', '”'), ("'", "'")}:
            query = query[1:-1].strip()
        if not query:
            raise SourceConnectorError('invalid_query')
        payload = await self._json(API, {'q': query, 'page': page, 'page_size': 10})
        if not isinstance(payload, dict) or not isinstance(payload.get('data'), list) or type(payload.get('total')) is not int or payload['total'] < 0:
            raise SourceConnectorError('invalid_response')
        from app.infrastructure.external.data_sources.open_data_sources import _candidate
        results = []
        for row in payload['data']:
            if not _public(row):
                continue
            external, title = row.get('id'), row.get('title')
            if not isinstance(external, str) or not re.fullmatch(ID, external) or not isinstance(title, str) or not title.strip():
                continue
            resources = list(_resources(row))
            if resources:
                results.append(_candidate(source, external, title, row.get('description'),
                    'https://www.data.gouv.fr/datasets/' + external, license=row.get('license'),
                    formats=sorted({ext for _, _, ext in resources})))
        return {'results': results, 'total_count': payload['total']}

    async def resolve(self, candidate):
        source, external = candidate.get('source_id'), candidate.get('external_id')
        if source not in SOURCES or not isinstance(external, str) or not re.fullmatch(ID, external):
            raise SourceConnectorError('invalid_candidate')
        row = await self._json(API + external + '/')
        if not isinstance(row, dict) or row.get('id') != external:
            raise SourceConnectorError('invalid_response')
        if not _public(row):
            return []
        from app.infrastructure.external.data_sources.open_data_sources import _identity, _filename
        options = []
        for resource, name, ext in _resources(row):
            checksum = resource.get('checksum')
            if checksum is not None:
                if not isinstance(checksum, dict):
                    raise SourceConnectorError('invalid_response')
                algorithm, digest = checksum.get('type'), checksum.get('value')
                length = {'md5': 32, 'sha1': 40, 'sha256': 64, 'sha512': 128}.get(algorithm)
                if not length or not isinstance(digest, str) or not re.fullmatch(rf'[a-fA-F0-9]{{{length}}}', digest):
                    raise SourceConnectorError('invalid_response')
                checksum = algorithm + ':' + digest.lower()
            version = resource.get('last_modified')
            if not isinstance(version, str):
                version = None
            options.append(dict(id=_identity(source, external, resource['id'], version, resource['url'], resource.get('filesize'), checksum),
                source_id=source, external_id=external, url=resource['url'], filename=_filename(name),
                source_filename=name, size=resource.get('filesize'), checksum=checksum, version=version,
                license=row.get('license'), record_url='https://www.data.gouv.fr/datasets/' + external,
                content_type=mimetypes.guess_type(name)[0] or 'application/octet-stream',
                available=True, reason='', kind='源站完整文件'))
        return options
