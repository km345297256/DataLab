"""Reject impossible frozen interfaces before requesting any function body."""
import ast
import copy
import json

import pytest

from app.domain.services.skills.skill_creation.privacy import ensure_context
from app.domain.services.skills.skill_creation.script_authoring import ScriptModulePlan, _check_plan
from app.domain.services.skills.skill_creation.script_policy import (
    SkillScriptValidationError, validate_script, validate_supported_imports,
)
from test_skill_creation_script_authoring import COUNT, MAIN, PLAN, author


REPRESENTATIVE_LIBRARIES = {
    "argparse", "pathlib", "csv", "json", "math", "statistics", "collections", "itertools",
    "datetime", "re", "typing", "numpy", "pandas", "scipy", "xarray", "rasterio",
    "geopandas", "sklearn", "matplotlib", "sys", "warnings", "datalab_json", "hashlib",
}


@pytest.mark.parametrize("module", sorted(REPRESENTATIVE_LIBRARIES))
def test_shared_dependency_check_retains_every_existing_library(module):
    validate_supported_imports(ast.parse(f"import {module} as allowed_library"))
    validate_supported_imports(ast.parse(f"from {module}.synthetic_submodule import item"))


@pytest.mark.parametrize("source", ["import os", "import io", "from subprocess import run",
    "import csv, synthetic_unapproved_module", "def helper():\n    import synthetic_unapproved_module"])
def test_dependency_admission_is_open_even_when_module_is_not_installed(source):
    validate_supported_imports(ast.parse(source))


@pytest.mark.asyncio
async def test_open_manifest_dependency_does_not_trigger_a_full_regeneration():
    plan = copy.deepcopy(PLAN)
    plan["imports"].extend(["import os", "import synthetic_uninstalled_module"])
    script, calls = await author([plan, COUNT, MAIN])
    assert [call["authoring_phase"] for call in calls] == ["plan", "unit", "unit"]
    assert all("repair_feedback" not in call for call in calls)
    assert "import os" in script.content
    validate_script(script)


@pytest.mark.asyncio
@pytest.mark.parametrize("signature,issue", [
    ("def count_rows(rows, rows):", "invalid_function_signature"),
    ("def count_rows(rows: MissingType):", "undeclared_signature_reference"),
    ("def count_rows(rows) -> MissingType:", "undeclared_signature_reference"),
    ("def count_rows(rows: main):", "undeclared_signature_dependency"),
])
async def test_impossible_manifest_signature_is_repaired_before_freezing(signature, issue):
    bad = copy.deepcopy(PLAN)
    bad["functions"][0]["signature"] = signature
    _, calls = await author([bad, PLAN, COUNT, MAIN])
    assert [call["authoring_phase"] for call in calls] == ["plan", "plan", "unit", "unit"]
    assert calls[1]["repair_feedback"]["reason"] == "invalid_schema"
    assert calls[1]["repair_feedback"]["details"] == {"authoring_issue": issue}


@pytest.mark.parametrize("signature,imports,dependencies", [
    ("def count_rows(rows: list[dict[str, float]], scale: float = 1.0):", [], []),
    ("def count_rows(rows: np.ndarray) -> float:", ["import numpy as np"], []),
    ("def count_rows(rows: count_rows):", [], []),
    ("def count_rows(rows: main):", [], ["U002"]),
    ("def count_rows(rows: 'DomainData'):", [], []),
])
def test_manifest_keeps_declared_types_literals_recursion_and_forward_dependency(signature, imports, dependencies):
    data = copy.deepcopy(PLAN)
    data["imports"].extend(imports)
    data["functions"][0]["signature"] = signature
    data["functions"][0]["depends_on"] = dependencies
    plan = ScriptModulePlan.model_validate(data)
    _check_plan(plan, ensure_context(None), {"R001"}, {"R001"})


@pytest.mark.asyncio
async def test_final_assembled_script_admits_new_dependencies_without_importing_them():
    script, _ = await author([PLAN, COUNT, MAIN])
    script.content += "\ndef unused_helper():\n    import synthetic_uninstalled_module\n"
    validate_script(script)
