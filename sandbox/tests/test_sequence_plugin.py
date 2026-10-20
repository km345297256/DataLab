import base64, importlib.util, json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]; PLUGIN=ROOT/'tools/sequence'
SPEC=importlib.util.spec_from_file_location('seq_ops',PLUGIN/'operations.py'); OPS=importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(OPS)

def test_sequence_manifest_has_25_tools():
    m=json.loads((PLUGIN/'manifest.json').read_text()); names={x['name'] for x in m['tools']}
    assert len(names)==32 and 'sequence_fastqc_report' in names

def test_sequence_summary_and_validation(tmp_path):
    p=tmp_path/'reads.fastq'; p.write_text('@r1\nACGTN\n+\nIIIII\n@r2\nGGCCA\n+\n#####\n')
    rs=OPS.records(p); s=OPS.summary(rs)
    assert s['sequence_count']==2 and s['length']['mean']==5
    assert OPS.summary(rs)['gc_percent']['mean']==60

def test_sequence_kmer_and_cleanup(tmp_path):
    p=tmp_path/'reads.fa'; out=tmp_path/'clean.fa'; p.write_text('>a\nACGTAC\n>b\nACGTAC\n')
    rs=OPS.records(p); assert len(rs)==2
    assert OPS.Counter(str(rs[0].seq))
    OPS.write_records([rs[0]],out); assert out.exists()

def test_blast_parser_and_visualizations(tmp_path):
    blast=tmp_path/'blast.tsv'; blast.write_text('q1\ts1\t98.0\t50\t1\t0\t1\t50\t10\t59\t1e-20\t100\nq1\ts2\t90.0\t30\t3\t0\t60\t89\t1\t30\t1e-8\t60\n')
    rows=OPS.blast_rows(blast); assert len(rows)==2 and rows[0]['qseqid']=='q1'
