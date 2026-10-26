"""Locate complete official astronomical entities without exporting row samples.

Metadata queries are paginated separately from source files. Downloads remain in
the common bounded-buffer streaming/object-storage path.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import html
import json
import re
import shlex
import zlib
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

from app.domain.external.data_source import SourceConnectorError

SOURCES = ('cds-vizier', 'mast')
TAP = 'https://tapvizier.cds.unistra.fr/TAPVizieR/tap/sync'
CDS = 'https://cdsarc.cds.unistra.fr/ftp/'
MAST = 'https://mast.stsci.edu/api/v0/invoke'
DOWNLOAD = 'https://mast.stsci.edu/api/v0.1/Download/file'
CDS_LICENSE = '依目录与原始作者许可使用，并引用原始文献及 CDS；https://cds.unistra.fr/vizier-org/licences_vizier.html'
MAST_LICENSE = '按任务/集合许可与署名要求使用；多数为公有领域，部分 HLSP 为 CC BY 4.0，DSS/GSC 等有特殊限制；https://archive.stsci.edu/publishing/data-use'
_TABLE = re.compile(r'(?:[IVX]+|B|J)/[A-Za-z0-9_+.-]+(?:/[A-Za-z0-9_+.-]+){1,6}')
_FILE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_+.-]*\.dat(?:\.gz)?')
_MAST_FILE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.+-]*\.(?:fits?|fits?\.gz|fits?\.fz|mxlo\.gz|mxhi\.gz)', re.I)


def _table(external):
    if not isinstance(external, str) or not _TABLE.fullmatch(external) or any(x in ('.', '..') for x in external.split('/')):
        raise SourceConnectorError('invalid_candidate')
    return external.rsplit('/', 1)


def _mast_external(external):
    if not isinstance(external, str) or not re.fullmatch(r'[1-9][0-9]{0,19}', external):
        raise SourceConnectorError('invalid_candidate')
    return external


def _mast_uri(uri):
    if not isinstance(uri, str) or len(uri) > 1200:
        return False
    # Source API identifiers, never an embedded HTTP URL or a caller's host.
    m = re.fullmatch(r'mast:([A-Za-z0-9_-]+)/((?:product|url/(?:pub|missions))/[A-Za-z0-9_+./-]+)', uri)
    return bool(m and all(x not in ('', '.', '..') for x in m[2].split('/'))
                and _MAST_FILE.fullmatch(m[2].rsplit('/', 1)[-1]))


def _mast_url(uri):
    # MAST's documented legacy URI maps exactly to its public STScI archive.
    # Prefer that original endpoint, not the Download gateway's proxy of it.
    if '/url/pub/' in uri:
        return 'https://archive.stsci.edu/pub/' + uri.split('/url/pub/', 1)[1]
    return DOWNLOAD + '?' + urlencode({'uri': uri})


def safe_file_url(source, url, external=None):
    try:
        if not isinstance(url, str) or len(url) > 2000 or any(ord(c) <= 32 or ord(c) == 127 for c in url):
            return False
        p = urlsplit(url)
        if p.scheme != 'https' or p.username is not None or p.password is not None or p.port not in (None, 443) or p.fragment:
            return False
        path = unquote(p.path)
        if '%' in path or '\\' in path or any(x in ('.', '..') for x in path.split('/')):
            return False
        if source == 'cds-vizier':
            if p.hostname != 'cdsarc.cds.unistra.fr' or p.query or not path.startswith('/ftp/') or re.search(r'%2f|%5c|%2e', p.path, re.I):
                return False
            archive, filename = path[5:].rsplit('/', 1)
            if not _FILE.fullmatch(filename):
                return False
            table = archive + '/' + re.sub(r'\.dat(?:\.gz)?$', '', filename)
            _table(table)
            return external is None or table == external
        if source == 'mast':
            if external is not None:
                _mast_external(external)
            if p.hostname == 'archive.stsci.edu':
                return (not p.query and '%' not in p.path and path.startswith('/pub/')
                        and all(x not in ('', '.', '..') for x in path[1:].split('/'))
                        and bool(re.fullmatch(r'/pub/[A-Za-z0-9_+./-]+', path))
                        and bool(_MAST_FILE.fullmatch(path.rsplit('/', 1)[-1])))
            pairs = parse_qsl(p.query, keep_blank_values=True)
            return p.hostname == 'mast.stsci.edu' and path == '/api/v0.1/Download/file' and len(pairs) == 1 and pairs[0][0] == 'uri' and _mast_uri(pairs[0][1])
    except (ValueError, TypeError, SourceConnectorError):
        return False
    return False


def is_astronomy_sample(data, option):
    """Require table rows or a genuine FITS header, never a successful error page."""
    if not isinstance(data, bytes) or not data:
        return False
    try:
        if data.startswith(b'\x1f\x8b'):
            # Only a bounded decompressed header/sample, including gzip bombs.
            data = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(data, 16384)
        if option.get('source_id') == 'mast':
            return data.startswith(b'SIMPLE  =                    T') and b'BITPIX  =' in data[:2880] and b'NAXIS   =' in data[:2880]
        if option.get('source_id') == 'cds-vizier':
            if not data or b'<' in data[:40] or b'\x00' in data:
                return False
            text = data.decode('ascii')
            lines = [line for line in text.splitlines() if line.strip() and not line.startswith('#')]
            return bool(lines and len(lines[0]) > 3 and re.search(r'[0-9]', lines[0]))
    except (zlib.error, UnicodeError):
        return False
    return False


def validate_astronomy_stream(source, stream, chosen, *, cancelled=None):
    """Verify gzip CRC/EOF and declared CDS row counts using bounded buffers."""
    if source not in SOURCES:
        return
    compressed = str(chosen.get('source_filename') or chosen.get('filename') or '').endswith('.gz')
    expected = (chosen.get('coverage') or {}).get('records') if source == 'cds-vizier' else None
    expected = expected if type(expected) is int and expected > 0 else None
    reader = None
    try:
        stream.seek(0)
        if cancelled is not None and cancelled.is_set():
            raise asyncio.CancelledError()
        if not compressed and expected is None:
            return
        reader = gzip.GzipFile(fileobj=stream, mode='rb') if compressed else stream
        rows = 0; nonblank = False
        while True:
            if cancelled is not None and cancelled.is_set():
                raise asyncio.CancelledError()
            chunk = reader.read(64 * 1024)
            if not chunk:
                break
            if expected is not None:
                parts = chunk.split(b'\n')
                for index, part in enumerate(parts):
                    nonblank = nonblank or bool(part.strip())
                    if index < len(parts) - 1:
                        rows += int(nonblank); nonblank = False
        if expected is not None and rows + int(nonblank) != expected:
            raise SourceConnectorError('download_integrity_failed')
    except (OSError, EOFError, zlib.error) as exc:
        raise SourceConnectorError('checksum_mismatch') from exc
    finally:
        if reader is not None and reader is not stream:
            reader.close()
        stream.seek(0)


def _tap_rows(payload, names):
    try:
        if [v['name'] for v in payload['metadata']] != names or not isinstance(payload['data'], list):
            raise ValueError()
        rows = payload['data']
        if any(not isinstance(r, list) or len(r) != len(names) for r in rows):
            raise ValueError()
        return rows
    except (KeyError, ValueError, TypeError):
        raise SourceConnectorError('invalid_response') from None


def _cds_where(query):
    words = query.strip().split()
    if not words or len(words) > 12 or any(not re.fullmatch(r'[A-Za-z0-9_+./-]{1,100}', w) for w in words):
        raise SourceConnectorError('invalid_query')
    clauses = []
    for word in words:
        # TAPVizieR uses a case-sensitive LIKE and has no LOWER/nocasematch UDF.
        variants = sorted({word, word.lower(), word.upper(), word.title()})
        clauses.append('(' + ' OR '.join(f"{column} LIKE '%{v}%'" for v in variants for column in ('table_name', 'description')) + ')')
    return ' AND '.join(clauses)


def _cds_listing(raw, table):
    rows = {}
    for m in re.finditer(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>.*?</a>\s+(\d{2}-[A-Za-z]{3}-\d{4} \d{2}:\d{2})', raw, re.S | re.I):
        name = html.unescape(m[1])
        if _FILE.fullmatch(name) and re.sub(r'\.dat(?:\.gz)?$', '', name) == table:
            if name in rows:
                raise SourceConnectorError('invalid_response')
            rows[name] = m[2]
    # Prefer the stored compressed original if both direct/decompressed names exist.
    if table + '.dat.gz' in rows:
        return table + '.dat.gz', rows[table + '.dat.gz']
    return next(iter(rows.items()), None)


def _cds_schema(raw, filename):
    name = filename.removesuffix('.gz')
    summary = re.search(r'^\s*' + re.escape(name) + r'\s+(\d+)\s+(\d+)\s+(.+)$', raw, re.M)
    if not summary or int(summary[1]) < 1 or int(summary[2]) < 1:
        return None
    if re.search(r'\b(?:references?|bibliography|notes?|authors?)\b', summary[3], re.I):
        return None
    section = re.search(r'Byte-by-byte Description of file:\s*' + re.escape(name) + r'\s*\n(.*?)(?=\nByte-by-byte Description|\Z)', raw, re.S | re.I)
    if not section:
        return None
    fields = re.findall(r'^\s*\d+(?:\s*-\s*\d+)?\s+[IFE]\d+(?:\.\d+)?\s+\S+\s+(\S+)', section[1], re.M)
    if len(fields) < 2:
        return None
    return dict(record_length=int(summary[1]), records=int(summary[2]), explanation=summary[3].strip())


def _mast_filters(query):
    try:
        parts = shlex.split(query)
    except ValueError:
        raise SourceConnectorError('invalid_query') from None
    names = {'mission': 'obs_collection', 'target': 'target_name', 'observation': 'obs_id', 'instrument': 'instrument_name', 'proposal': 'proposal_id'}
    if '=' not in query:
        values = {'target': query.strip()}
    else:
        values = {}
        for part in parts:
            key, sep, value = part.partition('=')
            if not sep or key not in names or key in values:
                raise SourceConnectorError('invalid_query')
            values[key] = value
    if not values or any(not v or len(v) > 120 or not re.fullmatch(r'[A-Za-z0-9_ +./:-]+', v) for v in values.values()):
        raise SourceConnectorError('invalid_query')
    return [{'paramName': names[k], 'values': [v]} for k, v in values.items()] + [{'paramName': 'dataRights', 'values': ['PUBLIC']}]


def _mast_page(payload, page, page_size):
    try:
        if not isinstance(payload, dict) or payload.get('status') != 'COMPLETE' or not isinstance(payload['data'], list):
            raise ValueError()
        meta = payload['paging']; total = meta['rowsFiltered']; rows = payload['data']
        if type(total) is not int or total < 0:
            raise ValueError()
        if total == 0 and rows == [] and meta['page'] in (0, page):
            return [], 0
        if meta['page'] != page or meta['pageSize'] != page_size or meta['pagesFiltered'] != (total + page_size - 1) // page_size:
            raise ValueError()
        if len(rows) != min(page_size, max(0, total - (page - 1) * page_size)) or meta['rows'] != len(rows):
            raise ValueError()
        return rows, total
    except (KeyError, TypeError, ValueError):
        raise SourceConnectorError('invalid_response') from None


class AstronomyFoundationAdapters:
    def __init__(self, fetch_json, fetch_text):
        self._json = fetch_json
        self._text = fetch_text

    async def _tap(self, query):
        return await self._json(TAP, {'REQUEST': 'doQuery', 'LANG': 'ADQL', 'FORMAT': 'json', 'QUERY': query})

    async def _mast(self, service, params, page, size):
        request = {'service': service, 'params': params, 'format': 'json', 'pagesize': size, 'page': page}
        payload = await self._json(MAST, {'request': json.dumps(request, separators=(',', ':'))})
        if isinstance(payload, dict) and payload.get('status') == 'EXECUTING':
            raise SourceConnectorError('upstream_timeout')
        return _mast_page(payload, page, size)

    async def search(self, source, query, page):
        from app.infrastructure.external.data_sources.open_data_sources import _candidate
        if source not in SOURCES:
            raise SourceConnectorError('unknown_source')
        if type(page) is not int or not 1 <= page <= 100:
            raise SourceConnectorError('invalid_cursor')
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise SourceConnectorError('invalid_query')
        if source == 'cds-vizier':
            where = _cds_where(query)
            count, found = await asyncio.gather(self._tap('SELECT COUNT(*) FROM TAP_SCHEMA.tables WHERE ' + where),
                self._tap(f'SELECT TOP {page * 10} table_name,description FROM TAP_SCHEMA.tables WHERE {where} ORDER BY table_name'))
            counts = _tap_rows(count, ['COUNT_ALL'])
            if len(counts) != 1 or type(counts[0][0]) is not int or counts[0][0] < 0:
                raise SourceConnectorError('invalid_response')
            total = counts[0][0]; rows = _tap_rows(found, ['table_name', 'description'])
            if len(rows) != min(total, page * 10) or len({r[0] for r in rows}) != len(rows):
                raise SourceConnectorError('inconsistent_source_snapshot')
            results = []
            for key, description in rows[(page - 1) * 10:page * 10]:
                key = key.strip('"'); _table(key)
                results.append(_candidate(source, key, key + ' · ' + description, description,
                    'https://vizier.cds.unistra.fr/viz-bin/VizieR?' + urlencode({'-source': key}),
                    license=CDS_LICENSE, formats=['dat'], coverage={'scope': '所选天文测量表的完整原始归档文件'},
                    metadata={'version': None}))
            return dict(results=results, total_count=total)
        rows, total = await self._mast('Mast.Caom.Filtered', {'columns': '*', 'filters': _mast_filters(query)}, page, 10)
        results = []
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or str(row.get('dataRights', '')).upper() != 'PUBLIC':
                continue
            external = _mast_external(str(row.get('obsid', '')))
            if external in seen:
                continue
            seen.add(external)
            results.append(_candidate(source, external, ' · '.join(str(row.get(k) or '') for k in ('obs_collection', 'obs_id', 'target_name')),
                str(row.get('obs_title') or '') + '；获取该观测直接关联的公开科学 FITS 文件。',
                'https://mast.stsci.edu/portal/Mashup/Clients/Mast/Portal.html?' + urlencode({'searchQuery': str(row.get('obs_id') or external)}),
                license=MAST_LICENSE, formats=['fits'], coverage={k: row.get(k) for k in ('obs_collection', 'obs_id', 'target_name', 'dataproduct_type', 't_min', 't_max', 's_ra', 's_dec', 'calib_level')},
                metadata={'version': None, 'observation_id': row.get('obs_id')}))
        return dict(results=results, total_count=total)

    async def resolve(self, candidate):
        from app.infrastructure.external.data_sources.open_data_sources import _filename, _identity
        source = candidate.get('source_id'); external = candidate.get('external_id')
        if source == 'cds-vizier':
            catalog, table = _table(external)
            directory = CDS + quote(catalog, safe='/+') + '/'
            listing, schema = await asyncio.gather(self._text(directory), self._text(directory + 'ReadMe'))
            found = _cds_listing(listing, table)
            if not found:
                return []
            filename, modified = found; summary = _cds_schema(schema, filename)
            if not summary:
                return []
            version = modified + '; ReadMe sha256:' + hashlib.sha256(schema.encode()).hexdigest()
            url = directory + filename
            return [dict(id=_identity(source, external, url, version), source_id=source, external_id=external, url=url,
                filename=_filename(filename), source_filename=filename, record_url=directory + 'ReadMe',
                content_type='application/gzip' if filename.endswith('.gz') else 'text/plain', size=None,
                checksum=None, version=version, license=CDS_LICENSE, available=True, reason='',
                kind='完整原始测量表', coverage=summary)]
        if source != 'mast':
            raise SourceConnectorError('invalid_candidate')
        external = _mast_external(external)
        # Recheck public access, so a stale search result never grants access.
        observations, _ = await self._mast('Mast.Caom.Filtered', {'columns': '*', 'filters': [
            {'paramName': 'obsid', 'values': [external]}, {'paramName': 'dataRights', 'values': ['PUBLIC']}]}, 1, 10)
        if not any(str(r.get('obsid')) == external and str(r.get('dataRights', '')).upper() == 'PUBLIC' for r in observations):
            return []
        result = []; seen = set(); page = 1; expected = None
        while True:
            rows, total = await self._mast('Mast.Caom.Products', {'obsid': external}, page, 500)
            if expected is not None and total != expected:
                raise SourceConnectorError('inconsistent_source_snapshot')
            expected = total
            for row in rows:
                uri = row.get('dataURI')
                if not isinstance(uri, str) or uri in seen:
                    raise SourceConnectorError('invalid_response')
                seen.add(uri)
                if (str(row.get('obsID')) != external or str(row.get('parent_obsid')) != external
                        or str(row.get('dataRights', '')).upper() != 'PUBLIC' or row.get('productType') != 'SCIENCE' or not _mast_uri(uri)):
                    continue
                filename = row.get('productFilename'); size = row.get('size')
                if not isinstance(filename, str) or filename != uri.rsplit('/', 1)[-1] or type(size) is not int or size <= 0:
                    raise SourceConnectorError('invalid_response')
                version = str(row.get('prvversion') or '源站当前处理版本未标明') + '; bytes=' + str(size)
                url = _mast_url(uri)
                result.append(dict(id=_identity(source, external, url, version), source_id=source, external_id=external,
                    url=url, filename=_filename(filename), source_filename=filename, record_url=candidate.get('source_url'),
                    content_type='application/gzip' if filename.endswith('.gz') else 'application/fits', size=size,
                    checksum=None, version=version, license=MAST_LICENSE, available=True, reason='', kind='完整科学观测文件'))
            if page * 500 >= total:
                break
            page += 1
        return result
