"""Resume validated work without crossing ownership or scientific decisions."""
import copy
import json
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import BaseModel, ConfigDict

from app.domain.services.skills.method_contract import MethodReview
from app.domain.services.skills.skill_creation.checkpoints import BuildCheckpoints, BuildScope, RejectedMethodRevision, content_digest
from app.domain.services.skills.skill_creation.models import HelperScript, SkillRecipe
from app.domain.services.skills.skill_creation.package import validate_recipe
from app.domain.services.skills.skill_creation.privacy import PrivacyContext, PrivateValue
from app.domain.services.skills.skill_creation_error import SkillCreationError, SkillPackageValidationError
from test_session_skill_creator import helper_script, recipe_payload
from test_skill_creation_pipeline import review_payload


class Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str


class MemoryCache:
    def __init__(self):
        self.entries, self.ttls = {}, {}

    async def get(self, key):
        return copy.deepcopy(self.entries.get(key))

    async def set(self, key, value, ttl=None):
        self.entries[key], self.ttls[key] = copy.deepcopy(value), ttl
        return True

    async def delete(self, key):
        return self.entries.pop(key, None) is not None


def scope(**changes):
    kwargs = dict(owner_id="scientist-one", session_id="analysis-one",
        source={"method": "mean with missing values excluded", "source_revision": "first"},
        configuration={"provider": "model-provider", "model": "scientific-model"}, code_version="authoring-v1")
    return BuildScope.create(**(kwargs | changes))


def builds(cache, **changes):
    return BuildCheckpoints(cache, scope(**changes))


async def generated(build, producer, *, stage="workflow", payload=None, check=None, **kwargs):
    return await build.run(Artifact, stage, "Preserve the completed method.",
        payload or {"contract": "stable"}, check, producer, **kwargs)


@pytest.mark.asyncio
async def test_retry_reuses_only_validated_artifact_and_runs_current_check_again():
    cache, check = MemoryCache(), Mock()
    producer = AsyncMock(return_value=Artifact(content="Reusable workflow with input validation."))
    first = await generated(builds(cache), producer, check=check)
    second = await generated(builds(cache), producer, check=check)
    assert first == second and first is not second
    assert producer.await_count == 1 and check.call_count == 2
    assert set(cache.ttls.values()) == {86400}


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"owner_id": "scientist-two"}, {"session_id": "analysis-two"},
    {"source": {"method": "median instead of mean"}},
    {"configuration": {"model": "other-model"}}, {"code_version": "authoring-v2"},
])
async def test_owner_session_source_model_and_code_isolation(change):
    cache = MemoryCache()
    producer = AsyncMock(return_value=Artifact(content="Validated method."))
    await generated(builds(cache), producer)
    await generated(builds(cache, **change), producer)
    assert producer.await_count == 2


@pytest.mark.asyncio
async def test_contract_or_recipe_revision_invalidates_dependent_stage_only():
    cache = MemoryCache()
    producer = AsyncMock(return_value=Artifact(content="Validated method."))
    for contract, script in [("first", "a"), ("first", "b"), ("second", "a"), ("first", "b")]:
        await generated(builds(cache), producer, payload={"contract": contract, "script": script})
    assert producer.await_count == 3


@pytest.mark.asyncio
async def test_new_domain_check_rejects_old_artifact_then_checks_regeneration():
    cache = MemoryCache()
    producer = AsyncMock(side_effect=[Artifact(content="old method"), Artifact(content="new method")])
    await generated(builds(cache), producer)
    def check(value):
        if value.content == "old method":
            raise SkillPackageValidationError("incomplete_skill_conditions")
    assert (await generated(builds(cache), producer, check=check)).content == "new method"
    assert producer.await_count == 2


@pytest.mark.asyncio
async def test_failed_generation_domain_check_does_not_create_checkpoint():
    cache = MemoryCache()
    with pytest.raises(SkillPackageValidationError):
        await generated(builds(cache), AsyncMock(return_value=Artifact(content="Incomplete method.")),
            check=Mock(side_effect=SkillPackageValidationError("incomplete_skill_conditions")))
    assert not cache.entries


