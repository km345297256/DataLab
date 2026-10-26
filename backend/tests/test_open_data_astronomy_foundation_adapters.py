"""Entity selection, pagination completeness, access checks and URL boundaries."""
import gzip
import asyncio
import io
import json
import threading

import pytest

from app.domain.external.data_source import SourceConnectorError
from app.infrastructure.external.data_sources.open_data_astronomy_foundation_adapters import (
    AstronomyFoundationAdapters, CDS, DOWNLOAD, _cds_schema, _mast_filters,
    is_astronomy_sample, safe_file_url, validate_astronomy_stream,
)

SCHEMA = '''II/131 Pleiades
File Summary:
 FileName    Lrecl Records Explanations
catalog.dat  120   1518   The Catalog of the Pleiades Flare stars
Byte-by-byte Description of file: catalog.dat
 2- 4 I3 --- TonF Serial number
 21-22 I2 h RAh Right ascension
 24-27 F4.1 min RAm Right ascension
'''
LISTING = '<a href="catalog.dat.gz">catalog.dat.gz</a> 01-Feb-2026 12:30 18K'
CDS_ENTITY = 'https://cdsarc.cds.unistra.fr/ftp/II/131/catalog.dat.gz'
URI = 'mast:IUE/url/pub/iue/data/lwp/13000/lwp13058.mxlo.gz'
from urllib.parse import urlencode
MAST_ENTITY = DOWNLOAD + '?' + urlencode({'uri': URI})


