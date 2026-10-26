"""Offline contracts for public ML repository files, versions and redirects."""
import hashlib

import httpx
import pytest

from app.domain.external.data_source import SourceConnectorError
from app.infrastructure.external.data_sources.open_data_sources import OpenDataSources
from app.infrastructure.external.data_sources.open_data_ml_expansion_adapters import safe_file_url

COMMIT = 'a' * 40
EXTERNAL = 'example/iris@' + COMMIT
DATA = b'sepal_length,species\n5.1,setosa\n'


def manifest(**updates):
    return dict(id='example/iris', sha=COMMIT, private=False, gated=False,
                siblings=[dict(rfilename='README.md', size=100), dict(rfilename='data/train.csv', size=len(DATA),
                          lfs=dict(size=len(DATA), sha256=hashlib.sha256(DATA).hexdigest()))], **updates)


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setenv('OPEN_DATA_DOWNLOAD_ROOT', str(tmp_path))
    from app.infrastructure.external.data_sources import open_data_sources
    monkeypatch.setattr(open_data_sources, 'CATALOG', (*open_data_sources.CATALOG, {'id': 'huggingface-datasets', 'title': 'Hugging Face'}))


@pytest.mark.asyncio
async def test_uci_dynamic_catalog_literal_name_and_fresh_csv():
    calls = []
    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith('/list'):
            return httpx.Response(200, json={'status': 200, 'data': [{'id': 53, 'name': 'Iris'}, {'id': 9, 'name': 'Auto MPG'}]})
        if request.url.path == '/api/dataset':
            assert request.url.params['id'] == '53'
            return httpx.Response(200, json={'status': 200, 'data': {'uci_id': 53, 'data_url': 'https://archive.ics.uci.edu/static/public/53/data.csv', 'last_updated': '2023-09-12'}})
        assert request.url.path == '/static/public/53/data.csv'
        return httpx.Response(200, content=DATA)
    source = OpenDataSources(transport=httpx.MockTransport(handler))
    result = await source.search('uci-ml', '"Iris"')
    candidate = result['results'][0]
    assert candidate['external_id'] == '53'
    await source.search('uci-ml', '53')
    assert calls.count('/api/datasets/list') == 1
    verified = await source.verify(candidate)
    assert len(verified) == 1
    forged = {**verified[0], 'url': 'http://localhost/private'}
    stream, name, _, receipt = await source.download(candidate, forged)
    try:
        assert stream.read() == DATA and name.endswith('.csv')
        assert receipt['sha256'] == hashlib.sha256(DATA).hexdigest()
    finally:
        stream.close()


@pytest.mark.asyncio
async def test_hf_pins_commit_excludes_gated_and_checks_lfs_download():
    def handler(request):
        if request.url.path == '/api/datasets':
            assert request.url.params['gated'] == 'false'
            return httpx.Response(200, json=[dict(id='example/iris', sha=COMMIT, private=False, gated=False),
                                            dict(id='example/restricted', sha=COMMIT, private=False, gated='auto')])
        if request.url.path.startswith('/api/datasets/'):
            assert request.url.path.endswith('/revision/' + COMMIT)
            assert request.url.params['blobs'] == 'true'
            return httpx.Response(200, json=manifest())
        if request.url.host == 'huggingface.co':
            return httpx.Response(302, headers={'location': 'https://us.aws.cdn.hf.co/xet-bridge-us/123/abc?Expires=123&Signature=example'})
        return httpx.Response(200, content=DATA)
    source = OpenDataSources(transport=httpx.MockTransport(handler))
    result = await source.search('huggingface-datasets', '"iris"')
    assert len(result['results']) == 1
    candidate = result['results'][0]
    options = await source.verify(candidate)
    assert len(options) == 1 and options[0]['version'] == COMMIT
    stream, _, _, receipt = await source.download(candidate, options[0])
    try:
        assert stream.read() == DATA and receipt['provider_checksum_verified'] is True
    finally:
        stream.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [{'sha': 'b'*40}, {'id': 'other/repo'}, {'gated': 'auto'}, {'private': True}])
async def test_hf_version_mismatch_or_restriction_not_available(change):
    row = manifest(); row.update(change)
    source = OpenDataSources(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=row)))
    if 'sha' in change or 'id' in change:
        with pytest.raises(SourceConnectorError, match='invalid_response'):
            await source.resolve({'source_id': 'huggingface-datasets', 'external_id': EXTERNAL})
    else:
        assert await source.resolve({'source_id': 'huggingface-datasets', 'external_id': EXTERNAL}) == []


