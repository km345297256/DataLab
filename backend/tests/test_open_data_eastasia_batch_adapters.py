from copy import deepcopy

import httpx
import pytest

from app.domain.external.data_source import SourceConnectorError
from app.infrastructure.external.data_sources.open_data_eastasia_batch_adapters import EastAsiaBatchAdapters, safe_file_url
from app.infrastructure.external.data_sources.open_data_sources import OpenDataSources

HK_ID = 'hk-hko-rss-tropical-cyclone-best-track-data'
HK_URL = 'https://data.weather.gov.hk/weatherAPI/hko_data/tc/HKO2024BST.csv'
TW_URL = 'https://ws.kinmen.gov.tw/001/Upload/0/relfile/0/0/9983a916-9d79-4a4e-80a0-171cb6ed2cf0.csv'


def hk():
    return {'name': HK_ID, 'title': 'Tropical cyclone best track', 'private': False, 'state': 'active',
            'metadata_modified': '2026-10-01', 'resources': [
                {'url': HK_URL, 'format': 'CSV', 'state': 'active', 'is_api': 'N'},
                {'url': 'https://evil.example/data.csv', 'format': 'CSV', 'state': 'active'},
                {'url': HK_URL, 'format': 'PDF', 'state': 'active'},
            ]}


def tw():
    return {'datasetId': 178492, 'title': '人口統計', 'cost': 'free', 'license': '1',
            'modifiedDate': '2026-10-01', 'distribution': [{'resourceFormat': 'CSV', 'resourceDownloadUrl': TW_URL}]}


@pytest.mark.asyncio
async def test_large_distribution_manifest_can_resolve_its_final_data_file():
    row = tw()
    row['distribution'] = [{'resourceFormat': 'PDF', 'resourceDownloadUrl': TW_URL}] * 10000 + row['distribution']
    async def fetch(*args, **kwargs):
        return {'success': True, 'result': row}
    options = await EastAsiaBatchAdapters(fetch).resolve({'source_id': 'taiwan-data', 'external_id': '178492'})
    assert len(options) == 1 and options[0]['url'] == TW_URL


@pytest.mark.parametrize('source,url', [
    ('hongkong-data', HK_URL), ('taiwan-data', TW_URL),
    ('taiwan-data', 'https://data.moi.gov.tw/MoiOD/System/DownloadFile.aspx?DATA=9983a916-9d79-4a4e-80a0-171cb6ed2cf0'),
])
def test_registered_original_file_routes(source, url):
    assert safe_file_url(source, url)


@pytest.mark.parametrize('url', [
    HK_URL.replace('https:', 'http:'), HK_URL.replace('data.weather.gov.hk', 'data.weather.gov.hk.evil.test'),
    HK_URL.replace('https://', 'https://user:pass@'), HK_URL + '#fragment', HK_URL + '?redirect=https://evil.test',
    HK_URL.replace('/tc/', '/tc/../'), HK_URL.replace('/tc/', '/tc/%2e%2e/'),
    HK_URL.replace('/tc/', '/tc/%2f'), 'https://127.0.0.1/weatherAPI/hko_data/a.csv',
    HK_URL.replace('data.weather.gov.hk', 'data.weather.gov.hk:444'), HK_URL.replace('.csv', '.html'),
    'https://data.gov.hk/filestore/README.html',
])
def test_untrusted_file_routes_are_rejected(url):
    assert not safe_file_url('hongkong-data', url)


@pytest.mark.asyncio
async def test_hk_catalog_lookup_matches_all_terms_and_caches_only_ids():
    calls = []
    async def get(url, params=None, **kwargs):
        calls.append((url, params))
        if url.endswith('package_list'):
            return {'success': True, 'result': [HK_ID, 'hk-example-population']}
        return {'success': True, 'result': hk()}
    adapter = EastAsiaBatchAdapters(get)
    page = await adapter.search('hongkong-data', 'tropical cyclone', 1)
    assert [row['external_id'] for row in page['results']] == [HK_ID]
    assert await adapter.search('hongkong-data', 'tropical population', 1) == {'results': [], 'total_count': 0}
    assert sum(url.endswith('package_list') for url, _ in calls) == 1
    options = await adapter.resolve(page['results'][0])
    assert len(options) == 1 and options[0]['url'] == HK_URL


