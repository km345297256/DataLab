"""Official complete statistical files; WHO JSON pages are streamed to staging.

No user-supplied URL is fetched. The national catalogues are re-read at resolve,
with explicit all-values selections for PxWeb (its omitted dimensions can
otherwise silently collapse to totals). Ingestion staging is not an input cache.
"""
from __future__ import annotations
import asyncio
import csv
import hashlib
import io
import json
import math
import re
import time
from datetime import datetime, timezone
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit
from app.domain.external.data_source import SourceConnectorError
from app.infrastructure.external.data_sources.open_data_transfer import Transfer, storage_error

SOURCES = ('malaysia-data-gov', 'norway-ssb', 'who-gho')
MY = 'https://data.gov.my'
NO = 'https://data.ssb.no/api/pxwebapi/v2'
WHO = 'https://ghoapi.azureedge.net/api'
WHO_TERMS = 'https://www.who.int/about/policies/publishing/data-policy/terms-and-conditions'
CC_BY = 'https://creativecommons.org/licenses/by/4.0/'
_MY_ID = r'[a-z][a-z0-9_-]{0,119}'
_WHO_ID = r'[A-Z][A-Za-z0-9_]{0,119}'
_DIM = r'[A-Za-z][A-Za-z0-9_]{0,79}'


def _valid_id(source, value):
    pattern = {'malaysia-data-gov': _MY_ID, 'norway-ssb': r'[0-9]{5}', 'who-gho': _WHO_ID}.get(source)
    if not pattern or not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise SourceConnectorError('invalid_candidate')
    return value


def safe_file_url(source, url, external=None):
    try:
        if not isinstance(url, str) or len(url) > 16000 or any(ord(c) <= 32 or ord(c) == 127 for c in url):
            return False
        p = urlsplit(url); path = unquote(p.path)
        if (p.scheme != 'https' or p.username is not None or p.password is not None or p.port not in (None,443)
                or p.fragment or '%' in path or '\\' in path or any(ord(c) < 32 or ord(c) == 127 for c in path)
                or re.search(r'%2f|%5c|%2e', p.path, re.I) or any(x in {'.','..'} for x in path.split('/'))):
            return False
        pairs = parse_qsl(p.query, keep_blank_values=True); params = dict(pairs)
        if len(pairs) != len(params): return False
        if source == 'malaysia-data-gov':
            return (p.hostname in {'storage.data.gov.my', 'storage.dosm.gov.my'} and not p.query
                    and bool(re.fullmatch(r'/[A-Za-z0-9_-]+(?:/[A-Za-z0-9_.-]+)*\.(?:csv|parquet)', path)))
        if source == 'who-gho':
            match = re.fullmatch('/api/('+_WHO_ID+')', path)
            return p.hostname == 'ghoapi.azureedge.net' and not p.query and bool(match) and (external is None or match[1] == external)
        if source == 'norway-ssb':
            match = re.fullmatch(r'/api/pxwebapi/v2/tables/([0-9]{5})/data', path)
            if p.hostname != 'data.ssb.no' or not match or external is not None and match[1] != external: return False
            if params.get('lang') != 'en' or params.get('outputFormat') != 'csv': return False
            dims = params.get('stub','').split(',')
            if not dims or len(dims) != len(set(dims)) or not all(re.fullmatch(_DIM,d) for d in dims): return False
            if set(params) != {'lang','outputFormat','stub', *('valueCodes['+d+']' for d in dims)}: return False
            return all(params['valueCodes['+d+']'] == '*' or all(re.fullmatch(r'[A-Za-z0-9_.+ -]{1,100}',v)
                       for v in params['valueCodes['+d+']'].split(',')) for d in dims)
    except (ValueError, TypeError, AttributeError): pass
    return False


def _option(source, external, url, filename, version, *, license=CC_BY, **extra):
    from app.infrastructure.external.data_sources.open_data_sources import _identity, _filename
    if not safe_file_url(source, url, external): raise SourceConnectorError('unsafe_download_url')
    return dict(id=_identity(source, external, version, url), source_id=source, external_id=external,
                url=url, filename=_filename(filename), source_filename=filename, size=None, checksum=None,
                content_type='application/json' if filename.endswith('.json') else 'application/vnd.apache.parquet' if filename.endswith('.parquet') else 'text/csv',
                version=version, license=license, available=True, reason='', kind='完整源数据', **extra)


