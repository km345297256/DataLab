from __future__ import annotations

import json
from typing import Any

OPERATIONS = {
    "cif_inspect": "inspect",
    "cif_validate_structure": "validate",
    "cif_extract_atoms": "extract",
    "cif_convert_xyz": "xyz",
    "cif_visualize_structure": "visualize",
    "cif_composition_density": "density",
    "cif_analyze_geometry": "geometry",
    "cif_simulate_pxrd": "pxrd",
    "cif_compare_structures": "compare",
    "cif_generate_supercell": "supercell",
    "cif_standardize_report": "report",
}

ADVANCED_OPERATIONS = {
    "cif_check_space_group": "symmetry",
    "cif_standardize_cell": "standardize",
    "cif_expand_symmetry": "expand",
    "cif_coordination_environment": "coordination",
    "cif_radial_distribution": "rdf",
    "cif_disorder_analysis": "disorder",
    "cif_match_structures": "match",
    "cif_calculate_pxrd": "pxrd",
    "cif_export_interactive_structure": "interactive",
}

def build_command(tool_name: str, arguments: dict[str, Any]) -> list[str]:
    if tool_name not in OPERATIONS and tool_name not in ADVANCED_OPERATIONS:
        raise ValueError(f"Unsupported chemistry tool: {tool_name}")
    input_path = arguments.get("input_path")
    if not isinstance(input_path, str) or not input_path:
        raise ValueError("input_path is required")
    script = "advanced_operations.py" if tool_name in ADVANCED_OPERATIONS else "operations.py"
    operation = ADVANCED_OPERATIONS[tool_name] if tool_name in ADVANCED_OPERATIONS else OPERATIONS[tool_name]
    command = ["python", f"/opt/ai-dataseek/tools/chemistry/{script}", operation, input_path]
    if tool_name in {"cif_compare_structures", "cif_match_structures"}:
        other = arguments.get("other_path")
        if not isinstance(other, str) or not other:
            raise ValueError("other_path is required")
        command.append(other)
    for name, value in arguments.items():
        if name in {"input_path", "other_path"} or value is None:
            continue
        flag = "--" + name.replace("_", "-")
        command.extend([flag, json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)])
    return command
