"""Entity identity, complete catalogs and scientific-format contracts."""
import base64
import struct
from urllib.parse import urlencode
import httpx
import pytest
from app.domain.external.data_source import SourceConnectorError
from app.infrastructure.external.data_sources.open_data_sources import OpenDataSources
from app.infrastructure.external.data_sources.open_data_earth_oct23_adapters import (
    EarthOct23Adapters, SOURCES, TDAT, WC, WC_BASE, _votable, _ne_catalog,
    _worldclim_catalog, _station_params, is_earthscope_sample, safe_file_url,
)
PARAMS=dict(net='IU',sta='ANMO',loc='00',cha='BHZ',start='2020-01-01T00:00:00',end='2020-01-01T00:01:00')
EXTERNAL=urlencode(PARAMS)
WAVE_URL='https://service.earthscope.org/fdsnws/dataselect/1/query?'+urlencode({**PARAMS,'format':'miniseed','nodata':'404'})
ENTITIES=[('heasarc',TDAT+'heasarc_a1point.tdat.gz','a1point'),
          ('natural-earth','https://naciscdn.org/naturalearth/110m/cultural/ne_110m_admin_0_countries.zip','110m/cultural/ne_110m_admin_0_countries'),
          ('worldclim',WC_BASE+'wc2.1_10m_elev.zip','wc2.1_10m_elev'),('earthscope',WAVE_URL,EXTERNAL)]


def votable(rows,status='OK'):
    data=b''.join(struct.pack('>I',len(v.encode()))+v.encode() for row in rows for v in row)
    return '<VOTABLE><RESOURCE><INFO name="QUERY_STATUS" value="'+status+'"/><TABLE><FIELD name="table_name" datatype="char" arraysize="*"/><FIELD name="description" datatype="char" arraysize="*"/><DATA><BINARY><STREAM encoding="base64">'+base64.b64encode(data).decode()+'</STREAM></BINARY></DATA></TABLE></RESOURCE></VOTABLE>'


def ne(stem='ne_110m_admin_0_countries',version='5.1.1',scale='110m',category='cultural'):
    return f'<h3>Countries</h3><a href="https://www.naturalearthdata.com/http//www.naturalearthdata.com/download/{scale}/{category}/{stem}.zip">Download countries</a><span>version {version}</span>'


def wave():
    body=bytearray(512);body[:20]=b'000001M ANMO 00BHZIU'
    body[20:24]=struct.pack('>HH',2020,1);body[30:32]=struct.pack('>H',241);body[44:46]=struct.pack('>H',64)
    return bytes(body)


def adapter(handle):
    s=OpenDataSources(transport=httpx.MockTransport(handle));return s,EarthOct23Adapters(s._json,s._text_metadata)


@pytest.mark.parametrize('source,url,external',ENTITIES)
def test_only_official_entities_and_bound_identity(source,url,external):
    assert safe_file_url(source,url,external)
    assert not safe_file_url(source,url,external+'other')
    for bad in (url.replace('https://','http://'),url.replace('https://','https://@'),url.replace('https://','https://u:p@'),url+'#part',url.replace('.zip','.pdf'),url.replace('/query','/%2e%2e/query'),url+'\n',url.replace('https://','https://evil.test/')):
        if bad!=url:assert not safe_file_url(source,bad,external),bad


def test_waveform_scope_rejects_duplicate_params_arbitrary_query_and_wrong_channel():
    assert not safe_file_url('earthscope',WAVE_URL+'&net=XX',EXTERNAL)
    assert not safe_file_url('earthscope',WAVE_URL.replace('BHZ','BHN'),EXTERNAL)
    assert not safe_file_url('earthscope',WAVE_URL+'&limit=100',EXTERNAL)
    assert not safe_file_url('earthscope',WAVE_URL.replace('dataselect','station'),EXTERNAL)
    assert is_earthscope_sample(wave(),EXTERNAL)
    assert not is_earthscope_sample(b'<html>login</html>',EXTERNAL)
    assert not is_earthscope_sample(wave(),EXTERNAL.replace('BHZ','BHN'))
    body=bytearray(wave());body[30:32]=b'\0\0';assert not is_earthscope_sample(body,EXTERNAL)