@pytest.mark.asyncio
async def test_privacy_is_checked_on_writes_and_reads_even_without_caller_check():
    cache, marker = MemoryCache(), "private-source-name"
    privacy = PrivacyContext((PrivateValue(marker, "identity"),))
    with pytest.raises(SkillPackageValidationError):
        await generated(BuildCheckpoints(cache, scope(), privacy=privacy),
            AsyncMock(return_value=Artifact(content=marker)))
    assert not cache.entries
    producer = AsyncMock(side_effect=[Artifact(content=marker), Artifact(content="Reusable generic input.")])
    await generated(builds(cache), producer)
    assert (await generated(BuildCheckpoints(cache, scope(), privacy=privacy), producer)).content == "Reusable generic input."
    assert producer.await_count == 2 and marker not in json.dumps(cache.entries)


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing-field", "extra-field", "wrong-binding", "nonfinite-expiry"])
async def test_corrupt_or_foreign_record_cannot_skip_generation(damage):
    cache = MemoryCache()
    producer = AsyncMock(return_value=Artifact(content="Validated portable content."))
    await generated(builds(cache), producer)
    entry = next(iter(cache.entries.values()))
    if damage == "missing-field":
        del entry["value"]["content"]
    elif damage == "extra-field":
        entry["value"]["foreign"] = "untrusted field"
    elif damage == "wrong-binding":
        entry["binding"] = "foreign-owner-binding"
    else:
        entry["expires_at"] = float("nan")
    await generated(builds(cache), producer)
    assert producer.await_count == 2


@pytest.mark.asyncio
async def test_expiry_is_absolute_and_reads_do_not_extend_it():
    cache, now = MemoryCache(), [100.0]
    producer = AsyncMock(return_value=Artifact(content="Validated portable content."))
    build = BuildCheckpoints(cache, scope(), ttl_seconds=10, clock=lambda: now[0])
    await generated(build, producer)
    now[0] = 109
    await generated(build, producer)
    now[0] = 110
    await generated(build, producer)
    assert producer.await_count == 2


@pytest.mark.asyncio
async def test_source_prompts_credentials_and_identity_are_only_hashed():
    cache = MemoryCache()
    values = ["private-owner", "private-session", "private-source", "private-credential", "private-prompt"]
    build = BuildCheckpoints(cache, BuildScope.create(owner_id=values[0], session_id=values[1],
        source={"body": values[2]}, configuration={"credential": values[3]}, code_version="v1"))
    await build.run(Artifact, "workflow", values[4], {"input": values[2]}, None,
        AsyncMock(return_value=Artifact(content="Portable workflow.")))
    assert not any(value in json.dumps(cache.entries) for value in values)


@pytest.mark.asyncio
async def test_partial_script_resume_keeps_first_script_and_retries_failed_second():
    cache, requests = MemoryCache(), []
    first = HelperScript.model_validate(helper_script())
    second = first.model_copy(update={"path": "scripts/second.py", "invocation": "python scripts/second.py --input INPUT --output OUTPUT"})
    fail_second = True
    async def create(script):
        requests.append(script.path)
        if script.path == second.path and fail_second:
            raise SkillCreationError("implementation", "invalid_schema")
        return script
    async def run(build):
        results = []
        for script in (first, second):
            def check(value):
                assert value.path == script.path
                validate_recipe(SkillRecipe.model_validate({**recipe_payload(), "scripts": [value.model_dump()]}))
            results.append(await build.run(HelperScript, "implementation", "Write a complete script.",
                {"script_request": script.model_dump(exclude={"content"})}, check, lambda: create(script)))
        return results
    with pytest.raises(SkillCreationError):
        await run(builds(cache))
    fail_second = False
    assert await run(builds(cache)) == [first, second]
    assert requests == [first.path, second.path, second.path]


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["method_review_rejected", "validation_oracle_rejected", "validation_oracle_uncertain"])
async def test_adverse_decision_survives_model_change_until_substantive_candidate_change(code):
    cache = MemoryCache()
    stage = "method_review" if code == "method_review_rejected" else "synthetic_plan_review"
    producer = AsyncMock(side_effect=SkillCreationError(stage, code))
    for build in (builds(cache), builds(cache, configuration={"model": "replacement-model"})):
        with pytest.raises(SkillCreationError) as error:
            await generated(build, producer, stage=stage, decision_identity="same-method")
        assert error.value.code == code
    assert producer.await_count == 1
    producer.side_effect = None
    producer.return_value = Artifact(content="Substantively revised candidate.")
    await generated(builds(cache), producer, stage=stage, decision_identity="revised-method")
    assert producer.await_count == 2


@pytest.mark.asyncio
async def test_negative_method_review_can_drive_revision_without_another_vote():
    cache = MemoryCache()
    payload = review_payload()
    payload["requirements"][0]["status"] = "missing"
    review = MethodReview.model_validate(payload)
    producer = AsyncMock(return_value=review)
    for build in (builds(cache), builds(cache, configuration={"model": "replacement-model"})):
        assert await build.run(MethodReview, "method_review", "Review methods.", {}, None,
            producer, decision_identity="same-method") == review
    assert producer.await_count == 1
    with pytest.raises(SkillCreationError) as error:
        await builds(cache).run(MethodReview, "method_review", "Review methods.", {},
            Mock(side_effect=ValueError("new validation failure")), producer, decision_identity="same-method")
    assert error.value.code == "method_review_rejected" and producer.await_count == 1


