"""Offline source-correction protocol checks; every source here is artificial."""
from copy import deepcopy
import json

import pytest
from langchain.messages import AIMessage

from acceptance.skill_creator_real_model import assert_reference_isolation, assert_source_authoring_boundary
from app.domain.services.skills.skill_creation.models import HelperScript
from app.domain.services.skills.skill_creation.package import validate_portable_document
from app.domain.services.skills.skill_creation.privacy import PrivacyContext, PrivateValue
from app.domain.services.skills.skill_creation.script_authoring import ScriptFunctionUnit, ScriptModulePlan, ScriptRepairSelection
from app.domain.services.skills.skill_creation.source_response import source_response_adapter, source_authoring_capabilities
from app.domain.services.skills.skill_creation.source_repair import build_source_repair_spec, source_repair_response_schema
from app.domain.services.skills.skill_creation.schema_repair import SchemaFieldRepairs
from test_skill_creation_gateway import make_gateway, requests
from test_skill_creator_interface_boundaries import reference_request


STAGES = ["implementation", "synthetic_script_repair", "synthetic_build_repair",
          "synthetic_method_repair", "synthetic_reference_implementation"]
PRIVATE = "artificial-original-input.csv"
SOURCE = "def calculate():\n    return '" + PRIVATE + "'\n"


def source_correction(stage, schema=HelperScript, reason="private_package_content"):
    request = reference_request("synthetic_reference_implementation")
    request["stage"] = stage
    if schema is ScriptFunctionUnit:
        request["function_request"] = {"unit_id": "U001", "name": "calculate",
            "signature": "def calculate():", "purpose": "Calculate artificial data.",
            "requirement_ids": ["R001"], "depends_on": []}
    adapter = source_response_adapter(schema, stage, request)
    privacy = PrivacyContext((PrivateValue(PRIVATE, "source_filename"),))
    source = SOURCE if reason == "private_package_content" else "def calculate():\n    return (\n"
    document = {**adapter.metadata, "content": source}
    spec = build_source_repair_spec(document, adapter, privacy, reason, {"line": 2})
    assert spec is not None
    original = {"reason": reason, "fields": ["content"], "hint": "Fix only the diagnosed artificial source.",
                "details": {"line": 2}}
    request.pop("response_format")
    request["schema"] = source_repair_response_schema(spec).model_json_schema()
    request["script_authoring_capabilities"] = source_authoring_capabilities(schema, stage)
    request["previous_candidate"] = privacy.redact_document(document)
    request["repair_feedback"] = {**original, "source_blocks": spec.public_blocks(privacy),
        "source_repair_context": {"basis": "original_check_of_bound_source", "feedback": deepcopy(original)}}
    return request


@pytest.mark.parametrize("stage", STAGES)
@pytest.mark.parametrize("schema", [HelperScript, ScriptFunctionUnit])
@pytest.mark.parametrize("reason", ["private_package_content", "script_syntax_invalid"])
def test_host_source_correction_forms_and_bad_patch_retries_keep_exact_capabilities(stage, schema, reason):
    request = source_correction(stage, schema, reason)
    for current_reason in (reason, "invalid_json", "empty_response", "output_truncated", "invalid_schema"):
        current = deepcopy(request)
        current["repair_feedback"]["reason"] = current_reason
        original = deepcopy(current)
        assert_source_authoring_boundary(stage, current)
        if stage == "synthetic_reference_implementation" and schema is HelperScript:
            assert_reference_isolation(stage, current, candidate_sources=["CANDIDATE_PRIVATE_CANARY"],
                                       original_sources=["ORIGINAL_PRIVATE_CANARY"])
        assert current == original


@pytest.mark.parametrize("mutate", [
    lambda r: r["schema"].update(additionalProperties=True),
    lambda r: r["schema"]["$defs"]["SourceBlockReplacement"]["properties"].update(path={"type": "string"}),
    lambda r: r["repair_feedback"]["source_blocks"][0].update(block_id="B999"),
    lambda r: r["repair_feedback"]["source_blocks"][0].update(start_line=True),
    lambda r: r["repair_feedback"]["source_blocks"][0].update(candidate_answers={"mean": 7}),
    lambda r: r["previous_candidate"].update(path="scripts/foreign.py"),
    lambda r: r["previous_candidate"].update(observed_answers={"mean": 7}),
    lambda r: r["script_authoring_capabilities"].update(source_form="function"),
    lambda r: r["repair_feedback"]["source_repair_context"].update(basis="invented_authority"),
    lambda r: r["repair_feedback"]["source_repair_context"]["feedback"].update(reason="method_review_rejected"),
    lambda r: r.update(response_format={"type": "python_source"}),
])
def test_source_correction_cannot_expand_wire_authority_or_change_artifact_identity(mutate):
    request = source_correction("synthetic_reference_implementation")
    mutate(request)
    with pytest.raises((AssertionError, ValueError)):
        assert_reference_isolation(request["stage"], request)


def test_source_correction_does_not_hide_candidate_source_in_blind_reference():
    request = source_correction("synthetic_reference_implementation")
    request["previous_candidate"]["content"] += "CANDIDATE_PRIVATE_CANARY"
    with pytest.raises(AssertionError):
        assert_reference_isolation(request["stage"], request, candidate_sources=["CANDIDATE_PRIVATE_CANARY"])


@pytest.mark.parametrize("schema", [ScriptModulePlan, ScriptRepairSelection])
def test_typed_json_authoring_field_repairs_keep_the_original_form(schema):
    stage = "implementation"
    request = {"stage": stage, "schema": SchemaFieldRepairs.model_json_schema(), "previous_candidate": {},
        "repair_feedback": {"document_schema": schema.model_json_schema(), "field_repairs": [{
            "field_id": "F001", "location": ["functions", 0, "purpose"], "type": "string",
            "action": "set", "hint": "Complete the artificial explanation."}]},
        "script_authoring_capabilities": source_authoring_capabilities(schema, stage)}
    assert_source_authoring_boundary(stage, request)
    request["repair_feedback"]["document_schema"]["additionalProperties"] = True
    with pytest.raises(AssertionError):
        assert_source_authoring_boundary(stage, request)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [AIMessage(content="{"), AIMessage(content=""),
    AIMessage(content='{"repairs":', response_metadata={"finish_reason": "length"}),
    {"repairs": [{"block_id": "B001", "replacement": 123}]}])
async def test_actual_gateway_initial_and_retry_source_requests_pass_offline_boundaries(failure):
    original = reference_request("synthetic_reference_implementation")
    payload = {k: v for k, v in original.items() if k not in {"stage", "response_format", "script_authoring_capabilities"}}
    privacy = PrivacyContext((PrivateValue(PRIVATE, "source_filename"),))
    patch = {"repairs": [{"block_id": "B001", "replacement": "    return 'public-current-selected-input.csv'\n"}]}
    gateway, calls, _ = make_gateway([AIMessage(content=SOURCE), failure, patch], privacy=privacy)
    await gateway.generate(HelperScript, original["stage"], "Preserve the complete artificial method.", payload,
                           lambda value: validate_portable_document(value, privacy))
    recorded = requests(calls)
    assert len(recorded) == 3
    for request in recorded:
        assert_source_authoring_boundary(request["stage"], request)
        assert_reference_isolation(request["stage"], request)
    assert recorded[1]["schema"] == recorded[2]["schema"]
    assert recorded[1]["repair_feedback"]["source_repair_context"] == recorded[2]["repair_feedback"]["source_repair_context"]
    assert PRIVATE not in json.dumps(recorded)