@pytest.mark.parametrize('query',['earthquake', 'net=IU sta=* loc=00 cha=BHZ start=2020-01-01T00:00:00 end=2020-01-01T00:01:00',
    ' '.join(k+'='+v for k,v in PARAMS.items())+' net=II',
    ' '.join(k+'='+v for k,v in PARAMS.items()).replace('2020-01-01T00:01:00','2020-01-01T00:00:00'),
    ' '.join(k+'='+v for k,v in PARAMS.items()).replace('2020-01-01','2020-02-31')])
def test_waveform_query_does_not_invent_scope(query):
    with pytest.raises(SourceConnectorError,match='invalid_query'):_station_params(query)


def test_tap_binary_rows_and_reject_overflow_truncation_errors():
    raw=votable([('"a1point"','HEAO A1 light curves'),('TAP_SCHEMA.columns','schema')])
    assert _votable(raw)=={'a1point':'HEAO A1 light curves'}
    for body in (raw.replace('value="OK"','value="OVERFLOW"'),raw.replace('value="OK"','value="ERROR"'),'<html>error</html>',
                 votable([('a1point','one'),('a1point','two')]),raw.replace('</VOTABLE>',''),raw.replace('<VOTABLE>','<!DOCTYPE a><VOTABLE>')):
        with pytest.raises(SourceConnectorError,match='invalid_response'):_votable(body)


@pytest.mark.asyncio
async def test_heasarc_joins_catalog_to_actual_files_and_resolve_refreshes_version():
    date=['2026-10-23 12:00']
    def handle(r):
        if '/vo/tap/' in r.url.path:return httpx.Response(200,text=votable([('a1point','HEAO A1 light curves'),('only_metadata','HEAO no file')]))
        assert str(r.url)==TDAT
        return httpx.Response(200,text=f'<a href="heasarc_a1point.tdat.gz">file</a> {date[0]} 4.7K')
    _,a=adapter(handle);result=await a.search('heasarc','HEAO',1)
    assert result['total_count']==1
    candidate=result['results'][0];option,=await a.resolve({**candidate,'url':'https://evil.test/x'})
    assert option['url']==ENTITIES[0][1] and option['size'] is None
    date[0]='2026-10-24 12:00'
    with pytest.raises(SourceConnectorError,match='source_version_changed'):await a.resolve(candidate)


def test_natural_earth_version_scope_and_catalog_not_bundled_sample():
    rows=_ne_catalog(ne()+ne('ne_110m_lakes'), '110m','cultural')
    assert len(rows)==2 and rows[ENTITIES[1][2]]['version']=='5.1.1'
    with pytest.raises(SourceConnectorError):_ne_catalog(ne().replace('version 5.1.1',''),'110m','cultural')
    with pytest.raises(SourceConnectorError):_ne_catalog(ne().replace('www.naturalearthdata.com','evil.test'),'110m','cultural')


@pytest.mark.asyncio
async def test_all_natural_earth_scales_categories_and_paging():
    calls=[]
    def handle(r):
        calls.append(str(r.url));scale,category,_=r.url.path.strip('/').split('/')[-1].split('-')
        return httpx.Response(200,text=''.join(ne(f'ne_{scale}_layer{i}',scale=scale,category=category) for i in range(3)))
    _,a=adapter(handle);result=await a.search('natural-earth','layer',2)
    assert result['total_count']==18 and len(result['results'])==8 and len(calls)==6
    chosen=result['results'][0];option,=await a.resolve(chosen)
    assert option['external_id']==chosen['external_id'] and option['filename'].endswith('.zip')
    assert len(calls)==7


def test_worldclim_preserves_every_published_variable_resolution_and_license_scope():
    body=''.join(f'<a href="{WC_BASE}wc2.1_{res}_{var}.zip">file</a>' for res in ('10m','5m','2.5m','30s') for var in ('elev','prec','bio'))
    rows=_worldclim_catalog(body)
    assert len(rows)==12 and all(r['version']=='2.1' for r in rows.values())
    assert '19' in rows['wc2.1_30s_bio']['description']
    assert '12' in rows['wc2.1_5m_prec']['description']
    assert 'SRTM' in rows['wc2.1_10m_elev']['description']
    assert '1970' in rows['wc2.1_10m_prec']['description']


@pytest.mark.asyncio
async def test_worldclim_resolve_has_real_catalog_version_license_original_archive():
    _,a=adapter(lambda r:httpx.Response(200,text=f'<a href="{ENTITIES[2][1]}">elevation 10m</a>'))
    result=await a.search('worldclim','elevation 10m',1);candidate=result['results'][0]
    option,=await a.resolve({**candidate,'url':'https://evil.test/x'})
    assert option['url']==ENTITIES[2][1] and option['size'] is None and option['version']=='2.1'
    assert '非商业' in option['license'] and option['source_filename']=='wc2.1_10m_elev.zip'


