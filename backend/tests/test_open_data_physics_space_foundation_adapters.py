"""Original file identity, full pagination and explicit scientific coverage."""
import httpx
import pytest
from app.domain.external.data_source import SourceConnectorError
from app.infrastructure.external.data_sources.open_data_sources import OpenDataSources
from app.infrastructure.external.data_sources.open_data_physics_space_foundation_adapters import (
    PhysicsSpaceFoundationAdapters, GWOSC, CDAS, safe_file_url, is_physics_space_sample, _query,
)
GW_URL = GWOSC + '/eventapi/json/GWTC-1-confident/GW150914/v3/H-H1_GWOSC_4KHZ_R1-1126259447-32.hdf5'
CDF_URL = 'https://cdaweb.gsfc.nasa.gov/sp_phys/data/ace/mag/level_2_cdaweb/mfi_h2/2009/ac_h2_mfi_20090601_v05.cdf'
CDF_ID = 'AC_H2_MFI|20090601T000000Z|20090602T000000Z'
DATASET = {'Id':'AC_H2_MFI','Label':'ACE Magnetic Field 1-Hour Level 2 Data','ObservatoryGroup':['ACE'],
           'Instrument':['MAG'], 'TimeInterval':{'Start':'1997-09-02T00:00:00.000Z','End':'2026-08-31T23:00:00.000Z'}}
FILE = {'Name':CDF_URL,'Length':28580,'StartTime':'2009-06-01T00:00:00.000Z','EndTime':'2009-06-01T23:00:00.000Z','LastModified':'2009-11-17T05:00:03.000Z'}
GWFILE = dict(gps_start=1126259447,detector='H1',sample_rate_kHz=4,duration=32,file_format='HDF',download_url=GW_URL)


def adapter(handle):
    s=OpenDataSources(transport=httpx.MockTransport(handle))
    return PhysicsSpaceFoundationAdapters(s._json,s._text_metadata)


def pages(rows,page=1,count=500,total=None):
    total=len(rows) if total is None else total
    return dict(results=rows,results_count=total,results_per_page=count,num_pages=(total+count-1)//count,
                page_number=page,next='https://evil.test/ignore' if page*count<total else None)


@pytest.mark.parametrize('source,url,external',[('gwosc',GW_URL,'GW150914-v3'),('nasa-cdaweb',CDF_URL,CDF_ID)])
def test_only_official_entity_urls_bound_to_selected_record(source,url,external):
    assert safe_file_url(source,url,external)
    assert not safe_file_url(source,url,'other')
    for bad in [url.replace('https://','http://'),url.replace('https://','https://evil.test/'),url.replace('https://','https://u:p@'),url+'?x=1',url+'#x',url+'\n',url.replace('/data/','/data/%2e%2e/'),url.replace('/json/','/json/%2f'),url.replace('.hdf5','.json'),url.replace('.cdf','.xml')]:
        if bad!=url:assert not safe_file_url(source,bad,external),bad


def test_master_cdf_not_entity_and_source_signatures():
    assert not safe_file_url('nasa-cdaweb',CDF_URL.replace('20090601','00000000'),CDF_ID)
    assert not safe_file_url('nasa-cdaweb',CDF_URL.replace('ac_h2_mfi_','ac_h1_mfi_'),CDF_ID)
    assert is_physics_space_sample('gwosc',b'\x89HDF\r\n\x1a\n'+b'\0'*24)
    for magic in (b'\0\0\xff\xff',b'\xcd\xf2\x60\x02',b'\xcd\xf3\0\x01'):
        assert is_physics_space_sample('nasa-cdaweb',magic+b'\0\0\xff\xff'+b'\0'*24)
    for source in ('gwosc','nasa-cdaweb'):
        assert not is_physics_space_sample(source,b'<html>download</html>'+b'\0'*32)
        assert not is_physics_space_sample(source,b'{"data":"catalog"}'+b'\0'*32)


@pytest.mark.parametrize('query',['ACE start=2009-01-01','ACE start=2009-01-02 end=2009-01-01',
    'ACE start=2009-02-31 end=2009-03-01','ACE start=2009-01-01 end=2009-01-02 start=2009-01-01',
    'ACE start=2009-01-01T00:00:00.5Z end=2009-01-02','start=2009-01-01 end=2009-01-02'])
def test_time_conditions_never_silently_discarded(query):
    with pytest.raises(SourceConnectorError,match='invalid_query'):_query(query)


