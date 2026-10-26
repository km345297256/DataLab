"""Public shapes survive ordinary dictionary assembly without leaking answers."""
import ast
import json

import pytest

from app.domain.services.skills.script_public_interface import public_script_interface
from app.domain.services.skills.skill_creation.checkpoints import content_digest
from app.domain.services.skills.skill_creation_error import SkillPackageValidationError
from app.domain.services.skills.skill_script_verification import script_interfaces
from app.domain.services.skills.skill_validation import ScriptValidationInterface, SyntheticValidationPlan, validate_validation_plan
from test_skill_validation_plan import interfaces, method_contract, plan_payload, sources


SOURCE = '''import argparse
import json
from pathlib import Path
PRIMARY_KEY = "group"

def describe(values):
    if not values:
        return {"n": 0, "mean": None, "sample_sd": None}
    return {"n": len(values), "mean": 987654321 / len(values), "sample_sd": "FORBIDDEN_SCIENTIFIC_ANSWER"}

def compute(rows):
    paired = []
    for entry in rows:
        summary = describe(entry)
        item = {PRIMARY_KEY: entry["group"], "excluded": 0}
        item.update(summary)
        item["p_value"] = 0.000123456
        paired.append(item)
    return paired, {"complete": True}

def validate_name(name):
    if "/" in name:
        raise ValueError("Use a filename")
    return name

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--report-name", default="report.json")
    p.add_argument("--alpha", type=float, default=0.123456789)
    args = p.parse_args()
    paired, quality = compute([])
    report = {"paired": paired, "quality": quality}
    output = Path(args.output)
    report_name = validate_name(args.report_name)
    report_path = output / report_name
    with report_path.open("w") as handle:
        json.dump(report, handle)
'''


def paired_shape(source=SOURCE):
    return public_script_interface(source)["json_outputs"][0]["shape"]["properties"]["paired"]["items"]


def case_plan(pointer="/paired/0/mean", filename="report.json", *, override=False):
    payload = plan_payload()
    for case in payload["scripts"][0]["cases"]:
        if override:
            case["argv"].extend(["--report-name", filename])
        if case["expected_exit_code"] == 0:
            case["assertions"] = [{"type": "json_value", "path": filename, "pointer": pointer,
                                    "expected": {"value": 0.25}}]
    return SyntheticValidationPlan.model_validate(payload)


def validate(plan, source=SOURCE):
    public = public_script_interface(source)
    interface = ScriptValidationInterface.model_validate({**interfaces()[0], **public,
        "arguments": ["--input", "--output", "--report-name", "--alpha"]})
    validate_validation_plan(plan, method_contract(), sources(), [interface])


def test_loop_local_summary_update_and_tuple_return_keep_flat_fields_without_any_answers():
    public = public_script_interface(SOURCE)
    assert public["output_mode"] == "directory"
    assert public["json_outputs"][0]["path"] == "@output/report.json"
    assert public["json_outputs"][0]["filename_argument"] == "--report-name"
    shape = paired_shape()
    assert shape == {"type": "object", "properties": {key: {"type": "unknown"}
        for key in ("group", "excluded", "n", "mean", "sample_sd", "p_value")}, "additional_keys": False}
    encoded = json.dumps(public)
    assert all(marker not in encoded for marker in ("987654321", "FORBIDDEN_SCIENTIFIC_ANSWER", "0.000123456",
               "0.123456789", "len(values)", "describe", "compute", "return", "ValueError"))


@pytest.mark.parametrize("construction", [
    'item = {PRIMARY_KEY: entry["group"], **summary, "p_value": 0.000123456}',
    'item = dict(summary)\n        item.update(group=entry["group"], p_value=0.000123456)',
    'item = {}\n        item.update(summary, group=entry["group"], p_value=0.000123456)',
])
def test_equivalent_literal_spread_and_keyword_updates_preserve_real_fixed_keys(construction):
    original = 'item = {PRIMARY_KEY: entry["group"], "excluded": 0}\n        item.update(summary)\n        item["p_value"] = 0.000123456'
    source = SOURCE.replace(original, construction)
    # dict() is a structure-preserving constructor; no expression evaluation.
    shape = paired_shape(source)
    assert set(shape["properties"]) == {"group", "n", "mean", "sample_sd", "p_value"}
    assert shape["additional_keys"] is False
    assert "describe" not in shape["properties"]


