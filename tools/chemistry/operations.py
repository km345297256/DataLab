#!/usr/bin/env python3
"""Small, deterministic CIF reader for structure inspection and conversion."""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import shlex
from pathlib import Path

def clean(value: str):
    value = value.strip().strip("'\"")
    if value in {".", "?"}: return None
    return value

def number(value):
    value = clean(str(value))
    if value is None: return None
    value = re.sub(r"\([^)]*\)$", "", value)
    try: return float(value)
    except ValueError: return None

def parse(path: Path):
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    data = {"path": path.name, "blocks": [], "tags": {}, "loops": []}
    block = None; i = 0
    while i < len(lines):
        raw = lines[i].strip()
        if not raw or raw.startswith("#"): i += 1; continue
        low = raw.lower()
        if low.startswith("data_"):
            block = raw[5:].strip(); data["blocks"].append(block); i += 1; continue
        if low == "loop_":
            i += 1; names = []
            while i < len(lines) and lines[i].strip().startswith("_"):
                names.append(lines[i].strip().split()[0].lower()); i += 1
            values = []
            while i < len(lines):
                text = lines[i].strip()
                if not text or text.startswith("#"): i += 1; continue
                if text.lower() == "loop_" or text.startswith("_") or text.lower().startswith("data_"): break
                if text.startswith(";"):
                    i += 1; continue
                values.extend(shlex.split(text, comments=True)); i += 1
            rows = [dict(zip(names, [clean(v) for v in values[j:j+len(names)]])) for j in range(0, len(values), len(names)) if len(values[j:j+len(names)]) == len(names)]
            data["loops"].append({"names": names, "rows": rows}); continue
        if raw.startswith("_"):
            parts = shlex.split(raw, comments=True)
            if len(parts) >= 2: data["tags"][parts[0].lower()] = clean(" ".join(parts[1:]))
        i += 1
    atom_loop = next((loop for loop in data["loops"] if any("_atom_site_" in name for name in loop["names"])), {"names": [], "rows": []})
    data["atoms"] = atom_loop["rows"]
    return data

def pick(row, suffixes):
    for suffix in suffixes:
        for key, value in row.items():
            if key.endswith(suffix): return value
    return None

def atoms(data):
    result = []
    for row in data["atoms"]:
        label = pick(row, ("_atom_site_label",)) or ""
        element = pick(row, ("_atom_site_type_symbol",)) or re.match(r"[A-Za-z]+", label).group(0) if re.match(r"[A-Za-z]+", label) else "X"
        result.append({"label": label, "element": element, "fract_x": number(pick(row, ("_atom_site_fract_x",))), "fract_y": number(pick(row, ("_atom_site_fract_y",))), "fract_z": number(pick(row, ("_atom_site_fract_z",))), "occupancy": number(pick(row, ("_atom_site_occupancy",))), "u_iso": number(pick(row, ("_atom_site_u_iso_or_equiv", "_atom_site_b_iso_or_equiv")))})
    return result

def cell(data):
    tags = data["tags"]
    return {key: number(tags.get(key)) for key in ("_cell_length_a", "_cell_length_b", "_cell_length_c", "_cell_angle_alpha", "_cell_angle_beta", "_cell_angle_gamma")}

def cartesian(frac, c):
    a,b,cc = c["_cell_length_a"],c["_cell_length_b"],c["_cell_length_c"]
    alpha,beta,gamma = [math.radians(c[k]) for k in ("_cell_angle_alpha","_cell_angle_beta","_cell_angle_gamma")]
    va=(a,0,0); vb=(b*math.cos(gamma),b*math.sin(gamma),0); cx=cc*math.cos(beta); cy=cc*(math.cos(alpha)-math.cos(beta)*math.cos(gamma))/math.sin(gamma); cz=math.sqrt(max(cc*cc-cx*cx-cy*cy,0))
    x,y,z=frac; return (x*va[0]+y*vb[0]+z*cx, y*vb[1]+z*cy, z*cz)

