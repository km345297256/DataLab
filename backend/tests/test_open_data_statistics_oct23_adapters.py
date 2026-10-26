import copy
import hashlib
import json
from urllib.parse import parse_qs
import httpx
import pytest
from app.domain.external.data_source import SourceConnectorError
from app.infrastructure.external.data_sources.open_data_sources import OpenDataSources
from app.infrastructure.external.data_sources.open_data_statistics_oct23_adapters import (
    MY, NO, WHO, SOURCES, StatisticsOct23Adapters, _partitions, safe_file_url,
)


def adapters(handler):
    source=OpenDataSources(transport=httpx.MockTransport(handler))
    return source,StatisticsOct23Adapters(source._json,source._text_metadata)


def no_meta(**changes):
    return {'class':'dataset','extension':{'px':{'tableid':'12345'}},'id':['Region','Tid'],
            'size':[2,2],'dimension':{'Region':{'category':{'index':{'01':0,'02':1}}},
                                    'Tid':{'category':{'index':{'2020':0,'2021':1}}}},'updated':'2026-10-23',**changes}


def who_row(id,**changes):
    return {'Id':id,'IndicatorCode':'DEMO_1','SpatialDim':'CHN','TimeDim':2020,'Value':'2.1','NumericValue':2.1,**changes}


def who_data(rows,total):
    return {'@odata.context':WHO+'/$metadata#DEMO_1','@odata.count':total,'value':rows}


def candidate(source,external):return {'source_id':source,'external_id':external,'source_url':'https://example.test'}


@pytest.mark.parametrize('source,url,external',[
    ('malaysia-data-gov','https://storage.dosm.gov.my/population/population_malaysia.csv','population_malaysia'),
    ('norway-ssb',NO+'/tables/12345/data?lang=en&outputFormat=csv&stub=Region,Tid&valueCodes[Region]=*&valueCodes[Tid]=*','12345'),
    ('who-gho',WHO+'/DEMO_1','DEMO_1'),
])
def test_exact_entity_hosts_paths_and_query(source,url,external):
    assert safe_file_url(source,url,external)
    for bad in [url.replace('https:','http:'),url.replace('https://','https://user@'),url+'#a',
                url.replace('https://','https://attacker.test/'),url.replace('/api/','/%2e%2e/api/'),url+'\x7f']:
        if bad!=url:assert not safe_file_url(source,bad,external)
    assert not safe_file_url('unknown',url,external)


@pytest.mark.parametrize('query',['',' ','""',None,'x'*501])
@pytest.mark.asyncio
async def test_invalid_query_does_not_fetch(query):
    _,a=adapters(lambda _:pytest.fail('unexpected network'))
    with pytest.raises(SourceConnectorError):await a.search('who-gho',query,1)


@pytest.mark.parametrize('source',SOURCES)
@pytest.mark.parametrize('external',['../secret','https://evil.test','x?foo=1',None,True])
@pytest.mark.asyncio
async def test_invalid_identifiers_do_not_fetch(source,external):
    _,a=adapters(lambda _:pytest.fail('unexpected network'))
    with pytest.raises(SourceConnectorError):await a.resolve(candidate(source,external))


@pytest.mark.asyncio
async def test_malaysia_search_complete_catalog_and_resolve_only_full_files():
    catalogue='id,title_en,title_bm,source\npopulation_malaysia,Population,Populasi,DOSM\nsdg_03-3-1,SDG health,SDG,DOSM\n'
    page={'props':{'pageProps':{'id':'population_malaysia','meta':{'id':'population_malaysia'},
          'link_csv':'https://storage.dosm.gov.my/population/population_malaysia.csv',
          'link_parquet':'https://storage.dosm.gov.my/population/population_malaysia.parquet',
          'link_preview':'https://storage.dosm.gov.my/population/latest.parquet','last_updated':'2026-10-23'}}}
    def handle(r):
        if str(r.url).endswith('dataset_list.csv'):return httpx.Response(200,text=catalogue)
        return httpx.Response(200,text='<script id="__NEXT_DATA__" type="application/json">'+json.dumps(page)+'</script>')
    s,a=adapters(handle);r=await a.search('malaysia-data-gov','population',1)
    options=await a.resolve(r['results'][0]);assert len(options)==2
    assert all('latest' not in o['url'] for o in options)
    page['props']['pageProps']['link_csv']='https://evil.test/file.csv'
    assert len(await a.resolve(r['results'][0]))==1
    page['props']['pageProps']['id']='other'
    with pytest.raises(SourceConnectorError):await a.resolve(r['results'][0])


@pytest.mark.asyncio
async def test_norway_requires_all_dimensions_and_preserves_all_time_values():
    s,a=adapters(lambda r:httpx.Response(200,json=no_meta()))
    result=await a.resolve(candidate('norway-ssb','12345'));assert len(result)==1
    p=parse_qs(result[0]['url'].partition('?')[2]);assert p['valueCodes[Region]']==['*'] and p['valueCodes[Tid]']==['*']
    assert p['stub']==['Region,Tid'] and result[0]['coverage']['total_cells']==4
    assert not safe_file_url('norway-ssb',result[0]['url'].replace('&valueCodes%5BTid%5D=%2A',''),'12345')
    assert not safe_file_url('norway-ssb',result[0]['url'],'99999')


