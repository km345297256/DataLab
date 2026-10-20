#!/usr/bin/env python3
from __future__ import annotations
import base64,csv,json,sys,xml.etree.ElementTree as ET
from pathlib import Path
import numpy as np
def A(): return json.loads(base64.urlsafe_b64decode(sys.argv[2]+'='*(-len(sys.argv[2])%4)))
def emit(v,o=None):
 v={'success':True,**v}
 if o: p=Path(o);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(v,ensure_ascii=False,indent=2),encoding='utf-8')
 print(json.dumps(v,ensure_ascii=False))
def identify(path):
 p=Path(path); raw=p.read_bytes()[:4096]; text=raw.decode('utf-8',errors='ignore'); kind='binary_or_unknown'; confidence='low'
 if text.lstrip().startswith('<'): kind='xml'; confidence='high'
 elif text.lstrip().startswith(('{','[')): kind='json'; confidence='high'
 elif b'\x00' not in raw and any(c in text for c in ',;\t'): kind='delimited_text'; confidence='medium'
 elif b'\x00' not in raw: kind='text'; confidence='medium'
 return {'extension':p.suffix.lower(),'format':kind,'confidence':confidence,'header_preview':text[:1000]}
def read(path):
 info=identify(path); p=Path(path)
 if info['format']=='xml':
  root=ET.parse(p).getroot(); return info,{'root':root.tag,'elements':sum(1 for _ in root.iter()),'metadata':{c.tag.rsplit('}',1)[-1]:((c.text or '').strip()[:500]) for c in root.iter() if c.text and len((c.text or '').strip())<500}}
 if info['format']=='json': return info,json.loads(p.read_text(encoding='utf-8'))
 text=p.read_text(encoding='utf-8',errors='replace'); lines=text.splitlines(); delim='\t' if '\t' in (lines[0] if lines else '') else (';' if ';' in (lines[0] if lines else '') else ','); rows=list(csv.reader(lines,delimiter=delim))[:10000]; return info,{'delimiter':delim,'rows':rows,'row_count':len(lines),'columns':len(rows[0]) if rows else 0}
def main():
 op=sys.argv[1]; a=A(); path=a['input_path'];
 if op=='jip_identify_format': emit(identify(path),a.get('output_path')); return
 info,data=read(path)
 if op=='jip_inspect': emit({'format':info,'data_summary':{k:v for k,v in data.items() if k not in {'rows','metadata'}},'metadata':data.get('metadata',{})},a.get('output_path')); return
 if op=='jip_validate':
  issues=[]
  if info['format']=='binary_or_unknown': issues.append('未识别为可解析的文本、XML、JSON或分隔文本格式')
  if info['format']=='delimited_text' and data.get('columns',0)<2: issues.append('分隔文本列数不足')
  emit({'valid':not issues,'issues':issues,'format':info},a.get('output_path')); return
 if op in {'jip_extract_metadata','jip_extract_data'}: emit({'format':info,'metadata':data.get('metadata',{}),'data':data if op=='jip_extract_data' else None},a.get('output_path')); return
 if op=='jip_batch_profile': emit({'files':[{'path':str(p),'format':identify(p)} for p in Path(path).parent.glob(Path(path).name)]},a.get('output_path')); return
 if op in {'jip_baseline_correct','jip_smooth','jip_normalize','jip_peak_detect','jip_peak_fit','jip_compare_series'}:
  rows=data.get('rows',[]); numeric=[]
  for row in rows[1:]:
   try: numeric.append(float(row[-1]))
   except: pass
  y=np.asarray(numeric,dtype=float); x=np.arange(len(y))
  if op=='jip_baseline_correct' and len(y): y=y-np.minimum.accumulate(y)
  if op=='jip_smooth' and len(y)>5:
   from scipy.signal import savgol_filter; y=savgol_filter(y,min(len(y)//2*2-1,11),3)
  if op=='jip_normalize' and len(y) and y.max()!=0: y=y/y.max()
  if op in {'jip_peak_detect','jip_peak_fit'}:
   from scipy.signal import find_peaks; ids,_=find_peaks(y,prominence=float(a.get('prominence',0))); emit({'peaks':[{'index':int(i),'value':float(y[i])} for i in ids],'note':'通用 JIP 数值列峰识别'},a.get('output_path')); return
  emit({'rows':[{'index':int(i),'value':float(v)} for i,v in zip(x,y)],'operation':op},a.get('output_path')); return
 if op=='jip_visualize':
  import matplotlib;matplotlib.use('Agg');import matplotlib.pyplot as plt
  rows=data.get('rows',[]); vals=[]
  for row in rows[1:]:
   try: vals.append(float(row[-1]))
   except: pass
  out=a.get('output_path') or 'jip.png'; Path(out).parent.mkdir(parents=True,exist_ok=True); plt.plot(vals);plt.xlabel('index');plt.ylabel('value');plt.tight_layout();plt.savefig(out,dpi=150);plt.close();emit({'output_path':out},None);return
 if op=='jip_convert_csv':
  out=Path(a['output_path']);out.parent.mkdir(parents=True,exist_ok=True); rows=data.get('rows',[])
  with out.open('w',newline='',encoding='utf-8') as f: csv.writer(f).writerows(rows)
  emit({'output_path':str(out),'rows':len(rows)},None);return
 if op=='jip_export_report': emit({'format':info,'summary':data if info['format']!='delimited_text' else {'rows':data.get('row_count'),'columns':data.get('columns')}},a.get('output_path'));return
 raise ValueError('unsupported JIP operation')
if __name__=='__main__':main()
