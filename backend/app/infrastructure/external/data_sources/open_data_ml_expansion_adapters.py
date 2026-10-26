"""UCI CSVs and commit-pinned public Hugging Face entity files."""
from __future__ import annotations

import asyncio
import mimetypes
import re
import time
from urllib.parse import parse_qsl, quote, unquote, urlsplit

from app.domain.external.data_source import SourceConnectorError

SOURCES = ('uci-ml', 'huggingface-datasets')
UCI = 'https://archive.ics.uci.edu'
HF = 'https://huggingface.co'
REPO = r'[A-Za-z0-9][A-Za-z0-9_.-]{0,95}/[A-Za-z0-9][A-Za-z0-9_.-]{0,95}'
HF_ID = REPO + r'@[a-f0-9]{40}'
DATA_EXTENSIONS = ('.csv', '.tsv', '.txt', '.json', '.jsonl', '.ndjson', '.parquet', '.arrow', '.sqlite', '.db',
                   '.zip', '.tar', '.gz', '.bz2', '.xz', '.zst', '.7z', '.h5', '.hdf5', '.npy', '.npz',
                   '.wav', '.flac', '.mp3', '.mp4', '.webm', '.jpg', '.jpeg', '.png', '.tif', '.tiff', '.webp')
CDN_HOSTS = frozenset({'cas-bridge.xethub.hf.co', 'cdn-lfs.huggingface.co', 'cdn-lfs-us-1.hf.co',
                       'cdn-lfs-eu-1.hf.co', 'us.aws.cdn.hf.co', 'us.gcp.cdn.hf.co'})


def _path(value):
    return (isinstance(value, str) and 0 < len(value) <= 1000 and '%' not in value and '\\' not in value
            and not value.startswith('/') and not any(ord(c) < 32 or ord(c) == 127 for c in value)
            and all(part not in {'', '.', '..'} for part in value.split('/')))


def safe_file_url(source, url, external=None):
    try:
        parsed = urlsplit(url)
        path = unquote(parsed.path)
        if (parsed.scheme != 'https' or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.fragment or not _path(path.lstrip('/'))
                or re.search(r'%2f|%5c|%2e', parsed.path, re.I)):
            return False
        if source == 'uci-ml':
            match = re.fullmatch(r'/static/public/([1-9][0-9]{0,8})/data\.csv', path)
            return parsed.hostname == 'archive.ics.uci.edu' and not parsed.query and bool(match) and (external is None or match[1] == external)
        if source != 'huggingface-datasets':
            return False
        if parsed.hostname in CDN_HOSTS:
            # Signed object destinations are accepted only after a trusted
            # manifest resolve URL redirects here; user URLs never reach this.
            return bool(re.fullmatch(r'/[A-Za-z0-9_./+=-]+', path)) and bool(parsed.query)
        if parsed.hostname != 'huggingface.co':
            return False
        match = re.fullmatch(r'/(?:datasets/(' + REPO + r')/resolve|api/resolve-cache/datasets/(' + REPO + r'))/([a-f0-9]{40})/(.+)', path)
        if not match or not _path(match[4]) or (external is not None and external != (match[1] or match[2]) + '@' + match[3]):
            return False
        expected = f'/datasets/{match[1] or match[2]}/resolve/{match[3]}/{match[4]}'
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        return len(pairs) == len(dict(pairs)) and all(key in {'download', 'etag', expected} for key, _ in pairs)
    except (ValueError, TypeError, AttributeError):
        return False


def _identifier(source, value):
    pattern = r'[1-9][0-9]{0,8}' if source == 'uci-ml' else HF_ID
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise SourceConnectorError('invalid_candidate')
    return value


def _public(row):
    return row.get('private') is False and row.get('gated') is False and not row.get('disabled', False)


def _license(row):
    return next((tag[8:] for tag in row.get('tags', []) if isinstance(tag, str) and tag.startswith('license:')), None)


def _option(source, external, url, filename, *, version=None, size=None, checksum=None, license=None):
    from app.infrastructure.external.data_sources.open_data_sources import _filename, _identity
    if not safe_file_url(source, url, external):
        raise SourceConnectorError('unsafe_download_url')
    return dict(id=_identity(source, external, version, url), source_id=source, external_id=external,
                url=url, filename=_filename(filename), source_filename=filename, size=size,
                checksum=checksum, version=version, license=license,
                record_url=f'{UCI}/dataset/{external}' if source == 'uci-ml' else f'{HF}/datasets/{external.split("@")[0]}/tree/{version}',
                content_type=mimetypes.guess_type(filename)[0] or 'application/octet-stream',
                available=True, reason='', kind='源站完整文件')