def mast_payload(rows, page=1, size=500, total=None):
    total = len(rows) if total is None else total
    return dict(status='COMPLETE', data=rows, paging=dict(page=page, pageSize=size,
        pagesFiltered=(total + size - 1)//size, rows=len(rows), rowsFiltered=total))


def product(uri=URI, **kw):
    row = dict(obsID=282672, parent_obsid=282672, dataRights='PUBLIC', productType='SCIENCE',
               dataURI=uri, productFilename=uri.rsplit('/', 1)[-1], size=1234, prvversion='1.0')
    row.update(kw)
    return row


@pytest.mark.parametrize('source,url,external', [('cds-vizier', CDS_ENTITY, 'II/131/catalog'), ('mast', MAST_ENTITY, '282672')])
def test_only_official_entity_urls(source, url, external):
    assert safe_file_url(source, url, external)
    for altered in (url.replace('https://', 'http://'), url.replace('https://', 'https://evil.test/'),
                    url.replace('https://', 'https://user@'), url + '#part', url + '\n', url.replace('/ftp/', '/ftp/%2e%2e/')):
        if altered != url:
            assert not safe_file_url(source, altered, external)
    assert not safe_file_url(source, url, '../foreign')


def test_no_arbitrary_embedded_urls_or_pagination_in_download():
    for uri in ('http://localhost/data.fits', 'mast:HST/product/../secret.fits', 'mast:HST/product/a.html', 'mast:HST/url/pub//a.fits'):
        assert not safe_file_url('mast', DOWNLOAD + '?' + urlencode({'uri': uri}), '282672')
    assert not safe_file_url('mast', MAST_ENTITY + '&uri=' + URI, '282672')
    assert not safe_file_url('cds-vizier', CDS_ENTITY, 'II/131/notes')
    assert not safe_file_url('cds-vizier', CDS_ENTITY.replace('catalog.dat.gz', 'ReadMe'), 'II/131/catalog')


def test_samples_validate_content_with_bounded_gzip_expansion():
    fits = b'SIMPLE  =                    T' + b' ' * 50 + b'BITPIX  =                   16' + b' ' * 50 + b'NAXIS   =                    2'
    assert is_astronomy_sample(fits, {'source_id': 'mast'})
    assert is_astronomy_sample(gzip.compress(fits), {'source_id': 'mast'})
    assert is_astronomy_sample(gzip.compress(b'  12  22.5   17.0\n'), {'source_id': 'cds-vizier'})
    for data in (b'<html>123 login</html>', b'{"status":"ERROR"}', b'', gzip.compress(b'<html>no access</html>'), b'\x1f\x8bBAD'):
        assert not is_astronomy_sample(data, {'source_id': 'mast'})
        assert not is_astronomy_sample(data, {'source_id': 'cds-vizier'})
    assert not is_astronomy_sample(gzip.compress(b'x' * 1000000), {'source_id': 'mast'})


def test_schema_requires_real_numeric_measurements_not_references():
    assert _cds_schema(SCHEMA, 'catalog.dat.gz')['records'] == 1518
    assert _cds_schema(SCHEMA.replace('The Catalog of the Pleiades Flare stars', 'References to papers'), 'catalog.dat.gz') is None
    assert _cds_schema(SCHEMA.replace('I2 h', 'A2 h').replace('F4.1 min', 'A4 min'), 'catalog.dat.gz') is None
    assert _cds_schema(SCHEMA.replace('1518', '0'), 'catalog.dat.gz') is None


@pytest.mark.asyncio
async def test_cds_retrieves_original_whole_table_not_tap_data_export():
    calls = []
    async def js(url, params):
        calls.append(params['QUERY'])
        if 'COUNT(*)' in params['QUERY']:
            return {'metadata': [{'name': 'COUNT_ALL'}], 'data': [[1]]}
        return {'metadata': [{'name': 'table_name'}, {'name': 'description'}], 'data': [['"II/131/catalog"', 'Pleiades flare measurements']]}
    async def text(url):
        return SCHEMA if url.endswith('ReadMe') else LISTING
    a = AstronomyFoundationAdapters(js, text)
    c = (await a.search('cds-vizier', 'Pleiades', 1))['results'][0]
    f = (await a.resolve(c))[0]
    assert f['url'] == CDS_ENTITY and f['size'] is None
    assert f['coverage']['records'] == 1518
    assert 'ReadMe sha256:' in f['version']
    assert all('TAP_SCHEMA.tables' in call for call in calls)
    assert f['record_url'] == CDS + 'II/131/ReadMe'


@pytest.mark.asyncio
async def test_cds_paginates_metadata_only_and_detects_races():
    async def js(url, params):
        if 'COUNT(*)' in params['QUERY']:
            return {'metadata': [{'name': 'COUNT_ALL'}], 'data': [[22]]}
        assert 'TOP 20 ' in params['QUERY']
        return {'metadata': [{'name': 'table_name'}, {'name': 'description'}], 'data': [[f'"II/131/t{i}"', 'Pleiades'] for i in range(20)]}
    a = AstronomyFoundationAdapters(js, None)
    result = await a.search('cds-vizier', 'Pleiades', 2)
    assert result['total_count'] == 22 and len(result['results']) == 10
    async def corrupt(url, params):
        p = await js(url, params)
        if 'COUNT(*)' not in params['QUERY']:
            p['data'].pop()
        return p
    with pytest.raises(SourceConnectorError, match='inconsistent_source_snapshot'):
        await AstronomyFoundationAdapters(corrupt, None).search('cds-vizier', 'Pleiades', 2)


@pytest.mark.parametrize('query', ["x' OR 1=1", 'mission=IUE mission=HST', 'target=*', 'target=', 'unknown=x', 'target="unterminated'])
async def test_bad_query_never_sends_network(query):
    def no_network(*args):
        pytest.fail('bad query reached network')
    a = AstronomyFoundationAdapters(no_network, no_network)
    with pytest.raises(SourceConnectorError, match='invalid_query'):
        await a.search('mast', query, 1)


def test_mast_exact_target_and_explicit_observation_scope():
    assert _mast_filters('M101')[0] == {'paramName': 'target_name', 'values': ['M101']}
    assert _mast_filters('mission=IUE observation=lwp13058')[:2] == [
        {'paramName': 'obs_collection', 'values': ['IUE']}, {'paramName': 'obs_id', 'values': ['lwp13058']}]
    assert _mast_filters('target="NGC 1234"')[0]['values'] == ['NGC 1234']


@pytest.mark.asyncio
async def test_mast_only_public_direct_science_entities_and_actual_files():
    async def js(url, params):
        r = json.loads(params['request'])
        if r['service'] == 'Mast.Caom.Filtered':
            return mast_payload([dict(obsid=282672, dataRights='PUBLIC', obs_id='lwp13058')], size=10)
        return mast_payload([
            product(), product(URI.replace('mxlo', 'mxhi'), dataRights='EXCLUSIVE_ACCESS'),
            product(URI.replace('mxlo', 'raw'), productType='AUXILIARY'),
            product(URI.replace('13058', '13059'), obsID=282673),
            product('mast:IUE/url/pub/data/preview.jpg'),
        ])
    a = AstronomyFoundationAdapters(js, None)
    files = await a.resolve({'source_id': 'mast', 'external_id': '282672', 'source_url': 'https://archive.stsci.edu/'})
    assert len(files) == 1 and files[0]['url'] == 'https://archive.stsci.edu/pub/iue/data/lwp/13000/lwp13058.mxlo.gz' and files[0]['size'] == 1234


def test_mast_original_public_archive_alias_has_no_arbitrary_proxy():
    url = 'https://archive.stsci.edu/pub/iue/data/lwp/13000/lwp13058.mxlo.gz'
    assert safe_file_url('mast', url, '282672')
    for bad in (url.replace('/pub/', '/private/'), url + '?url=https://evil.test/data.fits',
                url.replace('/13000/', '/%2e%2e/'), url.replace('archive.stsci.edu', 'archive.stsci.edu.evil.test')):
        assert not safe_file_url('mast', bad, '282672')
    kepler = 'mast:KEPLER/url/missions/kepler/lightcurves/0118/011804465/kplr011804465-2009131105131_llc.fits'
    assert safe_file_url('mast', DOWNLOAD + '?' + urlencode({'uri': kepler}), '282672')


@pytest.mark.asyncio
async def test_mast_resolves_all_product_pages_without_first_100_file_cap():
    calls = []
    async def js(url, params):
        r = json.loads(params['request']); calls.append(r)
        if r['service'] == 'Mast.Caom.Filtered':
            return mast_payload([dict(obsid=282672, dataRights='PUBLIC')], size=10)
        page = r['page']; start = (page-1) * 500; stop = min(page*500, 503)
        return mast_payload([product(f'mast:HST/product/observation_{i}.fits', size=5_000_000_000) for i in range(start, stop)], page=page, total=503)
    files = await AstronomyFoundationAdapters(js, None).resolve({'source_id': 'mast', 'external_id': '282672'})
    assert len(files) == 503 and all(f['size'] == 5_000_000_000 for f in files)
    assert [r['page'] for r in calls if r['service'] == 'Mast.Caom.Products'] == [1, 2]


@pytest.mark.asyncio
async def test_mast_rechecks_public_access_and_rejects_partial_api_results():
    async def private(url, params):
        return mast_payload([], size=10)
    assert await AstronomyFoundationAdapters(private, None).resolve({'source_id': 'mast', 'external_id': '282672'}) == []
    async def executing(url, params):
        return {'status': 'EXECUTING'}
    with pytest.raises(SourceConnectorError, match='upstream_timeout'):
        await AstronomyFoundationAdapters(executing, None).search('mast', 'M101', 1)
    async def truncated(url, params):
        return mast_payload([{'obsid': 282672, 'dataRights': 'PUBLIC'}], size=10, total=2)
    with pytest.raises(SourceConnectorError, match='invalid_response'):
        await AstronomyFoundationAdapters(truncated, None).search('mast', 'M101', 1)


def test_entire_gzip_crc_and_eof_checked_not_only_valid_header():
    packed = gzip.compress(b'SIMPLE  =                    T' + b'a' * 180000)
    chosen = {'source_filename': 'spectrum.mxlo.gz'}
    stream = io.BytesIO(packed)
    validate_astronomy_stream('mast', stream, chosen)
    assert stream.tell() == 0 and not stream.closed
    for damaged in (packed[:-8], packed[:-8] + b'BAD!' + packed[-4:]):
        stream = io.BytesIO(damaged)
        with pytest.raises(SourceConnectorError, match='checksum_mismatch'):
            validate_astronomy_stream('mast', stream, chosen)
        assert stream.tell() == 0 and not stream.closed


def test_cds_declared_rows_count_nonblank_lines_across_buffers():
    data = b'1' + b' ' * 65534 + b'\r\n\n  \n2 last row without newline'
    for compressed in (False, True):
        stream = io.BytesIO(gzip.compress(data) if compressed else data)
        chosen = {'source_filename': 'catalog.dat' + ('.gz' if compressed else ''), 'coverage': {'records': 2}}
        validate_astronomy_stream('cds-vizier', stream, chosen)
        assert stream.tell() == 0
        with pytest.raises(SourceConnectorError, match='download_integrity_failed'):
            validate_astronomy_stream('cds-vizier', stream, {**chosen, 'coverage': {'records': 3}})
        assert stream.tell() == 0 and not stream.closed


def test_whole_stream_validator_checks_cancellation_and_rewinds():
    event = threading.Event()
    class CancellingStream(io.BytesIO):
        def read(self, size=-1):
            assert size == 64 * 1024
            chunk = super().read(size)
            event.set()
            return chunk
    stream = CancellingStream(b'1\n' * 100000)
    with pytest.raises(asyncio.CancelledError):
        validate_astronomy_stream('cds-vizier', stream, {'source_filename': 'catalog.dat', 'coverage': {'records': 100000}}, cancelled=event)
    assert stream.tell() == 0 and not stream.closed