_ATOMIC_WEIGHTS = {"H":1.008,"C":12.011,"N":14.007,"O":15.999,"F":18.998,"P":30.974,"S":32.06,"Cl":35.45,"Br":79.904,"I":126.904,"Si":28.085,"Na":22.990,"K":39.098,"Ca":40.078,"Fe":55.845,"Co":58.933,"Ni":58.693,"Cu":63.546,"Zn":65.38,"Al":26.982,"Mg":24.305,"Li":6.94,"Mn":54.938,"Ti":47.867,"Cr":51.996}

def composition(site_atoms):
    counts = {}
    for atom in site_atoms:
        element = re.sub(r"[^A-Za-z]", "", str(atom.get("element") or "X"))
        element = element[:1].upper() + element[1:].lower()
        counts[element] = counts.get(element, 0.0) + (atom.get("occupancy") or 1.0)
    mass = sum(_ATOMIC_WEIGHTS.get(k, 0.0) * v for k, v in counts.items())
    return counts, mass

def cell_volume(c):
    a,b,cc = (c.get("_cell_length_" + x) for x in ("a","b","c"))
    alpha,beta,gamma = (math.radians(c.get("_cell_angle_" + x) or 90.0) for x in ("alpha","beta","gamma"))
    if None in (a,b,cc): return None
    value = 1 - math.cos(alpha)**2 - math.cos(beta)**2 - math.cos(gamma)**2 + 2*math.cos(alpha)*math.cos(beta)*math.cos(gamma)
    return a*b*cc*math.sqrt(max(value, 0.0))

def periodic_distance(left, right, c):
    delta = [((left[i] - right[i] + 0.5) % 1.0) - 0.5 for i in range(3)]
    p = cartesian(delta, c)
    return math.sqrt(sum(x*x for x in p))

def write_json(path, value):
    out = Path(path); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

def parse_float_arg(value, default):
    try: return float(value)
    except (TypeError, ValueError): return default

