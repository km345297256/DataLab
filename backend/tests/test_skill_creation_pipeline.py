"""All generated documents share the privacy boundary before later stages."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.domain.services.skills.method_contract import CoverageError, MethodReview, validate_review
from app.domain.services.skills.skill_creation.evidence import AnalysisContext
from app.domain.services.skills.skill_creation.pipeline import SkillCreationPipeline
from app.domain.services.skills.skill_creation.privacy import PrivacyContext, PrivateValue
from app.domain.services.skills.skill_creation_error import SkillCreationError, SkillPackageValidationError
from app.domain.services.skills.skill_validation import SyntheticValidationPlan, validate_validation_plan
from test_skill_validation_plan import interfaces, method_contract, plan_payload, sources


PRIVATE = "synthetic-private-credential-for-boundary"


def pipeline_for(document):
    async def generate(schema, stage, instruction, payload, check, *, contract):
        value = schema.model_validate(document)
        check(value)
        return value

    context = AnalysisContext(
        privacy=PrivacyContext((PrivateValue(PRIVATE, "credential"),)),
        evidence={}, sources=sources(), programs=[],
    )
    gateway = SimpleNamespace(generate=AsyncMock(side_effect=generate))
    pipeline = SkillCreationPipeline(context, gateway, script_verifier=AsyncMock())
    pipeline.contract = method_contract()
    return pipeline


def check_plan(value):
    validate_validation_plan(value, method_contract(), sources(), interfaces())


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["reason", "fixture", "oracle"])
async def test_synthetic_plan_cannot_publish_private_reason_or_fixture(location):
    payload = plan_payload()
    if location == "reason":
        payload["scripts"][0]["reason"] += " Source credential " + PRIVATE
    elif location == "fixture":
        payload["scripts"][0]["cases"][0]["input_files"][0]["rows"][0]["group"] = PRIVATE
    else:
        payload["scripts"][0]["cases"][0]["oracle_explanation"] += " Source credential " + PRIVATE
    # The declarative schema and ordinary plan validation accept these strings;
    # the creation boundary must independently reject the known private value.
    check_plan(SyntheticValidationPlan.model_validate(payload))
    pipeline = pipeline_for(payload)
    later_check = Mock(side_effect=check_plan)
    with pytest.raises(SkillPackageValidationError) as failure:
        await pipeline.generate(SyntheticValidationPlan, "synthetic_validation_plan", "Create synthetic cases.", {}, later_check)
    assert failure.value.code == "private_package_content"
    assert all(issue["category"] == "credential" for issue in failure.value.details["privacy_issues"])
    assert PRIVATE not in json.dumps(failure.value.details)
    later_check.assert_not_called()
    pipeline.script_verifier.assert_not_awaited()


@pytest.mark.asyncio
async def test_legal_oracle_is_preserved_without_rewriting_expected_values_or_fixtures():
    payload = plan_payload()
    # An output key with nested JSON-pointer syntax must not resemble a host
    # path merely because it has multiple slash-separated components.
    payload["scripts"][0]["cases"][0]["assertions"][1]["pointer"] = "/summary/status"
    pipeline = pipeline_for(payload)
    later_check = Mock(side_effect=check_plan)
    actual = await pipeline.generate(SyntheticValidationPlan, "synthetic_validation_plan", "Create synthetic cases.", {}, later_check)
    assert actual.model_dump(mode="json") == SyntheticValidationPlan.model_validate(payload).model_dump(mode="json")
    later_check.assert_called_once_with(actual)


def review_payload():
    return {
        "sources": [{"source_id": "S001", "status": "preserved", "explanation": "All source method details are preserved."}],
        "requirements": [{"requirement_id": "R001", "status": "preserved", "explanation": "All requirement computations are preserved."}],
        "implementation_appropriate": True,
        "implementation_reason": "The implementation preserves the required computations and input checks.",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["source_explanation", "implementation_reason"])
async def test_private_review_cannot_become_a_design_revision_prompt(location):
    payload = review_payload()
    if location == "source_explanation":
        payload["sources"][0]["explanation"] += " Credential " + PRIVATE
    else:
        payload["implementation_reason"] += " Credential " + PRIVATE
    validate_review(MethodReview.model_validate(payload), method_contract(), sources())
    pipeline = pipeline_for(payload)
    with pytest.raises(SkillPackageValidationError) as failure:
        await pipeline.generate(MethodReview, "method_review", "Review method fidelity.", {}, pipeline.check_review_shape)
    assert failure.value.code == "private_package_content"
    assert PRIVATE not in json.dumps(failure.value.details)


@pytest.mark.asyncio
async def test_private_generated_document_is_rejected_even_without_a_stage_specific_check():
    payload = review_payload()
    payload["implementation_reason"] += " Credential " + PRIVATE
    pipeline = pipeline_for(payload)
    with pytest.raises(SkillPackageValidationError, match="来源名称或疑似敏感信息"):
        await pipeline.generate(MethodReview, "method_review", "Review method fidelity.", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("problem", ["private", "incomplete", "duplicate", "host_check_failure"])
@pytest.mark.parametrize("negative_field", ["source", "requirement", "implementation"])
async def test_typed_negative_review_with_invalid_explanation_or_coverage_cannot_retry_into_positive(problem, negative_field):
    from test_skill_creation_gateway import make_gateway

    negative = review_payload()
    if negative_field == "source":
        negative["sources"][0]["status"] = "missing"
    elif negative_field == "requirement":
        negative["requirements"][0]["status"] = "changed"
    else:
        negative["implementation_appropriate"] = False
    if problem == "private":
        negative["implementation_reason"] += " Credential " + PRIVATE
    elif problem == "incomplete":
        negative["sources"].append({"source_id": "S999", "status": "preserved", "explanation": "This is an unknown source outside the current contract."})
    elif problem == "duplicate":
        negative["requirements"].append(dict(negative["requirements"][0]))
    gateway, calls, _ = make_gateway([negative, review_payload()],
        privacy=PrivacyContext((PrivateValue(PRIVATE, "credential"),)))
    context = AnalysisContext(privacy=gateway.privacy, evidence={}, sources=sources(), programs=[])
    pipeline = SkillCreationPipeline(context, gateway, script_verifier=AsyncMock())
    pipeline.contract = method_contract()
    check = pipeline.check_review_shape
    if problem == "host_check_failure":
        def check(value):
            raise RuntimeError("Invalid review check with private detail " + PRIVATE)
    with pytest.raises(SkillCreationError) as failure:
        await pipeline.generate(MethodReview, "method_review", "Review unchanged method.", {"same_package": True}, check)
    assert (failure.value.stage, failure.value.code) == ("method_review", "method_review_rejected")
    assert PRIVATE not in str(failure.value)
    assert len(calls) == 1
    assert not any(event.status == "repairing" for event in gateway.attempts)
    pipeline.script_verifier.assert_not_awaited()


@pytest.mark.asyncio
async def test_valid_negative_review_reaches_design_revision_without_reviewer_retry():
    from test_skill_creation_gateway import make_gateway

    negative = review_payload()
    negative["requirements"][0]["status"] = "missing"
    gateway, calls, _ = make_gateway([negative, review_payload()])
    context = AnalysisContext(privacy=gateway.privacy, evidence={}, sources=sources(), programs=[])
    pipeline = SkillCreationPipeline(context, gateway, script_verifier=AsyncMock())
    pipeline.contract = method_contract()
    value = await pipeline.generate(MethodReview, "method_review", "Review unchanged method.", {}, pipeline.check_review_shape)
    assert value.model_dump() == negative
    with pytest.raises(CoverageError) as failure:
        validate_review(value, pipeline.contract, context.sources)
    assert failure.value.code == "method_review_rejected"
    assert len(calls) == 1


def oracle_review_payload(plan):
    from app.domain.services.skills.skill_validation_review import review_targets

    def accepted():
        return {"status": "accepted", "explanation": "The independent method calculation supports this exact assertion."}
    return {"scripts": [{"script_path": script["script_path"], "support": accepted(), "cases": [
        {"case_id": case["case_id"], "input_validity": accepted(), "exit_behavior": accepted(),
         "method_coverage": accepted(), "assertions": [
             {"target_id": item["target_id"], "correctness": accepted(), "tolerance": accepted()}
             for item in case["targets"]]}
        for case in script["cases"]]} for script in review_targets(plan)]}


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["rejected", "uncertain"])
@pytest.mark.parametrize("problem", ["private", "incomplete"])
async def test_adverse_oracle_review_cannot_retry_into_acceptance_after_privacy_or_coverage_failure(verdict, problem):
    from app.domain.services.skills.skill_validation_review import ValidationPlanReview, validate_oracle_review
    from test_skill_creation_gateway import make_gateway

    plan = SyntheticValidationPlan.model_validate(plan_payload())
    negative = oracle_review_payload(plan)
    negative["scripts"][0]["support"]["status"] = verdict
    if problem == "private":
        negative["scripts"][0]["support"]["explanation"] += " Credential " + PRIVATE
    else:
        negative["scripts"][0]["cases"][0]["assertions"].pop()
    gateway, calls, _ = make_gateway([negative, oracle_review_payload(plan)],
        privacy=PrivacyContext((PrivateValue(PRIVATE, "credential"),)))
    context = AnalysisContext(privacy=gateway.privacy, evidence={}, sources=sources(), programs=[])
    pipeline = SkillCreationPipeline(context, gateway, script_verifier=AsyncMock())
    pipeline.contract = method_contract()
    with pytest.raises(SkillCreationError) as failure:
        await pipeline.generate(ValidationPlanReview, "synthetic_plan_review", "Independently judge the unchanged oracle.",
                                {"same_validation_plan": True}, lambda value: validate_oracle_review(value, plan))
    assert (failure.value.stage, failure.value.code) == ("synthetic_plan_review", "validation_oracle_" + verdict)
    assert PRIVATE not in str(failure.value)
    assert len(calls) == 1
    assert not any(event.status == "repairing" for event in gateway.attempts)
    pipeline.script_verifier.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["rejected", "uncertain"])
async def test_complete_adverse_oracle_review_keeps_verdict_for_external_decision(verdict):
    from app.domain.services.skills.skill_validation_review import ValidationPlanReview, validate_oracle_review
    from test_skill_creation_gateway import make_gateway

    plan = SyntheticValidationPlan.model_validate(plan_payload())
    negative = oracle_review_payload(plan)
    negative["scripts"][0]["cases"][0]["assertions"][0]["correctness"]["status"] = verdict
    gateway, calls, _ = make_gateway([negative, oracle_review_payload(plan)])
    pipeline = SkillCreationPipeline(
        AnalysisContext(privacy=gateway.privacy, evidence={}, sources=sources(), programs=[]), gateway, script_verifier=AsyncMock())
    pipeline.contract = method_contract()
    value = await pipeline.generate(ValidationPlanReview, "synthetic_plan_review", "Independently judge the unchanged oracle.",
                                    {}, lambda value: validate_oracle_review(value, plan))
    assert value.model_dump() == negative and value.verdict() == verdict
    assert len(calls) == 1
