"""Model-authored content cannot create dangling coverage pointers."""
from copy import deepcopy
import json

import pytest
from pydantic import ValidationError

from app.domain.services.skills.method_contract import CoverageError, MethodContract, validate_coverage
from app.domain.services.skills.skill_creation.models import HelperScript, SkillRecipe
from app.domain.services.skills.skill_creation.package import validate_implementation, validate_recipe
from app.domain.services.skills.skill_creation.workflow_authoring import (
    WorkflowDraft, check_workflow_draft, compile_workflow,
)


def contract(kind="method", statement="Compute HC3 covariance for the declared model."):
    return MethodContract(requirements=[{
        "id": "R001", "kind": kind, "statement": statement, "source_ids": ["S001"],
    }])


def draft_payload(step_count=6):
    return {
        "name": "covariance-analysis",
        "description": "Estimate the declared model covariance for newly selected data with explicit diagnostics.",
        "triggers": ["Model covariance", "Model diagnostics"],
        "goal": "Compute reproducible model diagnostics from newly selected inputs.",
        "applicability": ["The input contains the selected independent observations."],
        "parameters": [{"name": "input_path", "description": "Read only the currently selected input file.",
                        "value_type": "path", "required": True, "default": None,
                        "constraints": "The input must remain read only."}],
        "data_requirements": [{"content": "Verify the input fields and units before fitting."}],
        "dependencies": [{"content": "Verify required calculation tools are available."}],
        "steps": [{"action": f"Compute HC3 covariance for analysis phase {index + 1}.",
                   "decision": "Stop when the declared model cannot be estimated.",
                   "verification": "Verify the covariance dimensions and symmetry.",
                   "requirement_ids": ["R001"] if index == step_count - 1 else []}
                  for index in range(step_count)],
        "outputs": [{"name": "Covariance table", "format": "CSV", "content": "Export the complete covariance matrix.",
                     "acceptance": "The table preserves all declared coefficients."}],
        "validation_cases": [{"kind": kind, "input": f"Use synthetic {kind} input observations.",
                              "expected": "Report the expected synthetic validation outcome."}
                             for kind in ("normal", "boundary", "invalid")],
        "limitations": [{"content": "Reject interpretations unsupported by model assumptions."}],
        "implementation": {"kind": "tool_workflow", "reason": "Use the available trusted calculation tools.", "scripts": []},
    }


def compile_and_check(payload, method=None, *, pending=False):
    method = method or contract()
    result = compile_workflow(WorkflowDraft.model_validate(payload), method)
    validate_recipe(result)
    validate_implementation(result, method, [])
    validate_coverage(method, result.model_dump(), pending_scripts=[
        item.model_dump() for item in result.implementation.scripts
    ] if pending else None)
    return result


def test_authoring_schema_does_not_accept_model_authored_coverage_or_pointers():
    schema = WorkflowDraft.model_json_schema()
    assert "coverage" not in schema["properties"]
    assert "RequirementCoverage" not in schema["$defs"]
    assert "scripts" not in schema["properties"]
    assert "AuthoredScript" not in schema["$defs"] and "HelperScript" not in schema["$defs"]
    assert "content" not in schema["$defs"]["ScriptRequest"]["properties"]
    assert "requirement_ids" in schema["$defs"]["ScriptRequest"]["properties"]
    for root in ("parameters", "steps", "outputs", "validation_cases",
                 "data_requirements", "dependencies", "limitations"):
        definition = schema["properties"][root]["items"]["$ref"].split("/")[-1]
        assert "requirement_ids" in schema["$defs"][definition]["properties"]
    payload = draft_payload()
    payload["coverage"] = [{"requirement_id": "R001", "locations": ["/steps/6/action"]}]
    with pytest.raises(ValidationError):
        WorkflowDraft.model_validate(payload)


@pytest.mark.parametrize("step_count", [2, 6, 40])
def test_last_step_is_bound_by_its_content_without_model_index_arithmetic(step_count):
    payload = draft_payload(step_count)
    before = deepcopy(payload)
    result = compile_and_check(payload)
    assert result.coverage[0].locations == [f"/steps/{step_count - 1}"]
    assert payload == before
    assert result.steps[-1].action == payload["steps"][-1]["action"]
    assert "requirement_ids" not in result.steps[-1].model_dump()
    assert SkillRecipe.model_validate(result.model_dump()).model_dump() == result.model_dump()


def test_reorder_changes_only_host_locations_not_method_assignment():
    payload = draft_payload()
    original_action = payload["steps"][-1]["action"]
    payload["steps"].insert(0, payload["steps"].pop())
    result = compile_and_check(payload)
    assert result.coverage[0].locations == ["/steps/0"]
    assert result.steps[0].action == original_action


