"""Published integer terms, full MPS models, and exact elliptic-curve records."""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import html
import json
import re
import time
import zlib
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit

from app.domain.external.data_source import SourceConnectorError

SOURCES = ('oeis', 'miplib', 'lmfdb')
OEIS = 'https://oeis.org'
MIP = 'https://miplib.zib.de'
LMF = 'https://www.lmfdb.org'
LMF_API = LMF + '/api/ec_curvedata/'
LICENSES = {'oeis': 'https://creativecommons.org/licenses/by-sa/4.0/',
            'miplib': MIP + '/', 'lmfdb': LMF + '/license'}
PATTERNS = {'oeis': r'A[0-9]{6}', 'miplib': r'[A-Za-z0-9][A-Za-z0-9_.+-]{0,149}',
            'lmfdb': r'[1-9][0-9]{0,19}\.[a-z]{1,12}[1-9][0-9]{0,8}'}


def _id(source, value):
    if not isinstance(value, str) or not re.fullmatch(PATTERNS.get(source, r'(?!)'), value):
        raise SourceConnectorError('invalid_candidate')
    return value


def safe_file_url(source, url, external=None):
    try:
        if not isinstance(url, str) or len(url)>2000 or re.search(r'[\x00-\x20\x7f]',url): return False
        p=urlsplit(url);path=unquote(p.path)
        if (p.scheme!='https' or p.port not in (None,443) or p.username is not None or p.password is not None
                or p.fragment or '%' in path or '\\' in path or re.search(r'%2f|%5c|%2e',p.path,re.I)
                or any(s in {'.','..'} for s in path.split('/'))): return False
        if source=='oeis':
            m=re.fullmatch(r'/(A([0-9]{6}))/b([0-9]{6})\.txt',path)
            return p.hostname=='oeis.org' and not p.query and bool(m) and m[2]==m[3] and (external is None or m[1]==external)
        if source=='miplib':
            m=re.fullmatch(r'/WebData/instances/('+PATTERNS[source]+r')\.mps\.gz',path)
            return p.hostname=='miplib.zib.de' and not p.query and bool(m) and (external is None or m[1]==external)
        if source=='lmfdb':
            pairs=parse_qsl(p.query,keep_blank_values=True);q=dict(pairs);label=q.get('lmfdb_label','')[1:]
            return (p.hostname=='www.lmfdb.org' and path=='/api/ec_curvedata/' and len(pairs)==2
                    and set(q)=={'_format','lmfdb_label'} and q['_format']=='json' and q['lmfdb_label']=='s'+label
                    and bool(re.fullmatch(PATTERNS[source],label)) and (external is None or label==external))
    except (ValueError,TypeError,AttributeError): pass
    return False


def _plain(value):
    return ' '.join(html.unescape(re.sub(r'<[^>]+>',' ',value)).split())


def _oeis_rows(data):
    rows=data if isinstance(data,list) else data.get('results') if isinstance(data,dict) else None
    if rows is None and isinstance(data,dict) and data.get('count')==0: rows=[]
    if not isinstance(rows,list) or len(rows)>10:raise SourceConnectorError('invalid_response')
    for row in rows:
        if (not isinstance(row,dict) or type(row.get('number')) is not int or not 1<=row['number']<=999999
                or not isinstance(row.get('name'),str) or not row['name'].strip()):raise SourceConnectorError('invalid_response')
    if len({r['number'] for r in rows})!=len(rows):raise SourceConnectorError('invalid_response')
    return rows


def _curve_rows(data):
    if not isinstance(data,dict) or data.get('table')!='ec_curvedata' or not isinstance(data.get('data'),list):
        raise SourceConnectorError('invalid_response')
    rows=data['data']
    if len(rows)>100:raise SourceConnectorError('invalid_response')
    for row in rows:
        if not isinstance(row,dict):raise SourceConnectorError('invalid_response')
        _id('lmfdb',row.get('lmfdb_label'))
        if (not isinstance(row.get('ainvs'),list) or len(row['ainvs'])!=5 or any(type(x) is not int for x in row['ainvs'])
                or type(row.get('conductor')) is not int or row['conductor']<1 or type(row.get('rank')) is not int):
            raise SourceConnectorError('invalid_response')
    return rows


