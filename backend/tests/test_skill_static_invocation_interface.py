"""A public invocation supports parser-independent synthetic plan validation.

Only declaration metadata is shared with the oracle. Source layout and arbitrary
strings do not introduce options; actual argument effects still need execution.
"""
import copy
from types import SimpleNamespace

import pytest

from app.domain.services.skills.skill_creation.script_policy import validate_script
from app.domain.services.skills.skill_script_verification import script_interfaces
from app.domain.services.skills.skill_validation import SyntheticValidationPlan, validate_validation_plan
from app.domain.services.skills.skill_creation_error import SkillPackageValidationError
from test_skill_validation_plan import method_contract, plan_payload, sources


def recipe(content, invocation):
    script = SimpleNamespace(path="scripts/group_summary.py", content=content, invocation=invocation)
    return SimpleNamespace(scripts=[script], data_requirements=[], parameters=[], outputs=[], validation_cases=[])


@pytest.mark.parametrize("content", [
    "import sys\noptions = dict(zip(sys.argv[1::2], sys.argv[2::2]))\n",
    "import click\n@click.command()\n@click.option('--input')\n@click.option('--output')\n@click.option('--scale')\ndef main(input, output, scale):\n    pass\nmain()\n",
])
@pytest.mark.parametrize("scale", ["--scale 2", "--scale=2"])
def test_public_invocation_custom_parser_options_reach_plan_validation(content, scale):
    value = recipe(content, "python scripts/group_summary.py --input INPUT --output OUTPUT " + scale)
    validate_script(value.scripts[0])
    interfaces = script_interfaces(value)
    assert interfaces[0].arguments == ["--input", "--output", "--scale"]
    payload = plan_payload()
    for case in payload["scripts"][0]["cases"]:
        case["argv"].extend(["--scale", "2"])
    frozen = copy.deepcopy(payload)
    validate_validation_plan(SyntheticValidationPlan.model_validate(payload), method_contract(), sources(), interfaces)
    assert payload == frozen


def test_source_strings_are_not_new_options_and_ast_declarations_still_merge():
    value = recipe("""import argparse
parser = argparse.ArgumentParser()
parser.add_argument('--declared')
message = '--private-implementation-value'
""", "python scripts/group_summary.py --input INPUT --output OUTPUT --public-option=literal")
    interfaces = script_interfaces(value)
    assert interfaces[0].arguments == ["--declared", "--input", "--output", "--public-option"]
    payload = plan_payload()
    payload["scripts"][0]["cases"][0]["argv"].extend(["--private-implementation-value", "1"])
    with pytest.raises(SkillPackageValidationError) as failure:
        validate_validation_plan(SyntheticValidationPlan.model_validate(payload), method_contract(), sources(), interfaces)
    assert failure.value.code == "undeclared_validation_argument"


def test_new_input_manifest_convention_preserves_original_input_semantics():
    from app.domain.services.skills.skill_creation.prompts import SYSTEM_PROMPT
    from app.domain.services.skills.skill_creation.script_policy import script_authoring_capabilities
    contract = script_authoring_capabilities("complete_script")["runtime_contract"]["multiple_files"]
    assert "one JSON input manifest" in contract and "{input:filename}" in contract
    assert "must preserve original data fields and configuration locations" in contract
    assert "Do not rewrite an existing explicit CLI contract" in contract
    assert "不能改变来源已规定的数据字段或配置位置" in SYSTEM_PROMPT
    assert "验证器无法表达时如实报告接口不支持" in SYSTEM_PROMPT
