"""Malformed explanation text cannot turn an independent rejection into a vote."""
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.domain.services.skills.method_contract import MethodReview
from app.domain.services.skills.skill_creation.schema_repair import (
    SchemaFieldRepairs, SchemaRepairRejected, apply_schema_repairs, build_schema_repair_spec,
)
from app.domain.services.skills.skill_creation_error import SkillCreationError
from app.domain.services.skills.skill_validation import SyntheticValidationPlan
from app.domain.services.skills.skill_validation_review import ValidationPlanReview, validate_oracle_review
from app.domain.utils.model_response import ModelIncompleteResponseError
from test_skill_creation_gateway import make_gateway, requests
from test_skill_creation_pipeline import oracle_review_payload, review_payload
from test_skill_creation_checkpoints import MemoryCache, builds
from test_skill_validation_plan import plan_payload


def review_case(kind, verdict="rejected", text=""):
    if kind == "method":
        candidate = review_payload()
        candidate["requirements"][0].update(status="missing", explanation=text)
        return candidate, MethodReview, "method_review", lambda value: None, "method_review_rejected"
    plan = SyntheticValidationPlan.model_validate(plan_payload())
    candidate = oracle_review_payload(plan)
    candidate["scripts"][0]["cases"][0]["assertions"][0]["correctness"].update(status=verdict, explanation=text)
    return candidate, ValidationPlanReview, "synthetic_plan_review", lambda value: validate_oracle_review(value, plan), "validation_oracle_" + verdict


def repair_spec(candidate, schema):
    with pytest.raises(ValidationError) as failure:
        schema.model_validate(candidate)
    return build_schema_repair_spec(candidate, failure.value, schema, review_explanations=True)


def repairs(spec, explanation="The original independent calculation shows this expected value is incorrect."):
    return {"repairs": [{"field_id": item["field_id"], "action": "set", "value": explanation}
                        for item in spec.public_fields()]}


@pytest.mark.parametrize("kind,verdict,text", [
    ("method", "rejected", "bad"), ("oracle", "rejected", ""), ("oracle", "uncertain", " ... "),
])
@pytest.mark.asyncio
async def test_only_explanation_is_repaired_while_complete_adverse_review_reaches_design_revision(kind, verdict, text):
    candidate, schema, stage, check, code = review_case(kind, verdict, text)
    spec = repair_spec(candidate, schema)
    assert spec is not None
    patch = repairs(spec)
    gateway, calls, _ = make_gateway([candidate, patch])
    result = await gateway.generate(schema, stage, "Review independently.", {}, check)
    expected = apply_schema_repairs(candidate, spec, SchemaFieldRepairs.model_validate(patch))
    assert result.model_dump() == expected
    assert len(calls) == 2
    assert requests(calls)[1]["schema"]["title"] == "SchemaFieldRepairs"
    assert requests(calls)[1]["previous_candidate"] == candidate
    assert "不返回完整文档" in calls[1][0].content
    assert "不返回完整测试计划" not in calls[1][0].content
    if kind == "oracle":
        assert result.verdict() == verdict
    else:
        assert result.requirements[0].status == "missing"


@pytest.mark.parametrize("problem", ["status", "identifier", "container", "extra_field"])
@pytest.mark.parametrize("kind", ["method", "oracle"])
@pytest.mark.asyncio
async def test_negative_review_with_non_explanation_schema_error_is_not_rewritten(kind, problem):
    candidate, schema, stage, check, code = review_case(kind)
    if kind == "method":
        target = candidate["sources"][0]
        if problem == "status":
            target["status"] = "unknown"
        elif problem == "identifier":
            target.pop("source_id")
        elif problem == "container":
            candidate["sources"] = None
        else:
            target["foreign"] = "undeclared"
    else:
        target = candidate["scripts"][0]["cases"][0]
        if problem == "status":
            target["input_validity"]["status"] = "unknown"
        elif problem == "identifier":
            target.pop("case_id")
        elif problem == "container":
            candidate["scripts"][0]["support"] = None
        else:
            target["foreign"] = "undeclared"
    assert repair_spec(candidate, schema) is None
    gateway, calls, _ = make_gateway([candidate])
    with pytest.raises(SkillCreationError) as failure:
        await gateway.generate(schema, stage, "Review independently.", {}, check)
    assert failure.value.code == code and len(calls) == 1


