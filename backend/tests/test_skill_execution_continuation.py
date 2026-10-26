"""Explicit retries continue the latest failed code against the same oracle."""
import copy
from unittest.mock import AsyncMock

import pytest

from app.domain.services.skills.skill_creation_error import SkillCreationError, SkillPackageValidationError
from app.domain.services.skills.method_contract import CoverageError
from app.domain.services.skills.skill_script_verification import verify_skill_script_cases
from app.infrastructure.external.sandbox import skill_validation_worker as worker
from test_skill_creation_checkpoints import MemoryCache, builds
from test_skill_script_verification import generation, recipe, receipt, revalidate, isolated_reference_boundary
from test_skill_validation_plan import method_contract, sources
from test_skill_validation_plan import plan_payload


def cached_generator(build, base):
    async def generate(schema, stage, instruction, payload, check=None):
        async def produce():
            return await base(schema, stage, instruction, payload, check)
        return await build.run(schema, stage, instruction, payload, check, produce)
    return generate


async def verify(build, base, *, review=None):
    return await verify_skill_script_cases(recipe(), method_contract(), sources(),
        generate=cached_generator(build, base), revalidate=revalidate,
        review=review or AsyncMock(), checkpoints=build)


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_succeeds", [True, False])
async def test_explicit_retry_repairs_latest_failed_candidate_once_without_new_oracle(monkeypatch, retry_succeeds):
    cache, runs = MemoryCache(), []
    base, calls = generation()
    async def execute(scripts, plan):
        runs.append((copy.deepcopy(scripts), plan.model_dump(mode="json")))
        value = receipt("passed" if retry_succeeds and len(runs) == 4 else "failed")
        value["cases"][0]["output_sha256"] = {"summary.csv": "c" * 64}
        return value
    monkeypatch.setattr(worker, "validate_skill_scripts", execute)
    with pytest.raises(SkillCreationError) as error:
        await verify(builds(cache), base)
    assert error.value.code == "synthetic_execution_failed"
    review = AsyncMock()
    if retry_succeeds:
        result, frozen, evidence = await verify(builds(cache), base, review=review)
        assert evidence["status"] == "passed"
        review.assert_awaited_once_with(result)
    else:
        with pytest.raises(SkillCreationError) as error:
            await verify(builds(cache), base, review=review)
        assert error.value.code == "synthetic_execution_failed"
        frozen = runs[0][1]
        review.assert_awaited_once()
        assert {s.path: s.content for s in review.await_args.args[0].scripts} == runs[-1][0]
    assert [item["stage"] for item in calls] == [
        "synthetic_validation_plan", "synthetic_plan_review", "synthetic_script_repair", "synthetic_script_repair"]
    repairs = [item["payload"] for item in calls if item["stage"] == "synthetic_script_repair"]
    assert len(repairs) == 2 and len(runs) == 4
    assert repairs[1]["script"]["content"] == runs[1][0]["scripts/group_summary.py"]
    assert runs[1][0] == runs[2][0] != runs[0][0]
    assert runs[3][0] != runs[2][0]
    assert all(plan == frozen for _, plan in runs)
    saved = [entry["value"] for key, entry in cache.entries.items()
             if ":revision:" in key and "execution" in entry.get("value", {})]
    assert len(saved) == 1
    assert saved[0]["repair_used"] is True
    assert "plan_sha256" not in saved[0]["execution"] and "source_sha256" not in saved[0]["execution"]
    assert all("output_sha256" not in case for case in saved[0]["execution"]["cases"])


@pytest.mark.asyncio
async def test_restored_candidate_that_now_passes_still_receives_independent_method_review(monkeypatch):
    cache = MemoryCache()
    base, calls = generation()
    monkeypatch.setattr(worker, "validate_skill_scripts", AsyncMock(side_effect=[receipt("failed"), receipt("failed"), receipt()]))
    with pytest.raises(SkillCreationError):
        await verify(builds(cache), base)
    review = AsyncMock()
    result, _, _ = await verify(builds(cache), base, review=review)
    assert "implementation_repaired = True" in result.scripts[0].content
    assert len([item for item in calls if item["stage"] == "synthetic_script_repair"]) == 1
    review.assert_awaited_once_with(result)


@pytest.mark.asyncio
async def test_unchanged_repair_is_evicted_without_erasing_frozen_plan_or_prior_review(monkeypatch):
    cache = MemoryCache()
    base, calls = generation(repair="\n# cosmetic repair only\n")
    monkeypatch.setattr(worker, "validate_skill_scripts", AsyncMock(return_value=receipt("failed")))
    for _ in range(2):
        with pytest.raises(SkillCreationError) as error:
            await verify(builds(cache), base)
        assert error.value.code == "synthetic_repair_unchanged"
    assert [item["stage"] for item in calls] == [
        "synthetic_validation_plan", "synthetic_plan_review", "synthetic_script_repair", "synthetic_script_repair"]


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["frozen-plan", "contract", "original", "interface", "metadata", "input-write"])
async def test_corrupt_continuation_cannot_override_oracle_interface_or_readonly_inputs(monkeypatch, damage):
    cache = MemoryCache()
    base, _ = generation()
    monkeypatch.setattr(worker, "validate_skill_scripts", AsyncMock(side_effect=[receipt("failed"), receipt("failed")]))
    with pytest.raises(SkillCreationError):
        await verify(builds(cache), base)
    entry = next(entry["value"] for key, entry in cache.entries.items()
                 if ":revision:" in key and "execution" in entry.get("value", {}))
    if damage == "frozen-plan":
        entry["plan"]["scripts"][0]["cases"][0]["assertions"][0]["fields"]["mean"]["value"] = 10000
    elif damage in {"contract", "original", "interface"}:
        field = {"contract": "contract_digest", "original": "original_recipe_digest", "interface": "interfaces_digest"}[damage]
        entry[field] = "d" * 64
    elif damage == "metadata":
        entry["recipe"]["scripts"][0]["invocation"] += " --foreign FLAG"
    else:
        entry["recipe"]["scripts"][0]["content"] = entry["recipe"]["scripts"][0]["content"].replace("Path(a.output)", "Path(a.input)")
    run = AsyncMock(return_value=receipt())
    monkeypatch.setattr(worker, "validate_skill_scripts", run)
    before = copy.deepcopy(cache.entries)
    with pytest.raises(SkillCreationError) as error:
        await verify(builds(cache), base)
    assert (error.value.stage, error.value.code) == ("synthetic_execution", "synthetic_execution_failed")
    run.assert_not_awaited()
    # A corrupt persisted failure is retained as evidence, never silently
    # discarded to make the original candidate look like a fresh attempt.
    assert cache.entries == before


