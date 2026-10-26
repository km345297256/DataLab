"""Exact target review reuse must never become a new vote or a weaker oracle."""
from copy import deepcopy
import hashlib
import json

import pytest

from app.domain.services.skills.skill_creation_error import SkillCreationError, SkillPackageValidationError
from app.domain.services.skills.skill_oracle_reuse import (
    merge_reused_oracle_review, review_scopes_for_extension, review_targets_for_extension,
)
from app.domain.services.skills.skill_validation import SyntheticValidationPlan
from app.domain.services.skills.skill_validation_extension import (
    VerificationCoverageAmendment, apply_coverage_amendment,
)
from app.domain.services.skills.skill_validation_review import (
    ValidationPlanReview, review_targets, validate_oracle_review,
)
from test_skill_validation_extension import additions, new_case
from test_skill_validation_plan import plan_payload


CONTEXT = hashlib.sha256(b"frozen method, complete sources, public interfaces").hexdigest()
BINDING = {"baseline_context_digest": CONTEXT, "current_context_digest": CONTEXT}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def assessment(text):
    return {"status": "accepted", "explanation": text}


def reviewed(targets, label="original"):
    return ValidationPlanReview.model_validate({"scripts": [{
        "script_path": script["script_path"], "support": assessment(f"{label} support"),
        "cases": [{"case_id": case["case_id"],
            "input_validity": assessment(f"{label} input {case['case_id']}"),
            "exit_behavior": assessment(f"{label} exit {case['case_id']}"),
            "method_coverage": assessment(f"{label} coverage {case['case_id']}"),
            "assertions": [{"target_id": target["target_id"],
                "correctness": assessment(f"{label} {case['case_id']} {target['kind']} {target.get('field', '')}"),
                "tolerance": assessment(f"{label} {case['case_id']} {target['target_id']} exact tolerance")}
                for target in case["targets"]]} for case in script["cases"]]}
        for script in targets]})


def setup(amendment=None, payload=None):
    original = SyntheticValidationPlan.model_validate(payload or plan_payload())
    extended = apply_coverage_amendment(original, VerificationCoverageAmendment.model_validate(amendment or additions()))
    prior = reviewed(review_targets(original))
    return original, prior, extended


def pending_review(original, prior, extended):
    return reviewed(review_targets_for_extension(original, prior, extended, **BINDING), "fresh")


def test_reuse_saves_only_exact_accepted_targets_and_freshly_reviews_all_case_metadata():
    original, prior, extended = setup()
    # The old scientific target verdicts are accepted, but the original overall
    # decision is negative. Its case-level decision must not be copied forward.
    prior.scripts[0].cases[0].method_coverage.status = "rejected"
    prior.scripts[0].cases[0].method_coverage.explanation = "The exact output key set was not verified."
    old_before, new_before, review_before = (item.model_dump(mode="python") for item in (original, extended, prior))
    pending = review_targets_for_extension(original, prior, extended, **BINDING)
    assert [[target["target_id"] for target in case["targets"]] for case in pending[0]["cases"]] == [["T007"], ["T007"], ["T001"]]
    fresh = reviewed(pending, "fresh")
    merged, provenance = merge_reused_oracle_review(original, prior, extended, fresh, **BINDING)
    assert merged.verdict() == "accepted"
    assert prior.verdict() == "rejected"
    assert merged.scripts[0].support == fresh.scripts[0].support
    assert merged.scripts[0].cases[0].method_coverage == fresh.scripts[0].cases[0].method_coverage
    assert merged.scripts[0].cases[0].assertions[:6] == prior.scripts[0].cases[0].assertions
    assert merged.scripts[0].cases[0].assertions[-1] == fresh.scripts[0].cases[0].assertions[0]
    assert provenance["context_sha256"] == CONTEXT
    assert provenance["prior_review_sha256"] == digest(review_before)
    assert provenance["original_plan_sha256"] == digest(old_before)
    assert provenance["extended_plan_sha256"] == digest(new_before)
    assert provenance["merged_review_sha256"] == digest(merged.model_dump(mode="python"))
    assert provenance["extension_review_sha256"] == digest(fresh.model_dump(mode="python"))
    assert len(provenance["reused_targets"]) == 12
    assert len(provenance["reviewed_targets"]) == provenance["new_review_target_count"] == 3
    assert provenance["case_assessments_reused"] is False
    assert provenance["script_assessments_reused"] is False
    source = provenance["reused_targets"][0]
    assert source["source_verdicts"] == {"correctness": "accepted", "tolerance": "accepted"}
    assert source["source_assessment_sha256"] == digest(prior.scripts[0].cases[0].assertions[0].model_dump(mode="python"))
    assert original.model_dump(mode="python") == old_before
    assert extended.model_dump(mode="python") == new_before
    assert prior.model_dump(mode="python") == review_before
    merged.scripts[0].cases[0].assertions[0].correctness.explanation = "A mutation after merge"
    assert prior.model_dump(mode="python") == review_before
    assert fresh.scripts[0].cases[0].assertions[0].target_id == "T007"


