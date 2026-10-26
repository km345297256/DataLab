"""Official complete earth-science files, with explicit station/layer scope.

Metadata only locates data. Shared bounded probes and streaming acquisition verify
and retrieve the original entity; these adapters never copy analysis inputs.
"""
from __future__ import annotations
import asyncio
import base64
import binascii
import html
import re
import struct
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit
from app.domain.external.data_source import SourceConnectorError

SOURCES = ('heasarc', 'earthscope', 'natural-earth', 'worldclim', 'noaa-ghcn-daily')
GHCN = 'https://www.ncei.noaa.gov/pub/data/ghcn/daily/'
HEASARC = 'https://heasarc.gsfc.nasa.gov'
TDAT = HEASARC + '/FTP/heasarc/dbase/tdat_files/'
EARTHSCOPE = 'https://service.earthscope.org/fdsnws/'
NE = 'https://www.naturalearthdata.com'
WC = 'https://www.worldclim.org/data/worldclim21.html'
WC_BASE = 'https://geodata.ucdavis.edu/climate/worldclim/2_1/base/'
WC_VARS = {'tmin':'minimum temperature 最低气温 (°C)', 'tmax':'maximum temperature 最高气温 (°C)',
           'tavg':'average temperature 平均气温 (°C)', 'prec':'precipitation 降水 (mm)',
           'srad':'solar radiation 太阳辐射 (kJ m-2 day-1)', 'wind':'wind speed 风速 (m s-1)',
           'vapr':'water vapor pressure 水汽压 (kPa)', 'bio':'19 bioclimatic variables 生物气候变量',
           'elev':'elevation 高程 (m)'}


def _plain(s):
    return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]*>', ' ', s))).strip()


def _station_params(query):
    if not isinstance(query,str) or len(query)>500:raise SourceConnectorError('invalid_query')
    pairs=re.findall(r'(net|sta|loc|cha|start|end)=([^\s&]+)',query)
    if len(pairs)!=6 or len(dict(pairs))!=6 or re.sub(r'(net|sta|loc|cha|start|end)=[^\s&]+','',query).strip(' &'):
        raise SourceConnectorError('invalid_query')
    values=dict(pairs)
    for k,pattern in [('net',r'[A-Z0-9]{1,2}'),('sta',r'[A-Z0-9]{1,5}'),('loc',r'(?:[A-Z0-9]{2}|--)'),('cha',r'[A-Z0-9]{3}')]:
        if not re.fullmatch(pattern,values[k]):raise SourceConnectorError('invalid_query')
    dates=[]
    for k in ('start','end'):
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z?',values[k]):raise SourceConnectorError('invalid_query')
        try:date=datetime.fromisoformat(values[k].rstrip('Z')).replace(tzinfo=timezone.utc)
        except ValueError:raise SourceConnectorError('invalid_query') from None
        values[k]=date.replace(tzinfo=None).isoformat();dates.append(date)
    if dates[0]>=dates[1]:raise SourceConnectorError('invalid_query')
    return {k:values[k] for k in ('net','sta','loc','cha','start','end')}


def _station_external(external):
    try:
        pairs=parse_qsl(external,keep_blank_values=True)
        values=_station_params(' '.join(k+'='+v for k,v in pairs))
        if urlencode(values)!=external:raise ValueError()
        return values
    except (ValueError,TypeError,SourceConnectorError):raise SourceConnectorError('invalid_candidate') from None


