"""An original-source method defect, not an oracle answer, may authorize repair."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skill_review_fixtures import review_for_payload
from app.domain.services.skills.method_contract import MethodReview
from app.domain.services.skills.skill_creation_error import (
    SkillCreationError, SkillPackageValidationError, SkillRepairReviewRejected,
)
from app.domain.services.skills.skill_script_verification import verify_skill_script_cases
from app.infrastructure.external.sandbox import skill_validation_worker as worker
from test_session_skill_creator import helper_script
from test_skill_creation_checkpoints import MemoryCache, builds
from test_skill_reference_verification import host_receipt, method_review, output_files
from test_skill_script_verification import recipe, revalidate
from test_skill_validation_plan import method_contract, sources
from test_skill_validation_review import accepted_fixture_review, typed


BAD_EXPRESSION = "sum(values)"
GOOD_EXPRESSION = "sum(values) / len(values)"
REFERENCE_CANARY = "PRIVATE_REFERENCE_IMPLEMENTATION_839217"
PLAN_CANARY = "PRIVATE_SYNTHETIC_ORACLE_REASON_721938"
DIAGNOSTIC_CANARY = "PRIVATE_HOST_DIAGNOSTIC_617294"


def scenario(monkeypatch, *, audit_status="source_violation", reference=.25,
             candidate=.5, repaired=.25, repair="substantive", damage=None,
             first_failure="science", second_failure=None):
    """Use real reference/review logic and strict host receipts; no actual model."""
    plan = typed()
    normal = plan.scripts[0].cases[0]
    for row in normal.input_files[0].rows:
        row["value"] = .25
    normal.assertions[0].fields["mean"].value = .2500000002
    normal.oracle_explanation = PLAN_CANARY
    original = recipe()
    expression = GOOD_EXPRESSION if audit_status != "source_violation" else BAD_EXPRESSION
    original.scripts[0].content += f"\ndef mean_of_valid(values):\n    return {expression}\n"
    # The production adapter returns canonical typed source. Avoid creating an
    # artificial trailing-newline cache miss by mutating its model directly.
    original = type(original).model_validate(original.model_dump(mode="json"))
    calls, runs, events = [], [], []
    test = SimpleNamespace(original=original, before=original.model_dump(mode="json"),
        plan=plan, calls=calls, runs=runs, events=events, after_number=repaired)

    async def generate(schema, stage, instruction, payload, check):
        calls.append({"stage": stage, "payload": copy.deepcopy(payload), "instruction": instruction})
        events.append(stage)
        if stage == "synthetic_validation_plan":
            document = plan.model_dump()
        elif stage == "synthetic_reference_implementation":
            document = {**payload["script_request"], "content": helper_script()["content"] + f"\n# {REFERENCE_CANARY}\n"}
        elif stage == "synthetic_reference_review":
            document = method_review(payload)
        elif stage == "synthetic_plan_review":
            document = review_for_payload(accepted_fixture_review(payload["validation_plan"]), payload)
        elif stage == "synthetic_method_audit":
            repeated = len(stage_calls(test, stage)) > 1
            document = {"requirements": [{"requirement_id": requirement_id,
                "status": audit_status, "source_id": "S001", "source_quote": sources()[0]["text"],
                "script_path": item["script_path"], "code_quote": GOOD_EXPRESSION if repeated else expression,
                "explanation": ("The original all-missing semantics require an empty-input branch; division by the valid count does not handle that case."
                    if repeated else "The original arithmetic mean requires division by the number of valid values; this expression does not perform that division.")}
                for item in payload["review_scope"] for requirement_id in item["requirement_ids"]]}
            if damage == "source_quote":
                document["requirements"][0]["source_quote"] = "Invented requirement unrelated to original source."
            elif damage == "code_quote":
                document["requirements"][0]["code_quote"] = "unrelated_function(values)"
            elif damage == "requirement":
                document["requirements"][0]["requirement_id"] = "R999"
        else:
            assert stage in {"synthetic_method_repair", "synthetic_build_repair", "synthetic_script_repair"}
            document = copy.deepcopy(payload["script"])
            if repair == "cosmetic":
                document["content"] += "\n# The unchanged computation is described differently.\n"
            elif repair == "cli":
                document["content"] += "\np.add_argument('--foreign-input')\n"
            elif repair == "output":
                document["content"] = document["content"].replace("'summary.csv'", "'renamed.csv'")
            elif repair == "metadata":
                document["invocation"] += " --foreign INPUT"
            else:
                document["content"] = document["content"].replace(
                    "return " + BAD_EXPRESSION + "\n", "return " + GOOD_EXPRESSION + "\n")
                if document["content"].endswith("return " + BAD_EXPRESSION):
                    document["content"] += " / len(values)"
                if len(stage_calls(test, "synthetic_method_repair")) > 1:
                    document["content"] = document["content"].replace(
                        "def mean_of_valid(values):\n", "def mean_of_valid(values):\n    if not values:\n        return None\n")
                document["content"] += "\nmethod_repair_applied = True\n"
        value = schema.model_validate(document)
        if check:
            check(value)
        return value

    async def execute(programs, frozen, **kwargs):
        is_candidate = any("IMPLEMENTATION_MARKER_NOT_FOR_ORACLE" in source for source in programs.values())
        number = sum(run["candidate"] for run in runs) + 1
        events.append(("candidate_execution" if is_candidate else "reference_execution", number))

        def files(case):
            result = output_files(case)
            if case.kind == "normal":
                value = (candidate if number == 1 else test.after_number) if is_candidate else reference
                if is_candidate and number == 1 and first_failure == "status":
                    value = reference
                    result["status.json"] = json.dumps({"status": "bad"}).encode()
                result["summary.csv"] = ("group,mean,valid_count,missing_count,missing_rate\n"
                    f"synthetic-a,{value},2,0,0\n").encode()
            return result

        observed = host_receipt(programs, frozen, files=files)
        if is_candidate:
            observed["cases"][0]["diagnostic"] = DIAGNOSTIC_CANARY
            failure = first_failure if number == 1 else second_failure
            if failure == "build":
                case = observed["cases"][0]
                case.update(status="failed", reason="program_runtime_failure", actual_exit_code=1,
                    runtime_error_type="KeyError", runtime_build_evidence={"version": 1,
                    "origin": "host_uncaught_exception", "exception_type": "KeyError"})
                case.pop("checks", None)
                case.pop("output_sha256", None)
                observed["status"] = "failed"
        runs.append({"candidate": is_candidate, "programs": dict(programs),
            "plan": frozen.model_dump(mode="json"), "receipt": copy.deepcopy(observed)})
        return observed

    monkeypatch.setattr(worker, "validate_skill_scripts", execute)

    async def review(candidate_recipe):
        events.append("full_method_review")
        assert GOOD_EXPRESSION in candidate_recipe.scripts[0].content

    test.generate = generate
    test.review = AsyncMock(side_effect=review)
    return test


async def verify(test, *, checkpoints=None, review=None, generate=None):
    return await verify_skill_script_cases(test.original, method_contract(), sources(),
        generate=generate or test.generate, revalidate=revalidate,
        review=review or test.review, checkpoints=checkpoints)


def cached(checkpoint, generate):
    async def call(schema, stage, instruction, payload, check):
        return await checkpoint.run(schema, stage, instruction, payload, check,
            lambda: generate(schema, stage, instruction, payload, check))
    return call


def stage_calls(test, stage):
    return [item for item in test.calls if item["stage"] == stage]


def candidate_runs(test):
    return [item for item in test.runs if item["candidate"]]


@pytest.mark.asyncio
async def test_source_violation_repairs_blindly_then_reviews_before_same_plan_execution(monkeypatch):
    test = scenario(monkeypatch)
    result, frozen, receipt = await verify(test)
    assert receipt["status"] == "passed"
    audit, = stage_calls(test, "synthetic_method_audit")
    repair, = stage_calls(test, "synthetic_method_repair")
    assert set(audit["payload"]) == {"method_contract", "sources", "programs", "review_scope"}
    assert audit["payload"]["programs"] == {script.path: script.content for script in test.original.scripts}
    assert audit["payload"]["review_scope"] == [{"script_path": "scripts/group_summary.py", "requirement_ids": ["R001"]}]
    assert set(repair["payload"]) == {"method_contract", "sources", "script", "source_method_audit", "accepted_script_repairs"}
    assert repair["payload"]["source_method_audit"][0]["code_quote"] == BAD_EXPRESSION
    for request in (audit["payload"], repair["payload"]):
        encoded = json.dumps(request, ensure_ascii=False)
        for forbidden in (REFERENCE_CANARY, PLAN_CANARY, DIAGNOSTIC_CANARY,
                "0.2500000002", "frozen_validation_plan", "observed_validation", "reference_verification",
                "input_files", "oracle_explanation", '"atol"', '"rtol"', '"checks"', '"actual"'):
            assert forbidden not in encoded
    first, final = candidate_runs(test)
    assert first["plan"] == final["plan"] == frozen
    assert first["receipt"]["plan_sha256"] == final["receipt"]["plan_sha256"]
    assert first["programs"] != final["programs"]
    assert test.events.index("synthetic_method_repair") < test.events.index("full_method_review")
    assert test.events.index("full_method_review") < test.events.index(("candidate_execution", 2))
    assert test.original.model_dump(mode="json") == test.before
    test.review.assert_awaited_once_with(result)
    assert not stage_calls(test, "synthetic_script_repair") and not stage_calls(test, "synthetic_build_repair")


@pytest.mark.asyncio
@pytest.mark.parametrize("audit_status", ["preserved", "uncertain"])
async def test_correct_candidate_wrong_reference_cannot_authorize_scientific_rewrite(monkeypatch, audit_status):
    test = scenario(monkeypatch, audit_status=audit_status, reference=.5, candidate=.25)
    cache = MemoryCache()
    for _ in range(2):
        checkpoint = builds(cache)
        with pytest.raises(SkillCreationError) as error:
            await verify(test, checkpoints=checkpoint, generate=cached(checkpoint, test.generate))
        assert error.value.code == "reference_calculation_unconfirmed"
    assert not any(call["stage"].endswith("repair") for call in test.calls)
    assert len(stage_calls(test, "synthetic_method_audit")) == 1
    assert len(stage_calls(test, "synthetic_validation_plan")) == 1
    assert all(run["programs"] == {s.path: s.content for s in test.original.scripts} for run in candidate_runs(test))
    assert test.original.model_dump(mode="json") == test.before
    test.review.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["source_quote", "code_quote", "requirement"])
async def test_unbound_diagnostic_cannot_authorize_source_repair(monkeypatch, damage):
    test = scenario(monkeypatch, damage=damage)
    with pytest.raises((SkillCreationError, SkillPackageValidationError, ValueError)):
        await verify(test)
    assert len(candidate_runs(test)) == 1
    assert not stage_calls(test, "synthetic_method_repair")
    test.review.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("repair", ["cli", "output", "metadata", "cosmetic"])
async def test_method_repair_preserves_interface_and_requires_substantive_change(monkeypatch, repair):
    test = scenario(monkeypatch, repair=repair)
    with pytest.raises((SkillCreationError, SkillPackageValidationError)) as error:
        await verify(test)
    assert error.value.code == ("synthetic_repair_unchanged" if repair == "cosmetic" else "repaired_script_interface_changed")
    assert len(candidate_runs(test)) == 1
    test.review.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("second_failure", [None, "build"])
async def test_each_explicit_attempt_has_one_shared_repair_and_retains_latest_failed_source(monkeypatch, second_failure):
    test, cache = scenario(monkeypatch, repaired=.75, second_failure=second_failure), MemoryCache()
    checkpoint = builds(cache)
    with pytest.raises(SkillCreationError):
        await verify(test, checkpoints=checkpoint, generate=cached(checkpoint, test.generate))
    saved = await builds(cache).get_execution_revision()
    final = candidate_runs(test)[-1]
    assert saved is not None and saved.repair_used is True
    assert {s.path: s.content for s in saved.recipe.scripts} == final["programs"]
    assert saved.plan.model_dump(mode="json") == final["plan"]
    assert saved.execution["cases"] == final["receipt"]["cases"]
    assert len(candidate_runs(test)) == 2 and len(stage_calls(test, "synthetic_method_repair")) == 1
    assert not stage_calls(test, "synthetic_build_repair") and not stage_calls(test, "synthetic_script_repair")
    retry = builds(cache)
    with pytest.raises(SkillCreationError):
        await verify(test, checkpoints=retry, generate=cached(retry, test.generate))
    assert len(candidate_runs(test)) == 4
    assert candidate_runs(test)[2]["programs"] == final["programs"]
    assert candidate_runs(test)[3]["programs"] != final["programs"]
    assert all(run["plan"] == final["plan"] for run in candidate_runs(test))
    latest = await builds(cache).get_execution_revision()
    assert {s.path: s.content for s in latest.recipe.scripts} == candidate_runs(test)[3]["programs"]
    assert latest.execution["cases"] == candidate_runs(test)[3]["receipt"]["cases"]
    # The explicit retry grants one new repair, not an unbounded loop. Its
    # second real failure stops and keeps the actual latest source.
    if second_failure == "build":
        assert len(stage_calls(test, "synthetic_method_repair")) == len(stage_calls(test, "synthetic_build_repair")) == 1
    else:
        assert len(stage_calls(test, "synthetic_method_audit")) == len(stage_calls(test, "synthetic_method_repair")) == 2
        assert not stage_calls(test, "synthetic_build_repair")
    assert not stage_calls(test, "synthetic_script_repair")


@pytest.mark.asyncio
@pytest.mark.parametrize("first_failure", ["build", "status"])
async def test_prior_build_or_ordinary_repair_cannot_get_second_method_repair(monkeypatch, first_failure):
    test = scenario(monkeypatch, first_failure=first_failure, repaired=.75)
    with pytest.raises(SkillCreationError) as error:
        await verify(test)
    assert error.value.code == "reference_calculation_unconfirmed"
    assert len(candidate_runs(test)) == 2
    assert len(stage_calls(test, "synthetic_build_repair" if first_failure == "build" else "synthetic_script_repair")) == 1
    assert not stage_calls(test, "synthetic_method_repair")


@pytest.mark.asyncio
async def test_negative_full_review_keeps_pending_source_and_blocks_execution_on_resume(monkeypatch):
    test, cache = scenario(monkeypatch), MemoryCache()
    negative = MethodReview.model_validate(method_review(
        {"sources": sources(), "method_contract": method_contract().model_dump()}, verdict="changed"))
    producer = AsyncMock(return_value=negative)
    reviewed = []
    for _ in range(2):
        checkpoint = builds(cache)

        async def reject(candidate):
            reviewed.append(candidate.model_dump(mode="json"))
            value = await checkpoint.run(MethodReview, "method_review", "Review exact revised method.",
                {"skill_package": candidate.model_dump()}, None, producer)
            assert value == negative
            raise SkillCreationError("method_review", "method_review_rejected")

        with pytest.raises(SkillCreationError) as error:
            await verify(test, checkpoints=checkpoint, generate=cached(checkpoint, test.generate), review=reject)
        assert error.value.code == "method_review_rejected"
        saved = await builds(cache).get_execution_revision()
        assert saved.repair_used is True and saved.pending_recipe is not None
        assert saved.recipe.model_dump(mode="json") == test.before
        assert saved.pending_recipe.model_dump(mode="json") == reviewed[-1]
        assert saved.execution["cases"] == candidate_runs(test)[0]["receipt"]["cases"]
    assert len(candidate_runs(test)) == 1
    assert reviewed[0] == reviewed[1]
    assert len(stage_calls(test, "synthetic_method_repair")) == 1
    producer.assert_awaited_once()
    adverse = [entry for key, entry in cache.entries.items() if ":adverse:" in key]
    assert any(item.get("value") == negative.model_dump(mode="json") for item in adverse)


@pytest.mark.asyncio
async def test_resume_rejects_changed_frozen_plan_instead_of_silently_using_original(monkeypatch):
    test, cache = scenario(monkeypatch, repaired=.75), MemoryCache()
    checkpoint = builds(cache)
    with pytest.raises(SkillCreationError):
        await verify(test, checkpoints=checkpoint, generate=cached(checkpoint, test.generate))
    entry = next(entry["value"] for key, entry in cache.entries.items()
        if ":revision:" in key and "execution" in entry.get("value", {}))
    entry["plan"]["scripts"][0]["cases"][0]["assertions"][0]["fields"]["mean"]["value"] = 12345
    prior_run_count = len(candidate_runs(test))
    retry = builds(cache)
    with pytest.raises((SkillCreationError, SkillPackageValidationError, ValueError)):
        await verify(test, checkpoints=retry, generate=cached(retry, test.generate))
    assert len(candidate_runs(test)) == prior_run_count
    assert len(stage_calls(test, "synthetic_method_repair")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reject_again", [False, True])
async def test_real_typed_negative_authorizes_only_next_attempt_revision_of_pending_source(monkeypatch, reject_again):
    test, cache = scenario(monkeypatch), MemoryCache()
    negative = MethodReview.model_validate(method_review(
        {"sources": sources(), "method_contract": method_contract().model_dump()}, verdict="changed"))
    negative.requirements[0].explanation = "The original method requires all-missing groups to retain a missing mean; the helper has no empty-input branch."
    frozen_negative = negative.model_dump(mode="json")
    first = builds(cache)
    initial_review_sources = []

    async def initial_review(candidate):
        initial_review_sources.append(candidate.model_dump(mode="json"))
        raise SkillRepairReviewRejected(negative)

    with pytest.raises(SkillRepairReviewRejected):
        await verify(test, checkpoints=first, generate=cached(first, test.generate), review=initial_review)
    saved = await builds(cache).get_execution_revision()
    assert saved.pending_review.model_dump(mode="json") == frozen_negative
    assert saved.pending_recipe.model_dump(mode="json") == initial_review_sources[0]
    assert len(candidate_runs(test)) == len(stage_calls(test, "synthetic_method_repair")) == 1

    second = builds(cache)
    second_review_sources = []

    async def second_review(candidate):
        test.events.append("resumed_full_method_review")
        second_review_sources.append(candidate.model_dump(mode="json"))
        assert "if not values:" in candidate.scripts[0].content
        if reject_again:
            raise SkillRepairReviewRejected(negative)

    if reject_again:
        with pytest.raises(SkillRepairReviewRejected):
            await verify(test, checkpoints=second, generate=cached(second, test.generate), review=second_review)
        latest = await builds(cache).get_execution_revision()
        assert latest.pending_recipe.model_dump(mode="json") == second_review_sources[0]
        assert latest.pending_review.model_dump(mode="json") == frozen_negative
        assert len(candidate_runs(test)) == 1
    else:
        result, frozen, receipt = await verify(test, checkpoints=second,
            generate=cached(second, test.generate), review=second_review)
        assert receipt["status"] == "passed"
        assert result.model_dump(mode="json") == second_review_sources[0]
        assert all(run["plan"] == frozen for run in candidate_runs(test))
        assert test.events.index("resumed_full_method_review") < test.events.index(("candidate_execution", 2))
    repairs = stage_calls(test, "synthetic_method_repair")
    assert len(repairs) == 2 and len(stage_calls(test, "synthetic_method_audit")) == 1
    payload = repairs[1]["payload"]
    assert payload["script"] == initial_review_sources[0]["scripts"][0]
    assert payload["source_method_review"] == frozen_negative
    assert second_review_sources[0] != initial_review_sources[0]
    assert negative.model_dump(mode="json") == frozen_negative
    for forbidden in (REFERENCE_CANARY, PLAN_CANARY, DIAGNOSTIC_CANARY,
            "frozen_validation_plan", "observed_validation", "reference_verification", "input_files"):
        assert forbidden not in json.dumps(payload, ensure_ascii=False)
