"""Identity, scope, pagination and complete physical-data validation."""
import io
import json
import zipfile
from unittest.mock import AsyncMock
import pytest
from app.domain.external.data_source import SourceConnectorError
from app.domain.models.open_data_chem_materials_foundation_definitions import DEFINITIONS,SOURCE_INFO
from app.infrastructure.external.data_sources.open_data_chem_materials_foundation_adapters import (
    ChemMaterialsFoundationAdapters,SOURCES,COD,NIST,NOMAD,safe_file_url,
    is_chem_materials_sample,validate_chem_materials_stream,
)

DOI='10.1016/j.fluid.2014.11.015'
EID='----9KNOtIZc9bDFEWxgjeSRsJrC'
UPLOAD='r0ck_h3kQQGZdp5E41vb9g'
URLS={SOURCES[0]:COD+'/9005026.cif@291351','nist-thermoml':NIST+'/'+DOI+'.xml','nomad':NOMAD+'/entries/'+EID+'/raw'}
IDS={SOURCES[0]:'9005026','nist-thermoml':DOI,'nomad':EID}

def cod(num=9005026):
    return {'file':str(num),'svnrevision':'291351','flags':'has coordinates','mineral':'Quartz','formula':'Si O2','status':None}


def nist(doi=DOI,points=1):
    return {'Citation':{'sDOI':doi,'sTitle':'Vapor liquid equilibrium','dateCit':'2020-09-29'},'data_summary':{'total':{'data_points':points}},'THERMOML_MD5_CHECKSUM':'82e0e45d780b8178375647267986dc56'}


def nomad(**changes):
    return {'entry_id':EID,'upload_id':UPLOAD,'published':True,'processed':True,'with_embargo':False,'mainfile':'material/vasprun.xml',
            'license':'CC BY 4.0','publish_time':'2020-01-01','results':{'material':{'elements':['Si'],'chemical_formula_reduced':'Si'},
            'method':{'simulation':{'program_name':'VASP'}}},**changes}


@pytest.mark.parametrize('source',SOURCES)
def test_urls_are_exact_and_bound_to_records(source):
    url=URLS[source]
    assert safe_file_url(source,url,IDS[source])
    assert not safe_file_url(source,url,'foreign')
    for other in SOURCES:
        if source!=other:assert not safe_file_url(other,url)
    for invalid in (url+'?redirect=evil',url+'#x',url.replace('https://','http://'),url.replace('https://','https://user@'),
                    url.replace('https://','https://evil.test/'),url.replace('/cod/','/cod/%2e%2e/') if source==SOURCES[0] else url+'%00'):
        assert not safe_file_url(source,invalid)


@pytest.mark.asyncio
async def test_cod_pages_complete_results_caches_search_but_resolves_fresh_revision():
    fetch=AsyncMock(side_effect=[[cod(9005000+i) for i in range(23)],[{**cod(9005010),'svnrevision':'300001'}]])
    adapter=ChemMaterialsFoundationAdapters(fetch,AsyncMock())
    first=await adapter.search(SOURCES[0],'quartz',1)
    second=await adapter.search(SOURCES[0],'quartz',2)
    assert first['total_count']==23 and len(second['results'])==10
    assert second['results'][0]['external_id']=='9005010' and fetch.await_count==1
    option=(await adapter.resolve(second['results'][0]))[0]
    assert option['url'].endswith('9005010.cif@300001') and option['version']=='300001'


@pytest.mark.asyncio
async def test_cod_excludes_metadata_only_duplicates_and_errors():
    fetch=AsyncMock(return_value=[cod(),{**cod(9005001),'flags':'has coordinates, has errors'},
                                 {**cod(9005002),'duplicateof':'9005026'},{**cod(9005003),'flags':'bibliographic'}])
    result=await ChemMaterialsFoundationAdapters(fetch,AsyncMock()).search(SOURCES[0],'quartz',1)
    assert [r['external_id'] for r in result['results']]==['9005026']


@pytest.mark.asyncio
async def test_nist_requires_measurements_preserves_md5_and_full_scope():
    fetch=AsyncMock(side_effect=[{'size':2,'results':[{'id':'20.5000.trc.thermoml/'+DOI,'content':nist()},
                                 {'id':'20.5000.trc.thermoml/10.1021/none','content':nist('10.1021/none',0)}]},nist()])
    adapter=ChemMaterialsFoundationAdapters(fetch,AsyncMock())
    result=await adapter.search('nist-thermoml','ethanol density',2)
    assert len(result['results'])==1
    params=fetch.call_args_list[0].args[1]
    assert params['pageNum']==1 and params['query']=='"ethanol" AND "density"'
    option=(await adapter.resolve(result['results'][0]))[0]
    assert option['url']==URLS['nist-thermoml'] and option['checksum']=='md5:'+nist()['THERMOML_MD5_CHECKSUM']
    assert option['coverage']['data_points']==1