@pytest.mark.parametrize("status", ["rejected", "uncertain"])
@pytest.mark.parametrize("assessment_name", ["correctness", "tolerance"])
def test_adverse_original_target_is_never_reasked_or_copied_as_accepted(status, assessment_name):
    original, prior, extended = setup()
    getattr(prior.scripts[0].cases[0].assertions[0], assessment_name).status = status
    for operation in (review_targets_for_extension, review_scopes_for_extension):
        with pytest.raises(SkillCreationError) as error:
            operation(original, prior, extended, **BINDING)
        assert error.value.code == f"validation_oracle_{status}"
    # Even a purported complete new accepted review cannot wash out old dissent.
    with pytest.raises(SkillCreationError):
        merge_reused_oracle_review(original, prior, extended, reviewed(review_targets(extended)), **BINDING)


@pytest.mark.parametrize("baseline,current", [
    ("", ""), ("not-a-sha", "not-a-sha"), ("a" * 63, "a" * 63),
    ("G" * 64, "G" * 64), (CONTEXT.upper(), CONTEXT.upper()),
    (CONTEXT, "f" * 64), (None, None), (CONTEXT, None),
])
def test_context_must_be_an_exact_bound_sha256(baseline, current):
    original, prior, extended = setup()
    with pytest.raises(SkillPackageValidationError):
        review_targets_for_extension(original, prior, extended,
            baseline_context_digest=baseline, current_context_digest=current)


@pytest.mark.parametrize("mutation", ["input", "value", "tolerance", "scalar_type", "exit", "source", "support", "order"])
def test_no_same_context_digest_can_authorize_a_changed_original_scientific_plan(mutation):
    original, prior, extended = setup()
    case = extended.scripts[0].cases[0]
    if mutation == "input":
        case.input_files[0].rows[0]["value"] = 999
    elif mutation == "value":
        case.assertions[0].fields["mean"].value = 999
    elif mutation == "tolerance":
        case.assertions[0].fields["mean"].atol = 1
    elif mutation == "scalar_type":
        case.assertions[0].fields["valid_count"].atol = 0
    elif mutation == "exit":
        case.expected_exit_code = 1
    elif mutation == "source":
        case.source_ids = ["S999"]
    elif mutation == "support":
        extended.scripts[0].support = "unsupported"
    else:
        case.assertions[:2] = reversed(case.assertions[:2])
    with pytest.raises(SkillPackageValidationError):
        review_targets_for_extension(original, prior, extended, **BINDING)