def is_math_sample(source, data, external, *, complete=False):
    """Bounded prefix validation; never inflate an entire compressed model."""
    try:
        _id(source,external)
        if source=='miplib':
            if not data.startswith(b'\x1f\x8b'):return False
            raw=zlib.decompressobj(16+zlib.MAX_WBITS).decompress(data,65536)
            return bool(re.search(rb'(?m)^NAME\s+\S+',raw) and re.search(rb'(?m)^ROWS\s*$',raw))
        if source=='oeis':
            lines=data.decode('utf-8-sig').splitlines()
            if not complete:lines=lines[:-1]
            rows=[line.strip() for line in lines if line.strip() and not line.lstrip().startswith('#')]
            return bool(rows) and all(re.fullmatch(r'-?\d+\s+-?\d+',line) for line in rows)
        if source=='lmfdb':
            if complete:
                rows=_curve_rows(json.loads(data));return len(rows)==1 and rows[0]['lmfdb_label']==external
            # The authoritative manifest already validated all record fields.
            text=data.decode('utf-8',errors='ignore')
            return bool(re.search(r'"table"\s*:\s*"ec_curvedata"',text) and '"ainvs"' in text
                        and re.search(r'"lmfdb_label"\s*:\s*"'+re.escape(external)+'"',text))
    except (ValueError,UnicodeError,zlib.error,SourceConnectorError):pass
    return False


def validate_math_stream(source, stream, chosen, *, cancelled=None):
    """Check the whole compressed model without materializing or copying it."""
    if source != 'miplib':return
    found_end=False;tail=b''
    try:
        with gzip.GzipFile(fileobj=stream,mode='rb') as model:
            while True:
                if cancelled is not None and cancelled.is_set():raise asyncio.CancelledError()
                chunk=model.read(64*1024)
                if not chunk:break
                data=tail+chunk
                if re.search(rb'(?m)^ENDATA[ \t]*\r?\n',data):found_end=True
                tail=data[-128:]
        if not found_end and not re.search(rb'(?m)^ENDATA[ \t]*$',tail):raise SourceConnectorError('no_observations')
    except (OSError,EOFError,zlib.error) as exc:
        raise SourceConnectorError('checksum_mismatch') from exc
    finally:stream.seek(0)


