from __future__ import annotations
import base64,json,sys,re
from pathlib import Path
from collections import Counter
import numpy as np

def fail(m): print(json.dumps({'success':False,'error':m},ensure_ascii=False)); raise SystemExit(0)
def emit(d,out=None):
 p={'success':True,**d}
 if out: Path(out).parent.mkdir(parents=True,exist_ok=True); Path(out).write_text(json.dumps(p,ensure_ascii=False,indent=2),encoding='utf-8')
 print(json.dumps(p,ensure_ascii=False,default=str))
def records(path):
 p=Path(path)
 if not p.is_file(): fail('序列文件不存在')
 try:
  from Bio import SeqIO
  return list(SeqIO.parse(str(p),'fastq' if p.suffix.lower() in {'.fastq','.fq'} or (p.read_text(errors='ignore').lstrip().startswith('@')) else 'fasta'))
 except Exception as e: fail(f'序列解析失败: {e}')
def summary(rs):
 lengths=np.array([len(r.seq) for r in rs],int); gc=np.array([(str(r.seq).upper().count('G')+str(r.seq).upper().count('C'))/max(len(r.seq),1)*100 for r in rs])
 return {'sequence_count':len(rs),'length':{'min':int(lengths.min()) if len(lengths) else 0,'max':int(lengths.max()) if len(lengths) else 0,'mean':float(lengths.mean()) if len(lengths) else 0},'gc_percent':{'mean':float(gc.mean()) if len(gc) else 0,'min':float(gc.min()) if len(gc) else 0,'max':float(gc.max()) if len(gc) else 0},'n_fraction_mean':float(np.mean([str(r.seq).upper().count('N')/max(len(r.seq),1) for r in rs])) if rs else 0}
def write_records(rs,path,fastq=False,quality=40):
 from Bio import SeqIO
 Path(path).parent.mkdir(parents=True,exist_ok=True)
 if fastq:
  for r in rs:
   if not r.letter_annotations.get('phred_quality'): r.letter_annotations['phred_quality']=[quality]*len(r.seq)
 SeqIO.write(rs,path,'fastq' if fastq else 'fasta')
def blast_rows(path):
    rows=[]
    fields=['qseqid','sseqid','pident','length','mismatch','gapopen','qstart','qend','sstart','send','evalue','bitscore']
    for line in Path(path).read_text(errors='replace').splitlines():
        if not line.strip() or line.startswith('#'): continue
        parts=line.split('\t')
        if len(parts)<12: continue
        try: rows.append(dict(zip(fields,[parts[0],parts[1],float(parts[2]),int(parts[3]),int(parts[4]),int(parts[5]),int(parts[6]),int(parts[7]),int(parts[8]),int(parts[9]),float(parts[10]),float(parts[11])])))
        except ValueError: continue
    return rows