@pytest.mark.asyncio
async def test_hf_git_blob_hash_is_not_a_raw_file_checksum():
    row = manifest()
    row['siblings'] = [dict(rfilename='train.csv', size=len(DATA), blobId='b'*40),
                       dict(rfilename='dataset_info.json', size=100), dict(rfilename='load.py', size=100),
                       dict(rfilename='../escape.csv', size=100), dict(rfilename='model.safetensors', size=100)]
    source = OpenDataSources(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=row)))
    options = await source.resolve({'source_id': 'huggingface-datasets', 'external_id': EXTERNAL})
    assert len(options) == 1 and options[0]['checksum'] is None


@pytest.mark.asyncio
async def test_hf_plaintext_corpus_is_data_but_model_configuration_is_not():
    row = manifest()
    row['siblings'] = [dict(rfilename=name, size=100) for name in [
        'data/train.txt', 'dataset_infos.json', 'generation_config.json', 'vocab.json',
        'merges.txt', 'requirements.txt', 'dataset_dict.json', 'preprocessor_config.json',
        'added_tokens.json', 'state.json', 'model.safetensors.index.json']]
    source = OpenDataSources(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=row)))
    options = await source.resolve({'source_id': 'huggingface-datasets', 'external_id': EXTERNAL})
    assert [option['source_filename'] for option in options] == ['data/train.txt']


def test_hf_url_rejects_encoded_del_character():
    assert not safe_file_url('huggingface-datasets', f'https://huggingface.co/datasets/example/iris/resolve/{COMMIT}/data%7f.csv', EXTERNAL)


@pytest.mark.asyncio
async def test_hf_pinned_commit_keeps_all_file_shards_beyond_one_hundred():
    row = manifest()
    row['siblings'] = [dict(rfilename=f'train/part-{i}.parquet', size=1000) for i in range(125)]
    source = OpenDataSources(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=row)))
    options = await source.resolve({'source_id': 'huggingface-datasets', 'external_id': EXTERNAL})
    assert len(options) == 125 and len({item['id'] for item in options}) == 125
    assert options[-1]['source_filename'] == 'train/part-124.parquet'


@pytest.mark.parametrize('url', [
    'https://huggingface.co/datasets/example/iris/resolve/main/data.csv',
    f'https://huggingface.co/datasets/other/repo/resolve/{COMMIT}/data.csv',
    f'https://huggingface.co/datasets/example/iris/resolve/{COMMIT}/%2e%2e/secret.csv',
    f'https://huggingface.co/datasets/example/iris/resolve/{COMMIT}/data.csv?redirect=https://localhost',
    'https://us.aws.cdn.hf.co.evil.test/data.csv?Signature=x',
    'https://user:pass@us.aws.cdn.hf.co/data.csv?Signature=x',
    'http://us.aws.cdn.hf.co/data.csv?Signature=x',
    'https://us.aws.cdn.hf.co:444/data.csv?Signature=x',
    'https://localhost/data.csv',
])
def test_hf_rejects_untrusted_routes(url):
    assert not safe_file_url('huggingface-datasets', url, EXTERNAL)


def test_hf_official_cache_redirect_bound_to_same_commit():
    assert safe_file_url('huggingface-datasets', f'https://huggingface.co/api/resolve-cache/datasets/example/iris/{COMMIT}/data.csv?etag=%22abc%22', EXTERNAL)
    assert not safe_file_url('uci-ml', 'https://archive.ics.uci.edu/static/public/54/data.csv', '53')


@pytest.mark.asyncio
async def test_hf_pagination_does_not_hide_later_public_matches():
    def handler(request):
        assert request.url.params['limit'] == '21'
        rows = [dict(id=f'example/data{i}', sha=COMMIT, private=False, gated=False) for i in range(21)]
        return httpx.Response(200, json=rows)
    from app.infrastructure.external.data_sources.open_data_ml_expansion_adapters import MLExpansionAdapters
    source = OpenDataSources(transport=httpx.MockTransport(handler))
    result = await MLExpansionAdapters(source._json).search('huggingface-datasets', 'test', 2)
    assert len(result['results']) == 10 and result['total_count'] == 21
    assert result['results'][0]['external_id'].startswith('example/data10@')