@pytest.mark.asyncio
async def test_nist_mismatched_doi_and_bad_checksums_fail_closed():
    for record in (nist('10.1021/foreign'),{**nist(),'THERMOML_MD5_CHECKSUM':'bad'}):
        with pytest.raises(SourceConnectorError):
            await ChemMaterialsFoundationAdapters(AsyncMock(return_value=record),AsyncMock()).resolve({'source_id':'nist-thermoml','external_id':DOI})


@pytest.mark.asyncio
async def test_nomad_search_has_public_scope_formula_filter_and_excludes_embargo():
    fetch=AsyncMock(return_value={'pagination':{'total':2},'data':[nomad(),nomad(with_embargo=True)]})
    result=await ChemMaterialsFoundationAdapters(fetch,AsyncMock()).search('nomad','Si',2)
    assert len(result['results'])==1
    body=fetch.call_args.kwargs['json']
    assert body['owner']=='public' and body['pagination']['page']==2
    assert {'results.material.chemical_formula_reduced':'Si'} in body['query']['and']


@pytest.mark.asyncio
async def test_nomad_complete_manifest_has_no_100_file_truncation_and_rejects_foreign_upload():
    files=[{'path':'material/vasprun.xml','size':32}]+[{'path':f'material/other{i}.out','size':i} for i in range(121)]
    listing={'entry_id':EID,'data':{'entry_id':EID,'upload_id':UPLOAD,'mainfile':'material/vasprun.xml','files':files}}
    fetch=AsyncMock(side_effect=[{'data':nomad()},listing])
    options=await ChemMaterialsFoundationAdapters(fetch,AsyncMock()).resolve({'source_id':'nomad','external_id':EID})
    assert len(options[0]['source_manifest'])==122 and options[0]['size'] is None
    listing['data']['upload_id']='foreign'
    with pytest.raises(SourceConnectorError):
        await ChemMaterialsFoundationAdapters(AsyncMock(side_effect=[{'data':nomad()},listing]),AsyncMock()).resolve({'source_id':'nomad','external_id':EID})


def xml(values='<nPropValue>0.2</nPropValue>',doi=DOI):
    return ('<DataReport xmlns="http://www.iupac.org/namespaces/ThermoML"><Citation><sDOI>'+doi+'</sDOI></Citation>'
            '<PureOrMixtureData><NumValues>'+values+'</NumValues></PureOrMixtureData></DataReport>').encode()


@pytest.mark.parametrize('body',[xml(''),xml(doi='10.1021/foreign'),xml('<nPropValue>nan</nPropValue>'),xml()[:-5],b'<!DOCTYPE x>'+xml(),xml()*2])
def test_full_nist_rejects_metadata_wrong_identity_truncation_entities_and_nonfinite(body):
    with pytest.raises(SourceConnectorError):
        validate_chem_materials_stream('nist-thermoml',io.BytesIO(body),{'external_id':DOI})


def test_full_nist_validates_all_measurements_in_bounded_chunks_and_restores_stream():
    class Bounded(io.BytesIO):
        def read(self,n=-1):
            assert 0<=n<=65536
            return super().read(n)
    stream=Bounded(xml('<nPropValue>0.2</nPropValue>'*15000))
    validate_chem_materials_stream('nist-thermoml',stream,{'external_id':DOI,'coverage':{'data_points':15000}})
    assert stream.tell()==0
    with pytest.raises(SourceConnectorError,match='inconsistent_source_snapshot'):
        validate_chem_materials_stream('nist-thermoml',stream,{'external_id':DOI,'coverage':{'data_points':1}})


def zipped(data=b'<modeling><atominfo>Si</atominfo><structure>1 2 3</structure></modeling>',extra=False):
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w',compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr(UPLOAD+'/material/vasprun.xml',data);z.writestr('manifest.json',json.dumps([{'entry_id':EID,'upload_id':UPLOAD,'mainfile':UPLOAD+'/material/vasprun.xml'}]))
        if extra:z.writestr('../foreign',b'bad')
    stream.seek(0)
    chosen={'external_id':EID,'upload_id':UPLOAD,'mainfile':'material/vasprun.xml','source_manifest':[{'path':'material/vasprun.xml','size':len(data)}]}
    return stream,chosen