@pytest.mark.asyncio
async def test_earthscope_only_requested_public_channel_and_time_range():
    calls=[]
    def handle(r):
        calls.append(r);assert r.url.params['includerestricted']=='false'
        return httpx.Response(200,text='#Network|Station|Location|Channel|Latitude|Longitude|Elevation|Depth|Azimuth|Dip|SensorDescription|Scale|ScaleFreq|ScaleUnits|SampleRate|StartTime|EndTime\nIU|ANMO|00|BHZ|34|-106|1600|188|0|-90|STS|1|0.02|m/s|40|2018-01-01|')
    _,a=adapter(handle);candidate=(await a.search('earthscope',' '.join(k+'='+v for k,v in PARAMS.items()),1))['results'][0]
    option,=await a.resolve(candidate)
    assert option['url']==WAVE_URL and candidate['coverage']['end']==PARAMS['end']
    assert 'fdsn.org/networks/detail/IU/' in option['license']


@pytest.mark.asyncio
@pytest.mark.parametrize('source',SOURCES)
@pytest.mark.parametrize('external',['../private','https://evil.test',None,1,'x?limit=1'])
async def test_invalid_candidate_rejected_before_network(source,external):
    _,a=adapter(lambda _:pytest.fail('invalid identity must not fetch'))
    with pytest.raises(SourceConnectorError,match='invalid_candidate'):await a.resolve({'source_id':source,'external_id':external})

from app.infrastructure.external.data_sources.open_data_earth_oct23_adapters import GHCN, _ghcn_stations, is_ghcn_sample


def ghcn_station(key='USW00023183',name='PHOENIX AIRPORT'):
    row=[' ']*85;row[:11]=key;row[12:20]=f'{33.4278:8.4f}';row[21:30]=f'{-112.0039:9.4f}';row[31:37]=f'{337.4:6.1f}';row[38:40]='AZ';row[41:71]=f'{name:30}'
    return ''.join(row)


def ghcn_data(station='USW00023183',value=361):
    return (station+'193306TMAX'+''.join(f'{value:5d}  6' for _ in range(31))+'\n').encode()


def test_ghcn_original_fixed_width_measurements_and_identity():
    data=ghcn_data()
    assert is_ghcn_sample(data,'USW00023183')
    assert not is_ghcn_sample(data,'USW00099999')
    assert not is_ghcn_sample(ghcn_data(value=-9999),'USW00023183')
    assert not is_ghcn_sample(b'<html>error</html>','USW00023183')
    assert not is_ghcn_sample(data.replace(b'TMAX',b'TMA<'),'USW00023183')
    assert is_ghcn_sample(data+data[:40],'USW00023183')
    assert safe_file_url('noaa-ghcn-daily',GHCN+'all/USW00023183.dly','USW00023183')
    assert not safe_file_url('noaa-ghcn-daily',GHCN+'ghcnd-stations.txt')
    assert not safe_file_url('noaa-ghcn-daily',GHCN+'all/USW00023183.dly?limit=100','USW00023183')


@pytest.mark.asyncio
async def test_ghcn_country_name_search_full_station_history_and_refresh_version():
    version=['3.35-upd-2026101917'];calls=[]
    def handle(r):
        calls.append(str(r.url))
        if r.url.path.endswith('ghcnd-stations.txt'):return httpx.Response(200,text='\n'.join(ghcn_station('USW000'+str(i).zfill(5)) for i in range(23)))
        if r.url.path.endswith('ghcnd-countries.txt'):return httpx.Response(200,text='US United States\nCH China\n')
        if r.url.path.endswith('ghcnd-version.txt'):return httpx.Response(200,text='The current version of GHCN Daily is '+version[0])
        pytest.fail(str(r.url))
    _,a=adapter(handle);result=await a.search('noaa-ghcn-daily','United States',3)
    assert result['total_count']==23 and len(result['results'])==3
    c=result['results'][0];option,=await a.resolve(c)
    assert option['url']==GHCN+'all/'+c['external_id']+'.dly'
    assert option['version']==version[0] and option['content_type']=='text/plain'
    assert c['coverage']['country']=='United States' and '完整历史记录' in c['coverage']['scope']
    assert len([u for u in calls if u.endswith('ghcnd-stations.txt')])==1
    version[0]='3.35-upd-2026102017'
    with pytest.raises(SourceConnectorError,match='source_version_changed'):await a.resolve(c)
    assert 'noaa-ghcn-daily' not in a._cache