@pytest.mark.parametrize("kind,verdict", [("method", "rejected"), ("oracle", "rejected"), ("oracle", "uncertain")])
@pytest.mark.parametrize("failure_kind", ["full_rewrite", "bad_patch", "timeout", "truncated", "driver_truncated"])
@pytest.mark.asyncio
async def test_failed_explanation_recovery_keeps_sticky_adverse_code(kind, verdict, failure_kind):
    candidate, schema, stage, check, code = review_case(kind, verdict)
    if failure_kind == "timeout":
        after = [TimeoutError("synthetic transport failure")]
    elif failure_kind == "truncated":
        from langchain.messages import AIMessage
        after = [AIMessage(content="{", response_metadata={"finish_reason": "length"})] * 2
    elif failure_kind == "driver_truncated":
        after = [ModelIncompleteResponseError("max_tokens")] * 2
    elif failure_kind == "bad_patch":
        after = [{"repairs": [{"field_id": "F999", "action": "set", "value": "accepted"}]}] * 2
    else:
        positive = deepcopy(candidate)
        if kind == "method":
            positive["requirements"][0].update(status="preserved", explanation="Everything is preserved by the implementation.")
        else:
            positive["scripts"][0]["cases"][0]["assertions"][0]["correctness"].update(status="accepted", explanation="The expected value is correct.")
        after = [positive] * 2
    gateway, calls, _ = make_gateway([candidate, *after])
    async def produce():
        return await gateway.generate(schema, stage, "Review independently.", {}, check)
    producer = AsyncMock(side_effect=produce)
    cache = MemoryCache()
    for _ in range(2):
        with pytest.raises(SkillCreationError) as failure:
            await builds(cache).run(schema, stage, "Review independently.", {}, check, producer,
                                    decision_identity="same-scientific-candidate")
        assert failure.value.code == code
    assert producer.await_count == 1
    assert len(calls) == (2 if failure_kind == "timeout" else 3)
    assert not any(event.status == "passed" for event in gateway.attempts)


@pytest.mark.parametrize("false_value", [False, "false", 0])
@pytest.mark.asyncio
async def test_schema_accepted_false_forms_cannot_be_erased_by_unrelated_schema_failure(false_value):
    candidate = review_payload()
    candidate.update(implementation_appropriate=false_value, sources=None)
    gateway, calls, _ = make_gateway([candidate, review_payload()])
    with pytest.raises(SkillCreationError) as failure:
        await gateway.generate(MethodReview, "method_review", "Review independently.", {})
    assert failure.value.code == "method_review_rejected" and len(calls) == 1


def test_schema_field_ids_cannot_patch_status_or_replace_parent_to_reverse_vote():
    candidate, schema, *_ = review_case("oracle", "uncertain", "...")
    spec = repair_spec(candidate, schema)
    before = deepcopy(candidate)
    patch = repairs(spec)
    patch["repairs"][0]["value"] = {"status": "accepted", "explanation": "The calculation is correct."}
    with pytest.raises(SchemaRepairRejected):
        apply_schema_repairs(candidate, spec, SchemaFieldRepairs.model_validate(patch))
    assert candidate == before


@pytest.mark.asyncio
async def test_implementation_explanation_can_be_completed_without_changing_false_judgment():
    candidate = review_payload()
    candidate.update(implementation_appropriate=False)
    candidate.pop("implementation_reason")
    spec = repair_spec(candidate, MethodReview)
    assert spec.public_fields()[0]["location"] == ["implementation_reason"]
    gateway, calls, _ = make_gateway([candidate, repairs(spec)])
    result = await gateway.generate(MethodReview, "method_review", "Review independently.", {})
    assert result.implementation_appropriate is False and len(calls) == 2


@pytest.mark.asyncio
async def test_explanation_completion_cannot_erase_another_existing_adverse_judgment():
    candidate, schema, stage, check, _ = review_case("oracle", "rejected")
    candidate["scripts"][0]["support"]["status"] = "uncertain"
    gateway, calls, _ = make_gateway([candidate, repairs(repair_spec(candidate, schema))])
    result = await gateway.generate(schema, stage, "Review independently.", {}, check)
    assert result.verdict() == "uncertain"
    assert result.scripts[0].cases[0].assertions[0].correctness.status == "rejected"
    assert len(calls) == 2
