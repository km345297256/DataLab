"""Every independent harness stage accepts the same artificial V2 interface.

These are request-boundary regressions, not claims of model or scientific
success. Full computation/oracle tests remain separate and unchanged.
"""
from copy import deepcopy
import json

import pytest

from acceptance.skill_creator_real_model import (
    assert_oracle_isolation, assert_public_interface, assert_reference_isolation,
    assert_source_authoring_boundary, assertion_frames,
)
from app.domain.services.skills.skill_creation.interface_contracts import declared_validation_interface
from app.domain.services.skills.skill_creation.models import HelperScript, ScriptRequest
from app.domain.services.skills.skill_creation.schema_repair import SchemaFieldRepairs
from app.domain.services.skills.skill_creation.source_response import source_response_adapter, source_authoring_capabilities
from app.domain.services.skills.skill_reference_verification import reference_generation_payload
from app.domain.services.skills.skill_validation import (
    SyntheticValidationPlan, build_validation_generation_payload, validation_generation_schema,
)
from app.domain.services.skills.skill_validation_review import ValidationPlanReview, review_targets, _review_parts
from skill_interface_fixtures import group_summary_interface
from test_skill_validation_plan import method_contract, sources, plan_payload


def declared():
    interface = group_summary_interface()
    interface["cli_options"].append({"flags": ["--covariates"], "required": False,
        "value_type": "str", "action": "append", "nargs": "+", "default": [],
        "meaning": "Optional caller-selected covariates preserve an empty default collection."})
    interface["outputs"][1]["shape"]["properties"].update({
        "groups": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "mean": {"type": "number"}}}},
        "columns": {"type": "array", "items": {"type": "string"}, "column_name_prefix": ["group", "mean"]},
    })
    request = ScriptRequest(path="scripts/group_summary.py", purpose="Artificial independent grouped summary interface.",
        invocation="python scripts/group_summary.py --input INPUT --output OUTPUT", requirement_ids=["R001"],
        interface_contract=interface)
    return declared_validation_interface(request, validation_cases=[
        {"kind": "normal", "input": "Artificial grouped observations.", "expected": "Independent means and missingness."}])


def oracle_requests():
    context = build_validation_generation_payload(method_contract(), sources(), [declared()])
    yield {"stage": "synthetic_validation_plan", "schema": validation_generation_schema(), **context}
    plan = SyntheticValidationPlan.model_validate(plan_payload())
    full = review_targets(plan)
    parts = _review_parts(plan, full)
    for targets, scope in parts or [(full, None)]:
        yield {"stage": "synthetic_plan_review", "schema": ValidationPlanReview.model_json_schema(),
               **context, "validation_plan": plan.model_dump(mode="json"), "review_targets": targets,
               **({"review_scope": scope} if scope is not None else {})}


def reference_request(stage):
    plan = SyntheticValidationPlan.model_validate(plan_payload())
    context = reference_generation_payload(plan, method_contract(), sources(), [declared()])
    request = {"stage": stage, **context}
    metadata = {"path": plan.scripts[0].script_path, "purpose": "Independent artificial reference implementation.",
                "invocation": "python scripts/group_summary.py --input INPUT --output OUTPUT"}
    if stage == "synthetic_reference_implementation":
        request["script_request"] = metadata
        request["response_format"] = source_response_adapter(HelperScript, stage, request).request_format()
        request["script_authoring_capabilities"] = source_authoring_capabilities(HelperScript, stage)
    else:
        request["reference_scripts"] = [{**metadata, "content": "# Artificial independently authored reference body.\n"}]
    return request


def test_new_interface_reaches_all_oracle_review_and_field_repair_boundaries():
    for request in oracle_requests():
        original = deepcopy(request)
        assert_oracle_isolation(request["stage"], request)
        scoped = {**request, "schema": SchemaFieldRepairs.model_json_schema(), "previous_candidate": {},
                  "repair_feedback": {"document_schema": request["schema"], "field_repairs": [{
                      "field_id": "F001", "location": ["scripts", 0, "reason"], "type": "string",
                      "action": "set", "hint": "Complete the artificial explanation."}]}}
        assert_oracle_isolation(scoped["stage"], scoped)
        assert request == original


@pytest.mark.parametrize("stage", ["synthetic_reference_implementation", "synthetic_reference_review"])
def test_new_interface_reaches_both_blind_reference_boundaries(stage):
    request = reference_request(stage)
    original = deepcopy(request)
    own = {item["path"]: item["content"] for item in request.get("reference_scripts", [])}
    assert_reference_isolation(stage, request, candidate_sources=["CANDIDATE_SOURCE_PRIVATE_CANARY"],
        original_sources=["ORIGINAL_SOURCE_PRIVATE_CANARY"], reference_sources=own if stage.endswith("review") else None)
    assert_source_authoring_boundary(stage, request)
    assert request == original


@pytest.mark.parametrize("stage", ["implementation", "synthetic_script_repair", "synthetic_build_repair", "synthetic_method_repair"])
def test_all_actual_source_authoring_stages_keep_exact_host_capabilities(stage):
    source = reference_request("synthetic_reference_implementation")
    request = {"stage": stage, "script_request": source["script_request"]}
    if stage != "implementation":
        request["script"] = {**request["script_request"], "content": "# Artificial source needing a focused repair.\n"}
    request["response_format"] = source_response_adapter(HelperScript, stage, request).request_format()
    request["script_authoring_capabilities"] = source_authoring_capabilities(HelperScript, stage)
    assert_source_authoring_boundary(stage, request)


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(content="CANDIDATE_SOURCE_PRIVATE_CANARY"),
    lambda value: value["json_outputs"][0]["shape"].update(answer=12345),
    lambda value: value["arguments"].append("--invented"),
    lambda value: value["interface_contract"].update(candidate_results={"mean": 12345}),
    lambda value: value["cli_options"][0].update(default="ALTERED_AFTER_FREEZING"),
])
def test_shared_public_boundary_rejects_extra_code_answers_and_projection_drift(mutation):
    value = declared().model_dump(mode="json")
    mutation(value)
    with pytest.raises((AssertionError, ValueError)):
        assert_public_interface(value, include_validation_cases=True)


def test_reference_cannot_receive_oracle_examples_or_unprojected_source_metadata():
    request = reference_request("synthetic_reference_implementation")
    request["script_interfaces"][0]["validation_cases"] = [{"expected": "CANDIDATE_EXPECTED_VALUE"}]
    with pytest.raises(AssertionError, match="Non-public"):
        assert_reference_isolation(request["stage"], request)
    request = next(oracle_requests())
    request["sources"][0]["source_snapshot"] = "ORIGINAL_SOURCE_PRIVATE_CANARY"
    with pytest.raises(AssertionError, match="Original code/results"):
        assert_oracle_isolation(request["stage"], request)


def test_assertion_trace_records_only_safe_frame_locations():
    secret_local = "PRIVATE_MODEL_RESPONSE_MUST_NOT_BE_LOGGED"
    try:
        assert False, secret_local
    except AssertionError as error:
        frames = assertion_frames(error)
    assert frames and all(set(item) == {"file", "function", "line"} for item in frames)
    assert all("/" not in item["file"] and isinstance(item["line"], int) for item in frames)
    assert secret_local not in json.dumps(frames)
