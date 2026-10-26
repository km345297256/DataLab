"""The pre-code interface is shared, immutable and independent of candidate code."""
import json
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.domain.services.skills.skill_creation.interface_contracts import (
    ScriptInterfaceContract, declared_validation_interface, validate_declared_invocation,
    validate_declared_script_interface,
)
from app.domain.services.skills.skill_creation.models import HelperScript, ScriptRequest, SkillRecipe
from app.domain.services.skills.skill_creation.package import validate_implementation, validate_recipe
from app.domain.services.skills.skill_creation.workflow_authoring import WorkflowDraft, WorkflowDraftV2, compile_workflow
from app.domain.services.skills.skill_creation_error import SkillPackageValidationError
from app.domain.services.skills.skill_reference_verification import verify_reference_plan
from app.domain.services.skills.skill_script_verification import script_interfaces
from app.domain.services.skills.skill_validation import SyntheticValidationPlan, validate_validation_plan
from test_session_skill_creator import recipe_payload
from test_skill_reference_verification import host_receipt, method_review
from test_skill_validation_plan import method_contract, plan_payload, sources
from test_skill_workflow_authoring import draft_payload, contract as workflow_contract
from skill_interface_fixtures import group_summary_interface as declaration


SOURCE = '''import argparse
import json
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument('--input', required=True)
p.add_argument('--output', required=True)
a = p.parse_args()
private_implementation_marker = 'CANDIDATE_SOURCE_MUST_NOT_REACH_REFERENCE'
with (Path(a.output) / 'status.json').open('w') as stream:
    json.dump({'status': 'CANDIDATE_ANSWER_MUST_NOT_REACH_REFERENCE'}, stream)
'''


def request(**updates):
    return ScriptRequest.model_validate({"path": "scripts/group_summary.py", "purpose": "Compute grouped summaries with explicit missingness.",
        "invocation": "python scripts/group_summary.py --input INPUT --output OUTPUT", "requirement_ids": ["R001"],
        "interface_contract": declaration(), **updates})


def script(content=SOURCE):
    return HelperScript.model_validate({**request().model_dump(include={"path", "purpose", "invocation"}), "content": content})


def recipe(content=SOURCE):
    return SkillRecipe.model_validate({**recipe_payload(), "scripts": [script(content).model_dump()],
        "implementation": {"interface_version": 2, "kind": "parameterized_scripts",
            "reason": "The selected calculation is represented by a reusable executable.", "scripts": [request().model_dump()]}})


def test_new_authoring_requires_contract_but_old_stored_recipes_still_load():
    payload = draft_payload()
    payload["implementation"] = {"kind": "parameterized_scripts", "reason": "Generate a parameterized executable for the method.",
                                 "scripts": [request().model_dump(exclude={"interface_contract"})]}
    WorkflowDraft.model_validate(payload)
    with pytest.raises(ValidationError):
        WorkflowDraftV2.model_validate(payload)
    payload["implementation"]["scripts"][0]["interface_contract"] = declaration()
    compiled = compile_workflow(WorkflowDraftV2.model_validate(payload), workflow_contract())
    assert compiled.implementation.interface_version == 2
    assert compiled.implementation.scripts[0].interface_contract == ScriptInterfaceContract.model_validate(declaration())
    assert SkillRecipe.model_validate(compiled.model_dump()).model_dump() == compiled.model_dump()
    old = request().model_dump(exclude={"interface_contract"})
    assert ScriptRequest.model_validate(old).interface_contract is None


@pytest.mark.parametrize("change", [
    lambda value: value.update(output_mode="file"),
    lambda value: value["outputs"][1].update(path="@output/../private.json"),
    lambda value: value["inputs"][0].update(missing_semantics=[]),
    lambda value: value["outputs"][1].update(shape={"type": "unknown"}),
    lambda value: value["cli_options"][0].update(flags=["--output"]),
])
def test_incoherent_interfaces_fail_before_any_source_exists(change):
    value = declaration()
    change(value)
    with pytest.raises(ValidationError):
        ScriptInterfaceContract.model_validate(value)


def test_invocation_conflict_is_rejected_while_workflow_can_still_repair_it():
    value = request(invocation=request().invocation + " --scale 2")
    with pytest.raises(SkillPackageValidationError) as failure:
        validate_declared_invocation(value, value.interface_contract)
    assert failure.value.code == "invalid_declared_script_interface"
    candidate = recipe()
    candidate.implementation.scripts[0] = value
    with pytest.raises(SkillPackageValidationError):
        validate_implementation(candidate, method_contract(), [])


