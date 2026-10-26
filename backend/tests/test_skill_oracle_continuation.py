"""Owned retries correct the last oracle; they never re-vote unchanged science."""
import copy

import pytest

from app.domain.services.skills.skill_creation_error import SkillCreationError, SkillPackageValidationError
from app.domain.services.skills.skill_validation_review import (
    oracle_decision_identity, review_validation_plan, validate_plan_revision,
)
from test_skill_validation_plan import interfaces, method_contract, sources
from test_skill_validation_review import generation, review_for, typed


class RevisionStore:
    def __init__(self, plan=None, review=None):
        self.state = None if plan is None else {
            "plan": plan.model_dump(mode="json"), "review": review}
        self.saved = []

    async def load(self):
        return copy.deepcopy(self.state)

    async def save(self, plan, review):
        self.state = {"plan": plan.model_dump(mode="json"), "review": review.model_dump(mode="json")}
        self.saved.append(copy.deepcopy(self.state))


def correction(value):
    plan = typed()
    plan.scripts[0].cases[0].assertions[0].fields["mean"].value = value
    return plan


async def run(plan, generate, store):
    return await review_validation_plan(plan, method_contract(), sources(), interfaces(), generate=generate,
                                        load_revision=store.load, save_revision=store.save)


@pytest.mark.asyncio
async def test_retry_corrects_latest_rejected_plan_before_any_new_review():
    first, second, third = typed(), correction(4), correction(5)
    store = RevisionStore()
    generate, calls = generation([review_for(first, "rejected"), second.model_dump(mode="json"),
                                  review_for(second, "rejected")])
    with pytest.raises(SkillCreationError) as failure:
        await run(first, generate, store)
    assert failure.value.code == "validation_oracle_rejected"
    assert len(store.saved) == 2
    assert store.state["plan"] == second.model_dump(mode="json")
    assert [call["stage"] for call in calls] == [
        "synthetic_plan_review", "synthetic_validation_plan", "synthetic_plan_review"]

    generate, calls = generation([third.model_dump(mode="json"), review_for(third)])
    result, receipt = await run(first, generate, store)
    assert result == third and receipt["revision_count"] == 1
    assert [call["stage"] for call in calls] == ["synthetic_validation_plan", "synthetic_plan_review"]
    assert calls[0]["payload"]["previous_validation_plan"] == second.model_dump(mode="json")
    assert calls[0]["payload"]["validation_review"] == store.state["review"]
    assert "observed_validation" not in str(calls) and "implementation" not in str(calls)


@pytest.mark.asyncio
async def test_resumed_rejection_preserves_newest_candidate_for_next_user_retry():
    first, second = typed(), correction(4)
    store = RevisionStore(first, review_for(first, "rejected"))
    generate, calls = generation([second.model_dump(mode="json"), review_for(second, "rejected")])
    with pytest.raises(SkillCreationError) as failure:
        await run(first, generate, store)
    assert failure.value.code == "validation_oracle_rejected"
    assert len(calls) == 2 and len(store.saved) == 1
    assert store.state["plan"] == second.model_dump(mode="json")


@pytest.mark.asyncio
async def test_transport_failure_during_revision_preserves_last_reviewed_state():
    original = typed()
    store = RevisionStore(original, review_for(original, "rejected"))
    frozen = copy.deepcopy(store.state)
    async def unavailable(*args, **kwargs):
        raise SkillCreationError("synthetic_validation_plan", "model_unavailable")
    with pytest.raises(SkillCreationError) as failure:
        await run(original, unavailable, store)
    assert failure.value.code == "model_unavailable"
    assert store.state == frozen and store.saved == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["none", "description", "explanation", "reason", "coverage"])
async def test_same_or_metadata_only_oracle_cannot_be_reviewed_again(change):
    original = typed()
    store = RevisionStore(original, review_for(original, "rejected"))
    revised = original.model_copy(deep=True)
    case = revised.scripts[0].cases[0]
    if change == "description":
        case.description += " Updated description only."
    elif change == "explanation":
        case.oracle_explanation += " A different explanation for the same answer."
    elif change == "reason":
        revised.scripts[0].reason += " Same supported computation."
    elif change == "coverage":
        case.requirement_ids = list(reversed(case.requirement_ids))
    generate, calls = generation([revised.model_dump(mode="json")])
    with pytest.raises(SkillPackageValidationError, match="validation_oracle_revision_unchanged"):
        await run(original, generate, store)
    assert [call["stage"] for call in calls] == ["synthetic_validation_plan"]
    assert store.saved == []


