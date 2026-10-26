import httpx
import pytest

from app.infrastructure.external.data_sources import open_data_proxy
from app.infrastructure.external.data_sources.open_data_sources import OpenDataSources


def test_no_implicit_environment_proxy(monkeypatch):
    monkeypatch.delenv('OPEN_DATA_HUGGINGFACE_PROXY', raising=False)
    monkeypatch.setenv('HTTPS_PROXY', 'http://unrelated-proxy.invalid:1234')
    assert open_data_proxy.huggingface_proxy_mounts() == {}


@pytest.mark.parametrize('value', ['socks5://proxy:1', 'http://', 'http://proxy:0',
    'http://proxy:invalid', 'http://proxy/path', 'http://proxy?secret=1',
    'http://user:secret@proxy#fragment', 'http://proxy\n.invalid'])
def test_invalid_proxy_fails_without_disclosing_value(monkeypatch, value):
    monkeypatch.setenv('OPEN_DATA_HUGGINGFACE_PROXY', value)
    with pytest.raises(ValueError) as error:
        open_data_proxy.huggingface_proxy_mounts()
    assert str(error.value) == 'Invalid OPEN_DATA_HUGGINGFACE_PROXY configuration'


@pytest.mark.asyncio
async def test_only_exact_official_https_hosts_use_proxy(monkeypatch):
    monkeypatch.setenv('OPEN_DATA_HUGGINGFACE_PROXY', 'http://user:secret@proxy.invalid:7890')
    created = []
    def transport(**kwargs):
        created.append(kwargs)
        return httpx.MockTransport(lambda request: httpx.Response(200, text='proxy'))
    monkeypatch.setattr(open_data_proxy.httpx, 'AsyncHTTPTransport', transport)
    direct = httpx.MockTransport(lambda request: httpx.Response(200, text='direct'))
    async with httpx.AsyncClient(transport=direct, mounts=open_data_proxy.huggingface_proxy_mounts(), trust_env=False) as client:
        for host in open_data_proxy.HF_PROXY_HOSTS:
            assert (await client.get(f'https://{host}/test')).text == 'proxy'
        for url in ['https://huggingface.co.attacker.invalid/a', 'https://private.huggingface.co/a',
                    'http://huggingface.co/a', 'https://zenodo.org/a', 'https://localhost/a']:
            assert (await client.get(url)).text == 'direct'
    assert created == [{'proxy': 'http://user:secret@proxy.invalid:7890', 'trust_env': False}]


@pytest.mark.asyncio
async def test_injected_transport_remains_offline_even_when_proxy_configured(monkeypatch):
    monkeypatch.setenv('OPEN_DATA_HUGGINGFACE_PROXY', 'not-a-valid-proxy')
    source = OpenDataSources(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={'offline': True})))
    assert await source._json('https://huggingface.co/api/datasets') == {'offline': True}