def safe_file_url(source,url,external=None):
    try:
        if not isinstance(url,str) or len(url)>2000 or any(ord(c)<=32 or ord(c)==127 for c in url):return False
        p=urlsplit(url);path=unquote(p.path)
        if (p.scheme!='https' or p.username is not None or p.password is not None or p.port not in (None,443)
                or p.fragment or '%' in path or '\\' in path or re.search(r'%2f|%5c|%2e',p.path,re.I)
                or any(c in {'.','..'} for c in path.split('/'))):return False
        if source=='heasarc':
            m=re.fullmatch(r'/FTP/heasarc/dbase/tdat_files/heasarc_([a-z][a-z0-9_]{0,63})\.tdat\.gz',path)
            return p.hostname=='heasarc.gsfc.nasa.gov' and not p.query and bool(m) and (external is None or external==m[1])
        if source=='natural-earth':
            m=re.fullmatch(r'/naturalearth/(10m|50m|110m)/(cultural|physical)/(ne_\1_[A-Za-z0-9_]+)\.zip',path)
            return p.hostname=='naciscdn.org' and not p.query and bool(m) and (external is None or external=='/'.join(m.groups()))
        if source=='worldclim':
            m=re.fullmatch(r'/climate/worldclim/2_1/base/(wc2\.1_(?:10m|5m|2\.5m|30s)_(?:tmin|tmax|tavg|prec|srad|wind|vapr|bio|elev))\.zip',path)
            return p.hostname=='geodata.ucdavis.edu' and not p.query and bool(m) and (external is None or external==m[1])
        if source=='noaa-ghcn-daily':
            m=re.fullmatch(r'/pub/data/ghcn/daily/all/([A-Z]{2}[A-Za-z0-9_-]{9})\.dly',path)
            return p.hostname=='www.ncei.noaa.gov' and not p.query and bool(m) and (external is None or external==m[1])
        if source=='earthscope':
            pairs=parse_qsl(p.query,keep_blank_values=True);values=dict(pairs)
            if len(pairs)!=len(values) or set(values)!={'net','sta','loc','cha','start','end','format','nodata'}:return False
            if values.pop('format')!='miniseed' or values.pop('nodata')!='404':return False
            key=urlencode(_station_params(' '.join(k+'='+v for k,v in values.items())))
            return p.hostname=='service.earthscope.org' and path=='/fdsnws/dataselect/1/query' and (external is None or key==external)
    except (ValueError,TypeError,AttributeError,SourceConnectorError):pass
    return False


def is_earthscope_sample(data, external):
    """A real miniSEED 2 data record for the exact selected channel, not a page."""
    try:
        p=_station_external(external)
        return (len(data)>=48 and data[:6].isdigit() and data[6:7] in (b'D',b'R',b'Q',b'M') and data[7:8]==b' '
                and data[8:13].decode('ascii').strip()==p['sta'] and data[13:15].decode('ascii').strip()==('' if p['loc']=='--' else p['loc'])
                and data[15:18].decode('ascii')==p['cha'] and data[18:20].decode('ascii').strip()==p['net']
                and 1900<=int.from_bytes(data[20:22],'big')<=2200 and 1<=int.from_bytes(data[22:24],'big')<=366
                and int.from_bytes(data[30:32],'big')>0 and 48<=int.from_bytes(data[44:46],'big')<=len(data))
    except (UnicodeError,ValueError,SourceConnectorError):return False



def is_ghcn_sample(data,external):
    """Fixed-width original records retain measurements, units and quality flags."""
    if not isinstance(external,str) or not re.fullmatch(r'[A-Z]{2}[A-Za-z0-9_-]{9}',external):return False
    try:text=data.decode('ascii')
    except UnicodeError:return False
    rows=text.splitlines()
    if text and not text.endswith(('\n','\r')):rows=rows[:-1] # A bounded probe can end within the next row.
    if not rows:return False
    observations=False
    for row in rows:
        if len(row)!=269 or row[:11]!=external or not row[11:17].isdigit() or not 1<=int(row[15:17])<=12 or not re.fullmatch(r'[A-Z0-9]{4}',row[17:21]):return False
        for day in range(31):
            value=row[21+day*8:26+day*8]
            try:value=int(value)
            except ValueError:return False
            if value!=-9999:observations=True
    return observations


def _ghcn_version(raw):
    match=re.search(r'The current version of GHCN Daily is ([0-9]+\.[0-9]+-(?:upd|por)-[0-9]{10})',raw)
    if not match:raise SourceConnectorError('invalid_response')
    return match[1]