def test_exact_schema_names_and_cli_default_bytes_are_not_normalized():
    value = declaration()
    value["inputs"][0]["columns"][0] = " group "
    value["cli_options"].append({"flags": ["--label"], "required": False, "value_type": "str",
        "action": "append", "nargs": "+", "default": "  literal\n", "meaning": "Preserve the caller's exact label characters."})
    frozen = ScriptInterfaceContract.model_validate(value).model_dump()
    assert frozen["inputs"][0]["columns"][0] == " group "
    assert frozen["cli_options"][-1]["default"] == "  literal\n"


@pytest.mark.parametrize("default", [[], ["group", "value"], {"column": " group ", "missing": None}])
def test_container_cli_defaults_remain_exact_public_contract_values(default):
    value = declaration()
    value["cli_options"].append({"flags": ["--covariate"], "required": False, "value_type": "str",
        "action": "append", "default": default, "choices": ["group", "value"],
        "meaning": "Use the exact collection default until caller supplies covariates."})
    frozen = ScriptInterfaceContract.model_validate(value)
    assert frozen.cli_options[-1].default == default
    assert json.loads(frozen.model_dump_json())["cli_options"][-1]["default"] == default


@pytest.mark.parametrize("path", ["my output.csv", "输出.csv", "a" * 97 + ".csv", "../x.csv", "dir//x.csv"])
def test_unrepresentable_output_names_fail_before_code_or_oracle_generation(path):
    from app.domain.services.skills.skill_validation import _relative_path
    value = declaration()
    value["outputs"][0]["path"] = "@output/" + path
    with pytest.raises(ValueError):
        _relative_path(path)
    with pytest.raises(ValidationError):
        ScriptInterfaceContract.model_validate(value)


def test_nested_safe_output_names_have_the_same_declaration_and_assertion_grammar():
    from app.domain.services.skills.skill_validation import CSVHeaderAssertion
    value = declaration()
    value["outputs"][0]["path"] = "@output/tables/group-summary.csv"
    frozen = ScriptInterfaceContract.model_validate(value)
    path = frozen.outputs[0].path.removeprefix("@output/")
    assert CSVHeaderAssertion(type="csv_header", path=path, columns=frozen.outputs[0].columns).path == path


def test_stored_v2_without_interface_cannot_fall_back_to_ast_projection():
    candidate = recipe()
    candidate.implementation.scripts[0].interface_contract = None
    with pytest.raises(SkillPackageValidationError) as failure:
        script_interfaces(candidate)
    assert failure.value.code == "invalid_declared_script_interface"


def test_predeclared_column_prefix_keeps_real_host_output_checks_for_dynamic_code():
    from app.domain.services.skills.skill_public_output_contract import check_public_output, compile_public_output_contract
    value = declaration()
    value["outputs"][1]["shape"]["properties"]["columns"] = {
        "type": "array", "items": {"type": "string"}, "column_name_prefix": ["group", "mean"]}
    interface = declared_validation_interface(request(interface_contract=value))
    contract = compile_public_output_contract([interface])
    assert len(contract.rules) == 1
    assert check_public_output(contract.rules[0], b'{"status":"ok","columns":["group","mean","extra"]}')["status"] == "passed"
    assert check_public_output(contract.rules[0], b'{"status":"ok","columns":["mean","group"]}')["status"] == "failed"


@pytest.mark.parametrize("content,reason", [
    (SOURCE.replace("required=True)", "required=False)", 1), "cli_option_conflict"),
    (SOURCE.replace("a = p.parse_args()", "p.add_argument('--undeclared')\na = p.parse_args()"), "undeclared_cli_option"),
    (SOURCE.replace("Path(a.output) / 'status.json'", "Path(a.output)"), "file_directory_conflict"),
    (SOURCE.replace("'status':", "'renamed_status':"), "required_output_field_missing"),
    (SOURCE.replace("'status.json'", "'renamed.json'"), "output_binding_conflict"),
])
def test_candidate_and_package_reject_proven_conflicts_without_exposing_source(content, reason):
    with pytest.raises(SkillPackageValidationError) as failure:
        validate_declared_script_interface(script(content), request().interface_contract)
    assert failure.value.code == "declared_script_interface_mismatch"
    assert reason in {item["reason"] for item in failure.value.details["interface_issues"]}
    assert "CANDIDATE_" not in json.dumps(failure.value.details)
    with pytest.raises(SkillPackageValidationError, match="冻结"):
        validate_recipe(recipe(content))