@pytest.mark.parametrize("override", [False, True])
def test_invented_describe_nesting_is_rejected_before_execution_including_renamed_output(override):
    filename = "renamed.json" if override else "report.json"
    validate(case_plan(filename=filename, override=override))
    with pytest.raises(SkillPackageValidationError, match="validation_output_field_unknown"):
        validate(case_plan("/paired/0/describe/mean", filename, override=override))


def test_flat_to_nested_repair_changes_frozen_interface_digest():
    from test_session_skill_creator import recipe_payload, helper_script
    from app.domain.services.skills.skill_creation.models import HelperScript, SkillRecipe
    original = SkillRecipe.model_validate(recipe_payload())
    original.scripts = [HelperScript.model_validate(helper_script())]
    original.scripts[0].content = SOURCE
    changed = original.model_copy(deep=True)
    changed.scripts[0].content = SOURCE.replace('item.update(summary)', 'item["describe"] = summary')
    assert content_digest(script_interfaces(original)) != content_digest(script_interfaces(changed))
    assert "describe" in paired_shape(changed.scripts[0].content)["properties"]


def test_dynamic_dictionary_keys_do_not_invent_fields_or_hide_known_outer_structure():
    source = SOURCE.replace('item.update(summary)', 'item[entry["dynamic"]] = summary')
    shape = paired_shape(source)
    assert shape["additional_keys"] is True and set(shape["properties"]) == {"group", "excluded", "p_value"}
    validate(case_plan("/paired/0/new_key/mean"), source)  # Unknown dynamic keys are not falsely rejected.


def test_unknown_spread_preserves_names_but_does_not_claim_closed_or_nested_structure():
    source = SOURCE.replace('item.update(summary)', 'item.update(external_mapping(entry))')
    shape = paired_shape(source)
    assert shape["additional_keys"] is True
    assert shape["properties"]["group"] == {"type": "unknown"}
    validate(case_plan("/paired/0/unresolved/child"), source)


@pytest.mark.parametrize("body", [
    'if entry:\n            item = {"incorrect": 1}\n        else:\n            paired.append(item)',
    'if entry:\n            item = {"incorrect": 1}\n        paired.append(item)',
])
def test_sibling_branch_or_after_branch_use_does_not_inherit_conditional_assignment(body):
    prefix = SOURCE.index('        summary = describe(entry)')
    end = SOURCE.index('    return paired,', prefix)
    source = SOURCE[:prefix] + "        " + body + "\n" + SOURCE[end:]
    ast.parse(source)
    assert paired_shape(source) == {"type": "unknown"}


def test_filename_wrapper_that_reassigns_its_parameter_is_not_assumed_identity():
    source = SOURCE.replace('    return name', '    name = transform(name)\n    return name')
    assert public_script_interface(source)["json_outputs"] == []


def test_unknown_appended_element_prevents_fabricating_a_closed_element_schema():
    source = SOURCE.replace('    return paired,', '    paired.append(external_result())\n    return paired,')
    assert paired_shape(source) == {"type": "unknown"}


def test_mutation_after_append_is_included_in_the_serialized_object_shape():
    source = SOURCE.replace('        paired.append(item)', '        paired.append(item)\n        item["later"] = {"detail": 99}')
    assert paired_shape(source)["properties"]["later"] == {
        "type": "object", "properties": {"detail": {"type": "unknown"}}, "additional_keys": False}


def test_conditional_replacement_of_scalar_by_object_does_not_claim_fixed_nesting():
    source = SOURCE.replace('        paired.append(item)',
        '        if entry:\n            item["mean"] = {"detail": 99}\n        paired.append(item)')
    assert paired_shape(source)["properties"]["mean"] == {"type": "unknown"}


def test_heterogeneous_literal_array_does_not_infer_all_elements_from_first_entry():
    source = SOURCE.replace('paired, quality = compute([])',
        'paired, quality = ([{"field": {"nested": 99}}, {"field": 3}], {})')
    assert paired_shape(source)["properties"]["field"] == {"type": "unknown"}