def _query(query):
    if not isinstance(query,str) or not query.strip() or len(query)>500: raise SourceConnectorError('invalid_query')
    query=query.strip()
    if re.fullmatch(r'"[^"\n]+"',query):query=query[1:-1]
    if query in {'""', "''"}:raise SourceConnectorError('invalid_query')
    return query


def _partitions(codes, max_cells=800000):
    """Exact cartesian partition, never omit dimensions or truncate large tables."""
    pending=[codes]
    while pending:
        part=pending.pop()
        cells=math.prod(len(v) for v in part.values())
        if not cells: raise SourceConnectorError('invalid_response')
        # Bound query-string length too; '*' replaces unpartitioned dimensions.
        oversized=[k for k,v in part.items() if v != codes[k] and len(','.join(v))>2000]
        if cells <= max_cells and not oversized:
            yield part;continue
        dim=max(oversized or part, key=lambda k:len(part[k]))
        values=part[dim]
        if len(values)<2:raise SourceConnectorError('invalid_response')
        middle=len(values)//2
        pending.extend([{**part,dim:values[middle:]},{**part,dim:values[:middle]}])


def _who_page(data, external, limit):
    if (not isinstance(data,dict) or data.get('@odata.context') != WHO+'/$metadata#'+external
            or type(data.get('@odata.count')) is not int or data['@odata.count']<0
            or not isinstance(data.get('value'),list) or len(data['value'])>limit):
        raise SourceConnectorError('invalid_response')
    for row in data['value']:
        if (not isinstance(row,dict) or type(row.get('Id')) is not int or row['Id']<0
                or row.get('IndicatorCode') != external or not row.get('SpatialDim') or row.get('TimeDim') is None
                or not any(key in row for key in ('Value','NumericValue'))):
            raise SourceConnectorError('invalid_response')
    return data['value'],data['@odata.count']


def _observation(row):
    value=row.get('NumericValue')
    return (type(value) in (int,float) and math.isfinite(value)) or (isinstance(row.get('Value'),str)
            and row['Value'].strip().lower() not in {'','no data','n/a','null','none','...','..','.'})