def test_unknown_custom_parser_and_dynamic_shape_keep_the_same_pre_code_contract():
    # Neither a new parsing library nor dynamic construction is a rejection.
    dynamic = "import sys\noptions = dict(zip(sys.argv[1::2], sys.argv[2::2]))\n# Dynamic serialization is checked at runtime.\n"
    before = declared_validation_interface(request())
    validate_declared_script_interface(script(dynamic), request().interface_contract)
    original = script_interfaces(recipe())[0]
    changed = script_interfaces(recipe(dynamic))[0]
    assert original == changed
    assert original.model_dump(exclude={"validation_cases"}) == before.model_dump(exclude={"validation_cases"})
    encoded = original.model_dump_json()
    assert "CANDIDATE_" not in encoded and "def " not in encoded
    assert "zero value remains a valid observation" in encoded


@pytest.mark.parametrize("mutation,reason", [
    (lambda case: setattr(case.assertions[0], "path", "renamed.csv"), "undeclared_output_binding"),
    (lambda case: case.assertions[0].fields.update(count=case.assertions[0].fields.pop("valid_count")), "undeclared_csv_field"),
])
def test_validation_plan_checks_frozen_output_bindings_without_altering_answers(mutation, reason):
    plan = SyntheticValidationPlan.model_validate(plan_payload())
    mutation(plan.scripts[0].cases[0])
    frozen = plan.model_dump()
    with pytest.raises(SkillPackageValidationError) as failure:
        validate_validation_plan(plan, method_contract(), sources(), [declared_validation_interface(request())])
    assert failure.value.code == "declared_validation_interface_mismatch"
    assert reason in {item["reason"] for item in failure.value.details["interface_issues"]}
    assert plan.model_dump() == frozen


@pytest.mark.asyncio
async def test_blind_reference_rejects_same_frozen_interface_conflict_before_execution():
    plan = SyntheticValidationPlan.model_validate(plan_payload())
    execute = AsyncMock()
    async def generate(schema, stage, instruction, payload, check):
        assert stage == "synthetic_reference_implementation"
        assert "CANDIDATE_" not in json.dumps(payload)
        value = schema.model_validate({**payload["script_request"], "content": SOURCE.replace("'status':", "'wrong':")})
        check(value)
        return value
    with pytest.raises(SkillPackageValidationError) as failure:
        await verify_reference_plan(plan, method_contract(), sources(), [declared_validation_interface(request())],
            generate=generate, execute=execute)
    assert failure.value.code == "declared_script_interface_mismatch"
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_blind_reference_preserves_isolation_and_independent_execution():
    plan = SyntheticValidationPlan.model_validate(plan_payload())
    plan.scripts[0].cases[0].oracle_explanation += " SECRET_INDEPENDENT_DERIVATION"
    declared = script_interfaces(recipe())
    stages = []
    async def generate(schema, stage, instruction, payload, check):
        stages.append(stage)
        encoded = json.dumps(payload)
        if stage == "synthetic_reference_implementation":
            assert "CANDIDATE_" not in encoded and "SECRET_INDEPENDENT" not in encoded
            assert payload["script_interfaces"][0]["interface_contract"] == request().interface_contract.model_dump(mode="json")
            from acceptance.skill_creator_real_model import assert_reference_isolation
            assert_reference_isolation(stage, {"stage": stage, **payload}, candidate_sources=[SOURCE])
            document = {**payload["script_request"], "content": SOURCE.replace("CANDIDATE_", "INDEPENDENT_")}
        else:
            document = method_review(payload)
        value = schema.model_validate(document)
        check(value)
        return value
    execute = AsyncMock(side_effect=host_receipt)
    _, receipt = await verify_reference_plan(plan, method_contract(), sources(), declared, generate=generate, execute=execute)
    assert stages == ["synthetic_reference_implementation", "synthetic_reference_review"]
    assert receipt["status"] == "verified"
    assert execute.await_count == 1
