import gzip
import io
import json
import threading
from unittest.mock import AsyncMock

import httpx
import pytest

from app.domain.external.data_source import SourceConnectorError
from app.infrastructure.external.data_sources.open_data_math_foundation_adapters import (
    MathFoundationAdapters, SOURCES, OEIS, MIP, LMF_API, safe_file_url, is_math_sample, validate_math_stream,
)
from app.infrastructure.external.data_sources.open_data_sources import OpenDataSources


def curve(label='11.a1', **kw):
    return {'lmfdb_label':label,'ainvs':[0,-1,1,-7820,-263580],'conductor':11,'rank':0,
            'jinv':[-52893159101157376,11],**kw}


def oeis(number=45, **kw):
    return {'number':number,'name':'Fibonacci numbers','time':'2026-10-23',
            'link':[f'<a href="/A{number:06d}/b{number:06d}.txt">Table of n, a(n)</a>'],**kw}


def mip_catalog(count=23):
    return f'Browse below all {count} instances.<table><tbody>'+''.join(
        '<tr><td><a href="instance_details_demo'+str(i)+'.html">demo'+str(i)+'</a></td>'+
        ''.join('<td>'+x+'</td>' for x in ['easy','2','1','0','1','2','3','Author','network','1','binary benchmark'])+'</tr>'
        for i in range(count))+'</tbody></table>'


def adapter(handler):
    s=OpenDataSources(transport=httpx.MockTransport(handler))
    return s,s._adapters['oeis']


@pytest.mark.parametrize('source,url,external',[
    ('oeis',OEIS+'/A000045/b000045.txt','A000045'),
    ('miplib',MIP+'/WebData/instances/50v-10.mps.gz','50v-10'),
    ('lmfdb',LMF_API+'?_format=json&lmfdb_label=s11.a1','11.a1'),
])
def test_entity_url_is_pinned_and_rejects_unsafe_forms(source,url,external):
    assert safe_file_url(source,url,external)
    assert not safe_file_url(source,url,'wrong')
    for bad in [url.replace('https:','http:'),url.replace('https://','https://user@'),url+'#x',url+'\n',
                url.replace('https://','https://evil.test/'),url.replace('/', '/%2e%2e/',1),url+'&extra=1']:
        assert not safe_file_url(source,bad,external)


@pytest.mark.parametrize('source',SOURCES)
@pytest.mark.parametrize('external',[None,True,'../bad','https://evil.test','x?redirect=1'])
@pytest.mark.asyncio
async def test_invalid_id_never_networks(source,external):
    _,a=adapter(lambda _:pytest.fail('network'))
    with pytest.raises(SourceConnectorError):await a.resolve({'source_id':source,'external_id':external})


@pytest.mark.asyncio
async def test_oeis_exact_search_paging_and_published_file_requirement():
    requests=[];row=oeis()
    def handle(r):
        requests.append(r)
        return httpx.Response(200,json=[row])
    s,a=adapter(handle)
    r=await s.search('oeis','A000045');c=r['results'][0]
    assert requests[-1].url.params['q']=='id:A000045'
    assert not r['next_cursor']
    options=await a.resolve(c)
    assert options[0]['url']==OEIS+'/A000045/b000045.txt'
    row['link']=[];assert await a.resolve(c)==[]
    row['link']=['<a href="https://evil.test/A000045/b000045.txt">table</a>']
    assert await a.resolve(c)==[]


@pytest.mark.asyncio
async def test_oeis_pagination_does_not_invent_a_total_or_skip_rows():
    calls=[]
    def handle(r):
        start=int(r.url.params['start']);calls.append(start)
        return httpx.Response(200,json=[oeis(i) for i in range(start+1,min(start+11,14))])
    s,_=adapter(handle)
    first=await s.search('oeis','fibonacci')
    second=await s.search('oeis','fibonacci',first['next_cursor'])
    assert calls==[0,10] and len(first['results'])==10 and len(second['results'])==3
    assert not second['next_cursor']
    assert not set(c['external_id'] for c in first['results']) & set(c['external_id'] for c in second['results'])


@pytest.mark.asyncio
async def test_mip_full_catalogue_pagination_and_detail_manifest():
    calls=[]
    def handle(r):
        calls.append(str(r.url))
        if r.url.path=='/tag_collection.html':return httpx.Response(200,text=mip_catalog())
        return httpx.Response(200,text='<a href="WebData/instances/demo20.mps.gz">model</a><a href="WebData/solutions/demo20.sol.gz">solution</a>')
    s,a=adapter(handle)
    result=await a.search('miplib','binary',3)
    assert result['total_count']==23 and len(result['results'])==3
    options=await a.resolve(result['results'][0])
    assert len(options)==1 and options[0]['url'].endswith('/demo20.mps.gz')
    assert len([u for u in calls if u.endswith('tag_collection.html')])==1
    assert await a.resolve({'source_id':'miplib','external_id':'nonexistent'})==[]


