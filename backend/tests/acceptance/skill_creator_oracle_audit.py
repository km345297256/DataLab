"""Test-only provenance audit for reviews actually produced by the creator.

This captures production calls without replacing a verdict, a plan, or an
execution. Reuse summaries cannot authorize themselves: they must match a
complete earlier typed review observed in this acceptance run.
"""
from __future__ import annotations

import copy
import hashlib
import json

from app.domain.services.skills.skill_oracle_reuse import (
    merge_reused_oracle_review, review_targets_for_extension,
)
from app.domain.services.skills.skill_validation import SyntheticValidationPlan
from app.domain.services.skills.skill_validation_review import _validate_review_targets, review_targets


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def method_context(request):
    return {key: copy.deepcopy(request[key]) for key in ("method_contract", "sources", "script_interfaces")}


def reuse_targets(request, baseline):
    assert baseline is not None, "Reuse requires an observed complete prior production review"
    original, prior, context = baseline["plan"], baseline["review"], baseline["context"]
    current = method_context(request)
    assert canonical(current) == canonical(context), "Methods, sources or public interfaces changed during reuse"
    context_digest = digest(context)
    revised = SyntheticValidationPlan.model_validate(request["validation_plan"])
    pending = review_targets_for_extension(original, prior, revised,
        baseline_context_digest=context_digest, current_context_digest=digest(current))
    expected = {"original_plan_sha256": digest(original.model_dump(mode="python")),
        "prior_review_sha256": digest(prior.model_dump(mode="python")),
        "context_sha256": context_digest,
        "reused_target_count": sum(len(case["targets"]) for script in review_targets(original) for case in script["cases"]),
        "new_review_target_count": sum(len(case["targets"]) for script in pending for case in script["cases"]),
        "case_assessments_reused": False}
    assert canonical(request["oracle_reuse"]) == canonical(expected), "Reuse summary differs from observed review provenance"
    return pending


class OracleReviewAudit:
    def __init__(self):
        self.records = []
        self.baseline = None

    def record(self, plan, context, review, targets=None):
        target_list = review_targets(plan) if targets is None else targets
        _validate_review_targets(review, target_list)
        self.records.append({"plan": plan.model_copy(deep=True), "context": method_context(context),
            "review": review.model_copy(deep=True), "targets": copy.deepcopy(target_list),
            "full": targets is None})

    async def load_validated_revision(self, production_loader, context, **options):
        """Observe only a real saved negative after production revalidation.

        The production loader retains ownership, TTL, code/config scope,
        privacy and current-domain checks. A missing/rejected cache entry never
        authorizes an audit baseline or manufactures a review.
        """
        assert callable(options.get("check")), "Restored oracle needs the real current-domain validator"
        value = await production_loader(**options)
        if value is None:
            return None
        from app.domain.services.skills.method_contract import MethodContract
        from app.domain.services.skills.skill_creation.checkpoints import content_digest
        from app.domain.services.skills.skill_validation import validate_validation_plan
        from app.domain.services.skills.skill_validation_review import validate_oracle_review
        assert context is not None, "Saved oracle must be bound to the current reference context"
        context = method_context(context)
        assert value.contract_digest == content_digest(context["method_contract"])
        assert value.interfaces_digest == content_digest(context["script_interfaces"])
        assert value.review.verdict() in {"rejected", "uncertain"}
        validate_validation_plan(value.plan, MethodContract.model_validate(context["method_contract"]),
                                 context["sources"], context["script_interfaces"])
        validate_oracle_review(value.review, value.plan)
        self.record(value.plan, context, value.review)
        self.records[-1]["origin"] = "production_revalidated_checkpoint"
        return value

    def baseline_for(self, stage, request):
        if stage == "synthetic_validation_plan" and "eligible_cases" in request:
            matches = [record for record in self.records if record["full"] and
                canonical(record["plan"].model_dump(mode="json")) == canonical(request["previous_validation_plan"]) and
                canonical(record["review"].model_dump(mode="json")) == canonical(request["validation_review"]) and
                canonical(record["context"]) == canonical(method_context(request))]
            assert matches, "Amendment must preserve a complete review actually observed in this run"
            self.baseline = copy.deepcopy(matches[-1])
        if "oracle_reuse" in request:
            # A real cached amendment bypasses model invocation, so its request
            # is not observed here. Bind only to a complete review already
            # observed/revalidated by production in this run, never to a
            # self-declared review supplied by the new request.
            summary = request["oracle_reuse"]
            context = method_context(request)
            matches = [record for record in self.records if record["full"] and
                digest(record["plan"].model_dump(mode="python")) == summary["original_plan_sha256"] and
                digest(record["review"].model_dump(mode="python")) == summary["prior_review_sha256"] and
                digest(record["context"]) == summary["context_sha256"] and
                canonical(record["context"]) == canonical(context)]
            assert matches, "Reuse cannot omit the amendment's real prior review"
            baseline = copy.deepcopy(matches[-1])
            # Includes strict append-only validation, unchanged old target
            # judgments, and recomputation of every reuse-summary field.
            reuse_targets(request, baseline)
            self.baseline = baseline
            return self.baseline
        return None

    def assert_receipt(self, plan, receipt):
        baseline = self.baseline
        if baseline is None and receipt.get("reused_target_reviews"):
            # A fully cached amendment and review produce no model requests.
            # The final receipt still must match both actually revalidated
            # prior history and the typed fresh review returned by production.
            proofs = receipt["reused_target_reviews"]
            assert len(proofs) == 1, "Each extension must bind one actual prior review"
            proof = proofs[0]
            matches = [record for record in self.records if record["full"] and
                digest(record["plan"].model_dump(mode="python")) == proof["original_plan_sha256"] and
                digest(record["review"].model_dump(mode="python")) == proof["prior_review_sha256"] and
                digest(record["context"]) == proof["context_sha256"]]
            assert matches, "Cached reuse receipt needs the actual revalidated prior review"
            baseline = copy.deepcopy(matches[-1])
        if baseline is None:
            assert "reused_target_reviews" not in receipt
            return None
        original, prior, context = (baseline[key] for key in ("plan", "review", "context"))
        binding = {"baseline_context_digest": digest(context), "current_context_digest": digest(context)}
        pending = review_targets_for_extension(original, prior, plan, **binding)
        matches = [record for record in self.records if not record["full"] and
            canonical(record["plan"].model_dump(mode="python")) == canonical(plan.model_dump(mode="python")) and
            canonical(record["context"]) == canonical(context) and record["targets"] == pending]
        assert matches, "Final reuse needs the real newly produced target and case assessments"
        merged, provenance = merge_reused_oracle_review(original, prior, plan, matches[-1]["review"], **binding)
        assert merged.verdict() == "accepted"
        assert canonical(receipt["review"]) == canonical(merged.model_dump(mode="json"))
        assert canonical(receipt["reused_target_reviews"]) == canonical([provenance])
        self.baseline = baseline
        return provenance