def test_all_kind_sections_and_strings_compile_without_altering_content():
    statements = [
        ("input", "require the selected independent observations.", "parameters"),
        ("method", "Compute HC3 covariance for the declared model.", "steps"),
        ("output", "Export the complete covariance matrix.", "outputs"),
        ("validation", "Verify synthetic validation outcomes explicitly.", "validation_cases"),
        ("constraint", "Reject interpretations unsupported by model assumptions.", "limitations"),
    ]
    method = MethodContract(requirements=[{"id": f"R{index:03d}", "kind": kind, "statement": statement,
                                          "source_ids": ["S001"]}
                                         for index, (kind, statement, _) in enumerate(statements, 1)])
    payload = draft_payload()
    payload["steps"][-1]["requirement_ids"] = []
    for index, (_, _, root) in enumerate(statements, 1):
        payload[root][0]["requirement_ids"] = [f"R{index:03d}"]
    result = compile_and_check(payload, method)
    assert [item.requirement_id for item in result.coverage] == [item.id for item in method.requirements]
    assert [item.locations for item in result.coverage] == [[f"/{root}/0"] for _, _, root in statements]
    for root in ("data_requirements", "dependencies", "limitations"):
        assert getattr(result, root) == [item["content"] for item in payload[root]]


@pytest.mark.parametrize("mutation,expected", [
    ("missing", "incomplete_requirement_coverage"),
    ("wrong_section", "requirement_wrong_section"),
    ("missing_method", "method_identifiers_lost"),
    ("unknown_id", "unknown_implementation_reference"),
    ("duplicate_id", "duplicate_requirement_coverage"),
])
def test_link_compilation_preserves_coverage_and_scientific_rejection(mutation, expected):
    payload = draft_payload()
    last = payload["steps"][-1]
    if mutation == "missing":
        last["requirement_ids"] = []
    elif mutation == "wrong_section":
        last["requirement_ids"] = []
        payload["outputs"][0]["requirement_ids"] = ["R001"]
    elif mutation == "missing_method":
        last["action"] = "Compute an unspecified generic covariance method."
    elif mutation == "unknown_id":
        last["requirement_ids"] = ["R999"]
    else:
        last["requirement_ids"] = ["R001", "R001"]
    with pytest.raises(CoverageError, match=expected) as failure:
        compile_and_check(payload)
    if mutation in {"unknown_id", "duplicate_id"}:
        assert failure.value.details["binding_issues"][0]["location"][:3] == ["steps", 5, "requirement_ids"]
        assert failure.value.details["available_requirement_ids"] == ["R001"]
        assert "R999" not in json.dumps(failure.value.details)


def script_declaration():
    return {"path": "scripts/covariance.py", "purpose": "Calculate the declared covariance from selected input.",
            "invocation": "python scripts/covariance.py --input INPUT --output OUTPUT",
            "requirement_ids": ["R001"], "implementation_source_ids": []}


def pending_payload():
    payload = draft_payload()
    payload["steps"][-1]["requirement_ids"] = []
    payload["implementation"] = {"kind": "parameterized_scripts", "reason": "Compute the reusable covariance procedure.",
                                 "scripts": [script_declaration()]}
    return payload


def test_pending_declarations_resolve_only_real_slots_and_do_not_invent_script_content():
    payload = pending_payload()
    result = compile_and_check(payload, pending=True)
    assert result.coverage[0].locations == ["/scripts/0"]
    assert result.scripts == []
    with pytest.raises(CoverageError, match="unknown_coverage_location"):
        validate_coverage(contract(), result.model_dump())
    payload["implementation"]["scripts"] = []
    payload["implementation"]["kind"] = "tool_workflow"
    with pytest.raises(CoverageError, match="incomplete_requirement_coverage"):
        compile_and_check(payload)


@pytest.mark.parametrize("content", ["", "# Generated in the next stage.",
    "def covariance(values):\n    # Compute HC3 covariance for the declared model.\n    return values\n"])
def test_workflow_cannot_submit_placeholder_or_complete_source_bodies(content):
    # Real provider failure: the old dual script fields encouraged a declaration
    # with content="" in top-level scripts while implementation.scripts was also
    # populated. The workflow now has exactly one place for declarations.
    payload = pending_payload()
    request = script_declaration()
    payload["scripts"] = [{key: request[key] for key in ("path", "purpose", "invocation", "requirement_ids")} | {
        "content": content,
    }]
    with pytest.raises(ValidationError) as failure:
        WorkflowDraft.model_validate(payload)
    assert [(error["loc"], error["type"]) for error in failure.value.errors()] == [
        (("scripts",), "extra_forbidden")]


def test_declarations_compile_coverage_then_generated_source_keeps_storage_compatibility():
    result = compile_and_check(pending_payload(), pending=True)
    request = result.implementation.scripts[0]
    result.scripts = [HelperScript.model_validate({
        key: request.model_dump()[key] for key in ("path", "purpose", "invocation")
    } | {"content": "def covariance(values):\n    # Compute HC3 covariance for the declared model.\n    return values\n"})]
    validate_coverage(contract(), result.model_dump())
    assert result.coverage[0].locations == ["/scripts/0"]
    restored = SkillRecipe.model_validate(result.model_dump(mode="json"))
    assert restored.scripts == result.scripts and restored.coverage == result.coverage
    result.scripts[0].path = "scripts/unrelated.py"
    with pytest.raises(CoverageError, match="unexpected_generated_script"):
        validate_implementation(result, contract(), [])


