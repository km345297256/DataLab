"""Unfrozen reference failures can be rebuilt without discarding prior work."""
from unittest.mock import AsyncMock

import pytest

from skill_review_fixtures import review_for_payload

from app.domain.services.skills.method_contract import MethodReview
from app.domain.services.skills.skill_creation_error import SkillCreationError
from app.domain.services.skills.skill_script_verification import verify_skill_script_cases
from app.infrastructure.external.sandbox import skill_validation_worker as worker
from test_skill_creation_checkpoints import Artifact, MemoryCache, builds, generated
from test_skill_reference_verification import generation, host_receipt, method_review, output_files
from test_skill_script_verification import recipe, revalidate
from test_skill_validation_plan import method_contract, sources
from test_skill_validation_review import accepted_fixture_review, typed


def cached_generation(build, calls):
    reference_generate, _ = generation()

    async def generate(schema, stage, instruction, payload, check):
        async def produce():
            calls.append(stage)
            if stage == "synthetic_validation_plan":
                value = typed()
            elif stage == "synthetic_plan_review":
                value = schema.model_validate(review_for_payload(accepted_fixture_review(payload["validation_plan"]), payload))
            else:
                return await reference_generate(schema, stage, instruction, payload, check)
            check(value)
            return value
        return await build.run(schema, stage, instruction, payload, check, produce)
    return generate


async def verify(build, calls):
    return await verify_skill_script_cases(recipe(), method_contract(), sources(),
        generate=cached_generation(build, calls), revalidate=revalidate,
        review=AsyncMock(), checkpoints=build)


@pytest.mark.asyncio
async def test_unconfirmed_reference_evicts_only_unfrozen_artifacts_and_explicit_retry_rebuilds(monkeypatch):
    cache, calls, runs = MemoryCache(), [], []
    build = builds(cache)
    preserved = AsyncMock(return_value=Artifact(content="Previously validated scientific method and implementation."))
    for stage in ("method_requirements", "workflow", "implementation"):
        await generated(build, preserved, stage=stage)

    # Seed a distinct scientific rejection. Eviction must never remove this
    # ledger, even though a positive reference-review artifact is discarded.
    adverse_payload = {"candidate": "a previously rejected independent reference"}
    adverse = MethodReview.model_validate(method_review({"sources": sources(),
        "method_contract": method_contract().model_dump()}, verdict="missing"))
    await build.run(MethodReview, "synthetic_reference_review", "Review the independent reference.",
        adverse_payload, None, AsyncMock(return_value=adverse))
    before_adverse = {key: value for key, value in cache.entries.items() if ":adverse:" in key}
    assert len(before_adverse) == 1

    async def execute(programs, plan):
        runs.append(dict(programs))
        def files(case):
            values = output_files(case)
            if len(runs) == 1 and case.kind == "normal":
                # A real parser observation: the target row does not exist.
                # This is not a scalar correction and must stop the attempt.
                values["summary.csv"] = b"group,mean,valid_count,missing_count,missing_rate\n"
            return values
        return host_receipt(programs, plan, files=files)
    monkeypatch.setattr(worker, "validate_skill_scripts", execute)

    with pytest.raises(SkillCreationError) as failure:
        await verify(build, calls)
    assert (failure.value.stage, failure.value.code) == (
        "synthetic_reference_execution", "reference_calculation_unconfirmed")
    assert len(runs) == 1 and "synthetic_script_repair" not in calls
    assert {key: value for key, value in cache.entries.items() if ":adverse:" in key} == before_adverse

    retried = builds(cache)
    for stage in ("method_requirements", "workflow", "implementation"):
        await generated(retried, preserved, stage=stage)
    assert preserved.await_count == 3
    _, _, receipt = await verify(retried, calls)
    assert receipt["status"] == "passed"
    assert len(runs) == 3  # failed reference, new reference, unchanged tested program
    assert calls == ["synthetic_validation_plan", "synthetic_reference_implementation", "synthetic_reference_review",
                     "synthetic_validation_plan", "synthetic_reference_implementation", "synthetic_reference_review",
                     "synthetic_plan_review"]
    assert runs[-1] == {script.path: script.content for script in recipe().scripts}
    never_produce = AsyncMock()
    assert await retried.run(MethodReview, "synthetic_reference_review", "Review the independent reference.",
        adverse_payload, None, never_produce) == adverse
    never_produce.assert_not_awaited()


@pytest.mark.asyncio
async def test_reference_environment_outage_preserves_successful_authoring_for_retry(monkeypatch):
    cache, calls, runs = MemoryCache(), [], []
    async def execute(programs, plan):
        runs.append(dict(programs))
        if len(runs) == 1:
            return {"status": "unavailable", "reason": "sandbox_image_not_configured"}
        return host_receipt(programs, plan)
    monkeypatch.setattr(worker, "validate_skill_scripts", execute)
    with pytest.raises(SkillCreationError) as failure:
        await verify(builds(cache), calls)
    assert failure.value.code == "reference_execution_unavailable"
    _, _, receipt = await verify(builds(cache), calls)
    assert receipt["status"] == "passed" and len(runs) == 3
    assert calls == ["synthetic_validation_plan", "synthetic_reference_implementation", "synthetic_reference_review",
                     "synthetic_plan_review"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stage,code", [
    ("synthetic_reference_implementation", "model_unavailable"),
    ("synthetic_reference_implementation", "reference_calculation_unconfirmed"),
    ("synthetic_reference_review", "reference_calculation_unconfirmed"),
])
async def test_generation_or_scientific_review_failure_does_not_discard_unrelated_plan(monkeypatch, stage, code):
    from app.domain.services.skills import skill_script_verification as verifier
    cache, calls = MemoryCache(), []
    failure = AsyncMock(side_effect=SkillCreationError(stage, code))
    monkeypatch.setattr(verifier, "verify_reference_plan", failure)
    for _ in range(2):
        with pytest.raises(SkillCreationError) as error:
            await verify(builds(cache), calls)
        assert (error.value.stage, error.value.code) == (stage, code)
    assert calls == ["synthetic_validation_plan"]
