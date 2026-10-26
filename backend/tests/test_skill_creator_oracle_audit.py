"""The acceptance audit must bind reuse to actual earlier review calls."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from acceptance.skill_creator_oracle_audit import OracleReviewAudit, digest, reuse_targets
from acceptance.skill_creator_real_model import assert_oracle_isolation, assert_complex_coverage_extensions
from app.domain.services.skills.skill_oracle_reuse import merge_reused_oracle_review, review_targets_for_extension
from app.domain.services.skills.skill_creation_error import SkillCreationError, SkillPackageValidationError
from app.domain.services.skills.skill_validation import SyntheticValidationPlan, build_validation_generation_payload
from app.domain.services.skills.skill_validation_extension import VerificationCoverageAmendment, apply_coverage_amendment
from app.domain.services.skills.skill_validation_review import review_targets, coverage_revision_cases
from test_skill_oracle_reuse import reviewed
from test_skill_validation_extension import additions
from test_skill_validation_plan import plan_payload, method_contract, sources, interfaces


def fixture():
    original = SyntheticValidationPlan.model_validate(plan_payload())
    original = SyntheticValidationPlan.model_validate_json(original.model_dump_json())
    prior = reviewed(review_targets(original))
    prior.scripts[0].cases[0].method_coverage.status = "rejected"
    prior.scripts[0].cases[0].method_coverage.explanation = "The original exact report-key contract has no observable check."
    context = build_validation_generation_payload(method_contract(), sources(), interfaces())
    extended = apply_coverage_amendment(original, VerificationCoverageAmendment.model_validate(additions()))
    context_digest = digest(context)
    binding = {"baseline_context_digest": context_digest, "current_context_digest": context_digest}
    pending = review_targets_for_extension(original, prior, extended, **binding)
    audit = OracleReviewAudit()
    audit.record(original, context, prior)
    amendment = {"stage": "synthetic_validation_plan", "schema": VerificationCoverageAmendment.model_json_schema(),
        **deepcopy(context), "previous_validation_plan": original.model_dump(mode="json"),
        "validation_review": prior.model_dump(mode="json"),
        "eligible_cases": [{"script_path": original.scripts[0].script_path, "case_id": "group-invalid"}],
        "required_cases": [{"script_path": path, "case_id": case_id} for path, case_id in coverage_revision_cases(original, prior)]}
    request = {"stage": "synthetic_plan_review", "schema": {}, **deepcopy(context),
        "validation_plan": extended.model_dump(mode="json"), "review_targets": pending,
        "oracle_reuse": {"original_plan_sha256": digest(original.model_dump(mode="python")),
            "prior_review_sha256": digest(prior.model_dump(mode="python")), "context_sha256": context_digest,
            "reused_target_count": 12, "new_review_target_count": 3, "case_assessments_reused": False}}
    return audit, original, prior, extended, context, binding, amendment, request


def test_actual_observed_negative_review_is_required_before_a_reduced_request():
    audit, _, _, _, _, _, amendment, request = fixture()
    assert_oracle_isolation(amendment["stage"], amendment)
    audit.baseline_for(amendment["stage"], amendment)
    baseline = audit.baseline_for(request["stage"], request)
    assert_oracle_isolation(request["stage"], request, reuse_baseline=baseline)
    assert baseline["review"].verdict() == "rejected"
    assert all(target["target_id"] in {"T007", "T001"} for case in request["review_targets"][0]["cases"] for target in case["targets"])


def test_reduced_request_cannot_authorize_itself_without_observed_history():
    _, _, _, _, _, _, _, request = fixture()
    with pytest.raises(AssertionError, match="complete prior"):
        assert_oracle_isolation(request["stage"], request)
    with pytest.raises(AssertionError, match="real prior"):
        OracleReviewAudit().baseline_for(request["stage"], request)


@pytest.mark.parametrize("change", ["verdict", "input", "context", "no_history"])
def test_amendment_must_match_the_actual_earlier_review_including_negative_values(change):
    audit, _, _, _, _, _, amendment, _ = fixture()
    if change == "verdict":
        amendment["validation_review"]["scripts"][0]["cases"][0]["method_coverage"]["status"] = "accepted"
    elif change == "input":
        amendment["previous_validation_plan"]["scripts"][0]["cases"][0]["input_files"][0]["rows"][0]["value"] = 99
    elif change == "context":
        amendment["sources"][0]["text"] = "A different method."
    else:
        audit.records = []
    with pytest.raises(AssertionError, match="actually observed"):
        audit.baseline_for(amendment["stage"], amendment)


@pytest.mark.parametrize("field", ["original_plan_sha256", "prior_review_sha256", "context_sha256",
    "reused_target_count", "new_review_target_count", "case_assessments_reused", "extra"])
def test_every_reuse_summary_field_is_recomputed_from_the_observed_plan_and_review(field):
    audit, _, _, _, _, _, amendment, request = fixture()
    audit.baseline_for(amendment["stage"], amendment)
    request["oracle_reuse"][field] = "forged"
    with pytest.raises(AssertionError):
        assert_oracle_isolation(request["stage"], request, reuse_baseline=audit.baseline)


@pytest.mark.parametrize("change", ["extra_old", "omit_new", "omit_case", "changed_context"])
def test_pending_targets_must_match_the_bound_extension_exactly(change):
    audit, _, prior, _, _, _, amendment, request = fixture()
    audit.baseline_for(amendment["stage"], amendment)
    if change == "extra_old":
        request["review_targets"][0]["cases"][0]["targets"].append({"target_id": prior.scripts[0].cases[0].assertions[0].target_id})
    elif change == "omit_new":
        request["review_targets"][0]["cases"][0]["targets"].clear()
    elif change == "omit_case":
        request["review_targets"][0]["cases"].pop()
    else:
        request["sources"][0]["text"] = "Changed source"
    with pytest.raises(AssertionError):
        assert_oracle_isolation(request["stage"], request, reuse_baseline=audit.baseline)


def test_final_receipt_must_merge_actual_fresh_assessments_and_exact_prior_verdicts():
    audit, original, prior, extended, context, binding, amendment, request = fixture()
    audit.baseline_for(amendment["stage"], amendment)
    fresh = reviewed(request["review_targets"], "fresh")
    merged, provenance = merge_reused_oracle_review(original, prior, extended, fresh, **binding)
    receipt = {"review": merged.model_dump(mode="json"), "reused_target_reviews": [provenance]}
    with pytest.raises(AssertionError, match="real newly produced"):
        audit.assert_receipt(extended, receipt)
    audit.record(extended, context, fresh, request["review_targets"])
    assert audit.assert_receipt(extended, receipt) == provenance
    receipt["review"]["scripts"][0]["cases"][0]["assertions"][0]["correctness"]["explanation"] = "A replaced derivation"
    with pytest.raises(AssertionError):
        audit.assert_receipt(extended, receipt)


def test_new_provenance_cannot_mask_a_new_negative_case_verdict():
    audit, original, prior, extended, context, binding, amendment, request = fixture()
    audit.baseline_for(amendment["stage"], amendment)
    fresh = reviewed(request["review_targets"], "fresh")
    fresh.scripts[0].cases[1].method_coverage.status = "uncertain"
    audit.record(extended, context, fresh, request["review_targets"])
    merged, provenance = merge_reused_oracle_review(original, prior, extended, fresh, **binding)
    with pytest.raises(AssertionError):
        audit.assert_receipt(extended, {"review": merged.model_dump(mode="json"), "reused_target_reviews": [provenance]})


@pytest.mark.parametrize("damage", [None, "prior_hash", "context", "no_fresh", "no_prior", "negative"])
def test_fully_cached_receipt_still_requires_both_actual_typed_reviews(damage):
    audit, original, prior, extended, context, binding, _, request = fixture()
    fresh = reviewed(request["review_targets"], "real cached fresh review")
    if damage == "negative":
        fresh.scripts[0].cases[0].method_coverage.status = "rejected"
    merged, provenance = merge_reused_oracle_review(original, prior, extended, fresh, **binding)
    receipt = {"review": merged.model_dump(mode="json"), "reused_target_reviews": [provenance]}
    if damage == "no_prior":
        audit.records.clear()
    if damage == "context":
        context["sources"][0]["text"] = "A changed scientific method."
    if damage != "no_fresh":
        audit.record(extended, context, fresh, request["review_targets"])
    if damage == "prior_hash":
        receipt["reused_target_reviews"][0]["prior_review_sha256"] = "a" * 64
    assert audit.baseline is None
    if damage is None:
        assert audit.assert_receipt(extended, receipt) == provenance
        assert audit.baseline["review"].verdict() == "rejected"
    else:
        with pytest.raises(AssertionError):
            audit.assert_receipt(extended, receipt)
        assert audit.baseline is None


def test_known_complex_structure_gaps_require_real_checks_without_changing_fixed_fixtures():
    from acceptance.skill_creator_real_model import SCENARIOS
    path = Path(__file__).parent / "fixtures/skill_creator_postcondition_revision/plan.json"
    original = SyntheticValidationPlan.model_validate_json(path.read_text())
    before = original.model_dump_json()
    with pytest.raises(AssertionError, match="report-key"):
        assert_complex_coverage_extensions(original)
    extra = []
    for script in original.scripts:
        for case in script.cases:
            if case.expected_exit_code != 0:
                continue
            for output in {item.path for item in case.assertions if item.type == "json_value"}:
                extra.append({"script_path": script.script_path, "case_id": case.id, "assertion": {
                    "type": "json_object_keys", "path": output, "pointer": "", "keys": list(SCENARIOS["complex"].observed)}})
            if any(item.format == "json" and item.data.get("long_rows") == [] for item in case.input_files):
                for output in {item.path for item in case.assertions if item.type == "csv_rows"}:
                    extra.append({"script_path": script.script_path, "case_id": case.id,
                        "assertion": {"type": "csv_header", "path": output, "columns": ["id"]}})
    extended = apply_coverage_amendment(original, VerificationCoverageAmendment.model_validate({"assertion_additions": extra}))
    assert_complex_coverage_extensions(extended)
    no_header = extended.model_copy(deep=True)
    for script in no_header.scripts:
        for case in script.cases:
            case.assertions = [item for item in case.assertions if item.type != "csv_header"]
    with pytest.raises(AssertionError, match="empty-table header"):
        assert_complex_coverage_extensions(no_header)
    extra_key = extended.model_copy(deep=True)
    next(item for item in extra_key.scripts[0].cases[0].assertions if item.type == "json_object_keys").keys.append("unrequested")
    with pytest.raises(AssertionError, match="report-key"):
        assert_complex_coverage_extensions(extra_key)
    focused = extended.model_copy(deep=True)
    probe = focused.scripts[0].cases[0].model_copy(deep=True)
    probe.id = "additional-focused-null-probe"
    probe.kind = "boundary"
    probe.assertions = [next(item for item in probe.assertions if item.type == "json_value")]
    focused.scripts[0].cases.append(probe)
    # A focused new probe must not erase the original observable checks, but
    # does not have to repeat all unrelated outputs in every new input case.
    assert_complex_coverage_extensions(focused)
    for case_id in ("normal_full_methods", "boundary_edges_and_overrides"):
        missing = focused.model_copy(deep=True)
        missing.scripts[0].cases = [case for case in missing.scripts[0].cases if case.id != case_id]
        with pytest.raises(AssertionError, match="original complex structure"):
            assert_complex_coverage_extensions(missing)
    assert original.model_dump_json() == before


@pytest.mark.asyncio
async def test_saved_negative_recovery_uses_production_scope_and_keeps_exact_typed_review():
    from unittest.mock import Mock
    from acceptance.skill_creator_checkpoint_state import ArtificialCheckpointCache
    from app.domain.services.skills.skill_creation.checkpoints import BuildCheckpoints, content_digest
    from test_skill_creation_checkpoints import scope
    _, original, prior, _, context, _, amendment, request = fixture()
    cache = ArtificialCheckpointCache("complex")
    first = BuildCheckpoints(cache, scope())
    saved = {"plan": original, "review": prior,
        "contract_digest": content_digest(context["method_contract"]),
        "interfaces_digest": content_digest(context["script_interfaces"])}
    await first.save_oracle_revision(saved)
    restored = BuildCheckpoints(ArtificialCheckpointCache("complex", cache.snapshot()), scope())
    audit, check = OracleReviewAudit(), Mock()
    value = await audit.load_validated_revision(restored.get_oracle_revision, context, check=check)
    assert check.call_count == 1
    assert value.review.model_dump(mode="python") == prior.model_dump(mode="python")
    assert audit.records[-1]["origin"] == "production_revalidated_checkpoint"
    # A production cache hit for the amendment has no model request to observe.
    # The restored full review must still bind the reduced next request.
    baseline = audit.baseline_for(request["stage"], request)
    assert_oracle_isolation(request["stage"], request, reuse_baseline=baseline)
    assert audit.baseline["review"].verdict() == "rejected"


@pytest.mark.parametrize("damage", ["plan_hash", "review_hash", "context_hash", "context",
    "partial_history", "count", "old_value", "negative_target"])
def test_cached_amendment_binding_requires_exact_observed_review_and_unchanged_targets(damage):
    audit, _, _, _, _, _, _, request = fixture()
    if damage in {"plan_hash", "review_hash", "context_hash"}:
        key = {"plan_hash": "original_plan_sha256", "review_hash": "prior_review_sha256",
               "context_hash": "context_sha256"}[damage]
        request["oracle_reuse"][key] = "f" * 64
    elif damage == "context":
        request["sources"][0]["text"] = "A different scientific method."
    elif damage == "partial_history":
        audit.records[0]["full"] = False
    elif damage == "count":
        request["oracle_reuse"]["new_review_target_count"] += 1
    elif damage == "old_value":
        request["validation_plan"]["scripts"][0]["cases"][0]["input_files"][0]["rows"][0]["value"] = 99
    else:
        prior = audit.records[0]["review"]
        prior.scripts[0].cases[0].assertions[0].correctness.status = "rejected"
        request["oracle_reuse"]["prior_review_sha256"] = digest(prior.model_dump(mode="python"))
    with pytest.raises((AssertionError, SkillCreationError, SkillPackageValidationError)):
        audit.baseline_for(request["stage"], request)
    assert audit.baseline is None


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["owner", "code", "validator", "context", "no_check"])
async def test_saved_oracle_cannot_become_a_baseline_without_current_production_validation(damage):
    from unittest.mock import Mock
    from acceptance.skill_creator_checkpoint_state import ArtificialCheckpointCache
    from app.domain.services.skills.skill_creation.checkpoints import BuildCheckpoints, content_digest
    from test_skill_creation_checkpoints import scope
    _, original, prior, _, context, _, _, _ = fixture()
    cache = ArtificialCheckpointCache("complex")
    await BuildCheckpoints(cache, scope()).save_oracle_revision({"plan": original, "review": prior,
        "contract_digest": content_digest(context["method_contract"]),
        "interfaces_digest": content_digest(context["script_interfaces"])})
    changed = {"owner_id": "other"} if damage == "owner" else {"code_version": "new"} if damage == "code" else {}
    restored = BuildCheckpoints(ArtificialCheckpointCache("complex", cache.snapshot()), scope(**changed))
    audit, check = OracleReviewAudit(), Mock()
    if damage == "validator":
        check.side_effect = ValueError("current contract rejected cached record")
    if damage == "context":
        context["method_contract"]["requirements"][0]["statement"] = "Changed scientific method."
    if damage in {"context", "no_check"}:
        with pytest.raises(AssertionError):
            await audit.load_validated_revision(restored.get_oracle_revision, context,
                                               **({} if damage == "no_check" else {"check": check}))
    else:
        assert await audit.load_validated_revision(restored.get_oracle_revision, context, check=check) is None
    assert audit.records == [] and audit.baseline is None
