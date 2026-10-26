"""Official complete GWOSC HDF5 and CDAWeb CDF files; metadata never is data."""
from __future__ import annotations

import asyncio
import math
import re
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import unquote, urlsplit

from app.domain.external.data_source import SourceConnectorError

SOURCES = ('gwosc', 'nasa-cdaweb')
GWOSC = 'https://gwosc.org'
CDAS = 'https://cdaweb.gsfc.nasa.gov/WS/cdasr/1/dataviews/sp_phys/datasets'
EVENT = r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}'
DATASET = r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}'
BASIC = r'[0-9]{8}T[0-9]{6}Z'
GW_LICENSE = 'CC BY 4.0 — https://gwosc.org/acknowledgement/'
CDAS_LICENSE = 'SPDF默认CC0/Public Domain，个别提供方例外及引用要求以数据集为准 — https://hdrl.gsfc.nasa.gov/data_use_policy.html'


def _utc(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z)?', value):
        raise SourceConnectorError('invalid_query')
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).replace(tzinfo=timezone.utc)
    except ValueError:
        raise SourceConnectorError('invalid_query') from None


def _basic(date):
    return date.strftime('%Y%m%dT%H%M%SZ')


def _cdas_external(external):
    if not isinstance(external, str) or not re.fullmatch(DATASET + r'\|' + BASIC + r'\|' + BASIC, external):
        raise SourceConnectorError('invalid_candidate')
    dataset, start, end = external.split('|')
    try:
        dates = [datetime.strptime(s, '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc) for s in (start, end)]
    except ValueError:
        raise SourceConnectorError('invalid_candidate') from None
    if dates[0] > dates[1]:
        raise SourceConnectorError('invalid_candidate')
    return dataset, start, end


def _query(query):
    dates = re.findall(r'(?:^|\s)(start|end)=([^\s]+)', query)
    if dates and (len(dates) != 2 or len(dict(dates)) != 2):
        raise SourceConnectorError('invalid_query')
    text = re.sub(r'(?:^|\s)(?:start|end)=[^\s]+', ' ', query).strip()
    if not text or '=' in text:
        raise SourceConnectorError('invalid_query')
    values = dict(dates)
    interval = [_utc(values[k]) for k in ('start', 'end')] if values else None
    if interval and interval[0] > interval[1]:
        raise SourceConnectorError('invalid_query')
    # Do not discard fractional seconds silently; the original-file API takes seconds.
    if interval and any(d.microsecond for d in interval):
        raise SourceConnectorError('invalid_query')
    return text.strip('"').casefold().split(), interval


def safe_file_url(source, url, external=None):
    try:
        if not isinstance(url, str) or len(url) > 2000 or any(ord(c) <= 32 or ord(c) == 127 for c in url):
            return False
        p = urlsplit(url)
        path = unquote(p.path)
        if (p.scheme != 'https' or p.username is not None or p.password is not None or p.port not in (None, 443)
                or p.fragment or p.query or '%' in path or '\\' in path or re.search(r'%2f|%5c|%2e', p.path, re.I)
                or any(c in {'.', '..'} for c in path.split('/'))):
            return False
        if source == 'gwosc':
            m = re.fullmatch(r'/eventapi/json/([A-Za-z0-9_.-]+)/(' + EVENT + r')/v([1-9][0-9]*)/([A-Z]-[A-Z][0-9]_[A-Za-z0-9_]+-[0-9]+-[1-9][0-9]*)\.hdf5', path)
            return p.hostname == 'gwosc.org' and bool(m) and (external is None or external == m[2] + '-v' + m[3])
        if source == 'nasa-cdaweb':
            m = re.fullmatch(r'/(?:sp_phys/data|pub/data)/[A-Za-z0-9_./-]+/([A-Za-z0-9_-]+)_([0-9]{8}(?:[0-9]{6})?)_v([0-9]+(?:\.[0-9]+)*)\.cdf', path)
            if p.hostname not in {'cdaweb.gsfc.nasa.gov', 'spdf.gsfc.nasa.gov'} or not m or '/0MASTERS/' in path.upper():
                return False
            if m[2].startswith('00000000'):
                return False
            try:
                datetime.strptime(m[2][:8], '%Y%m%d')
            except ValueError:
                return False
            return external is None or m[1].casefold() == _cdas_external(external)[0].casefold()
    except (ValueError, TypeError, AttributeError, SourceConnectorError):
        pass
    return False


def is_physics_space_sample(source, data):
    """Scientific binary signatures, including older CDF 2 files still in SPDF."""
    if source == 'gwosc':
        return len(data) >= 32 and data.startswith(b'\x89HDF\r\n\x1a\n')
    if source == 'nasa-cdaweb':
        return (len(data) >= 32 and data[:4] in (b'\x00\x00\xff\xff', b'\xcd\xf2\x60\x02', b'\xcd\xf3\x00\x01')
                and data[4:8] in (b'\x00\x00\xff\xff', b'\xcc\xcc\x00\x01'))
    return False


class PhysicsSpaceFoundationAdapters:
    def __init__(self, fetch_json, fetch_text):
        self._json, self._text = fetch_json, fetch_text
        self._cache = {}
        self._locks = {}

    async def _gw_pages(self, path):
        result = []
        page = 1
        expected = None
        while True:
            payload = await self._json(GWOSC + '/api/v2/' + path, {'format': 'json', 'pagesize': 500, 'page': page})
            rows = payload.get('results') if isinstance(payload, dict) else None
            total = payload.get('results_count') if isinstance(payload, dict) else None
            count = payload.get('results_per_page') if isinstance(payload, dict) else None
            if (not isinstance(rows, list) or type(total) is not int or total < 0 or type(count) is not int or count <= 0
                    or payload.get('page_number') != page or (expected is not None and expected != total)):
                raise SourceConnectorError('invalid_response')
            expected = total
            result.extend(rows)
            pages = math.ceil(total / count)
            if payload.get('num_pages') != pages or len(result) > total:
                raise SourceConnectorError('invalid_response')
            if page >= pages:
                if len(result) != total or payload.get('next') is not None:
                    raise SourceConnectorError('invalid_response')
                return result
            if len(rows) != count or not payload.get('next'):
                raise SourceConnectorError('invalid_response')
            # Ignore next URLs: only construct requests against the original endpoint.
            page += 1

    async def _catalog(self, source):
        async with self._locks.setdefault(source, asyncio.Lock()):
            cached = self._cache.get(source)
            if cached and time.monotonic() - cached[0] < 3600:
                return cached[1]
            if source == 'gwosc':
                events = await self._gw_pages('events')
                rows = {}
                for event in events:
                    name = event.get('name')
                    if not isinstance(name, str) or not re.fullmatch(EVENT, name) or not isinstance(event.get('versions'), list):
                        raise SourceConnectorError('invalid_response')
                    for v in event['versions']:
                        number, catalog = v.get('version'), v.get('catalog')
                        if type(number) is not int or number <= 0 or not isinstance(catalog, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', catalog):
                            raise SourceConnectorError('invalid_response')
                        key = name + '-v' + str(number)
                        if key in rows:
                            raise SourceConnectorError('invalid_response')
                        rows[key] = {'name': name, 'version': number, 'catalog': catalog, 'aliases': event.get('aliases') or []}
            else:
                data = await self._json(CDAS)
                items = data.get('DatasetDescription') if isinstance(data, dict) else None
                if not isinstance(items, list):
                    raise SourceConnectorError('invalid_response')
                rows = {}
                for item in items:
                    name = item.get('Id')
                    if not isinstance(name, str) or not re.fullmatch(DATASET, name) or name in rows:
                        raise SourceConnectorError('invalid_response')
                    interval = item.get('TimeInterval') or {}
                    try:
                        start, end = _utc(interval.get('Start')), _utc(interval.get('End'))
                    except SourceConnectorError:
                        continue  # A product without public time coverage cannot resolve entity files.
                    if start > end or not isinstance(item.get('Label'), str):
                        continue
                    rows[name] = {**item, 'start': start, 'end': end}
            self._cache[source] = (time.monotonic(), rows)
            return rows

    async def search(self, source, query, page):
        from app.infrastructure.external.data_sources.open_data_sources import _candidate
        if source not in SOURCES:
            raise SourceConnectorError('unknown_source')
        if type(page) is not int or not 1 <= page <= 100:
            raise SourceConnectorError('invalid_cursor')
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise SourceConnectorError('invalid_query')
        terms, interval = _query(query)
        if source == 'gwosc' and query.strip().casefold() in {'gwosc', 'gravitational waves', 'gravitational wave', '引力波', '引力波数据'}:
            terms = []  # Browse the official event catalogue; no unsupported detector/parameter filter.
        if source == 'gwosc' and interval:
            raise SourceConnectorError('invalid_query')
        rows = await self._catalog(source)
        matches = []
        for key, row in rows.items():
            if source == 'gwosc':
                haystack = key + ' ' + row['catalog'] + ' ' + ' '.join(row['aliases'])
            else:
                haystack = key + ' ' + row['Label'] + ' ' + ' '.join(str(x) for field in ('Observatory', 'ObservatoryGroup', 'Instrument', 'InstrumentType') for x in row.get(field, []))
                if interval and (row['end'] < interval[0] or row['start'] > interval[1]):
                    continue
            if all(t in haystack.casefold() for t in terms):
                matches.append((key, row))
        matches.sort(key=lambda pair: pair[0])
        results = []
        for key, row in matches[(page - 1) * 10:page * 10]:
            if source == 'gwosc':
                results.append(_candidate(source, key, row['name'] + ' · v' + str(row['version']) + ' · ' + row['catalog'],
                    'GWOSC公开事件/候选/注入记录的完整应变文件；类别以发布目录为准，探测器、GPS时间窗口与采样率见文件清单。', GWOSC + '/api/v2/event-versions/' + key,
                    license=GW_LICENSE, formats=['HDF5'], metadata={'version': row['version'], 'catalog': row['catalog']},
                    coverage={'scope': '该发布版本公开的事件应变时间窗口，不是整轮观测运行'}))
            else:
                start, end = interval or [row['start'].replace(microsecond=0), (row['end'] + timedelta(seconds=bool(row['end'].microsecond))).replace(microsecond=0)]
                # Preserve a user-specified interval, including edges beyond data availability.
                external = '|'.join((key, _basic(start), _basic(end)))
                results.append(_candidate(source, external, key + ' · ' + row['Label'],
                    '完整原始CDF文件；UTC ' + start.isoformat() + ' — ' + end.isoformat() + '；文件可能跨越查询边界。',
                    CDAS + '?id=' + key, license=CDAS_LICENSE, formats=['CDF'], doi=row.get('Doi'),
                    metadata={'requested_interval': bool(interval)},
                    coverage={'start': start.isoformat(), 'end': end.isoformat(), 'available_start': row['start'].isoformat(),
                              'available_end': row['end'].isoformat(), 'scope': '所示UTC区间相交的所有完整原始文件；保留原有缺测和质量标志'}))
        return {'results': results, 'total_count': len(matches)}

    async def resolve(self, candidate):
        from app.infrastructure.external.data_sources.open_data_sources import _identity, _filename
        source, external = candidate.get('source_id'), candidate.get('external_id')
        if source not in SOURCES or not isinstance(external, str):
            raise SourceConnectorError('invalid_candidate')
        options = []
        if source == 'gwosc':
            m = re.fullmatch('(' + EVENT + r')-v([1-9][0-9]*)', external)
            if not m:
                raise SourceConnectorError('invalid_candidate')
            detail = await self._json(GWOSC + '/api/v2/event-versions/' + external, {'format': 'json'})
            if detail.get('name') != m[1] or detail.get('version') != int(m[2]) or not re.fullmatch(r'[A-Za-z0-9_.-]+', str(detail.get('catalog', ''))):
                raise SourceConnectorError('invalid_response')
            old = candidate.get('metadata') or {}
            if old.get('catalog') is not None and old['catalog'] != detail['catalog']:
                raise SourceConnectorError('source_version_changed')
            files = await self._gw_pages('event-versions/' + external + '/strain-files')
            for file in files:
                if file.get('file_format') != 'HDF':
                    continue
                url = file.get('download_url')
                if not safe_file_url(source, url, external) or urlsplit(url).path.split('/')[3] != detail['catalog']:
                    raise SourceConnectorError('unsafe_download_url')
                gps, duration, rate, detector = (file.get(k) for k in ('gps_start', 'duration', 'sample_rate_kHz', 'detector'))
                if any(type(n) is not int or n <= 0 for n in (gps, duration, rate)) or not re.fullmatch(r'[A-Z][0-9]', str(detector)):
                    raise SourceConnectorError('invalid_response')
                filename = urlsplit(url).path.rsplit('/', 1)[-1]
                if not filename.startswith(detector[0] + '-' + detector + '_') or not filename.endswith('-' + str(gps) + '-' + str(duration) + '.hdf5'):
                    raise SourceConnectorError('invalid_response')
                options.append(dict(url=url, size=None, checksum=None, version=external + '@' + detail['catalog'],
                    license=GW_LICENSE, content_type='application/x-hdf5',
                    kind=f'完整应变文件 · {detector} · GPS {gps} · {duration}秒 · {rate}kHz',
                    coverage={'gps_start': gps, 'duration_seconds': duration, 'detector': detector, 'sample_rate_khz': rate}))
            record = GWOSC + '/api/v2/event-versions/' + external
        else:
            dataset, start, end = _cdas_external(external)
            data = await self._json(CDAS, {'id': dataset})
            descriptions = data.get('DatasetDescription') if isinstance(data, dict) else None
            if not isinstance(descriptions, list) or len(descriptions) != 1 or descriptions[0].get('Id') != dataset:
                raise SourceConnectorError('invalid_response')
            payload = await self._json(CDAS + '/' + dataset + '/orig_data/' + start + ',' + end)
            files = payload.get('FileDescription') if isinstance(payload, dict) else None
            if not isinstance(files, list):
                raise SourceConnectorError('invalid_response')
            begin, finish = (datetime.strptime(s, '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc) for s in (start, end))
            for file in files:
                url = file.get('Name')
                if not safe_file_url(source, url, external):
                    # Only files whose official names bind them to this dataset are supported.
                    raise SourceConnectorError('unsafe_download_url')
                try:
                    first, last = _utc(file.get('StartTime')), _utc(file.get('EndTime'))
                except SourceConnectorError:
                    raise SourceConnectorError('invalid_response') from None
                if first > last or first > finish or last < begin or type(file.get('Length')) is not int or file['Length'] <= 0:
                    raise SourceConnectorError('invalid_response')
                filename = urlsplit(url).path.rsplit('/', 1)[-1]
                version = re.search(r'_v([0-9]+(?:\.[0-9]+)*)\.cdf$', filename)[1]
                options.append(dict(url=url, size=file['Length'], checksum=None, version=version + ' · ' + str(file.get('LastModified', '')),
                    license=CDAS_LICENSE, content_type='application/x-cdf', kind='完整原始CDF · ' + first.isoformat() + ' — ' + last.isoformat(),
                    coverage={'start': first.isoformat(), 'end': last.isoformat(), 'dataset_id': dataset}))
            record = CDAS + '?id=' + dataset
        urls = set()
        for option in options:
            url = option['url']
            if url in urls:
                raise SourceConnectorError('invalid_response')
            urls.add(url)
            filename = urlsplit(url).path.rsplit('/', 1)[-1]
            option.update(id=_identity(source, external, url, option['version']), source_id=source, external_id=external,
                          filename=_filename(filename), source_filename=filename, record_url=record, available=True, reason='')
        return options