@pytest.mark.asyncio
async def test_uncertain_decision_survives_retry_without_another_model_call():
    original = typed()
    store = RevisionStore()
    generate, calls = generation([review_for(original, "uncertain")])
    with pytest.raises(SkillCreationError) as failure:
        await run(original, generate, store)
    assert failure.value.code == "validation_oracle_uncertain"
    assert len(calls) == 1 and len(store.saved) == 1
    generate, calls = generation([])
    with pytest.raises(SkillCreationError) as failure:
        await run(original, generate, store)
    assert failure.value.code == "validation_oracle_uncertain"
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["accepted", "missing_target", "missing_requirement"])
async def test_invalid_saved_state_is_not_a_scientific_authority(corruption):
    original = typed()
    review = review_for(original, "accepted" if corruption == "accepted" else "rejected")
    store = RevisionStore(original, review)
    if corruption == "missing_target":
        store.state["review"]["scripts"][0]["cases"][0]["assertions"].pop()
    elif corruption == "missing_requirement":
        store.state["plan"]["scripts"][0]["cases"][0]["requirement_ids"] = ["R999"]
    generate, calls = generation([])
    with pytest.raises(SkillPackageValidationError):
        await run(original, generate, store)
    assert calls == []


@pytest.mark.parametrize("change", ["input", "argv", "target", "tolerance", "kind", "coverage"])
@pytest.mark.asyncio
async def test_resumed_revision_cannot_weaken_or_change_original_question(change):
    original = typed()
    revised = correction(4)
    case = revised.scripts[0].cases[0]
    if change == "input":
        case.input_files[0].rows[0]["value"] = 17
    elif change == "argv":
        case.argv[-1] = "{output}/other.csv"
    elif change == "target":
        case.assertions[1].pointer = "/easier_target"
    elif change == "tolerance":
        case.assertions[0].fields["mean"].atol = 1
    elif change == "kind":
        case.kind = "boundary"
        revised.scripts[0].cases[1].kind = "normal"
    elif change == "coverage":
        case.requirement_ids = ["R999"]
    store = RevisionStore(original, review_for(original, "rejected"))
    generate, calls = generation([revised.model_dump(mode="json")])
    with pytest.raises(SkillPackageValidationError):
        await run(original, generate, store)
    assert len(calls) == 1 and store.saved == []


def test_semantic_identity_ignores_prose_and_assertion_order_but_keeps_coverage():
    original = typed()
    revised = original.model_copy(deep=True)
    revised.scripts[0].reason += " Cosmetic wording."
    revised.scripts[0].cases.reverse()
    for case in revised.scripts[0].cases:
        case.description += " Presentation changed."
        case.oracle_explanation += " Same evidence restated."
        case.assertions.reverse()
    assert oracle_decision_identity(original) == oracle_decision_identity(revised)
    revised.scripts[0].cases[0].source_ids.append("S002")
    assert oracle_decision_identity(original) != oracle_decision_identity(revised)


@pytest.mark.parametrize("change", ["input_type", "expected", "tolerance", "argv", "support", "kind", "coverage"])
def test_semantic_identity_preserves_every_scientifically_observable_field(change):
    original = typed()
    revised = original.model_copy(deep=True)
    case = revised.scripts[0].cases[0]
    if change == "input_type":
        case.input_files[0].rows[0]["value"] = True
    elif change == "expected":
        case.assertions[0].fields["mean"].value = 99
    elif change == "tolerance":
        case.assertions[0].fields["mean"].atol = 0.5
    elif change == "argv":
        case.argv[-1] = "{output}/other.csv"
    elif change == "support":
        revised.scripts[0].support = "unsupported"
        revised.scripts[0].cases = []
    elif change == "kind":
        case.kind = "boundary"
        revised.scripts[0].cases[1].kind = "normal"
    else:
        case.requirement_ids.append("R002")
    assert oracle_decision_identity(original) != oracle_decision_identity(revised)


def test_duplicate_assertion_only_is_not_substantive_revision():
    original = typed()
    revised = original.model_copy(deep=True)
    revised.scripts[0].cases[0].assertions.append(copy.deepcopy(revised.scripts[0].cases[0].assertions[0]))
    assert oracle_decision_identity(original) == oracle_decision_identity(revised)
    with pytest.raises(SkillPackageValidationError, match="validation_oracle_revision_unchanged"):
        validate_plan_revision(original, revised)