@pytest.mark.parametrize("part", ["script", "case", "target", "duplicate", "unknown"])
def test_prior_review_must_be_complete_exactly_once(part):
    original, prior, extended = setup()
    if part == "script":
        prior.scripts = []
    elif part == "case":
        prior.scripts[0].cases.pop()
    elif part == "target":
        prior.scripts[0].cases[0].assertions.pop()
    elif part == "duplicate":
        prior.scripts[0].cases[0].assertions.append(prior.scripts[0].cases[0].assertions[0].model_copy(deep=True))
    else:
        prior.scripts[0].cases[0].assertions[0].target_id = "T999"
    with pytest.raises(SkillPackageValidationError):
        review_targets_for_extension(original, prior, extended, **BINDING)


def test_cases_without_new_targets_still_require_fresh_case_review_and_keep_original_ids():
    original, prior, extended = setup({"assertion_additions": [additions()["assertion_additions"][0]]})
    pending = review_targets_for_extension(original, prior, extended, **BINDING)
    assert [case["targets"] for case in pending[0]["cases"]][1:] == [[], []]
    scopes = review_scopes_for_extension(original, prior, extended, **BINDING)
    assert scopes == [
        {"script_path": "scripts/group_summary.py", "case_ids": ["group-normal"], "target_ids": ["T007"]},
        {"script_path": "scripts/group_summary.py", "case_ids": ["group-boundary"], "target_ids": []},
        {"script_path": "scripts/group_summary.py", "case_ids": ["group-invalid"], "target_ids": []}]
    fresh = reviewed(pending, "fresh")
    fresh.scripts[0].cases[1].method_coverage.status = "rejected"
    merged, _ = merge_reused_oracle_review(original, prior, extended, fresh, **BINDING)
    assert merged.verdict() == "rejected"
    assert merged.scripts[0].cases[1].method_coverage == fresh.scripts[0].cases[1].method_coverage
    fresh.scripts[0].cases.pop()
    with pytest.raises(SkillPackageValidationError):
        merge_reused_oracle_review(original, prior, extended, fresh, **BINDING)


@pytest.mark.parametrize("damage", ["extra_old_target", "missing_new_target", "unknown_target", "duplicate_target",
    "missing_case", "duplicate_case", "missing_script", "duplicate_script", "wrong_case"])
def test_extension_review_can_neither_omit_obligations_nor_revote_old_targets(damage):
    original, prior, extended = setup()
    fresh = pending_review(original, prior, extended)
    if damage == "extra_old_target":
        fresh.scripts[0].cases[0].assertions.append(prior.scripts[0].cases[0].assertions[0].model_copy(deep=True))
    elif damage == "missing_new_target":
        fresh.scripts[0].cases[0].assertions.clear()
    elif damage == "unknown_target":
        fresh.scripts[0].cases[0].assertions[0].target_id = "T999"
    elif damage == "duplicate_target":
        fresh.scripts[0].cases[0].assertions *= 2
    elif damage == "missing_case":
        fresh.scripts[0].cases.pop()
    elif damage == "duplicate_case":
        fresh.scripts[0].cases.append(fresh.scripts[0].cases[0].model_copy(deep=True))
    elif damage == "missing_script":
        fresh.scripts.clear()
    elif damage == "duplicate_script":
        fresh.scripts.append(fresh.scripts[0].model_copy(deep=True))
    else:
        fresh.scripts[0].cases[0].case_id = "some-other-case"
    with pytest.raises(SkillPackageValidationError):
        merge_reused_oracle_review(original, prior, extended, fresh, **BINDING)


