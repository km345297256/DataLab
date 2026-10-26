"""Diagnostic-specific repair keeps privacy and scientific verdicts intact."""
import json
from contextlib import contextmanager, nullcontext
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain.messages import AIMessage
from pydantic import BaseModel, ConfigDict, Field

from app.domain.services.skills.method_contract import CoverageError, MethodContract, RequirementCoverage, validate_coverage
from app.domain.services.skills.skill_creation.gateway import SynthesisGateway
from app.domain.services.skills.skill_creation.privacy import PrivacyContext, PrivateValue
from app.domain.services.skills.skill_creation_error import SkillCreationError, SkillPackageValidationError
from app.domain.utils.model_response import (
    ModelEmptyResponseError, ModelIncompleteResponseError, ModelRefusedResponseError,
)


class SmallRecipe(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str = Field(min_length=5)


VALID = {"name": "scientific-summary", "description": "Summarize the selected current input."}
PRIVATE = "original-private-owner-value"


def make_gateway(responses, *, privacy=None, on_progress=None):
    calls = []
    pending = iter(responses)

    async def invoke(messages):
        calls.append(messages)
        response = next(pending)
        if isinstance(response, Exception):
            raise response
        return response if isinstance(response, AIMessage) else AIMessage(content=json.dumps(response))

    usage = SimpleNamespace(record_from_message=AsyncMock())
    context = privacy or PrivacyContext((PrivateValue(PRIVATE, "identity"),))
    gateway = SynthesisGateway(
        model=SimpleNamespace(ainvoke=invoke), system="Design a reusable scientific workflow.",
        privacy=context, response_validation=lambda validator: nullcontext(), usage_recorder=usage,
        user_id="synthetic-owner", session_id="synthetic-session",
        settings=SimpleNamespace(model_provider="synthetic-provider", model_name="synthetic-model"),
        overrides={}, on_progress=on_progress,
    )
    return gateway, calls, usage


def check_privacy(context):
    def check(value):
        issues = context.scan_document(value)
        if issues:
            raise SkillPackageValidationError("private_package_content",
                details={"privacy_issues": [issue.as_dict() for issue in issues]})
    return check


def requests(calls):
    return [json.loads(messages[1].content) for messages in calls]


@pytest.mark.asyncio
async def test_schema_then_privacy_failures_each_receive_a_targeted_repair(caplog):
    progress = AsyncMock()
    gateway, calls, usage = make_gateway([
        {"name": "scientific-summary", PRIVATE: "not a declared field"},
        {**VALID, "description": f"Summarize input owned by {PRIVATE}."},
        VALID,
    ], on_progress=progress)
    result = await gateway.generate(SmallRecipe, "workflow", "Preserve every method.",
        {"immutable_requirements": ["Preserve HC3 and Holm correction."]}, check_privacy(gateway.privacy))

    assert result.model_dump() == VALID
    observed = requests(calls)
    assert len(observed) == usage.record_from_message.await_count == 3
    assert [request.get("repair_feedback", {}).get("reason") for request in observed] == [
        None, "invalid_schema", "private_package_content"]
    assert "previous_candidate" not in observed[1]
    assert observed[2]["previous_candidate"]["description"] == "Summarize input owned by {source_identity}."
    assert observed[2]["repair_feedback"]["details"]["privacy_issues"][0]["location"] == ["description"]
    assert observed[2]["repair_feedback"]["details"]["privacy_issues"][0]["category"] == "identity"
    assert all(request["immutable_requirements"] == ["Preserve HC3 and Holm correction."] for request in observed)
    assert [(event.attempt, event.status, event.reason) for event in gateway.attempts] == [
        (1, "running", None), (1, "repairing", "invalid_schema"),
        (2, "running", None), (2, "repairing", "private_package_content"),
        (3, "running", None), (3, "passed", None),
    ]
    assert progress.await_count == 6
    assert PRIVATE not in json.dumps(observed)
    assert PRIVATE not in caplog.text
    assert PRIVATE not in json.dumps([asdict(event) for event in gateway.attempts])


@pytest.mark.asyncio
async def test_identical_schema_failure_stops_after_two_attempts_even_if_values_change():
    gateway, calls, _ = make_gateway([
        {**VALID, "description": "x"}, {**VALID, "description": "y"}, VALID,
    ])
    with pytest.raises(SkillCreationError) as failure:
        await gateway.generate(SmallRecipe, "workflow", "Keep the methods.", {})
    assert (failure.value.stage, failure.value.code) == ("workflow", "invalid_schema")
    assert len(calls) == 2
    assert all(event.status != "passed" for event in gateway.attempts)


@pytest.mark.asyncio
async def test_same_diagnostic_stops_when_it_reappears_after_another_error():
    gateway, calls, _ = make_gateway([
        {**VALID, "description": f"Owned by {PRIVATE}."},
        {"name": VALID["name"]},
        {**VALID, "description": f"Data belongs to {PRIVATE}."},
        VALID,
    ])
    with pytest.raises(SkillCreationError) as failure:
        await gateway.generate(SmallRecipe, "workflow", "Keep the methods.", {}, check_privacy(gateway.privacy))
    assert failure.value.code == "private_package_content"
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_distinct_diagnostics_cannot_extend_generation_beyond_four_attempts():
    gateway, calls, _ = make_gateway([
        {"name": VALID["name"]},
        {**VALID, "description": "incomplete metadata"},
        {**VALID, "description": f"Owned by {PRIVATE}."},
        {**VALID, "description": "duplicate parameters"},
        VALID,
    ])
    privacy_check = check_privacy(gateway.privacy)

    def check(value):
        privacy_check(value)
        if value.description == "incomplete metadata":
            raise SkillPackageValidationError("invalid_skill_metadata")
        if value.description == "duplicate parameters":
            raise SkillPackageValidationError("duplicate_skill_parameters")

    with pytest.raises(SkillCreationError) as failure:
        await gateway.generate(SmallRecipe, "workflow", "Keep the methods.", {}, check)
    assert failure.value.code == "duplicate_skill_parameters"
    assert len(calls) == 4
    assert [event.reason for event in gateway.attempts if event.status == "repairing"] == [
        "invalid_schema", "invalid_skill_metadata", "private_package_content", "duplicate_skill_parameters"]


@pytest.mark.asyncio
async def test_private_schema_values_and_extra_keys_never_enter_repair_messages(caplog):
    gateway, calls, _ = make_gateway([
        {"name": VALID["name"], "description": {PRIVATE: PRIVATE}, PRIVATE: PRIVATE},
        VALID,
    ])
    await gateway.generate(SmallRecipe, "workflow", "Keep the methods.", {})
    repair = requests(calls)[1]
    assert repair["repair_feedback"]["fields"] == ["description"]
    assert {tuple(issue["location"]) for issue in repair["repair_feedback"]["details"]["schema_errors"]} == {
        ("description",), ("<extra_field>",)}
    assert "previous_candidate" not in repair
    assert PRIVATE not in json.dumps(requests(calls))
    assert PRIVATE not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", [
    AIMessage(content="", additional_kwargs={"refusal": "Provider refusal"}),
    AIMessage(content=json.dumps(VALID), response_metadata={"finish_reason": "content_filter"}),
    AIMessage(content=[{"type": "refusal", "refusal": "Provider refusal"}]),
])
async def test_model_refusal_is_terminal_even_with_a_later_success_available(refusal):
    gateway, calls, _ = make_gateway([refusal, VALID])
    with pytest.raises(SkillCreationError) as failure:
        await gateway.generate(SmallRecipe, "workflow", "Keep the methods.", {})
    assert failure.value.code == "model_refused"
    assert failure.value.retryable is False
    assert len(calls) == 1
    assert not any(event.status == "repairing" for event in gateway.attempts)