@pytest.mark.asyncio
async def test_cache_outage_keeps_generation_available_and_in_attempt_rejection_sticky(caplog):
    cache = MemoryCache()
    cache.get = AsyncMock(side_effect=ConnectionError("private detail"))
    cache.set = AsyncMock(side_effect=ConnectionError("private detail"))
    build, value = builds(cache), Artifact(content="Validated portable content.")
    assert await generated(build, AsyncMock(return_value=value)) == value
    producer = AsyncMock(side_effect=SkillCreationError("synthetic_plan_review", "validation_oracle_rejected"))
    for _ in range(2):
        with pytest.raises(SkillCreationError):
            await generated(build, producer, stage="synthetic_plan_review")
    assert producer.await_count == 1 and "private detail" not in caplog.text


def rejected_revision():
    review = review_payload()
    review["requirements"][0]["status"] = "missing"
    return RejectedMethodRevision(recipe=SkillRecipe.model_validate(recipe_payload()),
        review=MethodReview.model_validate(review), contract_digest=content_digest({"method": "first"}))


@pytest.mark.asyncio
async def test_latest_rejected_candidate_and_exact_review_are_restored_and_revalidated():
    cache, revision, check = MemoryCache(), rejected_revision(), Mock()
    assert await builds(cache).save_revision(revision, check=check)
    assert await builds(cache).get_revision(check=check) == revision
    assert check.call_count == 2
    revised = revision.model_copy(update={"recipe": revision.recipe.model_copy(update={"goal": "Revised complete method workflow."})})
    await builds(cache).save_revision(revised)
    assert await builds(cache).get_revision() == revised
    for changes in ({"owner_id": "other"}, {"source": {"method": "changed"}}, {"configuration": {"model": "changed"}}):
        assert await builds(cache, **changes).get_revision() is None


@pytest.mark.asyncio
async def test_revision_requires_negative_review_and_rechecks_contract_and_privacy():
    cache, revision = MemoryCache(), rejected_revision()
    with pytest.raises(ValueError, match="adverse"):
        await builds(cache).save_revision(revision.model_copy(update={"review": MethodReview.model_validate(review_payload())}))
    assert not cache.entries
    await builds(cache).save_revision(revision)
    assert await builds(cache).get_revision(check=Mock(side_effect=ValueError("contract mismatch"))) is None
    assert not cache.entries
    marker = "private-source-name"
    revision.recipe.goal += " " + marker
    with pytest.raises(SkillPackageValidationError):
        await BuildCheckpoints(cache, scope(), privacy=PrivacyContext((PrivateValue(marker, "identity"),))).save_revision(revision)
    assert not cache.entries


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["rejected", "uncertain"])
async def test_typed_oracle_verdict_cannot_change_via_retry_or_configuration(status):
    from app.domain.services.skills.skill_validation import SyntheticValidationPlan
    from app.domain.services.skills.skill_validation_review import ValidationPlanReview, validate_oracle_review
    from test_skill_creation_pipeline import oracle_review_payload
    from test_skill_validation_plan import plan_payload
    plan = SyntheticValidationPlan.model_validate(plan_payload())
    data = oracle_review_payload(plan)
    data["scripts"][0]["support"]["status"] = status
    review = ValidationPlanReview.model_validate(data)
    cache, producer = MemoryCache(), AsyncMock(return_value=review)
    for build in (builds(cache), builds(cache, configuration={"model": "replacement-model"})):
        actual = await build.run(ValidationPlanReview, "synthetic_plan_review", "Review oracle independently.",
            {"plan": plan.model_dump()}, lambda value: validate_oracle_review(value, plan), producer)
        assert actual == review and actual.verdict() == status
    assert producer.await_count == 1


@pytest.mark.asyncio
async def test_negative_new_review_with_invalid_domain_check_retains_fixed_rejection_only():
    cache, review = MemoryCache(), review_payload()
    review["requirements"][0]["status"] = "missing"
    producer = AsyncMock(return_value=MethodReview.model_validate(review))
    for build in (builds(cache), builds(cache)):
        with pytest.raises(SkillCreationError) as error:
            await build.run(MethodReview, "method_review", "Review methods.", {},
                Mock(side_effect=ValueError("unpublishable review detail")), producer)
        assert error.value.code == "method_review_rejected"
    assert producer.await_count == 1
    assert "unpublishable" not in json.dumps(cache.entries)
    assert all("value" not in entry for entry in cache.entries.values())


