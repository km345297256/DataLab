#!/usr/bin/env python3
"""CIF crystallography operations backed by pymatgen and spglib."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
from pymatgen.analysis.diffraction.xrd import XRDCalculator
from pymatgen.analysis.local_env import CrystalNN
from pymatgen.analysis.structure_matcher import StructureMatcher
from pymatgen.core import Structure
from pymatgen.io.cif import CifParser, CifWriter
from pymatgen.symmetry.analyzer import SpacegroupAnalyzer


def emit(payload: dict, output_path: str | None = None) -> None:
    payload = {"success": True, **payload}
    if output_path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))


def load_structure(path: str, *, primitive: bool = False) -> Structure:
    structures = CifParser(path, occupancy_tolerance=1.05).parse_structures(primitive=primitive)
    if not structures:
        raise ValueError("CIF contains no parseable crystal structure")
    return structures[0]


def declared_space_group(path: str) -> dict:
    import gemmi

    document = gemmi.cif.read_file(path)
    block = document[0]
    symbol = block.find_value("_space_group_name_H-M_alt") or block.find_value("_symmetry_space_group_name_H-M")
    number = block.find_value("_space_group_IT_number") or block.find_value("_symmetry_Int_Tables_number")
    return {"symbol": symbol or None, "number": int(number) if str(number).isdigit() else None}


def symmetry(args) -> None:
    structure = load_structure(args.input_path)
    analyzer = SpacegroupAnalyzer(structure, symprec=args.symprec, angle_tolerance=args.angle_tolerance)
    declared = declared_space_group(args.input_path)
    detected = {"symbol": analyzer.get_space_group_symbol(), "number": analyzer.get_space_group_number(), "crystal_system": analyzer.get_crystal_system(), "hall": analyzer.get_hall()}
    emit({"declared": declared, "detected": detected, "consistent": (not declared["number"] or declared["number"] == detected["number"]), "symprec": args.symprec, "angle_tolerance": args.angle_tolerance}, args.output_path)


def standardize(args) -> None:
    structure = load_structure(args.input_path)
    analyzer = SpacegroupAnalyzer(structure, symprec=args.symprec)
    if args.convention == "primitive":
        result = analyzer.get_primitive_standard_structure()
    elif args.convention == "refined":
        result = analyzer.get_refined_structure()
    else:
        result = analyzer.get_conventional_standard_structure()
    output = Path(args.output_path); output.parent.mkdir(parents=True, exist_ok=True)
    CifWriter(result, symprec=args.symprec).write_file(output)
    emit({"output_path": str(output), "convention": args.convention, "atom_count": len(result), "formula": result.composition.reduced_formula, "space_group": analyzer.get_space_group_symbol()})


def expand(args) -> None:
    structure = load_structure(args.input_path, primitive=False)
    refined = SpacegroupAnalyzer(structure, symprec=args.symprec).get_refined_structure()
    output = Path(args.output_path); output.parent.mkdir(parents=True, exist_ok=True)
    CifWriter(refined, symprec=args.symprec).write_file(output)
    emit({"output_path": str(output), "input_sites": len(structure), "expanded_sites": len(refined), "formula": refined.composition.formula})


def coordination(args) -> None:
    structure = load_structure(args.input_path)
    finder = CrystalNN(weighted_cn=True, distance_cutoffs=(0.5, 1.0))
    rows = []
    for index, site in enumerate(structure):
        try:
            neighbors = finder.get_nn_info(structure, index)
            rows.append({"site_index": index, "species": site.species_string, "fractional_coordinates": list(map(float, site.frac_coords)), "coordination_number": float(sum(item.get("weight", 1.0) for item in neighbors)), "neighbors": [{"site_index": int(item["site_index"]), "species": item["site"].species_string, "weight": float(item.get("weight", 1.0)), "distance_a": float(site.distance(item["site"]))} for item in neighbors]})
        except Exception as exc:
            rows.append({"site_index": index, "species": site.species_string, "error": type(exc).__name__})
    emit({"method": "CrystalNN", "site_count": len(structure), "sites": rows}, args.output_path)


def rdf(args) -> None:
    structure = load_structure(args.input_path)
    distances = []
    for index, site in enumerate(structure):
        for neighbor in structure.get_neighbors(site, args.radius):
            if neighbor.index > index:
                distances.append(float(neighbor.nn_distance))
    edges = np.linspace(0, args.radius, args.bins + 1)
    counts, _ = np.histogram(distances, bins=edges)
    centers = (edges[:-1] + edges[1:]) / 2
    shell = 4 * math.pi * centers**2 * np.diff(edges)
    density = len(structure) / structure.volume
    normalization = max(len(structure) * density, 1e-12) * shell / 2
    values = np.divide(counts, normalization, out=np.zeros_like(centers), where=normalization > 0)
    emit({"radius_a": args.radius, "bins": args.bins, "pair_count": len(distances), "rdf": [{"r_a": float(r), "g_r": float(g), "count": int(c)} for r, g, c in zip(centers, values, counts)]}, args.output_path)


def disorder(args) -> None:
    structure = load_structure(args.input_path)
    sites = []
    for index, site in enumerate(structure):
        occupancies = {str(specie): float(value) for specie, value in site.species.items()}
        total = sum(occupancies.values())
        if not site.is_ordered or abs(total - 1.0) > args.tolerance:
            sites.append({"site_index": index, "occupancies": occupancies, "occupancy_sum": total, "fractional_coordinates": list(map(float, site.frac_coords))})
    emit({"ordered": not sites, "disordered_site_count": len(sites), "sites": sites, "tolerance": args.tolerance}, args.output_path)


def match(args) -> None:
    left, right = load_structure(args.input_path), load_structure(args.other_path)
    matcher = StructureMatcher(ltol=args.ltol, stol=args.stol, angle_tol=args.angle_tolerance, primitive_cell=True, scale=True)
    emit({"matches": bool(matcher.fit(left, right)), "anonymous_matches": bool(matcher.fit_anonymous(left, right)), "left_formula": left.composition.reduced_formula, "right_formula": right.composition.reduced_formula, "left_sites": len(left), "right_sites": len(right), "tolerances": {"lattice": args.ltol, "site": args.stol, "angle": args.angle_tolerance}}, args.output_path)


def pxrd(args) -> None:
    structure = load_structure(args.input_path)
    pattern = XRDCalculator(wavelength=args.wavelength).get_pattern(structure, two_theta_range=(args.min_angle, args.max_angle), scaled=True)
    peaks = []
    for angle, intensity, hkls, d_hkl in zip(pattern.x, pattern.y, pattern.hkls, pattern.d_hkls):
        peaks.append({"two_theta_deg": float(angle), "relative_intensity": float(intensity), "d_a": float(d_hkl), "reflections": hkls})
    emit({"wavelength": args.wavelength, "two_theta_range": [args.min_angle, args.max_angle], "peak_count": len(peaks), "peaks": peaks}, args.output_path)


def interactive(args) -> None:
    structure = load_structure(args.input_path)
    atoms = [{"index": i, "element": site.species_string, "fractional": list(map(float, site.frac_coords)), "cartesian_a": list(map(float, site.coords)), "occupancies": {str(k): float(v) for k, v in site.species.items()}} for i, site in enumerate(structure[:args.max_atoms])]
    emit({"format": "dataseek-crystal-structure-v1", "formula": structure.composition.reduced_formula, "lattice_matrix_a": structure.lattice.matrix.tolist(), "cell": {"a": structure.lattice.a, "b": structure.lattice.b, "c": structure.lattice.c, "alpha": structure.lattice.alpha, "beta": structure.lattice.beta, "gamma": structure.lattice.gamma, "volume_a3": structure.volume}, "periodic": True, "atoms": atoms, "truncated": len(structure) > args.max_atoms}, args.output_path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("symmetry", "standardize", "expand", "coordination", "rdf", "disorder", "match", "pxrd", "interactive"))
    parser.add_argument("input_path"); parser.add_argument("other_path", nargs="?"); parser.add_argument("--output-path")
    parser.add_argument("--symprec", type=float, default=0.01); parser.add_argument("--angle-tolerance", type=float, default=5.0)
    parser.add_argument("--convention", choices=("primitive", "conventional", "refined"), default="conventional")
    parser.add_argument("--radius", type=float, default=10.0); parser.add_argument("--bins", type=int, default=100); parser.add_argument("--tolerance", type=float, default=0.01)
    parser.add_argument("--ltol", type=float, default=0.2); parser.add_argument("--stol", type=float, default=0.3)
    parser.add_argument("--wavelength", default="CuKa"); parser.add_argument("--min-angle", type=float, default=5.0); parser.add_argument("--max-angle", type=float, default=90.0); parser.add_argument("--max-atoms", type=int, default=10000)
    args = parser.parse_args()
    globals()[args.operation](args)


if __name__ == "__main__":
    main()
