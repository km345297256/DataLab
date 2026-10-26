"""Public physical data from COD, NIST ThermoML and NOMAD.

The adapters return original entity files; shared transfer handles resumable
streaming into object storage. Metadata caches never contain source replicas.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import math
import re
import shlex
import time
import zipfile
import xml.etree.ElementTree as ET
from collections import OrderedDict
from urllib.parse import unquote, urlsplit
from app.domain.external.data_source import SourceConnectorError

SOURCES = ('crystallography-open-database', 'nist-thermoml', 'nomad')
COD = 'https://www.crystallography.net/cod'
NIST = 'https://trc.nist.gov/ThermoML'
NIST_API = 'https://trc.nist.gov/ThermoML-API/objects'
NOMAD = 'https://nomad-lab.eu/backend/api/v1'
CC0 = 'https://creativecommons.org/publicdomain/zero/1.0/'
NIST_LICENSE = 'https://www.nist.gov/open/copyright-fair-use-and-licensing-statements-srd-data-software-and-technical-series-publications'
_DOI = r'10\.(?:1007|1016|1021)/[A-Za-z0-9][A-Za-z0-9_.()-]{0,180}'
_NOMAD_ID = r'[A-Za-z0-9_-]{20,40}'
_NIST_CONTEXT = json.dumps({'Citation':True,'data_summary':True,'THERMOML_MD5_CHECKSUM':True},separators=(',',':'))
SUPPORTED_PROGRAMS = ('VASP', 'Gaussian', 'Quantum Espresso', 'CP2K', 'FHI-aims')
_ELEMENTS = set('H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co Ni Cu Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb Te I Xe Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os Ir Pt Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm Md No Lr Rf Db Sg Bh Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og'.split())


def _reduced_formula(query):
    """NOMAD/OPTIMADE reduced formulas use alphabetical elements and gcd ratios."""
    if not re.fullmatch(r'(?:[A-Z][a-z]?[0-9]*)+',query):return None
    counts={}
    for element,number in re.findall(r'([A-Z][a-z]?)([0-9]*)',query):
        if element not in _ELEMENTS or len(number)>6 or number and int(number)==0:return None
        counts[element]=counts.get(element,0)+int(number or 1)
    divisor=math.gcd(*counts.values())
    return ''.join(element+(str(counts[element]//divisor) if counts[element]//divisor!=1 else '') for element in sorted(counts))


def _id(source, value):
    pattern = {SOURCES[0]:r'[1-9][0-9]{6}', 'nist-thermoml':_DOI, 'nomad':_NOMAD_ID}.get(source)
    if not pattern or not isinstance(value,str) or not re.fullmatch(pattern,value):
        raise SourceConnectorError('invalid_candidate')
    return value


def safe_file_url(source, url, external=None):
    try:
        if not isinstance(url,str) or len(url)>1000 or any(ord(c)<=32 or ord(c)==127 for c in url):return False
        p=urlsplit(url);path=unquote(p.path)
        if (p.scheme!='https' or p.username is not None or p.password is not None or p.port not in (None,443)
                or p.query or p.fragment or '%' in path or '\\' in path or re.search(r'%2f|%5c|%2e',p.path,re.I)
                or any(x in {'.','..'} for x in path.split('/'))):return False
        if source==SOURCES[0]:
            match=re.fullmatch(r'/cod/([1-9][0-9]{6})\.cif@([1-9][0-9]*)',path)
            return p.hostname=='www.crystallography.net' and bool(match) and (external is None or match[1]==external)
        if source=='nist-thermoml':
            match=re.fullmatch(r'/ThermoML/('+_DOI+r')\.xml',path)
            return p.hostname=='trc.nist.gov' and bool(match) and (external is None or match[1]==external)
        if source=='nomad':
            match=re.fullmatch(r'/backend/api/v1/entries/('+_NOMAD_ID+r')/raw',path)
            return p.hostname=='nomad-lab.eu' and bool(match) and (external is None or match[1]==external)
    except (ValueError,TypeError):pass
    return False


def _option(source, external, url, filename, version, license, **fields):
    from app.infrastructure.external.data_sources.open_data_sources import _filename,_identity
    if not safe_file_url(source,url,external):raise SourceConnectorError('unsafe_download_url')
    return dict(id=_identity(source,external,version,url),source_id=source,external_id=external,url=url,
                filename=_filename(filename),source_filename=filename,size=None,checksum=None,
                content_type={SOURCES[0]:'chemical/x-cif','nist-thermoml':'application/xml','nomad':'application/zip'}[source],
                version=version,license=license,available=True,reason='',kind='完整源数据',**fields)


def _valid_cod(row):
    return (isinstance(row,dict) and 'has coordinates' in str(row.get('flags','')).lower()
            and not row.get('duplicateof') and not row.get('status')
            and not re.search(r'error|retract',str(row.get('flags','')),re.I))


def _nist_record(row, external=None):
    if not isinstance(row,dict):raise SourceConnectorError('invalid_response')
    citation=row.get('Citation',{});doi=_id('nist-thermoml',citation.get('sDOI'))
    if external is not None and doi!=external:raise SourceConnectorError('invalid_candidate')
    total=row.get('data_summary',{}).get('total',{})
    if type(total.get('data_points')) is not int or total['data_points']<1:return None
    checksum=row.get('THERMOML_MD5_CHECKSUM')
    if not isinstance(checksum,str) or not re.fullmatch(r'[0-9a-fA-F]{32}',checksum):raise SourceConnectorError('invalid_response')
    return doi,citation,total,checksum.lower()


def _nomad_public(row):
    return (isinstance(row,dict) and row.get('published') is True and row.get('processed') is True
            and row.get('with_embargo') is False and bool(row.get('results',{}).get('material',{}).get('elements'))
            and row.get('results',{}).get('method',{}).get('simulation',{}).get('program_name') in SUPPORTED_PROGRAMS)


def _calculation_output(data):
    """Recognize numerical/structural outputs, never a readme or job script."""
    lower=data.lower()
    return ((b'<modeling' in lower and (b'<atominfo' in lower or b'<structure' in lower))
            or (b'gaussian' in lower and any(x in lower for x in (b'scf done:',b'standard orientation:',b'input orientation:')))
            or (b'program pwscf' in lower and b'number of atoms/cell' in lower)
            or (b'vasp.' in lower and b'ions per type' in lower)
            or (b'cp2k|' in lower and b'atomic coordinates' in lower)
            or (b'fhi-aims' in lower and b'number of atoms' in lower)
            or (b'_atom_site_fract_x' in lower and b'_cell_length_a' in lower))


def _raw_path(path):
    return (isinstance(path,str) and 0<len(path)<8192 and not path.startswith('/') and '\\' not in path
            and not any(ord(c)<32 or ord(c)==127 for c in path) and not any(p in ('','.','..') for p in path.split('/')))


class ChemMaterialsFoundationAdapters:
    def __init__(self,fetch_json,fetch_text):
        self._json,self._text=fetch_json,fetch_text
        self._cod_cache=OrderedDict();self._cod_lock=asyncio.Lock()

    async def _cod_rows(self,query):
        async with self._cod_lock:
            cached=self._cod_cache.get(query)
            if cached and time.monotonic()-cached[0]<300:
                self._cod_cache.move_to_end(query);return cached[1]
            params={'format':'json', 'id' if re.fullmatch(r'[1-9][0-9]{6}',query) else 'text':query}
            data=await self._json(COD+'/result',params)
            if not isinstance(data,list) or any(not isinstance(r,dict) for r in data):raise SourceConnectorError('invalid_response')
            rows=[r for r in data if _valid_cod(r)]
            for r in rows:
                _id(SOURCES[0],r.get('file'))
                if not re.fullmatch(r'[1-9][0-9]*',str(r.get('svnrevision',''))):raise SourceConnectorError('invalid_response')
            if len({r['file'] for r in rows})!=len(rows):raise SourceConnectorError('invalid_response')
            rows.sort(key=lambda r:r['file'])
            # Bound aggregate metadata memory; this is not a data-download limit.
            cost=len(json.dumps(rows,ensure_ascii=False))
            if cost<=1024*1024:
                self._cod_cache[query]=(time.monotonic(),rows,cost)
                while len(self._cod_cache)>16 or sum(x[2] for x in self._cod_cache.values())>4*1024*1024:
                    self._cod_cache.popitem(last=False)
            return rows

    async def search(self,source,query,page):
        from app.infrastructure.external.data_sources.open_data_sources import _candidate
        if source not in SOURCES:raise SourceConnectorError('unknown_source')
        if type(page) is not int or not 1<=page<=100:raise SourceConnectorError('invalid_cursor')
        if not isinstance(query,str) or not query.strip() or len(query)>500:raise SourceConnectorError('invalid_query')
        query=query.strip()
        if re.fullmatch(r'"[^"\n]+"',query):query=query[1:-1]
        if not query.strip() or query in {'""',"''"}:raise SourceConnectorError('invalid_query')
        if source==SOURCES[0]:
            rows=await self._cod_rows(query)
            return dict(results=[_candidate(source,r['file'],r.get('mineral') or r.get('chemname') or r.get('commonname') or r.get('title') or 'COD '+r['file'],
                        ' · '.join(str(x) for x in (r.get('formula'),r.get('title')) if x),COD+'/'+r['file']+'.html',license=CC0,
                        metadata={'version':str(r['svnrevision'])},coverage={'scope':'单个晶体结构完整 CIF'}) for r in rows[(page-1)*10:page*10]],total_count=len(rows))
        if source=='nist-thermoml':
            # Literal terms; the service also supports Lucene, but user text cannot inject operators.
            literal=' AND '.join('"'+term.replace('\\','\\\\').replace('"','\\"')+'"' for term in query.split())
            data=await self._json(NIST_API,{'query':literal,'pageNum':page-1,'pageSize':10,'sortFields':'id','requestContext':_NIST_CONTEXT})
            if not isinstance(data,dict) or not isinstance(data.get('results'),list) or len(data['results'])>10 or type(data.get('size')) is not int:raise SourceConnectorError('invalid_response')
            out=[]
            for item in data['results']:
                parsed=_nist_record(item.get('content'))
                if not parsed:continue
                doi,cit,total,checksum=parsed
                if item.get('id')!='20.5000.trc.thermoml/'+doi:raise SourceConnectorError('invalid_candidate')
                out.append(_candidate(source,doi,cit.get('sTitle') or doi,cit.get('sAbstract') or '',NIST+'/'+doi+'.html',license=NIST_LICENSE,
                                      metadata={'version':cit.get('dateCit'),'source_checksum':'md5:'+checksum},coverage={'data_points':total['data_points'],'scope':'该记录全部已归档实验数据'}))
            return dict(results=out,total_count=data['size'])
        formula=_reduced_formula(query)
        condition=({'results.material.chemical_formula_reduced':formula} if formula is not None
                   else {'text_search_contents':query})
        body={'owner':'public','query':{'and':[condition,{'processed':True},{'domain':'dft'},
              {'results.method.simulation.program_name:any':list(SUPPORTED_PROGRAMS)}]},
              'pagination':{'page_size':10,'page':page,'order_by':'entry_id','order':'asc'}}
        data=await self._json(NOMAD+'/entries/query',method='POST',json=body)
        if not isinstance(data,dict) or not isinstance(data.get('data'),list) or len(data['data'])>10 or type(data.get('pagination',{}).get('total')) is not int:raise SourceConnectorError('invalid_response')
        out=[]
        for row in data['data']:
            if not _nomad_public(row):continue
            external=_id(source,row.get('entry_id'));material=row['results']['material'];method=row['results']['method']['simulation']
            out.append(_candidate(source,external,(material.get('chemical_formula_descriptive') or material.get('chemical_formula_reduced') or '材料')+' · '+method['program_name'],
                                  str(row.get('mainfile','')), 'https://nomad-lab.eu/gui/entry/id/'+external,license=row.get('license'),
                                  metadata={'version':row.get('publish_time')},coverage={'scope':'单条材料计算的完整原始文件包'}))
        return dict(results=out,total_count=data['pagination']['total'])

    async def resolve(self,candidate):
        source=candidate.get('source_id');external=_id(source,candidate.get('external_id'))
        if source==SOURCES[0]:
            # Always re-read current official metadata; never trust cached candidates or URLs.
            rows=await self._json(COD+'/result',{'format':'json','id':external})
            if not isinstance(rows,list) or len(rows)!=1:return []
            row=rows[0]
            if row.get('file')!=external:raise SourceConnectorError('invalid_candidate')
            if not _valid_cod(row):return []
            revision=str(row.get('svnrevision',''))
            if not re.fullmatch(r'[1-9][0-9]*',revision):raise SourceConnectorError('invalid_response')
            return [_option(source,external,COD+'/'+external+'.cif@'+revision,'cod_'+external+'_r'+revision+'.cif',revision,CC0,
                            record_url=COD+'/'+external+'.html')]
        if source=='nist-thermoml':
            row=await self._json(NIST_API+'/20.5000.trc.thermoml/'+external,{'requestContext':_NIST_CONTEXT})
            parsed=_nist_record(row,external)
            if not parsed:return []
            doi,cit,total,checksum=parsed
            option=_option(source,external,NIST+'/'+doi+'.xml','thermoml_'+doi.replace('/','_')+'.xml',str(cit.get('dateCit',''))+':'+checksum,NIST_LICENSE,
                           record_url=NIST+'/'+doi+'.html',coverage={'data_points':total['data_points'],'scope':'该记录全部已归档实验数据'})
            option['checksum']='md5:'+checksum
            # This official endpoint sends compressed-representation Content-Range
            # with identity XML bytes. Use the bounded normal-GET probe centrally.
            option['probe_without_range']=True
            return [option]
        response=await self._json(NOMAD+'/entries/'+external)
        row=response.get('data') if isinstance(response,dict) else None
        if not isinstance(row,dict) or row.get('entry_id')!=external:raise SourceConnectorError('invalid_candidate')
        if not _nomad_public(row):return []
        upload=_id('nomad',row.get('upload_id'))
        listing=await self._json(NOMAD+'/entries/'+external+'/rawdir')
        raw=listing.get('data',{}) if isinstance(listing,dict) else {}
        if listing.get('entry_id')!=external or raw.get('entry_id')!=external or raw.get('upload_id')!=upload or raw.get('mainfile')!=row.get('mainfile'):raise SourceConnectorError('invalid_candidate')
        files=raw.get('files')
        if not isinstance(files,list) or not files:return []
        for f in files:
            if not isinstance(f,dict) or not _raw_path(f.get('path')) or type(f.get('size')) is not int or f['size']<0:raise SourceConnectorError('invalid_response')
        if len({f['path'] for f in files})!=len(files) or not any(f['path']==row['mainfile'] and f['size']>0 for f in files):raise SourceConnectorError('invalid_response')
        files=sorted(({'path':f['path'],'size':f['size']} for f in files),key=lambda f:f['path'])
        version=upload+':'+str(row.get('publish_time',''))
        option=_option(source,external,NOMAD+'/entries/'+external+'/raw','nomad_'+external+'_raw.zip',version,row.get('license'),
                       record_url='https://nomad-lab.eu/gui/entry/id/'+external,source_manifest=files,upload_id=upload,mainfile=row['mainfile'],
                       coverage={'files':len(files),'uncompressed_bytes':sum(f['size'] for f in files),'scope':'单条计算的全部原始文件，含同目录相关文件'})
        # File membership and size changes invalidate previously selected file IDs.
        option['id']=hashlib.sha256((option['id']+json.dumps(files,sort_keys=True)).encode()).hexdigest()[:24]
        return [option]


def _cod_coordinates(body):
    columns=[];values=[];header=False;multiline=False
    try:
        for line in body.decode('utf-8',errors='replace').splitlines():
            if line.startswith(';'):
                multiline=not multiline;continue
            if multiline or not line.strip() or line.lstrip().startswith('#'):continue
            tokens=shlex.split(line,comments=True)
            if not tokens:continue
            if tokens[0]=='loop_':columns=[];values=[];header=True;continue
            if header and tokens[0].startswith('_'):
                columns.extend(tokens);continue
            header=False
            axes=('_atom_site_fract_x','_atom_site_fract_y','_atom_site_fract_z')
            if not all(axis in columns for axis in axes):continue
            if tokens[0].startswith('_'):columns=[];values=[];continue
            values.extend(tokens)
            while len(values)>=len(columns):
                row,values=values[:len(columns)],values[len(columns):]
                coords=[re.sub(r'\([0-9]+\)$','',row[columns.index(axis)]) for axis in axes]
                if all(math.isfinite(float(value)) for value in coords):return True
    except ValueError:pass
    return False


def is_chem_materials_sample(source,body,external,*,complete=False):
    if source==SOURCES[0]:
        return bool(re.search(rb'(?m)^data_'+external.encode()+rb'\s',body) and b'_cell_length_a' in body and _cod_coordinates(body))
    if source=='nist-thermoml':
        # Numerical arrays can occur beyond the probe; resolve requires official data-point counts,
        # and whole-file validation below requires actual finite measurements and matching DOI.
        return b'<DataReport' in body and b'http://www.iupac.org/namespaces/ThermoML' in body and b'<Citation>' in body
    if source=='nomad':return body.startswith(b'PK\x03\x04')
    return False


def validate_chem_materials_stream(source,stream,chosen,*,cancelled=None):
    """Validate complete files with bounded reads. Never extract ZIPs or copy inputs."""
    def check_cancelled():
        if cancelled is not None and cancelled.is_set():raise asyncio.CancelledError()
    try:
        check_cancelled()
        if source=='nist-thermoml':
            parser=ET.XMLPullParser(events=('start','end'));doi=None;values=0;root=False;stack=[];tail=b''
            while chunk:=stream.read(64*1024):
                check_cancelled()
                check=(tail+chunk).upper();tail=chunk[-16:]
                if b'<!DOCTYPE' in check or b'<!ENTITY' in check:raise SourceConnectorError('invalid_response')
                parser.feed(chunk)
                for event,element in parser.read_events():
                    tag=element.tag.rsplit('}',1)[-1]
                    if not root:
                        if event!='start' or element.tag!='{http://www.iupac.org/namespaces/ThermoML}DataReport':raise SourceConnectorError('invalid_response')
                        root=True
                    if event=='start':stack.append(element)
                    if event=='end':
                        if tag=='sDOI':doi=element.text
                        if tag=='nPropValue':
                            if element.text is not None and math.isfinite(float(element.text)):values+=1
                        element.clear()
                        stack.pop()
                        if stack:stack[-1].remove(element)
            parser.close()
            if doi!=chosen['external_id'] or not values:raise SourceConnectorError('no_observations')
            expected=chosen.get('coverage',{}).get('data_points')
            if expected is not None and expected!=values:raise SourceConnectorError('inconsistent_source_snapshot')
        elif source=='nomad':
            expected={chosen['upload_id']+'/'+f['path']:f['size'] for f in chosen['source_manifest']}
            with zipfile.ZipFile(stream) as archive:
                infos=archive.infolist();names=[f.filename for f in infos]
                if len(names)!=len(set(names)) or set(names)!=set(expected)|{'manifest.json'}:raise SourceConnectorError('invalid_response')
                for info in infos:
                    if not _raw_path(info.filename) or (info.external_attr>>16)&0o170000==0o120000:raise SourceConnectorError('invalid_response')
                    if info.filename!='manifest.json' and info.file_size!=expected[info.filename]:raise SourceConnectorError('inconsistent_source_snapshot')
                # This API's manifest describes one entry; paths and sizes above describe
                # the complete raw set. Treat the manifest as bounded metadata.
                if archive.getinfo('manifest.json').file_size>32768:raise SourceConnectorError('invalid_response')
                manifest=json.loads(archive.read('manifest.json'))
                if (not isinstance(manifest,list) or len(manifest)!=1 or manifest[0].get('entry_id')!=chosen['external_id']
                        or manifest[0].get('upload_id')!=chosen['upload_id']
                        or manifest[0].get('mainfile')!=chosen['upload_id']+'/'+chosen['mainfile']):
                    raise SourceConnectorError('invalid_candidate')
                # The processed-public gate above is supplemented by raw numerical output.
                # Inspect bounded pieces of the primary calculation, not documentation.
                found=False;window=b''
                with archive.open(chosen['upload_id']+'/'+chosen['mainfile']) as mainfile:
                    while chunk:=mainfile.read(64*1024):
                        check_cancelled()
                        window=(window+chunk)[-128*1024:]
                        if _calculation_output(window):found=True;break
                if not found:raise SourceConnectorError('no_observations')
                # Reading every member through EOF validates each CRC, while keeping
                # both memory and cancellation latency bounded independently of size.
                for info in infos:
                    check_cancelled()
                    with archive.open(info) as member:
                        while member.read(64*1024):check_cancelled()
        elif source==SOURCES[0]:
            prefix=stream.read(16384)
            if not is_chem_materials_sample(source,prefix,chosen['external_id']):raise SourceConnectorError('no_observations')
    except (ET.ParseError,ValueError,KeyError,TypeError,zipfile.BadZipFile,RuntimeError) as exc:
        raise SourceConnectorError('invalid_response') from exc
    finally:stream.seek(0)
