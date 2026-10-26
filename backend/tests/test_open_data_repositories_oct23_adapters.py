import copy
from unittest.mock import AsyncMock
import pytest
from app.domain.external.data_source import SourceConnectorError
from app.domain.models.open_data_repositories_oct23_definitions import REPOSITORY_CONFIG, DEFINITIONS
from app.infrastructure.external.data_sources.open_data_repositories_oct23_adapters import RepositoriesOct23Adapters, safe_file_url

PIDS={'asu-research-data':'10.48349/ASU/1CY5AR','juelich-data':'10.26165/JUELICH-DATA/ABC123','repod':'10.18150/ABC123','deic-dataverse':'10.60612/DATADK/ABC123','bonndata':'10.60507/FK2/ABC123','tudo-data':'10.17877/TUDODATA-2026-F3XOXL'}
def dv(source='repod'):
 authority,identifier=PIDS[source].split('/',1)
 v={'versionNumber':1,'versionMinorNumber':0,'versionState':'RELEASED','datasetPersistentId':'doi:'+PIDS[source],'license':{'name':'CC BY 4.0'},'files':[{'restricted':False,'licenseUrl':'https://creativecommons.org/licenses/by/4.0/','dataFile':{'id':7,'filename':'measurements.tab','originalFileName':'measurements.csv','originalFileFormat':'text/csv','originalFileSize':50,'filesize':90,'checksum':{'type':'MD5','value':'a'*32}}}]}
 return {'status':'OK','data':{'protocol':'doi','authority':authority,'identifier':identifier,'latestVersion':v,'hasActiveGuestbook':False,'embargoActive':False}}
def candidate(source='repod'):return {'source_id':source,'external_id':'doi:'+PIDS[source],'metadata':{'version':'1.0'}}
def inv():
 return {'id':'r33k1-nmp76','is_published':True,'is_draft':False,'status':'published','deletion_status':{'is_deleted':False},'versions':{'index':1},'metadata':{'resource_type':{'id':'dataset'},'title':'Education experiment','description':'Participant scores','rights':[{'id':'cc-by-4.0'}]},'access':{'record':'public','files':'public','embargo':{'active':False}},'files':{'enabled':True,'count':1,'entries':{'scores.csv':{'key':'scores.csv','size':51,'checksum':'md5:'+'a'*32,'mimetype':'text/csv','access':{'hidden':False}}}}}
def inv_c():return {'source_id':'muenster-datastore','external_id':'r33k1-nmp76','metadata':{'version':'1'}}

@pytest.mark.asyncio
@pytest.mark.parametrize('source',list(PIDS))
async def test_original_file_manifest_bound_to_version_and_checksum(source):
 fetch=AsyncMock(return_value=dv(source));files=await RepositoriesOct23Adapters(fetch).resolve(candidate(source));assert len(files)==1
 f=files[0];assert f['source_filename']=='measurements.csv' and f['size']==50
 assert f['version']=='1.0' and f['checksum']=='md5:'+'a'*32 and f['license']==('https://creativecommons.org/licenses/by/4.0/' if source=='repod' else 'CC BY 4.0')
 assert f['url']==REPOSITORY_CONFIG[source]['base_url']+'/api/access/datafile/7?format=original' and safe_file_url(source,f['url'])

@pytest.mark.asyncio
async def test_unconverted_original_uses_plain_endpoint_for_older_dataverse():
 p=dv('juelich-data');data=p['data']['latestVersion']['files'][0]['dataFile']
 for key in ('originalFileName','originalFileFormat','originalFileSize'):data.pop(key)
 data['filename']='observations.csv'
 result=await RepositoriesOct23Adapters(AsyncMock(return_value=p)).resolve(candidate('juelich-data'))
 assert result[0]['url']=='https://data.fz-juelich.de/api/access/datafile/7'
 assert result[0]['size']==90 and safe_file_url('juelich-data',result[0]['url'])

@pytest.mark.asyncio
async def test_search_pagination_and_harvested_records():
 row={'type':'dataset','versionState':'RELEASED','global_id':'doi:10.18150/ABC123','majorVersion':1,'minorVersion':0,'name':'Study','fileCount':1}
 fetch=AsyncMock(return_value={'status':'OK','data':{'items':[row,{**row,'global_id':'doi:10.7910/DVN/ABC123'},{**row,'versionState':'DRAFT'}],'total_count':12}})
 r=await RepositoriesOct23Adapters(fetch).search('repod','soil moisture',2);assert len(r['results'])==1 and r['total_count']==12
 p=fetch.call_args.args[1];assert p['start']==10 and 'AND' in p['q'] and p['fq'][0]=='publicationStatus:Published'