def _ghcn_stations(raw,country_raw,version):
    countries={}
    for line in country_raw.splitlines():
        if not line.strip():continue
        if not re.fullmatch(r'[A-Z]{2} .+',line) or line[:2] in countries:raise SourceConnectorError('invalid_response')
        countries[line[:2]]=line[3:].strip()
    rows={}
    for line in raw.splitlines():
        if not line.strip():continue
        key=line[:11]
        if len(line)<71 or len(line)>85 or not re.fullmatch(r'[A-Z]{2}[A-Za-z0-9_-]{9}',key) or key in rows or key[:2] not in countries:raise SourceConnectorError('invalid_response')
        try:lat=float(line[12:20]);lon=float(line[21:30]);elev=float(line[31:37])
        except ValueError:raise SourceConnectorError('invalid_response') from None
        if not -90<=lat<=90 or not -180<=lon<=180 or not line[41:71].strip():raise SourceConnectorError('invalid_response')
        country=countries[key[:2]];name=line[41:71].strip()
        rows[key]={'external':key,'title':name+' · '+country+' · '+key,
                   'description':country+' · '+line[38:40].strip()+' · 完整单站逐日气候记录（全部源站现有月份与要素；不代表全国覆盖）',
                   'version':version,'url':GHCN+'all/'+key+'.dly','record':GHCN+'readme.txt',
                   'coverage':{'country':country,'country_code':key[:2],'station_id':key,'latitude':lat,'longitude':lon,
                               'elevation_m':None if elev==-999.9 else elev,'scope':'该台站源站完整历史记录；各要素起止时间不同，实际时间与缺测以文件为准'}}
    if not rows:raise SourceConnectorError('invalid_response')
    return rows

def _votable(raw):
    if '<!DOCTYPE' in raw.upper() or '<!ENTITY' in raw.upper():raise SourceConnectorError('invalid_response')
    try:
        root=ET.fromstring(raw)
        for n in root.iter():n.tag=n.tag.rsplit('}',1)[-1]
        statuses=root.findall('.//INFO[@name="QUERY_STATUS"]')
        if not statuses or any(n.get('value')!='OK' for n in statuses):raise ValueError()
        tables=root.findall('.//TABLE')
        if len(tables)!=1:raise ValueError()
        table=tables[0];fields=table.findall('FIELD')
        if [(f.get('name'),f.get('datatype'),f.get('arraysize')) for f in fields]!=[('table_name','char','*'),('description','char','*')]:raise ValueError()
        rows=[];td=table.find('./DATA/TABLEDATA')
        if td is not None:
            for tr in td.findall('TR'):
                cells=tr.findall('TD')
                if len(cells)!=2:raise ValueError()
                rows.append(tuple(c.text or '' for c in cells))
        else:
            stream=table.find('./DATA/BINARY/STREAM')
            if stream is None or stream.get('encoding')!='base64' or stream.get('href'):raise ValueError()
            data=base64.b64decode(re.sub(r'\s+','',stream.text or ''),validate=True);offset=0
            while offset<len(data):
                values=[]
                for _ in range(2):
                    length=struct.unpack('>I',data[offset:offset+4])[0];offset+=4
                    if length>len(data)-offset:raise ValueError()
                    values.append(data[offset:offset+length].decode('utf-8'));offset+=length
                rows.append(tuple(values))
        if not rows or len(rows)>=10000:raise ValueError()
        result={}
        for key,desc in rows:
            key=key.strip('"')
            if not re.fullmatch(r'[a-z][a-z0-9_]{0,63}',key):continue # TAP system schemas have no TDAT entity.
            if key in result:raise ValueError()
            result[key]=desc
        return result
    except (ET.ParseError,ValueError,TypeError,UnicodeError,struct.error,binascii.Error):raise SourceConnectorError('invalid_response') from None