def test_full_nomad_zip_validates_manifest_calculation_payload_crc_and_restores_stream():
    stream,chosen=zipped()
    validate_chem_materials_stream('nomad',stream,chosen)
    assert stream.tell()==0
    for stream,chosen in (zipped(extra=True),zipped(b'# README only, no numerical calculation')):
        with pytest.raises(SourceConnectorError):validate_chem_materials_stream('nomad',stream,chosen)
    stream,chosen=zipped();chosen['source_manifest'][0]['size']+=1
    with pytest.raises(SourceConnectorError,match='inconsistent_source_snapshot'):
        validate_chem_materials_stream('nomad',stream,chosen)


@pytest.mark.parametrize('page',[0,True,-1,101,'2'])
@pytest.mark.asyncio
async def test_invalid_pages_do_not_hit_network(page):
    fetch=AsyncMock()
    with pytest.raises(SourceConnectorError,match='invalid_cursor'):
        await ChemMaterialsFoundationAdapters(fetch,fetch).search('nomad','Si',page)
    fetch.assert_not_awaited()


def test_catalog_matches_adapters_and_samples_reject_html():
    assert set(SOURCES)=={r['id'] for r in DEFINITIONS}==set(SOURCE_INFO)
    for source in SOURCES:
        assert not is_chem_materials_sample(source,b'<html>error</html>',IDS[source])


def test_cancellation_stops_xml_and_zip_validation_and_restores_stream():
    import asyncio
    import threading
    event=threading.Event();event.set()
    for source,stream,chosen in [('nist-thermoml',io.BytesIO(xml()),{'external_id':DOI}),('nomad',*zipped())]:
        with pytest.raises(asyncio.CancelledError):validate_chem_materials_stream(source,stream,chosen,cancelled=event)
        assert stream.tell()==0


@pytest.mark.parametrize('query,expected',[('SiO2','O2Si'),('Si2O4','O2Si'),('Fe2O3','Fe2O3'),('NaCl','ClNa'),('CH3COOH','CH2O')])
@pytest.mark.asyncio
async def test_nomad_normalizes_empirical_formula_without_losing_stoichiometry(query,expected):
    fetch=AsyncMock(return_value={'data':[],'pagination':{'total':0}})
    await ChemMaterialsFoundationAdapters(fetch,AsyncMock()).search('nomad',query,1)
    assert {'results.material.chemical_formula_reduced':expected} in fetch.call_args.kwargs['json']['query']['and']


@pytest.mark.parametrize('query',['DFT','Si0O2','Si1.5O3','copper oxide','Xx2'])
@pytest.mark.asyncio
async def test_nomad_does_not_reinterpret_unsupported_formula_or_keywords(query):
    fetch=AsyncMock(return_value={'data':[],'pagination':{'total':0}})
    await ChemMaterialsFoundationAdapters(fetch,AsyncMock()).search('nomad',query,1)
    assert {'text_search_contents':query} in fetch.call_args.kwargs['json']['query']['and']


def test_cod_requires_numeric_xyz_rows_not_just_coordinate_headers():
    prefix=b'data_9005026\n_cell_length_a 5\nloop_\n_atom_site_label\n_atom_site_fract_x\n_atom_site_fract_y\n_atom_site_fract_z\n'
    assert not is_chem_materials_sample(SOURCES[0],prefix,'9005026')
    assert not is_chem_materials_sample(SOURCES[0],prefix+b'Si ? ? ?\n','9005026')
    assert is_chem_materials_sample(SOURCES[0],prefix+b'Si .1(2) .2 .3\n','9005026')


@pytest.mark.asyncio
async def test_nomad_manifest_order_cannot_invalidate_file_identity():
    files=[{'path':'material/vasprun.xml','size':32},{'path':'material/additional.xml','size':64}]
    def listing(items):
        return {'entry_id':EID,'data':{'entry_id':EID,'upload_id':UPLOAD,'mainfile':'material/vasprun.xml','files':items}}
    fetch=AsyncMock(side_effect=[{'data':nomad()},listing(files),{'data':nomad()},listing(list(reversed(files)))])
    adapter=ChemMaterialsFoundationAdapters(fetch,AsyncMock());candidate={'source_id':'nomad','external_id':EID}
    first=(await adapter.resolve(candidate))[0];second=(await adapter.resolve(candidate))[0]
    assert first['id']==second['id'] and first['source_manifest']==second['source_manifest']