@pytest.mark.asyncio
async def test_repair_cannot_change_cli_while_frozen_cases_happen_to_pass(monkeypatch):
    base, _ = generation(repair="\nparser = None\n")
    async def change_cli(schema, stage, instruction, payload, check=None):
        if stage == "synthetic_script_repair":
            value = schema.model_validate({**payload["script"],
                "content": payload["script"]["content"].replace("'--input'", "'--changed-input'")})
            if check:
                check(value)
            return value
        return await base(schema, stage, instruction, payload, check)
    run = AsyncMock(return_value=receipt("failed"))
    monkeypatch.setattr(worker, "validate_skill_scripts", run)
    with pytest.raises((SkillPackageValidationError, ValueError)):
        await verify(builds(MemoryCache()), change_cli)
    assert run.await_count == 1


@pytest.mark.asyncio
async def test_second_script_checks_accepted_first_repair_and_can_continue_after_aggregate_rejection(monkeypatch):
    original = recipe()
    original.scripts[0].content += "\naggregate_method_anchor = True\n"
    original.scripts.append(original.scripts[0].model_copy(update={"path": "scripts/second.py",
        "invocation": "python scripts/second.py --input INPUT --output OUTPUT"}))
    original = type(original).model_validate(original.model_dump(mode="json"))
    plan = plan_payload()
    plan["scripts"].append({**copy.deepcopy(plan["scripts"][0]), "script_path": "scripts/second.py"})
    base, _ = generation(plan)
    cache, repair_calls, second_attempts = MemoryCache(), [], 0

    def check_package(candidate):
        revalidate(candidate)
        if not any("aggregate_method_anchor = True" in script.content for script in candidate.scripts):
            raise CoverageError("method_identifiers_lost")

    async def generate(schema, stage, instruction, payload, check=None):
        nonlocal second_attempts
        if stage != "synthetic_script_repair":
            return await base(schema, stage, instruction, payload, check)
        repair_calls.append(copy.deepcopy(payload))
        content = payload["script"]["content"].replace("aggregate_method_anchor = True", "repair_applied = True")
        if payload["script"]["path"] == "scripts/second.py":
            second_attempts += 1
            if second_attempts > 1:
                content = payload["script"]["content"] + "\nsecond_repair_applied = True\n"
        value = schema.model_validate({**payload["script"], "content": content})
        if check:
            check(value)
        return value

    failures = receipt("failed")
    failures["cases"] += receipt("failed", script_path="scripts/second.py")["cases"]
    run = AsyncMock(side_effect=[failures, failures, receipt()])
    monkeypatch.setattr(worker, "validate_skill_scripts", run)

    async def attempt():
        build = builds(cache)
        return await verify_skill_script_cases(original, method_contract(), sources(),
            generate=cached_generator(build, generate), revalidate=check_package,
            review=AsyncMock(), checkpoints=build)

    with pytest.raises(CoverageError) as error:
        await attempt()
    assert error.value.code == "method_identifiers_lost"
    assert run.await_count == 1  # the unsafe aggregate never executes
    result, _, execution = await attempt()
    assert execution["status"] == "passed" and run.await_count == 3
    assert [call["script"]["path"] for call in repair_calls] == [
        "scripts/group_summary.py", "scripts/second.py", "scripts/second.py"]
    assert all(call["accepted_script_repairs"][0]["path"] == "scripts/group_summary.py"
               for call in repair_calls[1:])
    assert "aggregate_method_anchor = True" not in result.scripts[0].content
    assert "aggregate_method_anchor = True" in result.scripts[1].content


@pytest.mark.asyncio
async def test_final_aggregate_rejection_discards_only_repair_artifacts(monkeypatch):
    cache = MemoryCache()
    build = builds(cache)
    base, _ = generation()
    captured = []
    discard = build.discard_since
    async def track_discard(mark, *, stages):
        captured.append(set(stages))
        await discard(mark, stages=stages)
    build.discard_since = track_discard
    # Simulate an injected generator that did not call its checker. The final
    # boundary must still reject and evict its unsafe aggregate result.
    async def unchecked(schema, stage, instruction, payload, check=None):
        value = await base(schema, stage, instruction, payload, None)
        if stage == "synthetic_script_repair":
            value.invocation += " --foreign FLAG"
        return value
    monkeypatch.setattr(worker, "validate_skill_scripts", AsyncMock(return_value=receipt("failed")))
    with pytest.raises(SkillPackageValidationError) as error:
        await verify_skill_script_cases(recipe(), method_contract(), sources(),
            generate=unchecked, revalidate=revalidate, review=AsyncMock(), checkpoints=build)
    assert error.value.code == "repaired_script_interface_changed"
    assert captured == [{"synthetic_script_repair"}]