def _tdat_listing(raw):
    rows={}
    for m in re.finditer(r'<a\b[^>]*href=["\'](heasarc_([a-z][a-z0-9_]{0,63})\.tdat\.gz)["\'][^>]*>.*?</a>\s+(\d{4}-\d{2}-\d{2} \d{2}:\d{2})',raw,re.S):
        if m[2] in rows:raise SourceConnectorError('invalid_response')
        rows[m[2]]={'filename':m[1],'version':m[3]}
    if not rows:raise SourceConnectorError('invalid_response')
    return rows


def _ne_catalog(raw,scale,category):
    raw=re.sub(r'<!--.*?-->|<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>','',raw,flags=re.S|re.I)
    rows={};heading=''
    # Consume headings and links in document order so an alternative ZIP inherits its theme.
    pattern=r'<h[23]\b[^>]*>.*?</h[23]>|<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>.*?</a>'
    for m in re.finditer(pattern,raw,re.S|re.I):
        if m[1] is None:heading=_plain(m[0]);continue
        href=html.unescape(m[1]);suffix=re.search(r'/download/'+scale+'/'+category+r'/(ne_'+scale+r'_[A-Za-z0-9_]+)\.zip$',href)
        if not suffix or urlsplit(href).hostname!='www.naturalearthdata.com':continue
        version=re.search(r'version\s+([0-9]+(?:\.[0-9]+)+)',raw[m.end():m.end()+200],re.I)
        if not version:raise SourceConnectorError('invalid_response')
        key='/'.join((scale,category,suffix[1]))
        if key in rows and rows[key]['version']!=version[1]:raise SourceConnectorError('inconsistent_source_snapshot')
        rows[key]={'external':key,'title':heading+' · '+_plain(m[0])+' · '+scale,
                   'description':category+' · '+suffix[1].replace('_',' ')+' · 制图比例尺 1:'+{'10m':'10,000,000','50m':'50,000,000','110m':'110,000,000'}[scale],
                   'version':version[1],'url':'https://naciscdn.org/naturalearth/'+key+'.zip',
                   'record':NE+'/downloads/'+scale+'-'+category+'-vectors/'}
    if not rows:raise SourceConnectorError('invalid_response')
    return rows


def _worldclim_catalog(raw):
    raw=re.sub(r'<!--.*?-->|<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>','',raw,flags=re.S|re.I)
    rows={}
    for href in re.findall(r'<a\b[^>]*href=["\']([^"\']+)["\']',raw,re.I):
        # The official HTML publishes http links; this same public host supports HTTPS.
        href=html.unescape(href)
        if href.startswith('http://geodata.ucdavis.edu/'):href='https://'+href[7:]
        if not safe_file_url('worldclim',href):continue
        name=urlsplit(href).path.rsplit('/',1)[-1][:-4]
        match=re.fullmatch(r'wc2\.1_(10m|5m|2\.5m|30s)_('+ '|'.join(WC_VARS)+')',name)
        if not match:continue
        res,var=match.groups()
        rows[name]={'external':name,'title':'WorldClim 2.1 · '+WC_VARS[var]+' · '+res,
                    'description':('SRTM派生高程参考图层（不代表1970–2000高程平均）；全球陆地；' if var=='elev' else '1970–2000 气候常年值；全球陆地；')+({'elev':'完整高程栅格（SRTM派生）','bio':'完整19项生物气候变量'}.get(var,'完整12个月气候平均值'))+'；分辨率 '+res+'（角分m/角秒s，不是米）',
                    'version':'2.1','url':href,'record':WC}
    if not rows:raise SourceConnectorError('invalid_response')
    return rows


