#!/usr/bin/env python3
"""Bounded, format-aware molecular structure operators."""
from __future__ import annotations
import base64, csv, json, math, re, sys
from pathlib import Path

MAX_BYTES = 100 * 1024 * 1024
ELEMENTS = {"H":1.008,"C":12.011,"N":14.007,"O":15.999,"F":18.998,"P":30.974,"S":32.06,"Cl":35.45,"Br":79.904,"I":126.904,"Si":28.085,"Na":22.990,"K":39.098,"Ca":40.078,"Fe":55.845,"Co":58.933,"Ni":58.693,"Cu":63.546,"Zn":65.38,"Al":26.982,"Mg":24.305,"Li":6.94,"Mn":54.938,"Ti":47.867,"Cr":51.996}
RADII = {"H":0.31,"C":0.76,"N":0.71,"O":0.66,"F":0.57,"P":1.07,"S":1.05,"Cl":1.02,"Br":1.20,"I":1.39,"Si":1.11,"Na":1.66,"K":2.03,"Ca":1.76,"Fe":1.24,"Cu":1.32,"Zn":1.22}

def fail(message):
    print(json.dumps({"success":False,"error":message}, ensure_ascii=False)); raise SystemExit(0)

def write_json(path, value):
    out=Path(path); out.parent.mkdir(parents=True, exist_ok=True); out.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

def read(path):
    p=Path(path)
    if not p.is_file(): fail("input file not found")
    if p.stat().st_size > MAX_BYTES: fail("structure exceeds the 100 MB safety limit")
    return p, p.read_text(encoding="utf-8", errors="replace")

def element(raw):
    value=re.sub(r"[^A-Za-z]", "", str(raw or "X"))
    if not value: return "X"
    return value[:1].upper()+value[1:].lower()

def parse_xyz(text):
    lines=text.splitlines(); atoms=[]
    try: count=int(lines[0].strip())
    except (ValueError, IndexError): return atoms, "xyz"
    for line in lines[2:2+count]:
        bits=line.split()
        if len(bits)>=4:
            try: atoms.append({"index":len(atoms)+1,"element":element(bits[0]),"x":float(bits[1]),"y":float(bits[2]),"z":float(bits[3])})
            except ValueError: pass
    return atoms, "xyz"

def parse_mol(text):
    lines=text.splitlines(); atoms=[]
    if len(lines)<4: return atoms, "mol"
    try: count=int(lines[3][0:3].strip())
    except ValueError: return atoms, "mol"
    for idx,line in enumerate(lines[4:4+count],1):
        try: atoms.append({"index":idx,"element":element(line[31:34].strip()),"x":float(line[0:10]),"y":float(line[10:20]),"z":float(line[20:30])})
        except (ValueError, IndexError): pass
    return atoms, "mol"

def parse_mol2(text):
    atoms=[]; in_atoms=False
    for line in text.splitlines():
        if line.upper().startswith("@<TRIPOS>ATOM"): in_atoms=True; continue
        if line.upper().startswith("@<TRIPOS>") and in_atoms: break
        if in_atoms:
            bits=line.split()
            if len(bits)>=6:
                try: atoms.append({"index":len(atoms)+1,"element":element(bits[5].split('.')[0]),"x":float(bits[2]),"y":float(bits[3]),"z":float(bits[4]),"atom_name":bits[1]})
                except ValueError: pass
    return atoms, "mol2"

def parse_pdb(text):
    atoms=[]
    for line in text.splitlines():
        if line[:6].strip() not in {"ATOM","HETATM"}: continue
        try:
            raw=line[76:78].strip() or line[12:16].strip()
            atoms.append({"index":len(atoms)+1,"serial":int(line[6:11]),"element":element(raw),"x":float(line[30:38]),"y":float(line[38:46]),"z":float(line[46:54]),"chain":line[21:22].strip(),"residue":line[17:20].strip(),"residue_number":line[22:26].strip()})
        except (ValueError, IndexError): pass
    return atoms, "pdb"