def main():
 if len(sys.argv)!=3: fail('参数错误')
 op=sys.argv[1]
 try: a=json.loads(base64.urlsafe_b64decode(sys.argv[2]+'='*(-len(sys.argv[2])%4)).decode())
 except Exception: fail('参数编码无效')
 path=a.get('input_path'); out=a.get('output_path'); rs=records(path) if path else []
 if op in {'sequence_identify_format','sequence_inspect','sequence_stats','sequence_quality_encoding','sequence_quality_report'}:
  s=summary(rs); fmt='FASTQ' if Path(path).suffix.lower() in {'.fq','.fastq'} or Path(path).read_text(errors='ignore').lstrip().startswith('@') else 'FASTA'; result={'format':fmt,**s}
  if fmt=='FASTQ':
   q=[q for r in rs for q in r.letter_annotations.get('phred_quality',[])]; result['quality']={'min':min(q) if q else None,'max':max(q) if q else None,'mean':float(np.mean(q)) if q else None}
  emit(result,out); return
 if op=='sequence_validate':
  issues=[]; valid_chars=set('ACGTUNRYSWKMBDHV-')
  for r in rs[:100000]:
   bad=set(str(r.seq).upper())-valid_chars
   if bad: issues.append({'id':r.id,'invalid_bases':sorted(bad)})
   if r.letter_annotations.get('phred_quality') and len(r.letter_annotations['phred_quality'])!=len(r.seq): issues.append({'id':r.id,'error':'质量值长度不一致'})
  emit({'valid':not issues,'checked':len(rs),'issues':issues[:500]},out); return
 if op=='sequence_pair_validate':
  r1,r2=records(a['read1_path']),records(a['read2_path']); n=min(len(r1),len(r2)); mismatches=[]
  def key(r): return re.sub(r'([/ _])?[12]$','',r.id)
  for i in range(min(n,100000)):
   if key(r1[i])!=key(r2[i]): mismatches.append({'index':i,'read1':r1[i].id,'read2':r2[i].id})
  emit({'read1_count':len(r1),'read2_count':len(r2),'paired_count':n,'match':len(r1)==len(r2) and not mismatches,'mismatches':mismatches[:1000]},out); return
 if op=='sequence_position_quality':
  rows=[]
  for i in range(max((len(r) for r in rs),default=0)):
   q=[r.letter_annotations.get('phred_quality',[])[i] for r in rs if i<len(r) and r.letter_annotations.get('phred_quality')]
   if q: rows.append({'position':i+1,'mean':float(np.mean(q)),'q25':float(np.percentile(q,25)),'q75':float(np.percentile(q,75)),'low_fraction':float(np.mean(np.array(q)<20))})
  emit({'positions':rows},out); return
 if op=='sequence_position_bases':
  rows=[]
  for i in range(max((len(r) for r in rs),default=0)):
   c=Counter(str(r.seq).upper()[i] for r in rs if i<len(r)); total=sum(c.values()); rows.append({'position':i+1,**{b: c[b]/total if total else 0 for b in 'ACGTN'}})
  emit({'positions':rows},out); return
 if op in {'sequence_filter_quality','sequence_trim_adapters','sequence_trim_length','sequence_deduplicate','sequence_convert_fasta_fastq','sequence_reverse_complement','sequence_extract_unmapped'}:
  from Bio.Seq import Seq
  if op=='sequence_filter_quality': rs=[r for r in rs if len(r)>=int(a.get('min_length',30)) and str(r.seq).upper().count('N')/max(len(r),1)<=float(a.get('max_n_fraction',.1)) and (not r.letter_annotations.get('phred_quality') or np.mean(r.letter_annotations['phred_quality'])>=float(a.get('min_quality',20)))]
  elif op=='sequence_trim_adapters':
   for r in rs:
    for ad in a['adapters']:
     i=str(r.seq).upper().find(ad.upper()); r.seq=r.seq[:i] if i>=0 else r.seq
  elif op=='sequence_trim_length':
   for r in rs: r.seq=r.seq[int(a.get('start',0)):int(a['end']) if a.get('end') is not None else None]
  elif op=='sequence_deduplicate':
   seen=set(); rs=[r for r in rs if not (str(r.seq) in seen or seen.add(str(r.seq)))]
  elif op=='sequence_reverse_complement':
   for r in rs: r.seq=r.seq.reverse_complement()
  elif op=='sequence_extract_unmapped':
   names={x.strip().split()[0] for x in Path(a['names_path']).read_text().splitlines() if x.strip()}; rs=[r for r in rs if r.id in names]
  write_records(rs,out,Path(out).suffix.lower() in {'.fq','.fastq'},int(a.get('default_quality',40))); emit({'output_path':Path(out).name,'sequence_count':len(rs)}); return
 if op=='sequence_kmer_profile':
  k=int(a.get('k',5)); c=Counter(s[i:i+k] for r in rs for s in [str(r.seq).upper()] for i in range(max(0,len(s)-k+1))); emit({'k':k,'distinct_kmers':len(c),'total_kmers':sum(c.values()),'top_kmers':[{'kmer':x,'count':n} for x,n in c.most_common(100)]}); return
 if op=='sequence_complexity_check':
  result=[]
  for r in rs:
   s=str(r.seq).upper(); counts=Counter(s); entropy=-sum((n/len(s))*np.log2(n/len(s)) for n in counts.values()) if s else 0; hom=max(counts.values())/len(s) if s else 0
   if entropy<1.5 or hom>.8: result.append({'id':r.id,'entropy':float(entropy),'homopolymer_fraction':hom})
  emit({'low_complexity_count':len(result),'sequences':result[:1000]}); return
 if op=='sequence_batch_profile': emit({'files':[{'path':Path(p).name,**summary(records(p))} for p in a['input_paths']]}); return
 if op=='sequence_gc_visualize':
  import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
  vals=[(str(r.seq).upper().count('G')+str(r.seq).upper().count('C'))/max(len(r),1)*100 for r in rs]; plt.hist(vals,bins=30); plt.xlabel('GC %'); plt.ylabel('Sequences'); plt.tight_layout(); Path(out).parent.mkdir(parents=True,exist_ok=True); plt.savefig(out,dpi=150); plt.close(); emit({'output_path':Path(out).name,'sequence_count':len(rs)}); return
 if op=='sequence_fastqc_report':
  d=Path(a['output_dir']); d.mkdir(parents=True,exist_ok=True); payload={'source':Path(path).name,**summary(rs)}; (d/'sequence_quality.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2)); emit({'output_dir':d.name,'files':['sequence_quality.json']}); return
 if op=='reference_fasta_inspect': emit({'reference':Path(path).name,'sequences':[{'id':r.id,'length':len(r.seq),'gc_percent':(str(r.seq).upper().count('G')+str(r.seq).upper().count('C'))/max(len(r),1)*100} for r in rs]}); return
 if op=='sequence_extract_bed':
  refs={r.id:str(r.seq) for r in rs}; selected=[]
  for line in Path(a['bed_path']).read_text().splitlines():
   if not line or line.startswith('#'): continue
   chrom,start,end,*rest=line.split(); selected.append((rest[0] if rest else f'{chrom}:{start}-{end}',refs.get(chrom,'')[int(start):int(end)]))
  from Bio.SeqRecord import SeqRecord
  from Bio.Seq import Seq
  write_records([SeqRecord(Seq(s),id=n,description='') for n,s in selected],out); emit({'output_path':Path(out).name,'sequence_count':len(selected)}); return
 if op=='sequence_alignment_summary':
  txt=Path(path).read_text(errors='ignore'); nums=[int(x) for x in re.findall(r'(\d+)\s+(?:mapped|aligned)',txt,re.I)]; emit({'file':Path(path).name,'numeric_counts':nums,'text_lines':len(txt.splitlines())}); return
 if op=='sequence_coverage_profile':
  import pandas as pd; df=pd.read_csv(path,sep=None,engine='python'); col=next((c for c in df.columns if str(c).lower() in {'depth','coverage','cov'}),df.columns[-1]); v=df[col].to_numpy(float); emit({'column':str(col),'count':len(v),'mean_depth':float(np.mean(v)),'covered_fraction':float(np.mean(v>0)),'percentiles':{str(p):float(np.percentile(v,p)) for p in (1,25,50,75,95)}}); return
 if op=='sequence_depth_visualize':
  import pandas as pd,matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
  df=pd.read_csv(path,sep=None,engine='python'); col=df.columns[-1]; plt.plot(df[col].to_numpy(float)); plt.xlabel('Position'); plt.ylabel(str(col)); plt.tight_layout(); Path(out).parent.mkdir(parents=True,exist_ok=True); plt.savefig(out,dpi=150); plt.close(); emit({'output_path':Path(out).name}); return
 if op=='blast_result_inspect':
  rows=blast_rows(path); emit({'file':Path(path).name,'hit_count':len(rows),'query_count':len({r['qseqid'] for r in rows}),'subject_count':len({r['sseqid'] for r in rows}),'identity_mean':float(np.mean([r['pident'] for r in rows])) if rows else None,'top_hits':sorted(rows,key=lambda r:(r['bitscore'],-r['evalue']),reverse=True)[:100]}); return
 if op in {'blast_hit_visualize','blast_dotplot_visualize'}:
  rows=blast_rows(path)
  if not rows: fail('BLAST结果中没有可解析的 outfmt 6/7 命中记录')
  import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
  Path(out).parent.mkdir(parents=True,exist_ok=True); fig,ax=plt.subplots(figsize=(12,7))
  if op=='blast_dotplot_visualize':
   for r in rows[:10000]: ax.plot([r['qstart'],r['qend']],[r['sstart'],r['send']],alpha=.35,linewidth=1)
   ax.set_xlabel('Query position'); ax.set_ylabel('Subject position'); ax.set_title('BLAST alignment dot plot')
  else:
   top=sorted(rows,key=lambda r:(r['bitscore'],-r['evalue']),reverse=True)[:int(a.get('max_hits',100))]; subjects={s:i for i,s in enumerate(dict.fromkeys(r['sseqid'] for r in top))}
   for r in top:
    y=subjects[r['sseqid']]; ax.plot([r['qstart'],r['qend']],[y,y],linewidth=max(1,min(6,r['pident']/20)),alpha=.8); ax.scatter([r['qstart'],r['qend']],[y,y],s=10)
   ax.set_yticks(list(subjects.values()),list(subjects.keys())); ax.set_xlabel('Query position'); ax.set_ylabel('Subject'); ax.set_title('BLAST hit map')
  ax.grid(alpha=.2); fig.tight_layout(); fig.savefig(out,dpi=160); plt.close(fig); emit({'output_path':Path(out).name,'hit_count':len(rows),'rendered_hits':min(len(rows),int(a.get('max_hits',10000)))}); return
 if op in {'sequence_quality_boxplot','sequence_quality_heatmap','sequence_base_composition_visualize'}:
  import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
  if not rs or not any(r.letter_annotations.get('phred_quality') for r in rs): fail('输入中没有 FASTQ 质量值')
  Path(out).parent.mkdir(parents=True,exist_ok=True); fig,ax=plt.subplots(figsize=(12,7))
  if op=='sequence_quality_boxplot':
   q=[ [r.letter_annotations['phred_quality'][i] for r in rs if i<len(r) and r.letter_annotations.get('phred_quality')] for i in range(max(map(len,rs))) ]; ax.boxplot(q,showfliers=False); ax.set_xlabel('Position'); ax.set_ylabel('Phred quality')
  elif op=='sequence_quality_heatmap':
   matrix=np.array([r.letter_annotations.get('phred_quality',[]) for r in rs[:int(a.get('max_reads',500))]],dtype=float); ax.imshow(matrix,aspect='auto',interpolation='nearest',cmap='viridis',vmin=0,vmax=45); ax.set_xlabel('Position'); ax.set_ylabel('Read'); fig.colorbar(ax.images[0],ax=ax,label='Phred quality')
  else:
   positions=[]
   for i in range(max(map(len,rs))):
    c=Counter(str(r.seq).upper()[i] for r in rs if i<len(r)); total=sum(c.values()); positions.append([c[b]/total if total else 0 for b in 'ACGTN'])
   ax.stackplot(range(1,len(positions)+1),np.array(positions).T,labels=list('ACGTN')); ax.set_xlabel('Position'); ax.set_ylabel('Fraction'); ax.legend(loc='upper right')
  fig.tight_layout(); fig.savefig(out,dpi=160); plt.close(fig); emit({'output_path':Path(out).name,'sequence_count':len(rs)}); return
 fail('未知序列工具')
if __name__=='__main__': main()