class StatisticsOct23Adapters:
    def __init__(self, fetch_json, fetch_text):
        self._json,self._text=fetch_json,fetch_text
        self._catalog=None;self._catalog_at=0;self._lock=asyncio.Lock()

    async def _my_catalog(self):
        async with self._lock:
            if self._catalog is None or time.monotonic()-self._catalog_at>3600:
                raw=await self._text('https://storage.data.gov.my/metrics/dataset_list.csv')
                reader=csv.DictReader(io.StringIO(raw))
                if not {'id','title_en','title_bm','source'}<=set(reader.fieldnames or []):raise SourceConnectorError('invalid_response')
                rows=list(reader)
                if not rows or len({r['id'] for r in rows}) != len(rows):raise SourceConnectorError('invalid_response')
                for row in rows:_valid_id('malaysia-data-gov',row['id'])
                self._catalog=rows;self._catalog_at=time.monotonic()
            return self._catalog

    async def search(self, source, query, page):
        from app.infrastructure.external.data_sources.open_data_sources import _candidate
        if source not in SOURCES:raise SourceConnectorError('unknown_source')
        if type(page) is not int or not 1<=page<=100:raise SourceConnectorError('invalid_cursor')
        query=_query(query)
        if source=='malaysia-data-gov':
            terms=query.casefold().split()
            rows=[r for r in await self._my_catalog() if all(t in ' '.join(str(r.get(k,'')) for k in ('id','title_en','title_bm','category_en','subcategory_en','source')).casefold() for t in terms)]
            return dict(results=[_candidate(source,r['id'],r['title_en'],r['title_bm']+' · '+r['source'],MY+'/data-catalogue/'+r['id'],
                          license=CC_BY, coverage={'start':r.get('dataset_begin'),'end':r.get('dataset_end'),'geography':r.get('geography')})
                          for r in rows[(page-1)*10:page*10]],total_count=len(rows))
        if source=='norway-ssb':
            data=await self._json(NO+'/tables',{'lang':'en','query':query,'pageSize':10,'pageNumber':page,'includeDiscontinued':'true'})
            if (not isinstance(data,dict) or not isinstance(data.get('tables'),list) or len(data['tables'])>10
                    or type(data.get('page',{}).get('totalElements')) is not int):raise SourceConnectorError('invalid_response')
            rows=[]
            for r in data['tables']:
                external=_valid_id(source,r.get('id'))
                if r.get('category')!='public':continue
                rows.append(_candidate(source,external,r['label'],r.get('description') or 'Statistics Norway · '+', '.join(r.get('variableNames',[])),
                                       'https://www.ssb.no/en/statbank/table/'+external,license=CC_BY,
                                       metadata={'version':r.get('updated')},coverage={'start':r.get('firstPeriod'),'end':r.get('lastPeriod'),'scope':'表中全部维度，超大表按源站接口限制分片'}))
            return dict(results=rows,total_count=data['page']['totalElements'])
        literal=query.replace("'","''")
        condition="IndicatorCode eq '"+literal+"'" if re.fullmatch(_WHO_ID,query) and '_' in query else "contains(tolower(IndicatorName),'"+literal.lower()+"')"
        data=await self._json(WHO+'/Indicator',{'$filter':condition,'$orderby':'IndicatorCode','$count':'true','$top':10,'$skip':(page-1)*10})
        if not isinstance(data,dict) or not isinstance(data.get('value'),list) or type(data.get('@odata.count')) is not int or len(data['value'])>10:raise SourceConnectorError('invalid_response')
        rows=[]
        for r in data['value']:
            external=_valid_id(source,r.get('IndicatorCode'))
            rows.append(_candidate(source,external,r['IndicatorName'],'WHO GHO · 全部公开观测维度',WHO+'/'+external,license=WHO_TERMS,
                                   coverage={'scope':'该指标所有公开国家、时期和维度；获取时分页汇总为JSON'}))
        return dict(results=rows,total_count=data['@odata.count'])

    async def resolve(self,candidate):
        source=candidate['source_id'];external=_valid_id(source,candidate.get('external_id'))
        if source=='malaysia-data-gov':
            if not any(r['id']==external for r in await self._my_catalog()):return []
            raw=await self._text(MY+'/data-catalogue/'+external,accept='text/html')
            match=re.search(r'<script\b(?=[^>]*\bid=["\']__NEXT_DATA__["\'])[^>]*>(.*?)</script>',raw,re.S)
            if not match:raise SourceConnectorError('invalid_response')
            try:row=json.loads(match[1])['props']['pageProps']
            except (ValueError,KeyError,TypeError):raise SourceConnectorError('invalid_response') from None
            if row.get('id')!=external or row.get('meta',{}).get('id')!=external:raise SourceConnectorError('invalid_response')
            options=[]
            for ext in ('csv','parquet'):
                url=row.get('link_'+ext)
                if url and safe_file_url(source,url,external):
                    options.append(_option(source,external,url,external+'.'+ext,row.get('last_updated'),record_url=MY+'/data-catalogue/'+external, coverage={'data_as_of':row.get('data_as_of'),'scope':'源站当前文件的覆盖范围；分期发布的数据不代表全部历史分期'}))
            return options
        if source=='norway-ssb':
            data=await self._json(NO+'/tables/'+external+'/metadata',{'lang':'en'})
            try:
                if data['class']!='dataset' or data['extension']['px']['tableid']!=external:raise ValueError()
                ids=data['id'];sizes=data['size'];dimensions=data['dimension'];codes={}
                if not isinstance(ids,list) or len(ids)!=len(set(ids)) or len(ids)!=len(sizes) or not ids:raise ValueError()
                for key,size in zip(ids,sizes):
                    if not re.fullmatch(_DIM,key) or type(size) is not int or size<1:raise ValueError()
                    index=dimensions[key]['category']['index']
                    values=index if isinstance(index,list) else sorted(index,key=index.get)
                    if len(values)!=size or len(set(values))!=size or not all(isinstance(v,str) and re.fullmatch(r'[A-Za-z0-9_.+ -]{1,100}',v) for v in values):raise ValueError()
                    codes[key]=values
            except (KeyError,TypeError,ValueError):raise SourceConnectorError('invalid_response') from None
            partitions=list(_partitions(codes));options=[]
            for n,part in enumerate(partitions,1):
                params={'lang':'en','outputFormat':'csv','stub':','.join(ids),**{'valueCodes['+k+']':'*' if part[k]==codes[k] else ','.join(part[k]) for k in ids}}
                url=NO+'/tables/'+external+'/data?'+urlencode(params)
                suffix='' if len(partitions)==1 else f'_part_{n:04d}_of_{len(partitions):04d}'
                options.append(_option(source,external,url,'ssb_'+external+suffix+'.csv',data.get('updated'),
                    record_url='https://www.ssb.no/en/statbank/table/'+external,
                    coverage={'part':n,'parts':len(partitions),'cells':math.prod(map(len,part.values())),'total_cells':math.prod(sizes)},
                    kind_note='须获取全部分片才是完整表' if len(partitions)>1 else '完整统计表'))
            return options
        data=await self._json(WHO+'/Indicator',{'$filter':"IndicatorCode eq '"+external+"'"})
        if not isinstance(data,dict) or len(data.get('value',[]))!=1 or data['value'][0].get('IndicatorCode')!=external:return []
        return [_option(source,external,WHO+'/'+external,'who_'+external+'.json',None,license=WHO_TERMS,
                        record_url=WHO+'/'+external,acquisition_method='api_json_snapshot')]

    async def verify_who(self, options):
        if not options:return []
        option=options[0]
        data=await self._json(option['url'],{'$count':'true','$orderby':'Id','$top':100,'$skip':0})
        rows,total=_who_page(data,option['external_id'],100)
        if not rows or not any(_observation(r) for r in rows):return []
        return [{**option,'verification':{'method':'api_observation_probe','sample_records':len(rows),'source_records':total,
                   'checked_at':datetime.now(timezone.utc).isoformat()},'reason':'已读取官方真实观测，获取时保存完整指标JSON。'}]

    async def download_who(self,candidate,chosen,progress,receipt,*,transfer_key=None):
        transfer=await Transfer(transfer_key).acquire();transfer.reset();stream=transfer.file
        digest=hashlib.sha256();handed_off=False;count=0;total=None;last_id=-1;observations=0;pages=0
        def write(value):
            body=value.encode('utf-8');transfer.write(body);digest.update(body)
        try:
            write('{"@odata.context":'+json.dumps(WHO+'/$metadata#'+chosen['external_id'])+',"value":[')
            while total is None or count<total:
                data=await self._json(chosen['url'],{'$count':'true','$orderby':'Id','$top':1000,'$skip':count})
                rows,found=_who_page(data,chosen['external_id'],1000)
                if total is not None and found!=total:raise SourceConnectorError('inconsistent_source_snapshot')
                total=found;pages+=1
                if not rows and count<total:raise SourceConnectorError('inconsistent_source_snapshot')
                for row in rows:
                    if row['Id']<=last_id:raise SourceConnectorError('inconsistent_source_snapshot')
                    last_id=row['Id'];observations+=int(_observation(row))
                    if count:write(',')
                    write(json.dumps(row,ensure_ascii=False,allow_nan=False,separators=(',',':')));count+=1
                if count>total:raise SourceConnectorError('inconsistent_source_snapshot')
                if progress:await progress(stream.tell(),None)
                if not rows:break
            if not observations:raise SourceConnectorError('no_observations')
            write(']}')
            metadata=receipt(candidate,chosen,digest.hexdigest())
            metadata.update(snapshot_rows=count,snapshot_page_count=pages,acquisition_method='api_json_snapshot',
                            source_api_url=chosen['url'],source_checksum_note='完整JSON由官方API分页汇总；SHA-256为本地计算。')
            transfer.save({'source':{'url':chosen['url'],'kind':'api_json_snapshot'},'complete':True,'bytes_saved':stream.tell(),'prefix_sha256':digest.hexdigest()})
            handed_off=True
            return transfer.reader(),chosen['filename'],'application/json',metadata
        except (KeyError, TypeError, ValueError, AttributeError):
            raise SourceConnectorError('invalid_response') from None
        except OSError as error:raise storage_error(error) from None
        finally:
            if not handed_off:transfer.close()