@pytest.mark.asyncio
async def test_corrupted_adverse_record_cannot_return_a_positive_review():
    cache, review = MemoryCache(), review_payload()
    review["requirements"][0]["status"] = "missing"
    producer = AsyncMock(return_value=MethodReview.model_validate(review))
    await builds(cache).run(MethodReview, "method_review", "Review methods.", {}, None, producer)
    for key, entry in cache.entries.items():
        if ":adverse:" in key:
            entry["value"] = review_payload()
    with pytest.raises(SkillCreationError) as error:
        await builds(cache).run(MethodReview, "method_review", "Review methods.", {}, None, producer)
    assert error.value.code == "method_review_rejected" and producer.await_count == 1


@pytest.mark.asyncio
async def test_discard_failed_revision_only_evicts_touched_authoring_including_hits():
    cache, producer = MemoryCache(), AsyncMock(return_value=Artifact(content="Validated content."))
    build = builds(cache)
    await generated(build, producer, stage="method_requirements")
    await generated(build, producer, stage="workflow", payload={"revision": "initial"})
    await generated(build, producer, stage="workflow", payload={"revision": "repaired"})
    marker = build.mark()
    # The unchanged revision may itself have been a cache hit. It still needs
    # eviction so the next retry has a chance to author a substantive change.
    await generated(build, producer, stage="workflow", payload={"revision": "repaired"})
    await generated(build, producer, stage="implementation", payload={"revision": "repaired"})
    negative = AsyncMock(side_effect=SkillCreationError("method_review", "method_review_rejected"))
    with pytest.raises(SkillCreationError):
        await generated(build, negative, stage="method_review")
    await build.discard_since(marker, stages={"workflow", "implementation"})
    baseline = producer.await_count
    retry = builds(cache)
    await generated(retry, producer, stage="method_requirements")
    await generated(retry, producer, stage="workflow", payload={"revision": "initial"})
    assert producer.await_count == baseline
    await generated(retry, producer, stage="workflow", payload={"revision": "repaired"})
    await generated(retry, producer, stage="implementation", payload={"revision": "repaired"})
    assert producer.await_count == baseline + 2
    with pytest.raises(SkillCreationError):
        await generated(retry, negative, stage="method_review")
    assert negative.await_count == 1


def oracle_revision():
    from app.domain.services.skills.skill_creation.checkpoints import RejectedOracleRevision
    from app.domain.services.skills.skill_validation import SyntheticValidationPlan
    from app.domain.services.skills.skill_validation_review import ValidationPlanReview
    from test_skill_creation_pipeline import oracle_review_payload
    from test_skill_validation_plan import plan_payload
    plan = SyntheticValidationPlan.model_validate(plan_payload())
    review = oracle_review_payload(plan)
    review["scripts"][0]["support"]["status"] = "rejected"
    return RejectedOracleRevision(plan=plan, review=ValidationPlanReview.model_validate(review),
        contract_digest=content_digest({"contract": "current"}), interfaces_digest=content_digest({"interface": "current"}))


def execution_revision():
    from app.domain.services.skills.skill_creation.checkpoints import FailedExecutionRevision
    oracle = oracle_revision()
    return FailedExecutionRevision(recipe=SkillRecipe.model_validate(recipe_payload()), plan=oracle.plan,
        contract_digest=oracle.contract_digest, interfaces_digest=oracle.interfaces_digest,
        original_recipe_digest=content_digest(recipe_payload()),
        execution={"status": "failed", "scope": "synthetic", "cases": [
            {"id": "normal", "status": "failed", "reason": "assertion_mismatch",
             "checks": [{"passed": False, "expected": 2, "actual": 3}]}]})


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,factory", [("oracle", oracle_revision), ("execution", execution_revision)])
async def test_revision_storage_keeps_exact_frozen_plan_and_revalidates_every_read(kind, factory):
    cache, original, check = MemoryCache(), factory(), Mock()
    assert await getattr(builds(cache), f"save_{kind}_revision")(original, check=check)
    resumed = await getattr(builds(cache), f"get_{kind}_revision")(check=check)
    assert resumed == original and resumed is not original and check.call_count == 2
    # The stored synthetic expected values must never be mutated by a caller.
    resumed.plan.scripts[0].cases.clear()
    assert await getattr(builds(cache), f"get_{kind}_revision")() == original
    assert await getattr(builds(cache, owner_id="different"), f"get_{kind}_revision")() is None
    assert await getattr(builds(cache, source={"analysis": "changed"}), f"get_{kind}_revision")() is None
    if kind == "execution":
        with pytest.raises(SkillCreationError, match="合成用例执行"):
            await builds(cache).get_execution_revision(check=Mock(side_effect=ValueError("frozen contract changed")))
        assert cache.entries  # A corrupt frozen target cannot reset recovery.
    else:
        assert await builds(cache).get_oracle_revision(check=Mock(side_effect=ValueError("frozen contract changed"))) is None
        assert not cache.entries


