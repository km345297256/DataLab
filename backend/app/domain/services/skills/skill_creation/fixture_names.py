"""Allocate portable names for initial synthetic inputs without changing tests.

Only the pre-review authoring boundary calls this module. A declared fixture
name and the worker's exact input-reference grammar form a binding; ordinary
data strings, dictionary keys, expectations and already frozen plans do not.
"""
from __future__ import annotations

from copy import deepcopy

from app.domain.services.skills.skill_validation import (
    ScriptValidationInterface, SyntheticValidationPlan, validate_validation_plan,
)
from app.domain.services.skills.skill_validation_arguments import argument_reference


def _reference(value: str, mapping: dict[str, str]) -> str:
    reference = argument_reference(value)
    if reference is not None and reference[0] == "input" and reference[1] in mapping:
        return "{input:" + mapping[reference[1]] + "}"
    return value


def _json_bindings(value, mapping):
    # The original plan's complete validation proves that every JSON string
    # containing {input: is a complete, declared, non-self reference. Keys and
    # bare filename-looking values are ordinary data, never rewrite targets.
    if isinstance(value, str):
        return _reference(value, mapping) if value.startswith("{input:") else value
    if isinstance(value, list):
        return [_json_bindings(item, mapping) for item in value]
    if isinstance(value, dict):
        return {key: _json_bindings(item, mapping) for key, item in value.items()}
    return value


def _binding_form(document):
    """Compare all scientific data after erasing only physical binding names."""
    result = deepcopy(document)
    for script in result["scripts"]:
        for case in script["cases"]:
            mapping = {fixture["path"]: f"fixture-{index}." + fixture["format"]
                       for index, fixture in enumerate(case["input_files"])}
            case["argv"] = [_reference(value, mapping) for value in case["argv"]]
            for fixture in case["input_files"]:
                fixture["path"] = mapping[fixture["path"]]
                if fixture["format"] == "json":
                    fixture["data"] = _json_bindings(fixture["data"], mapping)
    return result


def normalize_initial_fixture_names(plan, privacy, *, contract, sources, interfaces):
    """Rename only source-filename collisions in a fully valid initial plan.

    A failed precondition returns the unchanged plan to the normal checks. No
    scientific check is waived and no attempt is made to infer undeclared file
    references. The caller must retain the complete final privacy scan.
    """
    if type(plan) is not SyntheticValidationPlan:
        return plan
    try:
        original = plan.model_dump(mode="json")
        validated = SyntheticValidationPlan.model_validate(original)
        declarations = [item if isinstance(item, ScriptValidationInterface)
                        else ScriptValidationInterface.model_validate(item) for item in interfaces]
        validate_validation_plan(validated, contract, sources, declarations)
    except (ValueError, TypeError):
        return plan

    result = deepcopy(original)
    occupied = {fixture["path"].casefold() for script in original["scripts"]
                for case in script["cases"] for fixture in case["input_files"]}
    # These interfaces come from the package that already passed the pipeline's
    # independent method review. No generated plan, prose, API spelling or code
    # guess can grant this role. Legacy/unknown/analysis-value names stay intact.
    binding_only = {item.script_path for item in declarations
                    if item.interface_contract is not None
                    and item.interface_contract.input_filename_role == "binding_only"}
    changed = False
    for script_index, script in enumerate(result["scripts"], 1):
        if script["script_path"] not in binding_only:
            continue
        for case_index, case in enumerate(script["cases"], 1):
            mapping = {}
            for fixture_index, fixture in enumerate(case["input_files"], 1):
                name = fixture["path"]
                issues = privacy.scan_document({"path": name}, limit=None)
                if not issues or {issue.category for issue in issues} != {"source_filename"}:
                    continue
                for counter in range(1, 129):
                    candidate = f"fixture-{script_index}-{case_index}-{fixture_index}-{counter}." + fixture["format"]
                    if candidate.casefold() in occupied or privacy.scan_document({"path": candidate}, limit=None):
                        continue
                    mapping[name] = candidate
                    occupied.add(candidate.casefold())
                    break
                else:
                    # Do not partially normalize an allocation we cannot prove.
                    return plan
            if not mapping:
                continue
            changed = True
            case["argv"] = [_reference(value, mapping) for value in case["argv"]]
            for fixture in case["input_files"]:
                fixture["path"] = mapping.get(fixture["path"], fixture["path"])
                if fixture["format"] == "json":
                    fixture["data"] = _json_bindings(fixture["data"], mapping)
    if not changed:
        return plan
    if _binding_form(original) != _binding_form(result):
        return plan
    try:
        normalized = SyntheticValidationPlan.model_validate(result)
        validate_validation_plan(normalized, contract, sources, declarations)
    except (ValueError, TypeError):
        return plan
    return normalized