class MLExpansionAdapters:
    def __init__(self, fetch_json):
        self._json = fetch_json
        self._catalog = None
        self._catalog_at = 0
        self._lock = asyncio.Lock()

    async def _uci_catalog(self):
        async with self._lock:
            if self._catalog is None or time.monotonic() - self._catalog_at > 3600:
                payload = await self._json(f'{UCI}/api/datasets/list')
                if payload.get('status') != 200 or not isinstance(payload.get('data'), list):
                    raise SourceConnectorError('invalid_response')
                rows = payload['data']
                if any(type(row.get('id')) is not int or row['id'] < 1 or not isinstance(row.get('name'), str) for row in rows):
                    raise SourceConnectorError('invalid_response')
                self._catalog, self._catalog_at = rows, time.monotonic()
            return self._catalog

    async def search(self, source, query, page):
        from app.infrastructure.external.data_sources.open_data_sources import _candidate
        if source not in SOURCES:
            raise SourceConnectorError('unknown_source')
        if type(page) is not int or not 1 <= page <= 100:
            raise SourceConnectorError('invalid_cursor')
        query = query.strip()
        if re.fullmatch(r'"[^"\n]+"', query):
            query = query[1:-1]
        if not query:
            raise SourceConnectorError('invalid_query')
        if source == 'uci-ml':
            rows = [row for row in await self._uci_catalog() if str(row['id']) == query or all(term in row['name'].lower() for term in query.lower().split())]
            return dict(results=[_candidate(source, str(row['id']), row['name'], 'UCI 官方数据集', f'{UCI}/dataset/{row["id"]}') for row in rows[(page-1)*10:page*10]], total_count=len(rows))
        limit = min(page*10+1, 1000)
        rows = await self._json(f'{HF}/api/datasets', {'search': query, 'limit': limit, 'sort': 'downloads', 'direction': -1, 'gated': 'false'})
        if not isinstance(rows, list) or len(rows) > limit:
            raise SourceConnectorError('invalid_response')
        results = []
        for row in rows[(page-1)*10:page*10]:
            if not _public(row):
                continue
            external = _identifier(source, str(row.get('id', '')) + '@' + str(row.get('sha', '')))
            results.append(_candidate(source, external, row['id'], str(row.get('description', ''))[:5000],
                                      f'{HF}/datasets/{row["id"]}', license=_license(row), metadata={'commit': row['sha']}))
        return dict(results=results, total_count=len(rows))

    async def resolve(self, candidate):
        source = candidate['source_id']
        if source not in SOURCES:
            raise SourceConnectorError('unknown_source')
        external = _identifier(source, candidate['external_id'])
        if source == 'uci-ml':
            payload = await self._json(f'{UCI}/api/dataset', {'id': external})
            if payload.get('status') != 200 or not isinstance(payload.get('data'), dict):
                return []
            row = payload['data']
            if str(row.get('uci_id')) != external:
                raise SourceConnectorError('invalid_response')
            url = row.get('data_url')
            if not url or not safe_file_url(source, url, external):
                return []
            return [_option(source, external, url, f'uci_{external}_data.csv', version=str(row.get('last_updated') or '') or None)]
        repo, commit = external.split('@')
        row = await self._json(f'{HF}/api/datasets/{repo}/revision/{commit}', {'blobs': 'true'})
        if row.get('id') != repo or row.get('sha') != commit:
            raise SourceConnectorError('invalid_response')
        if not _public(row):
            return []
        siblings = row.get('siblings')
        if not isinstance(siblings, list):
            raise SourceConnectorError('invalid_response')
        options = []
        for file in siblings:
            path = file.get('rfilename')
            if not _path(path) or not path.lower().endswith(DATA_EXTENSIONS):
                continue
            if re.match(r'(readme|licen[cs]e|metadata|dataset_infos?|dataset_dict|config|generation_config|'
                        r'tokenizer|special_tokens_map|added_tokens|vocab|merges|preprocessor_config|'
                        r'processor_config|feature_extractor|requirements|environment|schema|state)([._-]|$)',
                        path.rsplit('/', 1)[-1], re.I) or path.lower().endswith(('.safetensors.index.json', '.bin.index.json')):
                continue
            size = file.get('size')
            if type(size) is not int or size <= 0:
                continue
            lfs = file.get('lfs')
            checksum = None
            if lfs:
                digest = lfs.get('sha256')
                if lfs.get('size') != size or not isinstance(digest, str) or not re.fullmatch(r'[a-f0-9]{64}', digest):
                    raise SourceConnectorError('invalid_response')
                checksum = 'sha256:' + digest
            # Git blob IDs hash a header plus file bytes, not the raw file.
            options.append(_option(source, external, f'{HF}/datasets/{repo}/resolve/{commit}/{quote(path, safe="/")}',
                                   path, version=commit, size=size, checksum=checksum, license=_license(row)))
        return options