@pytest.mark.asyncio
async def test_taiwan_keyword_and_pagination_use_official_read_only_body():
    async def get(url, params=None, **kwargs):
        assert url == 'https://data.gov.tw/api/front/dataset/list'
        assert kwargs == {'method': 'POST', 'json': {'page_num': 2, 'page_limit': 10,
                                                   'bool': [{'fulltext': {'value': '人口', 'operator': 'AND'}}]}}
        return {'success': True, 'code': 200, 'payload': {'search_count': 11, 'search_result': [
            {'nid': 178492, 'title': '人口統計', 'status': 1, 'dataset_status': 'c01', 'charge': '免費'},
            {'nid': 178493, 'title': '付費資料', 'status': 1, 'dataset_status': 'c01', 'charge': '收費'},
        ]}}
    result = await EastAsiaBatchAdapters(get).search('taiwan-data', '人口', 2)
    assert result['total_count'] == 11
    assert [row['external_id'] for row in result['results']] == ['178492']


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation', ['private', 'inactive', 'version', 'identity'])
async def test_hk_rechecks_access_and_record_version(mutation):
    row = hk()
    if mutation == 'private': row['private'] = True
    if mutation == 'inactive': row['state'] = 'deleted'
    if mutation == 'version': row['metadata_modified'] = '2026-10-02'
    if mutation == 'identity': row['name'] = 'other-source'
    async def get(*args, **kwargs): return {'success': True, 'result': row}
    candidate = {'source_id': 'hongkong-data', 'external_id': HK_ID, 'metadata': {'version': '2026-10-01'}}
    if mutation in {'version', 'identity'}:
        with pytest.raises(SourceConnectorError): await EastAsiaBatchAdapters(get).resolve(candidate)
    else:
        assert await EastAsiaBatchAdapters(get).resolve(candidate) == []


@pytest.mark.asyncio
async def test_taiwan_paid_unlicensed_and_document_resources_never_become_files():
    row = tw()
    async def get(*args, **kwargs): return {'success': True, 'result': deepcopy(row)}
    adapter = EastAsiaBatchAdapters(get)
    candidate = {'source_id': 'taiwan-data', 'external_id': '178492'}
    assert len(await adapter.resolve(candidate)) == 1
    row['cost'] = 'paid'
    assert await adapter.resolve(candidate) == []
    row['cost'] = 'free'; row['license'] = ''
    assert await adapter.resolve(candidate) == []
    row['license'] = '1'; row['distribution'][0]['resourceFormat'] = 'PDF'
    assert await adapter.resolve(candidate) == []


@pytest.mark.asyncio
async def test_facade_download_reloads_manifest_and_ignores_forged_client_url():
    body = b'year,population\n2025,12000\n'
    async def handler(request):
        if request.url.host == 'data.gov.tw':
            return httpx.Response(200, json={'success': True, 'result': tw()})
        assert str(request.url) == TW_URL
        return httpx.Response(200, content=body, headers={'content-type': 'text/csv'})
    sources = OpenDataSources(transport=httpx.MockTransport(handler))
    candidate = {'source_id': 'taiwan-data', 'external_id': '178492', 'source_url': 'https://data.gov.tw/dataset/178492'}
    options = await sources.verify(candidate)
    assert len(options) == 1
    forged = {**options[0], 'url': 'http://127.0.0.1/private', 'size': 9999, 'filename': 'forged.exe'}
    stream, name, mime, receipt = await sources.download(candidate, forged)
    try:
        assert stream.read() == body
        assert name.endswith('.csv') and mime == 'text/csv'
        assert receipt['connector_id'] == 'taiwan-data'
        assert receipt['source_version'] == '2026-10-01'
    finally:
        stream.close()


@pytest.mark.asyncio
async def test_facade_rejects_redirect_to_unregistered_host():
    async def handler(request):
        if request.url.host == 'data.gov.tw':
            return httpx.Response(200, json={'success': True, 'result': tw()})
        return httpx.Response(302, headers={'location': 'https://evil.example/data.csv'})
    sources = OpenDataSources(transport=httpx.MockTransport(handler))
    candidate = {'source_id': 'taiwan-data', 'external_id': '178492'}
    option = (await sources.resolve(candidate))[0]
    with pytest.raises(SourceConnectorError):
        await sources.download(candidate, option)