@pytest.mark.asyncio
async def test_adapter_refusal_exception_is_terminal_and_not_a_provider_outage():
    # invoke_model_request raises this protocol error before the stage response
    # validator; production does not return the refusal AIMessage to the caller.
    gateway, calls, _ = make_gateway([ModelRefusedResponseError(), VALID])
    with pytest.raises(SkillCreationError) as failure:
        await gateway.generate(SmallRecipe, "workflow", "Keep the methods.", {})
    assert failure.value.code == "model_refused"
    assert failure.value.retryable is False
    assert len(calls) == 1
    assert not any(event.status == "repairing" for event in gateway.attempts)


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [ModelEmptyResponseError, ModelIncompleteResponseError])
async def test_adapter_empty_or_incomplete_response_receives_bounded_complete_regeneration(error_type):
    gateway, calls, _ = make_gateway([error_type(), VALID])
    result = await gateway.generate(SmallRecipe, "workflow", "Keep the methods.", {})
    assert result.model_dump() == VALID
    assert len(calls) == 2
    repair = requests(calls)[1]
    assert repair["repair_feedback"]["reason"] == "incomplete_response"
    assert "previous_candidate" not in repair


@pytest.mark.asyncio
async def test_repeated_adapter_empty_and_incomplete_responses_share_one_retry_bound():
    gateway, calls, _ = make_gateway([ModelEmptyResponseError(), ModelIncompleteResponseError(), VALID])
    with pytest.raises(SkillCreationError) as failure:
        await gateway.generate(SmallRecipe, "workflow", "Keep the methods.", {})
    assert failure.value.code == "incomplete_response"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_scientific_rejection_does_not_ask_the_same_reviewer_to_reverse_its_verdict():
    gateway, calls, _ = make_gateway([VALID, VALID])
    check_calls = []

    def scientific_review(value):
        check_calls.append(value)
        raise CoverageError("method_review_rejected", ["R001"])

    with pytest.raises(SkillCreationError) as failure:
        await gateway.generate(SmallRecipe, "method_review", "Judge whether HC3 was preserved.", {}, scientific_review)
    assert (failure.value.stage, failure.value.code) == ("method_review", "method_review_rejected")
    assert len(calls) == len(check_calls) == 1
    assert not any(event.status == "repairing" for event in gateway.attempts)


