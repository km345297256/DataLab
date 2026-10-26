"""Large public catalogues must not silently widen all metadata reads."""
import httpx
import pytest

from app.domain.external.data_source import SourceConnectorError
from app.infrastructure.external.data_sources import open_data_sources as module


@pytest.mark.asyncio
async def test_station_catalogue_allowance_keeps_default_metadata_reads_bounded(monkeypatch):
    monkeypatch.setattr(module, 'MAX_METADATA_BYTES', 8)
    sources = module.OpenDataSources(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b'x' * 20)))
    url = 'https://www.ncei.noaa.gov/pub/data/ghcn/daily/ghcnd-stations.txt'
    with pytest.raises(SourceConnectorError, match='response_too_large'):
        await sources._text_metadata(url)
    assert await sources._text_metadata(url, max_bytes=32) == 'x' * 20
    with pytest.raises(SourceConnectorError, match='response_too_large'):
        await sources._text_metadata(url, max_bytes=16)


@pytest.mark.asyncio
@pytest.mark.parametrize('url,params,limit', [
    ('https://example.test/catalog', None, 32),
    ('https://www.ncei.noaa.gov/pub/data/ghcn/daily/ghcnd-stations.txt', {'other': 'catalog'}, 32),
    ('https://www.ncei.noaa.gov/pub/data/ghcn/daily/ghcnd-stations.txt', None, 33 * 1024 * 1024),
])
async def test_larger_allowance_is_confined_to_one_trusted_catalogue(monkeypatch, url, params, limit):
    monkeypatch.setattr(module, 'MAX_METADATA_BYTES', 8)
    sources = module.OpenDataSources(transport=httpx.MockTransport(lambda request: pytest.fail('unexpected network')))
    with pytest.raises(SourceConnectorError, match='invalid_response'):
        await sources._text_metadata(url, params, max_bytes=limit)