def test_norway_partition_is_complete_disjoint_and_not_a_size_cap():
    codes={'Region':[str(n) for n in range(1000)],'Tid':[str(n) for n in range(2000,4000)]}
    chunks=list(_partitions(codes));assert len(chunks)>1
    assert sum(len(c['Region'])*len(c['Tid']) for c in chunks)==2000000
    assert all(len(c['Region'])*len(c['Tid'])<=800000 for c in chunks)
    # Every axis partition covers each original cell exactly once.
    pairs=set()
    small={'R':list('ABCDE'),'T':list('1234567')}
    for c in _partitions(small,max_cells=6):
        for r in c['R']:
            for t in c['T']:
                assert (r,t) not in pairs;pairs.add((r,t))
    assert len(pairs)==35


@pytest.mark.asyncio
async def test_bad_norway_manifest_cannot_become_partial_data():
    bad=no_meta(size=[2,1])
    _,a=adapters(lambda _:httpx.Response(200,json=bad))
    with pytest.raises(SourceConnectorError):await a.resolve(candidate('norway-ssb','12345'))


@pytest.mark.asyncio
async def test_who_query_literals_cannot_modify_odata_filter():
    captured=[]
    def handler(r):
        captured.append(dict(r.url.params));return httpx.Response(200,json={'value':[],'@odata.count':0})
    _,a=adapters(handler);await a.search('who-gho',"child's mortality",2)
    assert captured[0]['$filter']=="contains(tolower(IndicatorName),'child''s mortality')"
    assert captured[0]['$skip']=='10'


@pytest.mark.asyncio
@pytest.mark.parametrize('rows',[[who_row(1,IndicatorCode='OTHER')],[{'IndicatorCode':'DEMO_1','IndicatorName':'metadata'}]])
async def test_who_metadata_or_wrong_indicator_is_not_entity_data(rows):
    _,a=adapters(lambda _:httpx.Response(200,json=who_data(rows,1)))
    with pytest.raises(SourceConnectorError):await a.verify_who([{'url':WHO+'/DEMO_1','external_id':'DEMO_1'}])


@pytest.mark.asyncio
async def test_who_full_facade_pagination_download_and_no_missing_values_dropped(tmp_path,monkeypatch):
    monkeypatch.setenv('OPEN_DATA_DOWNLOAD_ROOT',str(tmp_path))
    rows=[who_row(i,NumericValue=None,Value=None) if i==2 else who_row(i) for i in range(1,1003)]
    requests=[]
    def handler(r):
        if r.url.path.endswith('/Indicator'):return httpx.Response(200,json={'value':[{'IndicatorCode':'DEMO_1'}]})
        params=dict(r.url.params);requests.append(params);skip=int(params['$skip']);top=int(params['$top'])
        return httpx.Response(200,json=who_data(rows[skip:skip+top],len(rows)))
    s,a=adapters(handler);c=candidate('who-gho','DEMO_1');o=(await s.verify(c))[0]
    stream,name,mime,receipt=await s.download(c,o)
    try:body=stream.read()
    finally:stream.close()
    data=json.loads(body);assert data['value']==rows and len(data['value'])==1002
    assert receipt['sha256']==hashlib.sha256(body).hexdigest() and receipt['snapshot_page_count']==2
    assert receipt['snapshot_rows']==1002 and receipt['provider_checksum_verified'] is False
    assert mime=='application/json' and name.endswith('.json')
    assert requests[-1]['$skip']=='1000' and requests[-1]['$top']=='1000'
    assert not list(tmp_path.glob('*.part'))


@pytest.mark.parametrize('failure',['changed_count','duplicate_id','empty_page','wrong_indicator','only_missing'])
@pytest.mark.asyncio
async def test_who_incomplete_or_invalid_snapshots_fail_and_release_staging(tmp_path,monkeypatch,failure):
    monkeypatch.setenv('OPEN_DATA_DOWNLOAD_ROOT',str(tmp_path))
    def handler(r):
        if r.url.path.endswith('/Indicator'):return httpx.Response(200,json={'value':[{'IndicatorCode':'DEMO_1'}]})
        skip=int(r.url.params['$skip'])
        if failure=='only_missing':return httpx.Response(200,json=who_data([who_row(1,NumericValue=None,Value='No data')],1))
        rows=[who_row(1)] if skip==0 else [who_row(2)]
        if skip and failure=='duplicate_id':rows=[who_row(1)]
        if skip and failure=='empty_page':rows=[]
        if failure=='wrong_indicator':rows=[who_row(1,IndicatorCode='OTHER')]
        return httpx.Response(200,json=who_data(rows,3 if skip and failure=='changed_count' else 2))
    s,a=adapters(handler);c=candidate('who-gho','DEMO_1');o=(await s.resolve(c))[0]
    with pytest.raises(SourceConnectorError):await s.download(c,o)
    assert not list(tmp_path.glob('*.part'))