def main():
    parser=argparse.ArgumentParser(); parser.add_argument("op", choices=["inspect","validate","extract","xyz","visualize","density","geometry","pxrd","compare","supercell","report"]); parser.add_argument("input_path"); parser.add_argument("other_path", nargs="?"); parser.add_argument("--output-path"); parser.add_argument("--format", default="csv"); parser.add_argument("--max-atoms", type=int, default=2000); parser.add_argument("--cutoff", type=float, default=3.0); parser.add_argument("--h", type=int, default=1); parser.add_argument("--k", type=int, default=1); parser.add_argument("--l", type=int, default=1); args=parser.parse_args()
    data=parse(Path(args.input_path)); site_atoms=atoms(data); c=cell(data)
    if args.op == "inspect":
        print(json.dumps({"success":True,"data":{"file":data["path"],"blocks":data["blocks"],"cell":c,"space_group":data["tags"].get("_symmetry_space_group_name_h-m","_space_group_name_h-m"),"formula":data["tags"].get("_chemical_formula_sum"),"atom_count":len(site_atoms),"elements":sorted({a["element"] for a in site_atoms})}},ensure_ascii=False)); return
    if args.op == "density":
        counts, mass = composition(site_atoms); volume = cell_volume(c); z = number(data["tags"].get("_cell_formula_units_z")) or 1
        # mass is g/mol for the declared asymmetric-unit sites; density conversion is g/cm3.
        density = (mass * z / 6.02214076e23) / (volume * 1e-24) if volume else None
        result={"success":True,"formula_counts":counts,"formula_mass_g_mol":mass,"cell_volume_a3":volume,"z":z,"theoretical_density_g_cm3":density,"basis":"declared atom sites and CIF cell metadata"}
        if args.output_path: write_json(args.output_path,result)
        print(json.dumps(result,ensure_ascii=False)); return
    if args.op == "geometry":
        points=[a for a in site_atoms if None not in (a["fract_x"],a["fract_y"],a["fract_z"])]
        distances=[]; neighbors=[]
        for i,left in enumerate(points):
            for j in range(i+1,len(points)):
                right=points[j]; d=periodic_distance((left["fract_x"],left["fract_y"],left["fract_z"]),(right["fract_x"],right["fract_y"],right["fract_z"]),c)
                if d <= args.cutoff and d > 1e-6: distances.append(d); neighbors.append({"a":left["label"],"b":right["label"],"element_a":left["element"],"element_b":right["element"],"distance_a":d})
        result={"success":True,"cutoff_a":args.cutoff,"pair_count":len(neighbors),"distance_min_a":min(distances) if distances else None,"distance_max_a":max(distances) if distances else None,"pairs":neighbors[:10000],"note":"periodic minimum-image distances; bond orders are not inferred"}
        if args.output_path: write_json(args.output_path,result)
        print(json.dumps(result,ensure_ascii=False)); return
    if args.op == "pxrd":
        # Kinematic Cu K-alpha preview: peak positions are geometrically estimated from cell lengths.
        wavelength=1.5406; peaks=[]
        for h in range(-4,5):
            for k in range(-4,5):
                for l in range(-4,5):
                    if (h,k,l)==(0,0,0): continue
                    d=1.0/math.sqrt((h/c["_cell_length_a"])**2+(k/c["_cell_length_b"])**2+(l/c["_cell_length_c"])**2) if all(c.get("_cell_length_"+x) for x in ("a","b","c")) else None
                    if d and wavelength/(2*d) <= 1: peaks.append({"h":h,"k":k,"l":l,"d_a":d,"two_theta_deg":math.degrees(2*math.asin(wavelength/(2*d)))})
        peaks.sort(key=lambda x:x["two_theta_deg"]); result={"success":True,"wavelength_a":wavelength,"peaks":peaks[:500],"note":"screening estimate using orthogonal reciprocal metric; use a crystallographic engine for publication PXRD"}
        if args.output_path: write_json(args.output_path,result)
        print(json.dumps(result,ensure_ascii=False)); return
    if args.op == "compare":
        if not args.other_path: raise ValueError("other_path is required")
        other=parse(Path(args.other_path)); oa=atoms(other); oc=cell(other); c1,_=composition(site_atoms); c2,_=composition(oa)
        result={"success":True,"same_composition":c1==c2,"same_cell_within_tolerance":all(abs((c.get(k) or 0)-(oc.get(k) or 0))<1e-4 for k in c if k in oc),"left_atoms":len(site_atoms),"right_atoms":len(oa),"left_space_group":data["tags"].get("_symmetry_space_group_name_h-m"),"right_space_group":other["tags"].get("_symmetry_space_group_name_h-m"),"method":"metadata and composition comparison; no symmetry matching"}
        print(json.dumps(result,ensure_ascii=False)); return
    if args.op == "supercell":
        out=Path(args.output_path or (Path(args.input_path).with_suffix(".supercell.cif"))); out.parent.mkdir(parents=True,exist_ok=True); nx,ny,nz=max(args.h,1),max(args.k,1),max(args.l,1); rows=[]
        for atom in site_atoms:
            for ix in range(nx):
                for iy in range(ny):
                    for iz in range(nz):
                        rows.append((atom["label"]+f"_{ix}{iy}{iz}",atom["element"],(atom["fract_x"]+ix)/nx,(atom["fract_y"]+iy)/ny,(atom["fract_z"]+iz)/nz,atom.get("occupancy") or 1.0))
        lines=["data_supercell",f"_cell_length_a {c.get('_cell_length_a',0)*nx}",f"_cell_length_b {c.get('_cell_length_b',0)*ny}",f"_cell_length_c {c.get('_cell_length_c',0)*nz}",f"_cell_angle_alpha {c.get('_cell_angle_alpha',90)}",f"_cell_angle_beta {c.get('_cell_angle_beta',90)}",f"_cell_angle_gamma {c.get('_cell_angle_gamma',90)}","loop_","_atom_site_label","_atom_site_type_symbol","_atom_site_fract_x","_atom_site_fract_y","_atom_site_fract_z","_atom_site_occupancy"]+[f"{label} {el} {x:.8f} {y:.8f} {z:.8f} {occ:.4f}" for label,el,x,y,z,occ in rows]; out.write_text("\n".join(lines)+"\n",encoding="utf-8"); print(json.dumps({"success":True,"output_path":str(out),"atoms":len(rows),"multipliers":[nx,ny,nz]},ensure_ascii=False)); return
    if args.op == "report":
        counts,mass=composition(site_atoms); result={"success":True,"file":data["path"],"blocks":data["blocks"],"cell":c,"cell_volume_a3":cell_volume(c),"formula":data["tags"].get("_chemical_formula_sum"),"composition":counts,"formula_mass_g_mol":mass,"atom_count":len(site_atoms),"elements":sorted({a["element"] for a in site_atoms}),"space_group":data["tags"].get("_symmetry_space_group_name_h-m") or data["tags"].get("_space_group_name_h-m"),"warnings":[]}; write_json(args.output_path or str(Path(args.input_path).with_suffix(".cif-report.json")),result); print(json.dumps(result,ensure_ascii=False)); return
    issues=[]
    if any(c[k] is None or c[k] <= 0 for k in ("_cell_length_a","_cell_length_b","_cell_length_c")): issues.append("missing or invalid cell lengths")
    if not site_atoms: issues.append("no atom-site loop found")
    labels=[a["label"] for a in site_atoms]; issues += ["duplicate atom labels"] if len(labels)!=len(set(labels)) else []
    bad=[a["label"] for a in site_atoms if None in (a["fract_x"],a["fract_y"],a["fract_z"])]
    if bad: issues.append(f"missing fractional coordinates for {len(bad)} atoms")
    if args.op == "validate":
        result={"success":not issues,"issues":issues,"atom_count":len(site_atoms),"cell":c}
        if args.output_path: Path(args.output_path).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps(result,ensure_ascii=False)); return
    if args.op == "extract":
        out=Path(args.output_path); out.parent.mkdir(parents=True,exist_ok=True)
        if args.format == "json": out.write_text(json.dumps(site_atoms,ensure_ascii=False,indent=2),encoding="utf-8")
        else:
            with out.open("w",newline="",encoding="utf-8") as f: w=csv.DictWriter(f,fieldnames=list(site_atoms[0]) if site_atoms else ["label"]); w.writeheader(); w.writerows(site_atoms)
        print(json.dumps({"success":True,"output_path":str(out),"rows":len(site_atoms)},ensure_ascii=False)); return
    if args.op == "xyz":
        out=Path(args.output_path); out.parent.mkdir(parents=True,exist_ok=True); points=[(a,cartesian((a["fract_x"],a["fract_y"],a["fract_z"]),c)) for a in site_atoms if None not in (a["fract_x"],a["fract_y"],a["fract_z"])]
        out.write_text(f"{len(points)}\n{data['path']}\n"+"\n".join(f"{a['element']} {p[0]:.8f} {p[1]:.8f} {p[2]:.8f}" for a,p in points)+"\n",encoding="utf-8"); print(json.dumps({"success":True,"output_path":str(out),"atoms":len(points)})); return
    import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
    from mpl_toolkits.mplot3d import Axes3D  # noqa: F401
    pts=[(a,cartesian((a["fract_x"],a["fract_y"],a["fract_z"]),c)) for a in site_atoms[:args.max_atoms] if None not in (a["fract_x"],a["fract_y"],a["fract_z"])]; fig=plt.figure(figsize=(8,6)); ax=fig.add_subplot(111,projection="3d")
    colors={};
    for a,p in pts: colors.setdefault(a["element"],[]).append(p)
    for element, values in colors.items(): ax.scatter([p[0] for p in values],[p[1] for p in values],[p[2] for p in values],label=element,s=28)
    ax.set(xlabel="X (Å)",ylabel="Y (Å)",zlabel="Z (Å)"); ax.legend(); fig.tight_layout(); Path(args.output_path).parent.mkdir(parents=True,exist_ok=True); fig.savefig(args.output_path,dpi=150); plt.close(fig); print(json.dumps({"success":True,"output_path":args.output_path,"atoms":len(pts)}))
if __name__ == "__main__": main()