def parse_vasp(text):
    lines=[x.strip() for x in text.splitlines() if x.strip()]; atoms=[]
    if len(lines)<8: return atoms, "vasp", {"valid":False}
    try: scale=float(lines[1]); vectors=[[float(v)*scale for v in lines[i].split()[:3]] for i in range(2,5)]
    except (ValueError, IndexError): return atoms, "vasp", {"valid":False}
    symbols=lines[5].split(); counts=[]; offset=6
    try: counts=[int(v) for v in lines[6].split()]; offset=7
    except ValueError:
        symbols=lines[6].split(); counts=[int(v) for v in lines[7].split()]; offset=8
    if lines[offset].lower().startswith("s"): offset+=1
    mode=lines[offset].lower(); offset+=1; direct=mode.startswith("d")
    serial=0
    for symbol,count in zip(symbols,counts):
        for _ in range(count):
            if offset>=len(lines): break
            bits=lines[offset].split(); offset+=1
            try:
                xyz=[float(v) for v in bits[:3]]
                if direct: xyz=[sum(xyz[j]*vectors[j][i] for j in range(3)) for i in range(3)]
                serial+=1; atoms.append({"index":serial,"element":element(symbol),"x":xyz[0],"y":xyz[1],"z":xyz[2]})
            except (ValueError, IndexError): pass
    return atoms, "vasp", {"vectors":vectors,"elements":symbols,"counts":counts,"coordinate_mode":mode,"valid":bool(atoms)}

def parse_structure(path, text):
    name=path.name.casefold(); suffix=path.suffix.casefold()
    if name in {"poscar","contcar"} or suffix==".vasp": return parse_vasp(text)
    if suffix in {".pdb",".ent"}: a,f=parse_pdb(text); return a,f,{}
    if suffix==".xyz": a,f=parse_xyz(text); return a,f,{}
    if suffix==".mol2": a,f=parse_mol2(text); return a,f,{}
    if suffix in {".mol",".sdf"}:
        a,f=parse_mol(text.split("$$$$",1)[0]); return a,f,{"records":text.count("$$$$") or 1}
    try:
        from pymatgen.core import Structure
        structure=Structure.from_file(str(path)); atoms=[{"index":i+1,"element":site.specie.symbol,"x":float(site.coords[0]),"y":float(site.coords[1]),"z":float(site.coords[2]),"fract_x":float(site.frac_coords[0]),"fract_y":float(site.frac_coords[1]),"fract_z":float(site.frac_coords[2])} for i,site in enumerate(structure)]
        return atoms,"cif",{"formula":structure.formula,"lattice":structure.lattice.matrix.tolist(),"volume":structure.volume}
    except Exception as exc: fail(f"unsupported or invalid structure format: {exc}")

def load(path): return parse_structure(*read(path))

def summary(path, atoms, fmt, meta):
    counts={}
    for atom in atoms: counts[atom["element"]]=counts.get(atom["element"],0)+1
    mass=sum(ELEMENTS.get(k,0)*v for k,v in counts.items())
    return {"success":True,"file":path.name,"format":fmt,"atom_count":len(atoms),"elements":dict(sorted(counts.items())),"formula":"".join(f"{k}{v if v!=1 else ''}" for k,v in sorted(counts.items())),"molecular_weight_g_mol":mass,"metadata":meta}

def coords(atoms): return [(a["x"],a["y"],a["z"]) for a in atoms if all(isinstance(a.get(k),(int,float)) for k in ("x","y","z"))]