class MathFoundationAdapters:
    def __init__(self,fetch_json,fetch_text):
        self._json,self._text=fetch_json,fetch_text
        self._mip_catalog=None;self._mip_at=0;self._mip_lock=asyncio.Lock()
        self._oeis_lock=asyncio.Lock();self._oeis_at=0

    async def _oeis(self,params):
        async with self._oeis_lock:
            delay=1-(time.monotonic()-self._oeis_at)
            if delay>0:await asyncio.sleep(delay)
            try:return await self._json(OEIS+'/search',params)
            finally:self._oeis_at=time.monotonic()

    async def _mip(self):
        async with self._mip_lock:
            if self._mip_catalog is None or time.monotonic()-self._mip_at>3600:
                raw=await self._text(MIP+'/tag_collection.html',accept='text/html')
                expected=re.search(r'Browse below all\s+([0-9,]+)\s+instances',raw)
                bodies=re.findall(r'<tbody\b[^>]*>(.*?)</tbody>',raw,re.S|re.I)
                rows=[]
                for body in bodies:
                    for tr in re.findall(r'<tr\b[^>]*>(.*?)</tr>',body,re.S|re.I):
                        cells=re.findall(r'<td\b[^>]*>(.*?)</td>',tr,re.S|re.I)
                        if not cells:continue
                        m=re.search(r'href=["\']instance_details_([^"\']+)\.html["\']',cells[0])
                        if not m or len(cells)!=12:raise SourceConnectorError('invalid_response')
                        external=_id('miplib',html.unescape(m[1]));values=[_plain(c) for c in cells]
                        if values[0]!=external:raise SourceConnectorError('invalid_response')
                        rows.append(dict(id=external,description=' · '.join(values[1:]),values=values))
                if not rows or len({r['id'] for r in rows})!=len(rows) or expected and len(rows)!=int(expected[1].replace(',','')):
                    raise SourceConnectorError('invalid_response')
                self._mip_catalog=rows;self._mip_at=time.monotonic()
            return self._mip_catalog

    async def search(self,source,query,page):
        from app.infrastructure.external.data_sources.open_data_sources import _candidate
        if source not in SOURCES:raise SourceConnectorError('unknown_source')
        if type(page) is not int or not 1<=page<=100:raise SourceConnectorError('invalid_cursor')
        if not isinstance(query,str) or not query.strip() or len(query)>500:raise SourceConnectorError('invalid_query')
        query=query.strip().strip('"').strip();start=(page-1)*10
        if not query:raise SourceConnectorError('invalid_query')
        if source=='oeis':
            if re.fullmatch(PATTERNS[source],query.upper()):query='id:'+query.upper()
            data=await self._oeis({'q':query,'fmt':'json','start':start});rows=_oeis_rows(data)
            # New OEIS JSON is a list without total: use only a pagination lower bound.
            total=data.get('count') if isinstance(data,dict) else start+len(rows)+(len(rows)==10)
            if type(total) is not int or total<start+len(rows):raise SourceConnectorError('invalid_response')
            return dict(results=[_candidate(source,f'A{r["number"]:06d}',r['name'],
                'OEIS 官方已发布整数数值表；文件覆盖有限项。',OEIS+f'/A{r["number"]:06d}',license=LICENSES[source],
                coverage={'scope':'原始 b-file 已发布的全部有限项'},metadata={'modified':r.get('time') or r.get('created')}) for r in rows],total_count=total)
        if source=='miplib':
            terms=[] if query.casefold() in {'miplib','optimization','mixed integer programming','integer programming','优化','运筹学','混合整数规划'} else query.casefold().split()
            rows=[r for r in await self._mip() if all(t in (r['id']+' '+r['description']).casefold() for t in terms)]
            return dict(results=[_candidate(source,r['id'],'MIPLIB 2017 · '+r['id'],r['description'],MIP+'/instance_details_'+r['id']+'.html',
                license=LICENSES[source],coverage={'scope':'单个实例完整 MPS 优化模型'}) for r in rows[start:start+10]],total_count=len(rows))
        params={'_format':'json','_offset':start,'_sort':'lmfdb_label','_fields':'lmfdb_label,ainvs,conductor,rank,jinv'}
        if re.fullmatch(PATTERNS[source],query):params['lmfdb_label']='s'+query
        else:
            remaining=re.sub(r'(?:conductor|rank)\s*=\s*[0-9]+',' ',query,flags=re.I)
            if remaining.strip().casefold() not in {'','elliptic curves','elliptic curve','椭圆曲线','elliptic curves over q'}:
                return dict(results=[],total_count=0)
            for key,value in re.findall(r'(conductor|rank)\s*=\s*([0-9]+)',query,re.I):
                if len(value)>20 or key.lower() in params:raise SourceConnectorError('invalid_query')
                params[key.lower()]='i'+str(int(value))
        rows=_curve_rows(await self._json(LMF_API,params))
        return dict(results=[_candidate(source,r['lmfdb_label'],'椭圆曲线 '+r['lmfdb_label'],
            f'有理数域 · 导子 {r["conductor"]} · 秩 {r["rank"]} · Weierstrass 系数 {r["ainvs"]}',
            LMF+'/EllipticCurve/Q/'+r['lmfdb_label'],license=LICENSES[source],coverage={'scope':'单条椭圆曲线全部公开字段'}) for r in rows[:10]],
            total_count=start+len(rows)+(len(rows)==100))

    async def resolve(self,candidate):
        from app.infrastructure.external.data_sources.open_data_sources import _identity,_filename
        source=candidate['source_id'];external=_id(source,candidate.get('external_id'));version=None
        if source=='oeis':
            rows=_oeis_rows(await self._oeis({'q':'id:'+external,'fmt':'json'}))
            if not rows:return []
            if len(rows)!=1 or rows[0]['number']!=int(external[1:]):raise SourceConnectorError('invalid_response')
            url=OEIS+'/'+external+'/b'+external[1:]+'.txt'
            links=rows[0].get('link',[])
            if not isinstance(links,list):raise SourceConnectorError('invalid_response')
            # Require an explicitly published numeric file, not OEIS's generated short fallback.
            hrefs=[urljoin(OEIS+'/'+external+'/',html.unescape(h)) for link in links if isinstance(link,str)
                   for h in re.findall(r'href=["\']([^"\']+)["\']',link)]
            if url not in hrefs:return []
            filename=external+'_b'+external[1:]+'.txt';mime='text/plain';version=rows[0].get('time') or rows[0].get('created')
        elif source=='miplib':
            if not any(r['id']==external for r in await self._mip()):return []
            raw=await self._text(MIP+'/instance_details_'+external+'.html',accept='text/html')
            url=MIP+'/WebData/instances/'+external+'.mps.gz'
            hrefs=[urljoin(MIP+'/',html.unescape(h)) for h in re.findall(r'href=["\']([^"\']+)["\']',raw)]
            if url not in hrefs:return []
            filename=external+'.mps.gz';mime='application/gzip';version='MIPLIB2017'
        else:
            url=LMF_API+'?'+urlencode({'_format':'json','lmfdb_label':'s'+external})
            rows=_curve_rows(await self._json(LMF_API,{'_format':'json','lmfdb_label':'s'+external}))
            if not rows:return []
            if len(rows)!=1 or rows[0]['lmfdb_label']!=external:raise SourceConnectorError('invalid_response')
            version=hashlib.sha256(json.dumps(rows[0],sort_keys=True,separators=(',',':')).encode()).hexdigest()
            filename='lmfdb_ec_'+external+'.json';mime='application/json'
        if not safe_file_url(source,url,external):raise SourceConnectorError('unsafe_download_url')
        return [dict(id=_identity(source,external,url,version),source_id=source,external_id=external,url=url,
                     filename=_filename(filename),source_filename=filename,content_type=mime,size=None,checksum=None,
                     version=version,license=LICENSES[source],available=True,reason='',kind='源站完整数值数据')]