@pytest.mark.asyncio
async def test_gwosc_all_versions_and_manifest_pages_no_metadata_entities():
    calls=[]
    def handle(r):
        calls.append(str(r.url))
        if r.url.path.endswith('/events'):
            return httpx.Response(200,json=pages([dict(name='GW150914',aliases=[],versions=[dict(version=3,catalog='GWTC-1-confident'),dict(version=4,catalog='GWTC-2.1-confident')])]))
        if r.url.path.endswith('/strain-files'):
            page=int(r.url.params['page'])
            file=GWFILE if page==1 else {**GWFILE,'file_format':'TXT','download_url':GW_URL.replace('.hdf5','.txt.gz')}
            return httpx.Response(200,json=pages([file],page=page,count=1,total=2))
        return httpx.Response(200,json=dict(name='GW150914',version=3,catalog='GWTC-1-confident'))
    a=adapter(handle)
    result=await a.search('gwosc','GW150914',1)
    assert result['total_count']==2
    assert (await a.search('gwosc','引力波',1))['total_count']==2
    assert (await a.search('gwosc','gravitational waves',1))['total_count']==2
    c=result['results'][0];f,=await a.resolve({**c,'url':'https://evil.test'})
    assert f['url']==GW_URL and f['coverage']['duration_seconds']==32 and f['coverage']['sample_rate_khz']==4
    assert all('evil.test' not in u for u in calls) and f['checksum'] is None
    assert (await a.search('gwosc','GWTC-2.1-confident',1))['total_count']==1


@pytest.mark.asyncio
async def test_changed_gw_catalog_rejected_and_nonexistent_files_not_fabricated():
    a=adapter(lambda r:httpx.Response(200,json=pages([]) if r.url.path.endswith('strain-files') else dict(name='GW150914',version=3,catalog='GWTC-1-confident')))
    assert await a.resolve(dict(source_id='gwosc',external_id='GW150914-v3'))==[]
    with pytest.raises(SourceConnectorError,match='source_version_changed'):
        await a.resolve(dict(source_id='gwosc',external_id='GW150914-v3',metadata={'catalog':'other'}))


@pytest.mark.asyncio
async def test_cda_search_preserves_requested_dates_or_provider_entire_coverage():
    a=adapter(lambda r:httpx.Response(200,json={'DatasetDescription':[DATASET]}))
    c=(await a.search('nasa-cdaweb','ACE magnetic',1))['results'][0]
    assert c['external_id']=='AC_H2_MFI|19970902T000000Z|20260831T230000Z'
    assert c['coverage']['end'].startswith('2026-08-31')
    c=(await a.search('nasa-cdaweb','AC_H2_MFI start=2009-06-01 end=2009-06-02',1))['results'][0]
    assert c['external_id']==CDF_ID
    assert (await a.search('nasa-cdaweb','ACE start=1900-01-01 end=1901-01-01',1))['total_count']==0


@pytest.mark.asyncio
async def test_cda_resolves_all_original_files_binds_dates_to_identity_not_metadata():
    calls=[]
    files=[{**FILE,'Name':CDF_URL.replace('20090601',f'20090601{i:06d}')} for i in range(125)]
    def handle(r):
        calls.append(r)
        return httpx.Response(200,json={'FileDescription':files} if '/orig_data/' in r.url.path else {'DatasetDescription':[DATASET]})
    result=await adapter(handle).resolve(dict(source_id='nasa-cdaweb',external_id=CDF_ID,metadata={'start':'2025'},url='https://evil.test'))
    assert len(result)==125 and len({r['id'] for r in result})==125
    assert calls[-1].url.path.endswith('20090601T000000Z,20090602T000000Z')
    assert result[0]['size']==28580 and result[0]['coverage']['start'].startswith('2009-06-01')
    assert '2009-11-17' in result[0]['version'] and result[0]['checksum'] is None


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value',[('Name',CDF_URL.replace('ac_h2_mfi','ac_h1_mfi')),('Name',CDF_URL.replace('20090601','00000000')),('Length',-1),('StartTime','2010-01-01T00:00:00Z'),('EndTime','2009-05-01T00:00:00Z')])
async def test_cda_rejects_wrong_file_identity_time_size(field,value):
    a=adapter(lambda r:httpx.Response(200,json={'FileDescription':[{**FILE,field:value}]} if '/orig_data/' in r.url.path else {'DatasetDescription':[DATASET]}))
    with pytest.raises(SourceConnectorError):await a.resolve(dict(source_id='nasa-cdaweb',external_id=CDF_ID))


@pytest.mark.asyncio
@pytest.mark.parametrize('source',['gwosc','nasa-cdaweb'])
@pytest.mark.parametrize('external',[None,1,'https://evil.test','../x','x?key=2','AC_H2_MFI|20090231T000000Z|20090301T000000Z'])
async def test_invalid_candidate_never_fetches(source,external):
    a=adapter(lambda r:pytest.fail('bad identity fetched'))
    with pytest.raises(SourceConnectorError,match='invalid_candidate'):await a.resolve(dict(source_id=source,external_id=external))


@pytest.mark.asyncio
async def test_inconsistent_or_truncated_api_pagination_rejected():
    a=adapter(lambda r:httpx.Response(200,json={**pages([GWFILE],count=2,total=3),'next':None}))
    with pytest.raises(SourceConnectorError,match='invalid_response'):await a._gw_pages('events')


@pytest.mark.asyncio
async def test_official_marginal_candidate_and_injection_names_do_not_break_catalog():
    names=['151012.2','blind_injection','GRB051103','200219_201407']
    a=adapter(lambda r:httpx.Response(200,json=pages([dict(name=n,aliases=[],versions=[dict(version=1,catalog='O1_O2-Preliminary')]) for n in names])))
    assert (await a.search('gwosc','GWOSC',1))['total_count']==4