class EarthOct23Adapters:
    def __init__(self,fetch_json,fetch_text):
        self._json,self._text=fetch_json,fetch_text;self._cache={};self._locks={}

    async def _catalog(self,source,force=False,external=None):
        key=source
        if source=='natural-earth' and external is not None:
            parts=external.split('/')
            if len(parts)!=3 or parts[0] not in ('10m','50m','110m') or parts[1] not in ('cultural','physical'):raise SourceConnectorError('invalid_candidate')
            key=source+':'+parts[0]+':'+parts[1]
        async with self._locks.setdefault(key,asyncio.Lock()):
            saved=self._cache.get(key)
            if not force and saved and time.monotonic()-saved[0]<3600:return saved[1]
            if source=='heasarc':
                raw=await self._text(HEASARC+'/xamin/vo/tap/sync',{'REQUEST':'doQuery','LANG':'ADQL','QUERY':'SELECT table_name,description FROM TAP_SCHEMA.tables','MAXREC':10000,'FORMAT':'text/xml'})
                descriptions=_votable(raw);files=_tdat_listing(await self._text(TDAT,accept='text/html'))
                rows={k:{**v,'external':k,'title':descriptions[k],'description':'HEASARC · '+k+' · 官方完整天文目录数据表',
                         'url':TDAT+v['filename'],'record':HEASARC+'/W3Browse/all/'+k+'.html'} for k,v in files.items() if k in descriptions}
            elif source=='natural-earth':
                selections=[(parts[0],parts[1])] if external is not None else [(s,c) for s in ('10m','50m','110m') for c in ('cultural','physical')]
                rows={}
                for scale,category in selections:
                    rows.update(_ne_catalog(await self._text(NE+'/downloads/'+scale+'-'+category+'-vectors/',accept='text/html'),scale,category))
            elif source=='worldclim':rows=_worldclim_catalog(await self._text(WC,accept='text/html'))
            elif source=='noaa-ghcn-daily':
                version=_ghcn_version(await self._text(GHCN+'ghcnd-version.txt'))
                countries=await self._text(GHCN+'ghcnd-countries.txt')
                raw=await self._text(GHCN+'ghcnd-stations.txt',max_bytes=32*1024*1024)
                rows=_ghcn_stations(raw,countries,version)
            else:raise SourceConnectorError('unknown_source')
            if not rows:raise SourceConnectorError('invalid_response')
            self._cache[key]=(time.monotonic(),rows);return rows

    async def _station(self,values):
        raw=await self._text(EARTHSCOPE+'station/1/query',{
            'net':values['net'],'sta':values['sta'],'loc':values['loc'],'cha':values['cha'],
            'starttime':values['start'],'endtime':values['end'],'level':'channel','format':'text',
            'includecomments':'false','includerestricted':'false','nodata':404})
        lines=raw.strip().splitlines()
        if not lines or [v.strip() for v in lines[0].split('|')][:4]!=['#Network','Station','Location','Channel']:raise SourceConnectorError('invalid_response')
        rows=[]
        for line in lines[1:]:
            cells=line.split('|')
            if len(cells)!=17 or cells[:4]!=[values['net'],values['sta'],'' if values['loc']=='--' else values['loc'],values['cha']]:raise SourceConnectorError('invalid_response')
            rows.append(cells)
        return rows

    async def search(self,source,query,page):
        from app.infrastructure.external.data_sources.open_data_sources import _candidate
        if source not in SOURCES:raise SourceConnectorError('unknown_source')
        if type(page) is not int or not 1<=page<=100:raise SourceConnectorError('invalid_cursor')
        if not isinstance(query,str) or not query.strip() or len(query)>500:raise SourceConnectorError('invalid_query')
        if source=='earthscope':
            values=_station_params(query);rows=await self._station(values)
            if not rows or page>1:return dict(results=[],total_count=int(bool(rows)))
            external=urlencode(values)
            candidate=_candidate(source,external,'.'.join(values[k] for k in ('net','sta','loc','cha'))+' · '+values['start']+'—'+values['end'],
                '公开原始 miniSEED 波形；'+str(len(rows))+' 个通道响应时期；原始计数，未去仪器响应。',EARTHSCOPE+'dataselect/1/',
                license='依各台网许可使用并引用数据与EarthScope服务 — https://www.earthscope.org/how-to-cite/',metadata={'version':None},
                coverage={**values,'scope':'仅指定台网、台站、位置、通道和时间范围','channel_epochs':len(rows)},
                limitations=['台网许可与引用：https://www.fdsn.org/networks/detail/'+values['net']+'/'])
            return dict(results=[candidate],total_count=1)
        terms=query.strip().strip('"').casefold().split()
        if not terms:raise SourceConnectorError('invalid_query')
        rows=[r for r in (await self._catalog(source)).values() if all(t in (r['title']+' '+r['description']+' '+r['external']).casefold() for t in terms)]
        rows.sort(key=lambda r:r['external'])
        return dict(results=[_candidate(source,r['external'],r['title'],r['description'],r['record'],license=self._license(source),metadata={'version':r['version']},
                           coverage=r.get('coverage') or {'scope':r['description']}) for r in rows[(page-1)*10:page*10]],total_count=len(rows))

    @staticmethod
    def _license(source):
        return {'heasarc':'免费使用并按任务要求署名 — https://heasarc.gsfc.nasa.gov/docs/heasarc/data_policy.html',
                'natural-earth':'Public Domain — https://www.naturalearthdata.com/about/terms-of-use/',
                'earthscope':'依各台网许可使用并引用数据与EarthScope服务 — https://www.earthscope.org/how-to-cite/',
                'noaa-ghcn-daily':'公开使用，按NOAA与原始提供方要求引用 — https://www.ncei.noaa.gov/access/metadata/landing-page/bin/iso?id=gov.noaa.ncdc:C00861',
                'worldclim':'学术及非商业使用；再分发或商业使用须事先许可 — https://worldclim.org/about.html'}[source]

    async def resolve(self,candidate):
        from app.infrastructure.external.data_sources.open_data_sources import _identity,_filename
        source=candidate['source_id'];external=candidate.get('external_id')
        if source not in SOURCES or not isinstance(external,str):raise SourceConnectorError('invalid_candidate')
        if source=='noaa-ghcn-daily' and not re.fullmatch(r'[A-Z]{2}[A-Za-z0-9_-]{9}',external):raise SourceConnectorError('invalid_candidate')
        if source=='heasarc' and not re.fullmatch(r'[a-z][a-z0-9_]{0,63}',external):raise SourceConnectorError('invalid_candidate')
        if source=='worldclim' and not safe_file_url(source,WC_BASE+external+'.zip',external):raise SourceConnectorError('invalid_candidate')
        if source=='natural-earth' and not safe_file_url(source,'https://naciscdn.org/naturalearth/'+external+'.zip',external):raise SourceConnectorError('invalid_candidate')
        if source=='earthscope':
            values=_station_external(external)
            if not await self._station(values):return []
            url=EARTHSCOPE+'dataselect/1/query?'+urlencode({**values,'format':'miniseed','nodata':'404'})
            filename='_'.join(values[k] for k in ('net','sta','loc','cha'))+'_'+values['start'].replace(':','')+'_'+values['end'].replace(':','')+'.mseed'
            version=None;record=EARTHSCOPE+'dataselect/1/';ctype='application/vnd.fdsn.mseed'
        else:
            rows=await self._catalog(source,force=source!='noaa-ghcn-daily',external=external);row=rows.get(external)
            if row is None:return []
            version=_ghcn_version(await self._text(GHCN+'ghcnd-version.txt')) if source=='noaa-ghcn-daily' else row['version'];old=(candidate.get('metadata') or {}).get('version')
            if old is not None and old!=version:
                self._cache.pop(source,None)
                raise SourceConnectorError('source_version_changed')
            url=row['url'];filename=urlsplit(url).path.rsplit('/',1)[-1];record=row['record'];ctype='text/plain' if source=='noaa-ghcn-daily' else 'application/gzip' if source=='heasarc' else 'application/zip'
        if not safe_file_url(source,url,external):raise SourceConnectorError('unsafe_download_url')
        return [dict(id=_identity(source,external,url,version),source_id=source,external_id=external,url=url,
                     filename=_filename(filename),source_filename=filename,record_url=record,content_type=ctype,
                     size=None,checksum=None,version=version,license=(self._license(source)+'；https://www.fdsn.org/networks/detail/'+values['net']+'/' if source=='earthscope' else self._license(source)),available=True,reason='',kind='源站完整文件')]