def test_semantic_mapping_preserves_field_assessment_when_dictionary_insertion_order_changes():
    original, prior, extended = setup()
    old_case = original.scripts[0].cases[0]
    new_case = extended.scripts[0].cases[0]
    new_case.assertions[0].fields = dict(reversed(list(new_case.assertions[0].fields.items())))
    pending = pending_review(original, prior, extended)
    merged, provenance = merge_reused_oracle_review(original, prior, extended, pending, **BINDING)
    old_fields = {target["field"]: prior.scripts[0].cases[0].assertions[int(target["target_id"][1:]) - 1]
        for target in review_targets(original)[0]["cases"][0]["targets"] if target["kind"] == "csv_field"}
    for target in review_targets(extended)[0]["cases"][0]["targets"]:
        if target["kind"] == "csv_field":
            actual = merged.scripts[0].cases[0].assertions[int(target["target_id"][1:]) - 1]
            assert actual.correctness == old_fields[target["field"]].correctness
            assert actual.tolerance == old_fields[target["field"]].tolerance
    mapping = {target["target_id"]: target["source_target_id"] for target in provenance["reused_targets"]
        if target["case_id"] == old_case.id}
    assert mapping["T002"] == "T005" and mapping["T005"] == "T002"


def test_new_rejection_cases_are_fully_fresh_and_do_not_borrow_another_cases_target_id():
    amendment = additions()
    amendment["new_cases"] = [new_case()]
    original, prior, extended = setup(amendment)
    pending = review_targets_for_extension(original, prior, extended, **BINDING)
    assert pending[0]["cases"][-1] == {"case_id": "group-invalid-extra", "targets": [
        {"target_id": "T001", "kind": "output_empty", "assertion_index": 0,
         "assertion_type": "output_empty", "expected": True,
         "observation_scope": "isolated_output_directory", "reads_scientific_output": False}]}
    fresh = reviewed(pending, "fresh")
    fresh.scripts[0].cases[-1].assertions[0].correctness.status = "uncertain"
    merged, provenance = merge_reused_oracle_review(original, prior, extended, fresh, **BINDING)
    assert merged.verdict() == "uncertain"
    assert not any(item["case_id"] == "group-invalid-extra" for item in provenance["reused_targets"])
    assert any(item["case_id"] == "group-invalid-extra" for item in provenance["reviewed_targets"])


def test_unsupported_script_still_gets_a_fresh_support_decision():
    payload = plan_payload()
    payload["scripts"].append({"script_path": "scripts/other.py", "support": "unsupported",
        "reason": "This interface has no supported observable test data.", "cases": []})
    original, prior, extended = setup(payload=payload)
    fresh = pending_review(original, prior, extended)
    fresh.scripts[1].support.status = "uncertain"
    merged, _ = merge_reused_oracle_review(original, prior, extended, fresh, **BINDING)
    assert merged.verdict() == "uncertain"
    assert review_scopes_for_extension(original, prior, extended, **BINDING)[-1] == {
        "script_path": "scripts/other.py", "case_ids": [], "target_ids": []}


def test_large_extension_partitions_only_new_targets_without_renumbering_them():
    amendment = {"assertion_additions": [{"script_path": "scripts/group_summary.py", "case_id": "group-normal",
        "assertion": {"type": "json_object_keys", "path": f"shape_{index:03}.json", "pointer": "", "keys": []}}
        for index in range(130)]}
    original, prior, extended = setup(amendment)
    scopes = review_scopes_for_extension(original, prior, extended, **BINDING)
    normal = [scope for scope in scopes if scope["case_ids"] == ["group-normal"]]
    assert [len(scope["target_ids"]) for scope in normal] == [64, 64, 2]
    assert sorted(target for scope in normal for target in scope["target_ids"]) == [f"T{n:03}" for n in range(7, 137)]
    fresh = pending_review(original, prior, extended)
    merged, provenance = merge_reused_oracle_review(original, prior, extended, fresh, **BINDING)
    validate_oracle_review(merged, extended)
    assert provenance["new_review_target_count"] == 130


def test_noop_extension_cannot_be_used_to_revote_case_coverage():
    original, prior, _ = setup()
    with pytest.raises(SkillPackageValidationError) as error:
        review_targets_for_extension(original, prior, original.model_copy(deep=True), **BINDING)
    assert error.value.code == "validation_oracle_revision_unchanged"