@pytest.mark.asyncio
async def test_oracle_revision_requires_adverse_verdict_and_validates_private_reason():
    cache, revision = MemoryCache(), oracle_revision()
    revision.review.scripts[0].support.status = "accepted"
    with pytest.raises(ValueError, match="adverse"):
        await builds(cache).save_oracle_revision(revision)
    assert not cache.entries
    revision.review.scripts[0].support.status = "uncertain"
    assert await builds(cache).save_oracle_revision(revision)
    assert (await builds(cache).get_oracle_revision()).review.verdict() == "uncertain"
    private = "private-independent-review-marker"
    revision.review.scripts[0].support.explanation = private
    build = BuildCheckpoints(MemoryCache(), scope(), privacy=PrivacyContext((PrivateValue(private, "identity"),)))
    with pytest.raises(SkillPackageValidationError):
        await build.save_oracle_revision(revision)
    assert not build.cache.entries


@pytest.mark.asyncio
@pytest.mark.parametrize("status,scope_value", [("passed", "synthetic"), ("failed", "user_data"), ("unavailable", "synthetic")])
async def test_execution_checkpoint_cannot_store_unverified_success_or_real_analysis(status, scope_value):
    cache, revision = MemoryCache(), execution_revision()
    revision.execution.update(status=status, scope=scope_value)
    with pytest.raises(ValueError, match="failed synthetic"):
        await builds(cache).save_execution_revision(revision)
    assert not cache.entries


@pytest.mark.asyncio
async def test_execution_checkpoint_rejects_private_observed_values():
    cache, revision, private = MemoryCache(), execution_revision(), "private-actual-value-marker"
    revision.execution["cases"][0]["checks"][0]["actual"] = private
    build = BuildCheckpoints(cache, scope(), privacy=PrivacyContext((PrivateValue(private, "identity"),)))
    with pytest.raises(SkillPackageValidationError):
        await build.save_execution_revision(revision)
    assert not cache.entries


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["get", "set", "delete"])
async def test_stalled_optional_storage_is_bounded_and_disabled_for_attempt(operation):
    import asyncio
    cache = MemoryCache()
    async def stalled(*args, **kwargs):
        await asyncio.Event().wait()
    hanging = AsyncMock(side_effect=stalled)
    build = BuildCheckpoints(cache, scope(), cache_io_timeout_seconds=0.01)
    producer = AsyncMock(return_value=Artifact(content="Validated portable content."))
    async with asyncio.timeout(1):
        if operation == "delete":
            marker = build.mark()
            await generated(build, producer)
            cache.delete = hanging
            await build.discard_since(marker, stages={"workflow"})
        else:
            setattr(cache, operation, hanging)
            await generated(build, producer)
        assert not build._remote_cache_available
        # A failed remote cache is not probed at every later model stage.
        await generated(build, producer, stage="implementation")
        await generated(build, producer, stage="method_requirements")
        before = producer.await_count
        await generated(build, producer, stage="implementation")
        assert producer.await_count == before
    assert hanging.await_count == 1


@pytest.mark.asyncio
async def test_handled_write_failure_disables_remote_io_but_keeps_local_results():
    cache = MemoryCache()
    cache.get = AsyncMock(return_value=None)
    cache.set = AsyncMock(return_value=False)
    build = builds(cache)
    producer = AsyncMock(return_value=Artifact(content="Validated portable content."))
    await generated(build, producer)
    reads = cache.get.await_count
    await generated(build, producer)
    await generated(build, producer, stage="implementation")
    assert producer.await_count == 2 and cache.set.await_count == 1
    assert cache.get.await_count == reads and not build._remote_cache_available


@pytest.mark.asyncio
async def test_caller_cancellation_is_not_hidden_as_optional_cache_failure():
    import asyncio
    cache = MemoryCache()
    cache.get = AsyncMock(side_effect=asyncio.CancelledError())
    producer = AsyncMock(return_value=Artifact(content="Validated portable content."))
    with pytest.raises(asyncio.CancelledError):
        await generated(builds(cache), producer)
    producer.assert_not_awaited()