def test_multiple_script_declarations_bind_their_real_pending_positions():
    payload = pending_payload()
    payload["implementation"]["scripts"].append({**script_declaration(),
        "path": "scripts/diagnostics.py", "requirement_ids": ["R002"],
        "invocation": "python scripts/diagnostics.py --input input.csv --output output"})
    method = contract()
    method.requirements.append(method.requirements[0].model_copy(update={"id": "R002"}))
    result = compile_and_check(payload, method, pending=True)
    assert result.scripts == []
    assert [entry.locations for entry in result.coverage] == [["/scripts/0"], ["/scripts/1"]]


def test_script_generation_still_must_preserve_declared_method():
    result = compile_and_check(pending_payload(), pending=True)
    declaration = result.implementation.scripts[0]
    result.scripts = [HelperScript.model_validate({
        key: declaration.model_dump()[key] for key in ("path", "purpose", "invocation")
    } | {"content": "def covariance(values):\n    # Unspecified generic calculation.\n    return values\n"})]
    # Coverage sees the completed script, not a label claiming that its method exists.
    with pytest.raises(CoverageError, match="method_identifiers_lost"):
        validate_coverage(contract(), result.model_dump())


def test_native_paired_validation_failure_reports_authored_limitations_label():
    # Regression from a real-model artificial paired analysis: R014 requires
    # test-case coverage, but the response associated it only with limitation 4.
    method = contract()
    method.requirements.append(method.requirements[0].model_copy(update={
        "id": "R014", "kind": "validation",
        "statement": "须提供并覆盖以下验证：字段映射生效、缺失配对整对剔除并计入excluded、最小样本边界（n=2与n=12可接受，n<2或n>12拒绝）、重复标识拒绝、方向为after-minus-before、样本标准差使用n-1分母、p_value不加1且使用1e-12容差、全零差值情形。",
    }))
    payload = draft_payload()
    payload["limitations"] = [{"content": "These limitations do not execute verification.", "requirement_ids": []}
                              for _ in range(5)]
    payload["limitations"][4]["requirement_ids"] = ["R014"]
    before = deepcopy(payload)
    with pytest.raises(CoverageError, match="requirement_wrong_section") as failure:
        check_workflow_draft(WorkflowDraft.model_validate(payload), method,
                             lambda recipe: validate_coverage(method, recipe.model_dump()))
    assert failure.value.identifiers == ["R014"]
    assert failure.value.details["authored_issues"] == [{
        "code": "requirement_wrong_section", "requirement_id": "R014", "requirement_kind": "validation",
        "locations": [["limitations", 4, "requirement_ids"]],
        "allowed_sections": [["validation_cases"], ["steps"]], "repair_action": "need_validation",
    }]
    assert "coverage_issues" not in failure.value.details and "available_locations" not in failure.value.details
    assert payload == before


def test_missing_link_feedback_names_required_content_without_inventing_association():
    method = contract()
    payload = draft_payload()
    payload["steps"][-1]["requirement_ids"] = []
    with pytest.raises(CoverageError, match="incomplete_requirement_coverage") as failure:
        check_workflow_draft(WorkflowDraft.model_validate(payload), method,
                             lambda recipe: validate_coverage(method, recipe.model_dump()))
    issue = failure.value.details["authored_issues"][0]
    assert issue["locations"] == []
    assert issue["allowed_sections"] == [["steps"], ["implementation", "scripts"]]
    assert issue["repair_action"] == "need_method"


def test_script_link_feedback_uses_authored_declaration_location_not_future_script_body():
    payload = pending_payload()
    method = contract("validation", "Verify covariance errors with meaningful synthetic cases.")
    with pytest.raises(CoverageError, match="requirement_wrong_section") as failure:
        check_workflow_draft(WorkflowDraft.model_validate(payload), method,
            lambda recipe: validate_coverage(method, recipe.model_dump(),
                pending_scripts=[item.model_dump() for item in recipe.implementation.scripts]))
    assert failure.value.details["authored_issues"][0]["locations"] == [
        ["implementation", "scripts", 0, "requirement_ids"]]


def test_authored_feedback_preserves_missing_scientific_anchor_details():
    payload = draft_payload()
    payload["steps"][-1]["action"] = "Compute unspecified covariance for the declared model."
    method = contract()
    with pytest.raises(CoverageError, match="method_identifiers_lost") as failure:
        check_workflow_draft(WorkflowDraft.model_validate(payload), method,
                             lambda recipe: validate_coverage(method, recipe.model_dump()))
    assert failure.value.details["missing_anchors"]
    assert failure.value.details["authored_issues"][0]["locations"] == [["steps", 5, "requirement_ids"]]


def test_correct_authored_content_still_passes_unchanged_checks():
    method = contract()
    payload = draft_payload()
    result = check_workflow_draft(WorkflowDraft.model_validate(payload), method,
                                  lambda recipe: validate_coverage(method, recipe.model_dump()))
    assert result == compile_and_check(payload)
