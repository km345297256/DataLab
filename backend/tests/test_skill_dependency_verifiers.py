"""Runtime dependency gaps stop validation without rewriting the algorithm."""
import hashlib
from unittest.mock import AsyncMock

import pytest

from app.domain.services.skills import skill_script_verification as candidate_verifier
from app.domain.services.skills.skill_creation_error import SkillCreationError
from app.domain.services.skills.skill_reference_verification import verify_reference_plan
from app.infrastructure.external.sandbox import skill_validation_worker as worker
from test_skill_script_verification import generation, recipe, revalidate
from test_skill_reference_verification import generation as reference_generation
from test_skill_validation_plan import interfaces, method_contract, sources
from test_skill_validation_review import typed


def dependency_receipt(programs, plan, **kwargs):
    return {
        "scope": "synthetic", "status": "failed",
        "source_sha256": {path: hashlib.sha256(source.encode()).hexdigest()
                          for path, source in programs.items()},
        "plan_sha256": hashlib.sha256(worker._json(plan.model_dump(mode="json"))).hexdigest(),
        "cases": [{
            "script_path": script.script_path, "id": case.id, "kind": case.kind,
            "expected_exit_code": case.expected_exit_code, "actual_exit_code": 1,
            "status": "failed", "reason": "dependency_unavailable", "input_unchanged": True,
            "runtime_dependency_evidence": {
                "version": 1, "origin": "host_import_exception", "exception_type": "ModuleNotFoundError"},
        } for script in plan.scripts for case in script.cases],
    }


@pytest.mark.asyncio
async def test_candidate_missing_dependency_does_not_request_a_replacement_implementation(monkeypatch):
    async def reference(plan, *args, **kwargs):
        return plan.model_copy(deep=True), {
            "corrections": [], "execution": {"cases": []},
            "corrected_plan_sha256": hashlib.sha256(worker._json(plan.model_dump(mode="json"))).hexdigest(),
        }

    monkeypatch.setattr(candidate_verifier, "verify_reference_plan", reference)
    execute = AsyncMock(side_effect=dependency_receipt)
    monkeypatch.setattr(worker, "validate_skill_scripts", execute)
    generate, calls = generation()
    review = AsyncMock()
    with pytest.raises(SkillCreationError) as failure:
        await candidate_verifier.verify_skill_script_cases(
            recipe(), method_contract(), sources(), generate=generate, revalidate=revalidate, review=review)
    assert (failure.value.stage, failure.value.code) == ("synthetic_execution", "dependency_unavailable")
    assert [item["stage"] for item in calls] == ["synthetic_validation_plan", "synthetic_plan_review"]
    execute.assert_awaited_once()
    review.assert_not_awaited()


@pytest.mark.asyncio
async def test_reference_missing_dependency_does_not_rewrite_reference_or_test_expectations():
    generate, calls = reference_generation()
    execute = AsyncMock(side_effect=dependency_receipt)
    plan = typed()
    before = plan.model_dump(mode="json")
    with pytest.raises(SkillCreationError) as failure:
        await verify_reference_plan(plan, method_contract(), sources(), interfaces(),
                                    generate=generate, execute=execute)
    assert (failure.value.stage, failure.value.code) == ("synthetic_reference_execution", "dependency_unavailable")
    assert [stage for stage, _ in calls] == ["synthetic_reference_implementation", "synthetic_reference_review"]
    assert plan.model_dump(mode="json") == before
    execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_unbound_dependency_claim_cannot_gain_the_environment_diagnosis():
    def broken_receipt(programs, plan, **kwargs):
        value = dependency_receipt(programs, plan)
        value["source_sha256"] = {}
        return value

    generate, calls = reference_generation()
    with pytest.raises(SkillCreationError) as failure:
        await verify_reference_plan(typed(), method_contract(), sources(), interfaces(),
                                    generate=generate, execute=AsyncMock(side_effect=broken_receipt))
    assert (failure.value.stage, failure.value.code) == (
        "synthetic_reference_execution", "reference_calculation_unconfirmed")
    assert len(calls) == 2