@pytest.mark.asyncio
async def test_unexpected_validator_error_is_not_reinterpreted_as_bad_model_output():
    gateway, calls, _ = make_gateway([VALID, VALID])

    def broken_host_validation(value):
        raise RuntimeError("synthetic internal defect")

    with pytest.raises(RuntimeError, match="synthetic internal defect"):
        await gateway.generate(SmallRecipe, "workflow", "Keep the methods.", {}, broken_host_validation)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_validator_defect_inside_adapter_callback_keeps_its_original_exception():
    gateway, calls, _ = make_gateway([VALID, VALID])
    active = {}
    original_invoke = gateway.model.ainvoke
    defect = RuntimeError("synthetic callback defect")

    @contextmanager
    def response_validation(validator):
        active["validator"] = validator
        try:
            yield
        finally:
            active.clear()

    async def invoke_with_driver_validation(messages):
        response = await original_invoke(messages)
        # The real driver executes the registered stage validator before
        # ainvoke returns; emulate that order without any provider or storage.
        active["validator"](response)
        return response

    def broken_host_validation(value):
        raise defect

    gateway.response_validation = response_validation
    gateway.model.ainvoke = invoke_with_driver_validation
    with pytest.raises(RuntimeError) as failure:
        await gateway.generate(SmallRecipe, "workflow", "Keep the methods.", {}, broken_host_validation)
    assert failure.value is defect
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_provider_error_remains_a_safe_provider_failure():
    gateway, calls, _ = make_gateway([RuntimeError("synthetic private provider diagnostic"), VALID])
    with pytest.raises(SkillCreationError) as failure:
        await gateway.generate(SmallRecipe, "workflow", "Keep the methods.", {})
    assert failure.value.code == "model_unavailable"
    assert "synthetic private provider diagnostic" not in failure.value.public_message
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_invalid_coverage_slot_receives_exact_safe_position_and_fixed_repair_guidance():
    class CoverageDocument(BaseModel):
        steps: list[dict[str, str]]
        coverage: list[RequirementCoverage]

    contract = MethodContract(requirements=[{"id": "R011", "kind": "method",
        "statement": "Use HC3 and Holm while retaining the scientific method.", "source_ids": ["S001"]}])
    original = {"steps": [{"action": "Apply HC3 covariance.", "verification": "Check the Holm correction."}],
        "coverage": [{"requirement_id": "R011", "locations": ["/applicability/2", "/steps/0/action", "/steps/0/verification"]}]}
    corrected = {**original, "coverage": [{"requirement_id": "R011", "locations": ["/steps/0/action", "/steps/0/verification"]}]}
    gateway, calls, _ = make_gateway([original, corrected])
    result = await gateway.generate(CoverageDocument, "workflow", "Preserve all methods.", {},
        lambda value: validate_coverage(contract, value.model_dump()))
    assert result.model_dump() == corrected
    feedback = requests(calls)[1]["repair_feedback"]
    assert feedback["reason"] == "invalid_coverage_location"
    assert feedback["fields"] == ["R011"]
    assert feedback["details"]["coverage_issues"][0]["location"] == ["coverage", 0, "locations", 0]
    assert "details.coverage_issues" in feedback["hint"]
    assert "goal、applicability、implementation、coverage" in feedback["hint"]
    assert "不得删除要求" in feedback["hint"]
    assert "/applicability/2" not in json.dumps(feedback)
