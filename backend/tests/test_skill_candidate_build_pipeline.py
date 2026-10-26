"""Blind build recovery cannot substitute for frozen scientific validation."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from skill_review_fixtures import review_for_payload
from app.domain.services.skills.method_contract import MethodReview
from app.domain.services.skills.skill_creation.models import HelperScript
from app.domain.services.skills.skill_creation_error import SkillCreationError, SkillPackageValidationError
from app.domain.services.skills.skill_script_verification import verify_skill_script_cases
from app.infrastructure.external.sandbox import skill_validation_worker as worker
from test_session_skill_creator import helper_script
from test_skill_creation_checkpoints import MemoryCache, builds
from test_skill_reference_verification import host_receipt, method_review, output_files
from test_skill_script_verification import recipe, revalidate
from test_skill_validation_plan import method_contract, sources
from test_skill_validation_review import accepted_fixture_review, typed

REFERENCE_CANARY = "INDEPENDENT_REFERENCE_CODE_NOT_A_REPAIR_ANSWER"
DIAGNOSTIC_CANARY = "PRIVATE_STDERR_WITH_SCIENTIFIC_ANSWER_739281"
REPAIR = "\nbuild_field_projection_repaired = True\n"


def scenario(monkeypatch, *, damage=None, second="passed", repair="substantive", corrections=True):
    """Real reference/oracle flow; only isolated execution is mocked.

    Observations use the production assertion parser, with full source/plan
    hashes. A crash contributes no checks or output observations.
    """
    plan = typed()
    normal = plan.scripts[0].cases[0]
    for row in normal.input_files[0].rows:
        row["value"] = .25
    normal.assertions[0].fields["mean"].value = .2500000002 if corrections else .25
    normal.oracle_explanation = "The arithmetic mean of two quarter-valued measurements is one quarter."
    original, calls, runs, events = recipe(), [], [], []
    before = original.model_dump(mode="json")

    async def generate(schema, stage, instruction, payload, check):
        calls.append({"stage": stage, "instruction": instruction, "payload": copy.deepcopy(payload)})
        events.append(stage)
        if stage == "synthetic_validation_plan":
            document = plan.model_dump()
        elif stage == "synthetic_reference_implementation":
            document = {**payload["script_request"], "content": helper_script()["content"] + f"\n# {REFERENCE_CANARY}\n"}
        elif stage == "synthetic_reference_review":
            document = method_review(payload)
        elif stage == "synthetic_plan_review":
            document = review_for_payload(accepted_fixture_review(payload["validation_plan"]), payload)
        else:
            assert stage == "synthetic_build_repair"
            document = copy.deepcopy(payload["script"])
            if repair == "cosmetic":
                document["content"] += "\n# only commentary, no source change\n"
            elif repair == "cli":
                document["content"] += "\np.add_argument('--different-input')\n"
            elif repair == "output":
                document["content"] = document["content"].replace("'summary.csv'", "'renamed.csv'")
            elif repair == "metadata":
                document["invocation"] += " --foreign PARAMETER"
            else:
                document["content"] += REPAIR
        value = schema.model_validate(document)
        if check:
            check(value)
        return value

    async def execute(programs, frozen, **kwargs):
        candidate = any("IMPLEMENTATION_MARKER_NOT_FOR_ORACLE" in code for code in programs.values())
        number = sum(run["candidate"] for run in runs) + 1
        events.append(("candidate_execution" if candidate else "reference_execution", number))

        def files(case):
            result = output_files(case)
            if case.kind == "normal":
                mean = .5 if candidate and number > 1 and second == "disagrees" else .25
                result["summary.csv"] = ("group,mean,valid_count,missing_count,missing_rate\n"
                    f"synthetic-a,{mean},2,0,0\n").encode()
            return result

        observed = host_receipt(programs, frozen, files=files)
        if candidate and (number == 1 or second == "crash" or (second == "crash_once" and number == 2)):
            failed = observed["cases"][0]
            failed.update(status="failed", reason="program_runtime_failure", actual_exit_code=1,
                diagnostic=f"KeyError: {DIAGNOSTIC_CANARY}", runtime_error_type="KeyError",
                runtime_build_evidence={"version": 1, "origin": "host_uncaught_exception", "exception_type": "KeyError"})
            failed.pop("checks", None)
            failed.pop("output_sha256", None)
            observed["status"] = "failed"
            if number == 1:
                if damage == "scientific":
                    neighbour = observed["cases"][1]
                    neighbour.update(status="failed", reason="assertion_mismatch")
                    neighbour["checks"][-1].update(passed=False, actual="wrong-status")
                elif damage == "invalid":
                    observed["cases"][-1].update(status="failed", actual_exit_code=0, reason="exit_code_mismatch")
                elif damage == "observed_value":
                    failed["checks"] = [{"id": "assertion_1", "passed": False, "actual": .5}]
                elif damage == "forged_proof":
                    failed["runtime_build_evidence"]["origin"] = "stderr"
                elif damage == "wrong_source_hash":
                    observed["source_sha256"] = {path: "0" * 64 for path in programs}
                elif damage == "wrong_plan_hash":
                    observed["plan_sha256"] = "0" * 64
        runs.append({"candidate": candidate, "programs": dict(programs),
            "plan": frozen.model_dump(mode="json"), "receipt": copy.deepcopy(observed), "kwargs": kwargs})
        return observed

    monkeypatch.setattr(worker, "validate_skill_scripts", execute)

    async def review(candidate):
        events.append("candidate_method_review")
        assert REPAIR.strip() in candidate.scripts[0].content

    return SimpleNamespace(original=original, before=before, plan=plan, calls=calls, runs=runs,
        events=events, generate=generate, review=AsyncMock(side_effect=review))


async def verify(test, *, checkpoints=None, generate=None, review=None):
    return await verify_skill_script_cases(test.original, method_contract(), sources(),
        generate=generate or test.generate, revalidate=revalidate,
        review=review or test.review, checkpoints=checkpoints)


def candidate_runs(test):
    return [run for run in test.runs if run["candidate"]]


def build_calls(test):
    return [call for call in test.calls if call["stage"] == "synthetic_build_repair"]


def cached_generator(checkpoint, base):
    async def generate(schema, stage, instruction, payload, check):
        return await checkpoint.run(schema, stage, instruction, payload, check,
            lambda: base(schema, stage, instruction, payload, check))
    return generate


@pytest.mark.asyncio
async def test_pre_observation_crash_repairs_blindly_before_corrected_oracle_comparison(monkeypatch):
    test = scenario(monkeypatch)
    result, frozen, receipt = await verify(test)
    assert receipt["status"] == "passed" and len(receipt["reference_verification"]["corrections"]) == 1
    assert len(build_calls(test)) == 1
    assert not any(call["stage"] == "synthetic_script_repair" for call in test.calls)
    request = build_calls(test)[0]["payload"]
    assert set(request) == {"method_contract", "sources", "script", "build_failures", "accepted_script_repairs"}
    assert request["script"] == test.original.scripts[0].model_dump()
    assert request["build_failures"] == [{"script_path": "scripts/group_summary.py",
        "case_id": "group-normal", "exception_type": "KeyError"}]
    assert request["accepted_script_repairs"] == []
    wire = json.dumps(request, ensure_ascii=False)
    for forbidden in (REFERENCE_CANARY, DIAGNOSTIC_CANARY, "0.2500000002", "frozen_validation_plan",
            "observed_validation", "reference_verification", "reference_scripts", "input_files",
            "atol", "rtol", "output_sha256", "runtime_build_evidence"):
        assert forbidden not in wire
    assert not {"expected", "actual", "checks", "execution"} & set(request)
    first, final = candidate_runs(test)
    assert first["plan"] == final["plan"] == frozen
    assert first["receipt"]["plan_sha256"] == final["receipt"]["plan_sha256"]
    assert first["programs"] != final["programs"]
    assert test.original.model_dump(mode="json") == test.before
    assert test.events.index("synthetic_build_repair") < test.events.index("candidate_method_review")
    assert test.events.index("candidate_method_review") < test.events.index(("candidate_execution", 2))
    test.review.assert_awaited_once_with(result)
    build = receipt["runtime_build_recovery"]
    assert build["initial_execution"] == first["receipt"]
    assert build["repaired_source_sha256"] == final["receipt"]["source_sha256"]
    assert DIAGNOSTIC_CANARY in json.dumps(build["initial_execution"])


@pytest.mark.asyncio
@pytest.mark.parametrize("corrections", [True, False])
async def test_second_build_crash_saves_latest_source_without_new_repair_or_reference_eviction(monkeypatch, corrections):
    test, cache = scenario(monkeypatch, second="crash", corrections=corrections), MemoryCache()
    checkpoint, evictions = builds(cache), []
    discard = checkpoint.discard_since

    async def record_discard(mark, *, stages):
        evictions.append(set(stages))
        await discard(mark, stages=stages)
    checkpoint.discard_since = record_discard
    with pytest.raises(SkillCreationError) as error:
        await verify(test, checkpoints=checkpoint, generate=cached_generator(checkpoint, test.generate))
    assert (error.value.stage, error.value.code) == ("synthetic_execution", "synthetic_execution_failed")
    assert len(build_calls(test)) == 1 and len(candidate_runs(test)) == 2
    assert not any(call["stage"] == "synthetic_script_repair" for call in test.calls)
    test.review.assert_awaited_once()
    first, final = candidate_runs(test)
    assert first["plan"] == final["plan"] and not evictions
    saved = await builds(cache).get_execution_revision()
    assert saved is not None
    assert {s.path: s.content for s in saved.recipe.scripts} == final["programs"] != first["programs"]
    assert saved.plan.model_dump(mode="json") == final["plan"]
    assert saved.execution["cases"][0]["runtime_build_evidence"] == final["receipt"]["cases"][0]["runtime_build_evidence"]


@pytest.mark.asyncio
@pytest.mark.parametrize("damage,code", [
    ("scientific", "reference_calculation_unconfirmed"), ("invalid", "reference_calculation_unconfirmed"),
    ("observed_value", "reference_calculation_unconfirmed"), ("forged_proof", "synthetic_execution_failed"),
    ("wrong_source_hash", "synthetic_execution_failed"), ("wrong_plan_hash", "synthetic_execution_failed"),
])
async def test_mixed_failures_or_unbound_proof_never_authorize_blind_repair(monkeypatch, damage, code):
    test = scenario(monkeypatch, damage=damage)
    with pytest.raises(SkillCreationError) as error:
        await verify(test)
    assert error.value.code == code
    assert len(candidate_runs(test)) == 1 and not build_calls(test)
    test.review.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("repair", ["cli", "output", "metadata"])
async def test_blind_repair_cannot_change_public_interface(monkeypatch, repair):
    test = scenario(monkeypatch, repair=repair)
    with pytest.raises(SkillPackageValidationError) as error:
        await verify(test)
    assert error.value.code == "repaired_script_interface_changed"
    assert len(candidate_runs(test)) == 1 and len(build_calls(test)) == 1
    test.review.assert_not_awaited()


@pytest.mark.asyncio
async def test_cosmetic_build_change_is_not_a_substantive_repair(monkeypatch):
    test = scenario(monkeypatch, repair="cosmetic")
    with pytest.raises(SkillCreationError) as error:
        await verify(test)
    assert (error.value.stage, error.value.code) == ("synthetic_build_repair", "synthetic_repair_unchanged")
    assert len(candidate_runs(test)) == 1
    test.review.assert_not_awaited()


@pytest.mark.asyncio
async def test_negative_method_review_is_preserved_and_blocks_reexecution(monkeypatch):
    test, cache = scenario(monkeypatch), MemoryCache()
    checkpoint = builds(cache)
    negative = MethodReview.model_validate(method_review(
        {"sources": sources(), "method_contract": method_contract().model_dump()}, verdict="changed"))
    before, producer, requests = negative.model_dump(mode="json"), AsyncMock(return_value=negative), []

    async def review(candidate):
        requests.append({"method_contract": method_contract().model_dump(), "skill_package": candidate.model_dump()})
        observed = await checkpoint.run(MethodReview, "method_review", "Review the repaired program.",
            requests[-1], None, producer)
        assert observed.model_dump(mode="json") == before
        raise SkillCreationError("method_review", "method_review_rejected")

    with pytest.raises(SkillCreationError) as error:
        await verify(test, checkpoints=checkpoint, review=review)
    assert (error.value.stage, error.value.code) == ("method_review", "method_review_rejected")
    assert len(candidate_runs(test)) == 1 and len(build_calls(test)) == 1
    records = [entry for key, entry in cache.entries.items() if ":adverse:" in key]
    assert len(records) == 1 and records[0]["code"] == "method_review_rejected" and records[0]["value"] == before
    restored = await builds(cache).run(MethodReview, "method_review", "Review the repaired program.",
        requests[0], None, producer)
    assert restored.model_dump(mode="json") == before
    producer.assert_awaited_once()


@pytest.mark.asyncio
async def test_validated_blind_repair_cache_survives_later_reference_disagreement(monkeypatch):
    test, cache = scenario(monkeypatch, second="disagrees"), MemoryCache()
    checkpoint, evictions = builds(cache), []
    discard = checkpoint.discard_since

    async def record_discard(mark, *, stages):
        evictions.append(set(stages))
        await discard(mark, stages=stages)
    checkpoint.discard_since = record_discard
    with pytest.raises(SkillCreationError) as error:
        await verify(test, checkpoints=checkpoint, generate=cached_generator(checkpoint, test.generate))
    assert (error.value.stage, error.value.code) == ("synthetic_reference_execution", "reference_calculation_unconfirmed")
    assert len(candidate_runs(test)) == 2 and len(build_calls(test)) == 1
    assert not evictions  # the independently frozen plan is retained too
    keys = [key for stage, key in checkpoint._accessed_artifacts if stage == "synthetic_build_repair"]
    assert len(keys) == 1 and keys[0] in cache.entries
    assert REPAIR.strip() in cache.entries[keys[0]]["value"]["content"]
    call = build_calls(test)[0]
    producer = AsyncMock(side_effect=AssertionError("A validated blind build must not be regenerated"))
    checked = []
    restored = await builds(cache).run(HelperScript, call["stage"], call["instruction"], call["payload"],
        lambda value: checked.append(value.content), producer)
    assert REPAIR.strip() in restored.content and checked == [restored.content]
    producer.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_retry_executes_latest_build_revision_with_same_frozen_plan(monkeypatch):
    test, cache = scenario(monkeypatch, second="crash_once"), MemoryCache()
    first = builds(cache)
    with pytest.raises(SkillCreationError) as failure:
        await verify(test, checkpoints=first, generate=cached_generator(first, test.generate))
    assert failure.value.code == "synthetic_execution_failed"
    latest = candidate_runs(test)[-1]
    second = builds(cache)
    result, frozen, receipt = await verify(test, checkpoints=second,
        generate=cached_generator(second, test.generate))
    candidates = candidate_runs(test)
    assert len(candidates) == 3 and receipt["status"] == "passed"
    assert candidates[2]["programs"] == latest["programs"] != candidates[0]["programs"]
    assert all(run["plan"] == frozen for run in candidates)
    assert {item.path: item.content for item in result.scripts} == latest["programs"]
    assert len(build_calls(test)) == 1
    assert [call["stage"] for call in test.calls].count("synthetic_validation_plan") == 1
    assert [call["stage"] for call in test.calls].count("synthetic_reference_implementation") == 1
    assert test.review.await_count == 2