def main():
    if len(sys.argv)!=3: fail("operation and arguments are required")
    op=sys.argv[1]
    try: args=json.loads(base64.urlsafe_b64decode(sys.argv[2]+"="*(-len(sys.argv[2])%4)).decode())
    except Exception: fail("invalid arguments")
    path,text=read(args.get("input_path","")); atoms,fmt,meta=load(str(path))
    if op in {"molecular_inspect","crystal_inspect_vasp"}: print(json.dumps(summary(path,atoms,fmt,meta),ensure_ascii=False)); return
    if op=="molecular_validate":
        points=coords(atoms); duplicates=len(points)-len({tuple(round(v,6) for v in p) for p in points}); result={"success":True,"valid":bool(atoms) and len(points)==len(atoms),"format":fmt,"atom_count":len(atoms),"coordinate_count":len(points),"duplicate_coordinate_count":duplicates,"warnings":([] if atoms else ["no atoms parsed"])}
        if args.get("output_path"): write_json(args["output_path"],result)
        print(json.dumps(result,ensure_ascii=False)); return
    if op=="molecular_extract_atoms":
        out=Path(args["output_path"]); out.parent.mkdir(parents=True,exist_ok=True)
        if args.get("format","csv")=="json": write_json(out,atoms)
        else:
            keys=sorted({k for a in atoms for k in a});
            with out.open("w",newline="",encoding="utf-8") as stream: writer=csv.DictWriter(stream,fieldnames=keys); writer.writeheader(); writer.writerows(atoms)
        print(json.dumps({"success":True,"atom_count":len(atoms),"output_path":out.name},ensure_ascii=False)); return
    if op=="molecular_composition": result=summary(path,atoms,fmt,meta)
    elif op=="molecular_geometry":
        points=coords(atoms); result={"success":True,"atom_count":len(atoms),"bounding_box":({"min":[min(p[i] for p in points) for i in range(3)],"max":[max(p[i] for p in points) for i in range(3)]} if points else None),"centroid":([sum(p[i] for p in points)/len(points) for i in range(3)] if points else None)}
        ds=[math.dist(points[i],points[j]) for i in range(len(points)) for j in range(i+1,len(points)) if math.dist(points[i],points[j])>1e-8]; result["nearest_distance"] = min(ds) if ds else None
    elif op=="molecular_detect_bonds":
        points=coords(atoms); tol=float(args.get("tolerance",0.25)); bonds=[]
        for i in range(len(points)):
            for j in range(i+1,len(points)):
                d=math.dist(points[i],points[j]); limit=RADII.get(atoms[i]["element"],0.77)+RADII.get(atoms[j]["element"],0.77)+tol
                if d>0.3 and d<=limit: bonds.append({"a":i+1,"b":j+1,"element_a":atoms[i]["element"],"element_b":atoms[j]["element"],"distance":d})
        result={"success":True,"candidate_bond_count":len(bonds),"bonds":bonds[:20000],"tolerance":tol,"note":"geometric candidates only; bond order is not inferred"}
    elif op in {"molecular_convert_xyz","molecular_standardize_coordinates"}:
        decimals=int(args.get("decimals",6)); out=Path(args["output_path"]); out.parent.mkdir(parents=True,exist_ok=True); lines=[str(len(atoms)),f"source={path.name} format={fmt}"]+[f"{a['element']} {a.get('x',0):.{decimals}f} {a.get('y',0):.{decimals}f} {a.get('z',0):.{decimals}f}" for a in atoms]; out.write_text("\n".join(lines)+"\n",encoding="utf-8"); result={"success":True,"atom_count":len(atoms),"output_path":out.name}
    elif op=="molecular_convert_cif":
        out=Path(args["output_path"]); out.parent.mkdir(parents=True,exist_ok=True); lines=["data_dataseek","_audit_creation_method 'AI-DataSeek molecular conversion'","loop_","_atom_site_label","_atom_site_type_symbol","_atom_site_Cartn_x","_atom_site_Cartn_y","_atom_site_Cartn_z"]+[f"A{i} {a['element']} {a.get('x',0):.6f} {a.get('y',0):.6f} {a.get('z',0):.6f}" for i,a in enumerate(atoms,1)]; out.write_text("\n".join(lines)+"\n",encoding="utf-8"); result={"success":True,"atom_count":len(atoms),"output_path":out.name,"warning":"源文件未提供晶胞时，输出仅包含笛卡尔坐标，不能视为完整周期晶体 CIF"}
    elif op=="molecular_compare_structures":
        other=Path(args.get("other_path","")); oa,of,om=load(str(other)); result={"success":True,"left":summary(path,atoms,fmt,meta),"right":summary(other,oa,of,om),"atom_count_difference":len(atoms)-len(oa)}
    elif op=="molecular_visualize":
        import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
        points=coords(atoms)[:int(args.get("max_atoms",2000))]; out=Path(args["output_path"]); out.parent.mkdir(parents=True,exist_ok=True); fig=plt.figure(figsize=(8,6)); ax=fig.add_subplot(111,projection="3d")
        colors={"H":"#f0f0f0","C":"#444444","N":"#356ae6","O":"#e33b3b","S":"#e5b82e"}; ax.scatter([p[0] for p in points],[p[1] for p in points],[p[2] for p in points],c=[colors.get(a["element"],"#58a56f") for a in atoms[:len(points)]],s=24); ax.set_xlabel("X (Å)"); ax.set_ylabel("Y (Å)"); ax.set_zlabel("Z (Å)"); fig.tight_layout(); fig.savefig(out,dpi=150); plt.close(fig); result={"success":True,"atom_count_plotted":len(points),"output_path":out.name}
    elif op=="molecular_split_sdf":
        out=Path(args["output_dir"]); out.mkdir(parents=True,exist_ok=True); records=[r.strip()+"\n$$$$\n" for r in text.split("$$$$") if r.strip()][:int(args.get("max_records",1000))]; files=[]
        for i,record in enumerate(records,1): p=out/f"molecule-{i:04d}.sdf"; p.write_text(record,encoding="utf-8"); files.append(p.name)
        write_json(out/"split_manifest.json",{"success":True,"record_count":len(files),"files":files}); result={"success":True,"record_count":len(files),"output_dir":out.name,"manifest":"split_manifest.json"}
    else: fail(f"unknown operation: {op}")
    if args.get("output_path") and op not in {"molecular_convert_xyz","molecular_standardize_coordinates","molecular_convert_cif","molecular_extract_atoms","molecular_visualize"}: write_json(args["output_path"],result)
    print(json.dumps(result,ensure_ascii=False))

if __name__=="__main__": main()
