"""A real adverse reference review authorizes code repair, never another vote."""
import copy
import json
from unittest.mock import AsyncMock

import pytest

from app.domain.services.skills.method_contract import CoverageError, MethodReview
from app.domain.services.skills.skill_creation.checkpoints import content_digest
from app.domain.services.skills.skill_creation.models import HelperScript
from app.domain.services.skills.skill_creation_error import SkillCreationError, SkillPackageValidationError
from app.domain.services.skills.skill_reference_verification import (
    reference_generation_payload, verify_reference_plan,
)
from test_session_skill_creator import helper_script
from test_skill_creation_checkpoint_integration import Gateway, pipeline, verifier
from test_skill_creation_checkpoints import MemoryCache, builds
from test_skill_reference_verification import host_receipt, method_review
from test_skill_validation_plan import interfaces, method_contract, sources
from test_skill_validation_review import typed


def program(version=0):
    # Distinct, static-valid programs for testing authoring/review transitions;
    # host_receipt independently tests the execution-evidence parser boundary.
    code = helper_script()["content"].strip()
    return code.replace("str(len(rows))", f"str(len(rows) + {version})")


def responder(programs, verdicts):
    implementations, assessments = iter(programs), iter(verdicts)
    def reply(stage, payload):
        if stage == "synthetic_reference_implementation":
            return {**payload["script_request"], "content": next(implementations)}
        assert stage == "synthetic_reference_review"
        return method_review(payload, verdict=next(assessments))
    return reply


async def run(gateway, cache=None, *, execute=None, plan=None, declared=None):
    build = pipeline(cache if cache is not None else MemoryCache(), gateway, verifier())
    build.contract = method_contract()
    result = await verify_reference_plan(plan or typed(), method_contract(), sources(), declared or interfaces(),
        generate=build.generate, execute=execute or AsyncMock(side_effect=host_receipt), checkpoints=build.checkpoints)
    return result, build


@pytest.mark.asyncio
async def test_real_negative_causes_one_blind_code_revision_then_review_before_any_execution():
    original, revised = program(-1), program(0)
    gateway = Gateway(responder([original, revised], ["changed", "preserved"]))
    executions = []
    async def execute(programs, plan):
        assert [stage for stage, _ in gateway.calls] == [
            "synthetic_reference_implementation", "synthetic_reference_review",
            "synthetic_reference_implementation", "synthetic_reference_review"]
        assert set(programs.values()) == {revised}
        executions.append(programs)
        return host_receipt(programs, plan)
    (corrected, receipt), _ = await run(gateway, execute=execute)
    assert corrected == typed() and receipt["status"] == "verified" and len(executions) == 1
    first, review, revision, final_review = [payload for _, payload in gateway.calls]
    expected = {"reference_scripts": review["reference_scripts"],
                "review": method_review(review, verdict="changed")}
    assert revision["reference_revision"] == expected
    assert final_review["reference_scripts"][0]["content"] == revised
    for payload in (review, revision, final_review):
        assert {key: payload[key] for key in ("method_contract", "sources", "script_interfaces", "blind_cases")} == {
            key: first[key] for key in ("method_contract", "sources", "script_interfaces", "blind_cases")}
        assert not {"validation_plan", "execution", "observed_validation", "skill_package"} & payload.keys()
    assert set(revision) == set(first) | {"reference_revision"}