@pytest.mark.asyncio
@pytest.mark.parametrize('block',['draft','restricted','embargo','file_embargo','guestbook'])
async def test_nonpublic_or_guestbook_data_not_offered(block):
 p=dv();v=p['data']['latestVersion']
 if block=='draft':v['versionState']='DRAFT'
 elif block=='restricted':v['files'][0]['restricted']=True
 elif block=='embargo':v['files'][0]['embargo']={'dateAvailable':'2099-01-01'}
 elif block=='file_embargo':v['files'][0]['dataFile']['embargo']={'dateAvailable':'2099-01-01'}
 else:p['data']['guestbookId']=1
 assert await RepositoriesOct23Adapters(AsyncMock(return_value=p)).resolve(candidate())==[]

@pytest.mark.asyncio
async def test_no_file_count_truncation():
 p=dv();template=p['data']['latestVersion']['files'][0];p['data']['latestVersion']['files']=[]
 for i in range(150):
  f=copy.deepcopy(template);f['dataFile']['id']=i+1;p['data']['latestVersion']['files'].append(f)
 assert len(await RepositoriesOct23Adapters(AsyncMock(return_value=p)).resolve(candidate()))==150

@pytest.mark.asyncio
@pytest.mark.parametrize('name',['README.csv','codebook.csv','analysis.py','paper.pdf','ASIST_Ethogram.tsv','metadata.json'])
async def test_not_documentation_or_code(name):
 p=dv();p['data']['latestVersion']['files'][0]['dataFile']['originalFileName']=name
 assert await RepositoriesOct23Adapters(AsyncMock(return_value=p)).resolve(candidate())==[]

@pytest.mark.asyncio
async def test_historical_version_and_identity():
 latest=dv();latest['data']['latestVersion']['versionNumber']=2;v=dv()['data']['latestVersion'];fetch=AsyncMock(side_effect=[latest,{'status':'OK','data':v}])
 assert (await RepositoriesOct23Adapters(fetch).resolve(candidate()))[0]['version']=='1.0'
 assert fetch.call_args.args[0].endswith('/versions/1.0')
 v['datasetPersistentId']='doi:10.18150/OTHER1'
 with pytest.raises(SourceConnectorError):await RepositoriesOct23Adapters(AsyncMock(side_effect=[latest,{'status':'OK','data':v}])).resolve(candidate())

@pytest.mark.parametrize('url',['http://repod.icm.edu.pl/api/access/datafile/7?format=original','https://repod.icm.edu.pl.attacker.invalid/api/access/datafile/7?format=original','https://user@repod.icm.edu.pl/api/access/datafile/7?format=original','https://repod.icm.edu.pl/api/access/datafile/7?format=original&extra=1','https://repod.icm.edu.pl/api/access/datafile/7?format=original#part','https://repod.icm.edu.pl/api/access/datafile/7%7f?format=original','https://repod.icm.edu.pl/api/access/datafile/%2e%2e?format=original'])
def test_url_rejects_untrusted_destination(url):assert not safe_file_url('repod',url)
def test_exact_buckets_only():
 u='https://dataverse-prod-user-files.s3.us-west-2.amazonaws.com/10.48349/ASU/1CY5AR/abc-123.orig?signature=opaque'
 assert safe_file_url('asu-research-data',u) and not safe_file_url('repod',u) and not safe_file_url('asu-research-data',u.replace('dataverse-prod-user-files','attacker'))

@pytest.mark.asyncio
async def test_muenster_search_pins_version():
 fetch=AsyncMock(return_value={'hits':{'hits':[inv()],'total':1}});r=await RepositoriesOct23Adapters(fetch).search('muenster-datastore','education scores',1)
 assert r['results'][0]['metadata']['version']=='1' and r['results'][0]['source_url'].endswith('/records/r33k1-nmp76')
 assert 'metadata.resource_type.id:dataset AND' in fetch.call_args.args[1]['q']