@pytest.mark.asyncio
async def test_mip_truncated_catalogue_is_not_presented_as_complete():
    _,a=adapter(lambda _:httpx.Response(200,text=mip_catalog().replace('all 23','all 24')))
    with pytest.raises(SourceConnectorError):await a.search('miplib','binary',1)


@pytest.mark.asyncio
async def test_lmfdb_ten_row_pagination_over_hundred_row_api_and_filters():
    requests=[]
    def handle(r):
        requests.append(r);start=int(r.url.params['_offset'])
        return httpx.Response(200,json={'table':'ec_curvedata','data':[curve(f'{i}.a1') for i in range(start+1,start+101)]})
    s,a=adapter(handle)
    first=await a.search('lmfdb','rank=2 conductor=11',1);second=await a.search('lmfdb','rank=2 conductor=11',2)
    assert requests[0].url.params['rank']=='i2' and requests[0].url.params['conductor']=='i11'
    assert [r.url.params['_offset'] for r in requests]==['0','10']
    assert first['results'][-1]['external_id']=='10.a1' and second['results'][0]['external_id']=='11.a1'
    assert await a.search('lmfdb','unrelated proteins',1)=={'results':[],'total_count':0}


def test_lmfdb_not_exposed_until_automated_access_is_reliable():
    from app.domain.models.open_data_registry import source_catalog, connector_sources
    assert 'lmfdb' not in {s['id'] for s in source_catalog()}
    assert 'lmfdb' not in {s.id for s in connector_sources()}


@pytest.mark.asyncio
async def test_lmfdb_resolve_preserves_big_integers_and_pins_record_version():
    row=curve(ainvs=[0,-1,1,10**100,-10**120])
    _,a=adapter(lambda _:httpx.Response(200,json={'table':'ec_curvedata','data':[row]}))
    c={'source_id':'lmfdb','external_id':'11.a1'}
    first=(await a.resolve(c))[0];second=(await a.resolve(c))[0]
    assert first['id']==second['id']
    row['rank']=2
    assert (await a.resolve(c))[0]['id']!=first['id']


@pytest.mark.parametrize('source,external,body',[
    ('oeis','A000045',b'# terms\n0 0\n1 1\n2 99999999999999999999999999999999999999999999\n'),
    ('miplib','demo',gzip.compress(b'NAME demo\nROWS\n N OBJ\n L C1\nCOLUMNS\n x OBJ 1\nRHS\nENDATA\n')),
    ('lmfdb','11.a1',json.dumps({'table':'ec_curvedata','data':[curve()]}).encode()),
])
def test_actual_data_shapes_and_wrong_entity(source,external,body):
    assert is_math_sample(source,body,external,complete=True)
    for bad in [b'<html>login</html>',b'{"error":"unavailable"}',b'# README only\n',b'',gzip.compress(b'README')]:
        assert not is_math_sample(source,bad,external,complete=True)
    if source=='lmfdb':assert not is_math_sample(source,body,'14.a1',complete=True)


@pytest.mark.asyncio
async def test_probe_and_download_reject_numeric_source_error_text():
    row=oeis();body=b'No terms available\n'
    def handle(r):
        if r.url.path=='/search':return httpx.Response(200,json=[row])
        return httpx.Response(200,content=body,headers={'content-type':'text/plain'})
    s,a=adapter(handle);c={'source_id':'oeis','external_id':'A000045'};o=(await a.resolve(c))[0]
    assert await s._probe_file(o) is None
    with pytest.raises(SourceConnectorError):await s.download(c,o)


def test_mip_full_validation_rejects_truncated_gzip_bad_crc_and_missing_model_end():
    good=gzip.compress(b'NAME demo\nROWS\n N OBJ\n'+b' x OBJ 1\n'*20000+b'ENDATA\n')
    valid=io.BytesIO(good);validate_math_stream('miplib',valid,{})
    assert valid.tell()==0
    corrupt=bytearray(good);corrupt[-8]^=1
    for body in (good[:-8],bytes(corrupt),gzip.compress(b'NAME demo\nROWS\n N OBJ\n')):
        stream=io.BytesIO(body)
        with pytest.raises(SourceConnectorError):validate_math_stream('miplib',stream,{})
        assert stream.tell()==0


def test_mip_validation_can_be_cancelled_without_closing_the_owned_stream():
    import asyncio
    stop=threading.Event();stop.set();stream=io.BytesIO(gzip.compress(b'NAME demo\nROWS\nENDATA\n'))
    with pytest.raises(asyncio.CancelledError):validate_math_stream('miplib',stream,{},cancelled=stop)
    assert not stream.closed and stream.tell()==0