@pytest.mark.parametrize('raw',[ghcn_station()+'\n'+ghcn_station(),ghcn_station().replace('33.4278','99.4278'),ghcn_station().replace('USW000','ZZW000')])
def test_ghcn_bad_or_duplicate_catalog_records_fail(raw):
    with pytest.raises(SourceConnectorError,match='invalid_response'):_ghcn_stations(raw,'US United States','3.35-upd-2026101917')


def test_ghcn_preserves_official_brazil_hyphenated_station_ids():
    key='BR00A6-0010'
    rows=_ghcn_stations(ghcn_station(key),'BR Brazil','3.35-upd-2026101917')
    assert key in rows and safe_file_url('noaa-ghcn-daily',rows[key]['url'],key)
    assert is_ghcn_sample(ghcn_data(key),key)


def test_natural_earth_ignores_script_and_rejects_conflicting_releases():
    rows=_ne_catalog(ne()+'<script>'+ne('ne_110m_fake')+'</script>','110m','cultural')
    assert len(rows)==1
    with pytest.raises(SourceConnectorError,match='inconsistent_source_snapshot'):_ne_catalog(ne()+ne(version='5.2.0'),'110m','cultural')


@pytest.mark.parametrize('key',['US10adam001','US10box_001'])
def test_ghcn_preserves_official_case_sensitive_station_ids(key):
    rows=_ghcn_stations(ghcn_station(key),'US United States','3.35-upd-2026101917')
    assert key in rows and safe_file_url('noaa-ghcn-daily',rows[key]['url'],key)
    assert is_ghcn_sample(ghcn_data(key),key)
    assert not is_ghcn_sample(ghcn_data(key),key.upper())

@pytest.mark.asyncio
@pytest.mark.parametrize('body',[b'<html>access denied</html>',ghcn_data('USW00099999'),ghcn_data(value=-9999)])
async def test_ghcn_facade_rejects_non_observations_before_advertising(body,tmp_path,monkeypatch):
    monkeypatch.setenv('OPEN_DATA_DOWNLOAD_ROOT',str(tmp_path))
    def handle(r):
        if r.url.path.endswith('ghcnd-stations.txt'):return httpx.Response(200,text=ghcn_station())
        if r.url.path.endswith('ghcnd-countries.txt'):return httpx.Response(200,text='US United States\n')
        if r.url.path.endswith('ghcnd-version.txt'):return httpx.Response(200,text='The current version of GHCN Daily is 3.35-upd-2026101917')
        assert r.url.path.endswith('/all/USW00023183.dly')
        return httpx.Response(200,content=body,headers={'content-type':'text/plain'})
    s,_=adapter(handle);c=(await s.search('noaa-ghcn-daily','Phoenix'))['results'][0]
    assert await s.verify(c)==[]
    option,=await s.resolve(c)
    with pytest.raises(SourceConnectorError):await s.download(c,option)


@pytest.mark.asyncio
async def test_ghcn_facade_preserves_complete_file_flags_and_provenance(tmp_path,monkeypatch):
    monkeypatch.setenv('OPEN_DATA_DOWNLOAD_ROOT',str(tmp_path));body=ghcn_data()+ghcn_data().replace(b'193306',b'193307')
    def handle(r):
        if r.url.path.endswith('ghcnd-stations.txt'):return httpx.Response(200,text=ghcn_station())
        if r.url.path.endswith('ghcnd-countries.txt'):return httpx.Response(200,text='US United States\n')
        if r.url.path.endswith('ghcnd-version.txt'):return httpx.Response(200,text='The current version of GHCN Daily is 3.35-upd-2026101917')
        return httpx.Response(200,content=body,headers={'content-type':'text/plain','content-length':str(len(body))})
    s,_=adapter(handle);c=(await s.search('noaa-ghcn-daily','Phoenix'))['results'][0];option,=await s.verify(c)
    stream,name,ctype,receipt=await s.download(c,option)
    try:
        assert stream.read()==body and name=='USW00023183.dly' and ctype=='text/plain'
        assert receipt['source_version']=='3.35-upd-2026101917' and receipt['external_id']=='USW00023183'
    finally:stream.close()