@pytest.mark.asyncio
async def test_second_negative_stops_but_next_trigger_revises_latest_rejection_without_repolling():
    cache, execute = MemoryCache(), AsyncMock(side_effect=host_receipt)
    gateway = Gateway(responder([program(-2), program(-1), program(0)], ["changed", "changed", "preserved"]))
    with pytest.raises(SkillCreationError) as failure:
        await run(gateway, cache, execute=execute)
    assert (failure.value.stage, failure.value.code) == ("synthetic_reference_review", "reference_calculation_unconfirmed")
    execute.assert_not_awaited()
    saved = await builds(cache).get_reference_revision()
    assert saved.reference_scripts[0].content == program(-1)
    assert saved.review.implementation_appropriate is False
    (corrected, receipt), _ = await run(gateway, cache, execute=execute)
    assert receipt["status"] == "verified" and corrected == typed()
    assert len(gateway.calls) == 6
    resume = gateway.calls[4][1]["reference_revision"]
    assert resume["reference_scripts"] == [item.model_dump(mode="json") for item in saved.reference_scripts]
    assert resume["review"] == saved.review.model_dump(mode="json")
    assert [stage for stage, _ in gateway.calls[4:]] == ["synthetic_reference_implementation", "synthetic_reference_review"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cosmetic", [lambda code: code, lambda code: '"""Improved explanation."""\n# clearer\n' + code])
async def test_unchanged_ast_gets_no_new_vote_and_clears_only_current_source_cache(cosmetic):
    cache, execute = MemoryCache(), AsyncMock(side_effect=host_receipt)
    gateway = Gateway(responder([program(-1), cosmetic(program(-1)), program(0)], ["changed", "preserved"]))
    with pytest.raises(SkillCreationError):
        await run(gateway, cache, execute=execute)
    execute.assert_not_awaited()
    assert [stage for stage, _ in gateway.calls].count("synthetic_reference_review") == 1
    rejected = await builds(cache).get_reference_revision()
    assert rejected.reference_scripts[0].content == program(-1)
    # The exact revision request is generated again: the cosmetic cache entry
    # was evicted, but its original rejection remains reusable, not re-voted.
    await run(gateway, cache, execute=execute)
    assert len(gateway.calls) == 5
    assert gateway.calls[2][1] == gateway.calls[3][1]
    assert sum(stage == "synthetic_reference_review" for stage, _ in gateway.calls) == 2


@pytest.mark.asyncio
async def test_failure_mid_revision_resumes_validated_code_and_typed_adverse_review():
    cache, failures = MemoryCache(), 0
    base = responder([program(-1), program(0)], ["changed", "preserved"])
    def reply(stage, payload):
        nonlocal failures
        if "reference_revision" in payload and failures == 0:
            failures += 1
            raise SkillCreationError(stage, "model_unavailable")
        return base(stage, payload)
    gateway = Gateway(reply)
    with pytest.raises(SkillCreationError):
        await run(gateway, cache)
    await run(gateway, cache)
    assert [stage for stage, _ in gateway.calls] == ["synthetic_reference_implementation", "synthetic_reference_review",
        "synthetic_reference_implementation", "synthetic_reference_implementation", "synthetic_reference_review"]
    assert gateway.calls[2][1] == gateway.calls[3][1]


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["preserved", "changed"])
@pytest.mark.parametrize("defect", ["missing", "duplicate", "foreign"])
async def test_negative_is_not_permission_to_bypass_review_coverage(verdict, defect):
    base = responder([program(-1)], [verdict])
    def reply(stage, payload):
        document = base(stage, payload)
        if stage == "synthetic_reference_review":
            if defect == "missing": document["requirements"] = []
            elif defect == "duplicate": document["requirements"] *= 2
            else: document["requirements"][0]["requirement_id"] = "R999"
        return document
    gateway, execute = Gateway(reply), AsyncMock()
    with pytest.raises((CoverageError, SkillCreationError)):
        await run(gateway, execute=execute)
    execute.assert_not_awaited()
    assert not any("reference_revision" in payload for _, payload in gateway.calls)


@pytest.mark.asyncio
async def test_execution_disagreement_does_not_regenerate_reference():
    gateway = Gateway(responder([program(0)], ["preserved"]))
    async def execute(programs, plan):
        receipt = host_receipt(programs, plan)
        receipt["cases"][0]["checks"][-1]["actual"] = None
        return receipt
    with pytest.raises(SkillCreationError) as failure:
        await run(gateway, execute=execute)
    assert failure.value.stage == "synthetic_reference_execution"
    assert len(gateway.calls) == 2


@pytest.mark.asyncio
async def test_old_code_only_adverse_record_is_not_erased_or_repolled():
    cache = MemoryCache()
    base = responder([program(-1)], ["changed"])
    def old_reply(stage, payload):
        if stage == "synthetic_reference_review":
            raise SkillCreationError(stage, "reference_calculation_unconfirmed")
        return base(stage, payload)
    old = Gateway(old_reply)
    with pytest.raises(SkillCreationError):
        await run(old, cache)
    before = copy.deepcopy(cache.entries)
    current = Gateway(lambda *_: pytest.fail("A code-only adverse judgment cannot be re-polled"))
    with pytest.raises(SkillCreationError):
        await run(current, cache)
    assert current.calls == [] and cache.entries == before


@pytest.mark.asyncio
async def test_revised_script_metadata_cannot_change():
    base = responder([program(-1), program(0)], ["changed"])
    def reply(stage, payload):
        document = base(stage, payload)
        if "reference_revision" in payload:
            document["invocation"] += " --alternate-input DATA"
        return document
    gateway, execute = Gateway(reply), AsyncMock()
    with pytest.raises(SkillCreationError):
        await run(gateway, execute=execute)
    assert len(gateway.calls) == 3
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_saved_context_change_cannot_apply_review_to_different_inputs():
    cache = MemoryCache()
    gateway = Gateway(responder([program(-2), program(-1), program(0)], ["changed", "changed", "preserved"]))
    with pytest.raises(SkillCreationError):
        await run(gateway, cache)
    plan = typed()
    plan.scripts[0].cases[0].input_files[0].rows[0]["value"] = "5"
    await run(gateway, cache, plan=plan)
    assert "reference_revision" not in gateway.calls[4][1]


@pytest.mark.asyncio
async def test_reference_revision_is_private_owner_scoped_and_revalidated():
    from app.domain.services.skills.skill_creation.checkpoints import BuildCheckpoints
    from app.domain.services.skills.skill_creation.privacy import PrivacyContext, PrivateValue
    from test_skill_creation_checkpoints import scope
    cache = MemoryCache()
    context = reference_generation_payload(typed(), method_contract(), sources(), interfaces())
    reference = HelperScript.model_validate({**helper_script(), "path": "scripts/group_summary.py",
        "invocation": "python scripts/group_summary.py --input INPUT --output OUTPUT"})
    review = MethodReview.model_validate(method_review(context, verdict="changed"))
    review.requirements[0].explanation = "The declared target /status does not preserve the required output behavior."
    record = {"reference_scripts": [reference], "review": review, "blind_context_digest": content_digest(context)}
    await builds(cache).save_reference_revision(record, blind_cases=context["blind_cases"])
    saved = await builds(cache).get_reference_revision(blind_cases=context["blind_cases"])
    assert saved.review == review
    assert await builds(cache, owner_id="other-owner").get_reference_revision(blind_cases=context["blind_cases"]) is None
    serialized = json.dumps(cache.entries)
    assert "blind_cases" not in serialized and "input_files" not in serialized and "scientist-one" not in serialized
    private = BuildCheckpoints(cache, scope(), privacy=PrivacyContext((PrivateValue("argparse", "identity"),)))
    assert await private.get_reference_revision(blind_cases=context["blind_cases"]) is None
    with pytest.raises(SkillPackageValidationError):
        await private.save_reference_revision(record, blind_cases=context["blind_cases"])


@pytest.mark.asyncio
async def test_typed_negative_ledger_remains_sticky_after_revision_state_is_invalidated():
    cache = MemoryCache()
    gateway = Gateway(responder([program(-1), program(-1)], ["changed"]))
    with pytest.raises(SkillCreationError):
        await run(gateway, cache)
    # Drop only the resumable record as an expired/stale record would be; the
    # semantic decision still prevents another review of the same candidate.
    for key in list(cache.entries):
        if ":revision:" in key:
            del cache.entries[key]
    resume = Gateway(responder([program(0)], ["preserved"]))
    await run(resume, cache)
    assert [stage for stage, _ in resume.calls] == ["synthetic_reference_implementation", "synthetic_reference_review"]
    assert "reference_revision" in resume.calls[0][1]


def two_scripts():
    plan, declared = typed(), interfaces()
    second = plan.scripts[0].model_copy(deep=True, update={"script_path": "scripts/second_summary.py"})
    plan.scripts.append(second)
    declared.append({**copy.deepcopy(declared[0]), "script_path": second.script_path,
        "invocation": "python scripts/second_summary.py --input INPUT --output OUTPUT"})
    return plan, declared


@pytest.mark.asyncio
async def test_partial_reference_revision_resumes_first_validated_script_without_restarting_review():
    plan, declared = two_scripts()
    cache, failed = MemoryCache(), False
    def reply(stage, payload):
        nonlocal failed
        if stage == "synthetic_reference_review":
            is_revised = any(script["content"] == program(0) for script in payload["reference_scripts"])
            return method_review(payload, verdict="preserved" if is_revised else "changed")
        path = payload["script_request"]["path"]
        if "reference_revision" in payload and path == "scripts/second_summary.py" and not failed:
            failed = True
            raise SkillCreationError(stage, "model_unavailable")
        return {**payload["script_request"], "content": program(0 if "reference_revision" in payload else -1)}
    gateway = Gateway(reply)
    with pytest.raises(SkillCreationError):
        await run(gateway, cache, plan=plan, declared=declared)
    (corrected, receipt), _ = await run(gateway, cache, plan=plan, declared=declared)
    assert corrected == plan and receipt["status"] == "verified"
    revisions = [payload for stage, payload in gateway.calls
                 if stage == "synthetic_reference_implementation" and "reference_revision" in payload]
    assert [item["script_request"]["path"] for item in revisions] == [
        "scripts/group_summary.py", "scripts/second_summary.py", "scripts/second_summary.py"]
    assert sum(stage == "synthetic_reference_review" for stage, _ in gateway.calls) == 2


@pytest.mark.asyncio
async def test_reference_revision_preserves_other_scripts_when_one_program_substantively_changes():
    plan, declared = two_scripts()
    gateway = Gateway(responder([program(-1), program(0), program(0), program(0)], ["changed", "preserved"]))
    (corrected, receipt), _ = await run(gateway, plan=plan, declared=declared)
    assert corrected == plan and receipt["status"] == "verified"
    reviews = [payload["reference_scripts"] for stage, payload in gateway.calls if stage == "synthetic_reference_review"]
    assert reviews[0][0]["content"] != reviews[1][0]["content"]
    assert reviews[0][1] == reviews[1][1]


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", ["missing_coverage", "wrong_path", "duplicate_script", "private_source"])
async def test_corrupt_saved_reference_is_discarded_without_losing_original_adverse_verdict(defect):
    cache = MemoryCache()
    gateway = Gateway(responder([program(-1), program(-1)], ["changed"]))
    with pytest.raises(SkillCreationError):
        await run(gateway, cache)
    entries = [entry for key, entry in cache.entries.items() if ":revision:" in key]
    assert len(entries) == 1
    value = entries[0]["value"]
    if defect == "missing_coverage": value["review"]["requirements"] = []
    elif defect == "wrong_path": value["reference_scripts"][0]["path"] = "scripts/other.py"
    elif defect == "duplicate_script": value["reference_scripts"] *= 2
    else: value["reference_scripts"][0]["content"] += "\n# /Users/alice/private/secret.py"
    resume = Gateway(responder([program(0)], ["preserved"]))
    await run(resume, cache)
    assert [stage for stage, _ in resume.calls] == ["synthetic_reference_implementation", "synthetic_reference_review"]
    restored = resume.calls[0][1]["reference_revision"]
    assert restored["reference_scripts"][0]["content"] == program(-1)
    assert len(restored["review"]["requirements"]) == 1
