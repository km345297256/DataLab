import copy
import hashlib

import httpx
import pytest

from app.domain.external.data_source import SourceConnectorError
from app.infrastructure.external.data_sources.open_data_france_oct22_adapters import FranceOct22Adapters, safe_file_url
from app.infrastructure.external.data_sources.open_data_sources import OpenDataSources

ID = 'a' * 24
RID = '12345678-1234-1234-1234-123456789abc'
URL = 'https://static.data.gouv.fr/resources/example/20261022-123000/data.csv'
DATA = b'year,population\n2020,1234\n'


def record():
    return dict(id=ID, title='Population', description='Official data', private=False,
        access_type='open', deleted=None, archived=None, license='lov2', resources=[dict(id=RID,
        url=URL, filetype='file', format='csv', filesize=len(DATA), last_modified='2026-10-22',
        checksum={'type': 'sha1', 'value': hashlib.sha1(DATA).hexdigest()})])


@pytest.mark.asyncio
async def test_search_resolve_complete_download_and_forged_url_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv('OPEN_DATA_DOWNLOAD_ROOT', str(tmp_path))
    calls = []
    def handler(request):
        calls.append(str(request.url))
        if request.url.host == 'static.data.gouv.fr':
            return httpx.Response(200, content=DATA, headers={'Content-Type': 'text/csv'})
        if request.url.path.endswith('/' + ID + '/'):
            return httpx.Response(200, json=record())
        assert request.url.params['q'] == 'population' and request.url.params['page_size'] == '10'
        return httpx.Response(200, json={'data': [record()], 'total': 1})
    facade = OpenDataSources(transport=httpx.MockTransport(handler))
    adapter = FranceOct22Adapters(facade._json)
    result = await adapter.search('france-data-gouv', '"population"', 1)
    candidate = result['results'][0]
    options = await facade.verify(candidate)
    assert len(options) == 1
    stream, _, _, receipt = await facade.download(candidate, {**options[0], 'url': 'http://localhost/secrets'})
    try:
        assert stream.read() == DATA
        assert receipt['sha256'] == hashlib.sha256(DATA).hexdigest() and receipt['provider_checksum_verified']
    finally:
        stream.close()
    assert all('localhost' not in url for url in calls)


@pytest.mark.parametrize('change', [{'private': True}, {'deleted': '2020'}, {'archived': True}, {'access_type': 'restricted'}])
@pytest.mark.asyncio
async def test_restricted_metadata_is_not_data(change):
    row = record(); row.update(change)
    async def fetch(*args, **kwargs):
        return row
    assert await FranceOct22Adapters(fetch).resolve({'source_id': 'france-data-gouv', 'external_id': ID}) == []


@pytest.mark.asyncio
async def test_all_files_preserved_and_non_data_or_external_files_excluded():
    row = record(); original = row['resources'][0]
    row['resources'] = [{**copy.deepcopy(original), 'id': f'{i:08x}-1234-1234-1234-123456789abc',
        'url': URL.replace('data.csv', f'part-{i}.csv')} for i in range(125)]
    row['resources'] += [{**original, 'url': URL.replace('data.csv', 'readme.txt')},
                        {**original, 'url': 'https://external.invalid/data.csv'},
                        {**original, 'filetype': 'api'}, {**original, 'filesize': True},
                        *[{**original, 'type': kind, 'title': 'Lexique'} for kind in ['documentation', 'api', 'code']]]
    async def fetch(*args, **kwargs):
        return row
    assert len(await FranceOct22Adapters(fetch).resolve({'source_id': 'france-data-gouv', 'external_id': ID})) == 125


@pytest.mark.asyncio
async def test_manifest_id_and_checksum_must_be_valid():
    row = record(); row['resources'][0]['checksum']['value'] = 'bad'
    async def fetch(*args, **kwargs):
        return row
    with pytest.raises(SourceConnectorError, match='invalid_response'):
        await FranceOct22Adapters(fetch).resolve({'source_id': 'france-data-gouv', 'external_id': ID})
    row['id'] = 'b' * 24
    with pytest.raises(SourceConnectorError, match='invalid_response'):
        await FranceOct22Adapters(fetch).resolve({'source_id': 'france-data-gouv', 'external_id': ID})


@pytest.mark.parametrize('url', ['http://static.data.gouv.fr/resources/a/20261022-123000/a.csv',
    'https://static.data.gouv.fr.evil.invalid/resources/a/20261022-123000/a.csv',
    URL+'?redirect=http://localhost', URL+'#secret', URL.replace('/data.csv','/%2e%2e/secret'),
    URL.replace('static.data.gouv.fr','user:pass@static.data.gouv.fr'),
    URL.replace('data.csv','%00data.csv'), URL.replace('data.csv','%0ddata.csv'), URL.replace('data.csv','data%7f.csv'),
    URL.replace('/data.csv','/%252e%252e'), 'https://static.data.gouv.fr/admin/file.csv'])
def test_unsafe_hosts_paths_queries_rejected(url):
    assert not safe_file_url('france-data-gouv', url)


def test_old_and_current_hosted_file_routes_allowed():
    assert safe_file_url('france-data-gouv', URL)
    assert safe_file_url('france-data-gouv', 'https://www.data.gouv.fr/storage/f/2014-07-03T10-03-20/a.csv')


@pytest.mark.asyncio
async def test_publisher_replacement_changes_file_identity_without_timestamp_change():
    row = record()
    async def fetch(*args, **kwargs):
        return row
    adapter = FranceOct22Adapters(fetch)
    candidate = {'source_id': 'france-data-gouv', 'external_id': ID}
    original = (await adapter.resolve(candidate))[0]['id']
    row['resources'][0]['checksum']['value'] = 'b' * 40
    assert (await adapter.resolve(candidate))[0]['id'] != original
    row['resources'][0]['checksum']['value'] = hashlib.sha1(DATA).hexdigest()
    row['resources'][0]['filesize'] += 1
    assert (await adapter.resolve(candidate))[0]['id'] != original