@pytest.mark.asyncio
@pytest.mark.parametrize('field',['files_private','record_private','embargo','draft','deleted','publication','disabled'])
async def test_muenster_requires_open_dataset(field):
 r=inv()
 if field=='files_private':r['access']['files']='restricted'
 elif field=='record_private':r['access']['record']='restricted'
 elif field=='embargo':r['access']['embargo']['active']=True
 elif field=='draft':r['is_draft']=True
 elif field=='deleted':r['deletion_status']['is_deleted']=True
 elif field=='publication':r['metadata']['resource_type']['id']='publication-article'
 else:r['files']['enabled']=False
 assert await RepositoriesOct23Adapters(AsyncMock(return_value=r)).resolve(inv_c())==[]

@pytest.mark.asyncio
async def test_muenster_complete_manifest_hash_and_version():
 r=inv();r['files']['count']=2
 with pytest.raises(SourceConnectorError):await RepositoriesOct23Adapters(AsyncMock(return_value=r)).resolve(inv_c())
 r=inv();r['files']['entries']['scores.csv']['checksum']='md5:bad'
 with pytest.raises(SourceConnectorError):await RepositoriesOct23Adapters(AsyncMock(return_value=r)).resolve(inv_c())
 r=inv();r['versions']['index']=2
 with pytest.raises(SourceConnectorError):await RepositoriesOct23Adapters(AsyncMock(return_value=r)).resolve(inv_c())

@pytest.mark.asyncio
async def test_muenster_fresh_manifest_not_caller_url():
 r=inv();result=await RepositoriesOct23Adapters(AsyncMock(return_value=r)).resolve({**inv_c(),'url':'https://attacker.invalid/a.csv'})
 u=result[0]['url'];assert u=='https://datastore.uni-muenster.de/api/records/r33k1-nmp76/files/scores.csv/content'
 assert result[0]['checksum']=='md5:'+'a'*32 and safe_file_url('muenster-datastore',u,'r33k1-nmp76')
 assert not safe_file_url('muenster-datastore',u,'other-ab123')
 r['files']['entries']['scores.csv']['access']['hidden']=True
 assert await RepositoriesOct23Adapters(AsyncMock(return_value=r)).resolve(inv_c())==[]

def test_seven_independent_sources():assert len(DEFINITIONS)==len({r['id'] for r in DEFINITIONS})==7

@pytest.mark.asyncio
async def test_repod_fairchive_manifest_restores_original_excel_and_file_license():
 p=dv();p['data']['latestVersion'].pop('datasetPersistentId');d=p['data']['latestVersion']['files'][0]['dataFile']
 d.pop('originalFileName');d['originalFileFormat']='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
 c=candidate();c['metadata']={}
 r=await RepositoriesOct23Adapters(AsyncMock(return_value=p)).resolve(c)
 assert r[0]['source_filename']=='measurements.xlsx' and r[0]['version']=='1.0'
 assert r[0]['url'].endswith('?format=original') and r[0]['license']=='https://creativecommons.org/licenses/by/4.0/'
 for key in ('hasActiveGuestbook','embargoActive'):
  blocked=copy.deepcopy(p);blocked['data'][key]=True
  assert await RepositoriesOct23Adapters(AsyncMock(return_value=blocked)).resolve(c)==[]

@pytest.mark.asyncio
async def test_repod_search_omitted_version_is_resolved_before_download():
 row={'type':'dataset','global_id':'doi:10.18150/ABC123','name':'Study','published_at':'2026-10-23'}
 fetch=AsyncMock(return_value={'status':'OK','data':{'items':[row],'total_count':1}})
 result=await RepositoriesOct23Adapters(fetch).search('repod','chemistry',1)
 assert len(result['results'])==1 and result['results'][0]['metadata']['version'] is None
 row['versionState']='DRAFT'
 assert (await RepositoriesOct23Adapters(fetch).search('repod','chemistry',1))['results']==[]


def test_observed_bonn_and_muenster_redirects_are_tightly_scoped():
 assert safe_file_url('bonndata','https://bonndata.uni-bonn.de/static/datafile/1581/observations.zip')
 u='https://s3.uni-muenster.de/datastore-data/42/84/db75-c646-44d0-8329-df3e5e3c5583/data?X-Amz-Signature=opaque'
 assert safe_file_url('muenster-datastore',u)
 assert not safe_file_url('muenster-datastore',u.replace('/datastore-data/','/other-bucket/'))
 assert not safe_file_url('muenster-datastore',u.replace('s3.uni-muenster.de','attacker.uni-muenster.de'))
 assert not safe_file_url('bonndata','https://bonndata.uni-bonn.de/static/private/secrets.csv')
