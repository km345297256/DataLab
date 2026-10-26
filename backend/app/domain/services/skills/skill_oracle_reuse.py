"""Carry forward exact accepted target reviews across append-only coverage.

This module never authors a verdict. Script support and every case-level
assessment come from a fresh review. Existing scientific target assessments can
be copied only from a complete typed review under the identical method/source/
interface context, after the host proves that all old plan content is unchanged.
"""
from __future__ import annotations

from collections import defaultdict, deque
import copy
import hashlib
import json
import re

from app.domain.services.skills.skill_creation_error import SkillCreationError, SkillPackageValidationError
from app.domain.services.skills.skill_validation import SyntheticValidationPlan
from app.domain.services.skills.skill_validation_extension import validate_coverage_extension
from app.domain.services.skills.skill_validation_review import (
    REVIEW_TARGETS_PER_PART, ValidationPlanReview, _validate_review_targets,
    review_targets, validate_oracle_review,
)


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _digest(value) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _require(condition):
    if not condition:
        raise SkillPackageValidationError("validation_oracle_revision_invalid")


def _identity(path, case, target):
    assertion = case.assertions[target["assertion_index"]].model_dump(mode="python")
    if assertion["type"] == "json_object_keys":
        assertion["keys"] = sorted(assertion["keys"])
    return {"script_path": path, "case_id": case.id,
        "case_context": case.model_dump(mode="python", exclude={"assertions"}),
        "target": {"kind": target["kind"], "assertion": assertion,
                   **({"field": target["field"]} if "field" in target else {})}}


def _prepare(original_plan, prior_review, extended_plan, *, baseline_context_digest, current_context_digest):
    _require(isinstance(baseline_context_digest, str) and
             re.fullmatch(r"[0-9a-f]{64}", baseline_context_digest) is not None)
    _require(isinstance(current_context_digest, str) and current_context_digest == baseline_context_digest)
    validate_coverage_extension(original_plan, extended_plan)
    validate_oracle_review(prior_review, original_plan)
    adverse = [assessment.status for script in prior_review.scripts for case in script.cases
               for target in case.assertions for assessment in (target.correctness, target.tolerance)
               if assessment.status != "accepted"]
    if adverse:
        # All old scientific values are frozen. Re-asking an unchanged rejected
        # or uncertain target would be a new vote, not a coverage extension.
        raise SkillCreationError("synthetic_plan_review", "validation_oracle_uncertain"
                                 if "uncertain" in adverse else "validation_oracle_rejected")
    old_cases = {(script.script_path, case.id): case for script in original_plan.scripts for case in script.cases}
    new_cases = {(script.script_path, case.id): case for script in extended_plan.scripts for case in script.cases}
    old_reviews = {(script.script_path, case.case_id, target.target_id): target
                   for script in prior_review.scripts for case in script.cases for target in case.assertions}
    available = defaultdict(deque)
    for script in review_targets(original_plan):
        for case in script["cases"]:
            source_case = old_cases[(script["script_path"], case["case_id"])]
            for target in case["targets"]:
                identity = _identity(script["script_path"], source_case, target)
                available[_canonical(identity)].append((target["target_id"], identity))
    full_targets = review_targets(extended_plan)
    pending = copy.deepcopy(full_targets)
    reused = {}
    provenance = []
    for script in pending:
        for case in script["cases"]:
            path, identifier = script["script_path"], case["case_id"]
            new_targets = []
            for target in case["targets"]:
                identity = _identity(path, new_cases[(path, identifier)], target)
                prior = available.get(_canonical(identity))
                if not prior:
                    new_targets.append(target)
                    continue
                source_id, original_identity = prior.popleft()
                assessment = old_reviews[(path, identifier, source_id)]
                reused[(path, identifier, target["target_id"])] = assessment.model_copy(deep=True,
                    update={"target_id": target["target_id"]})
                provenance.append({"script_path": path, "case_id": identifier,
                    "target_id": target["target_id"], "source_target_id": source_id,
                    "target_identity_sha256": _digest(original_identity),
                    "source_assessment_sha256": _digest(assessment.model_dump(mode="python")),
                    "source_verdicts": {"correctness": assessment.correctness.status,
                                        "tolerance": assessment.tolerance.status}})
            case["targets"] = new_targets
    _require(not any(available.values()))
    record = {"version": 1, "scope": "unchanged_target_reviews",
        "context_sha256": baseline_context_digest,
        "original_plan_sha256": _digest(original_plan.model_dump(mode="python")),
        "extended_plan_sha256": _digest(extended_plan.model_dump(mode="python")),
        "prior_review_sha256": _digest(prior_review.model_dump(mode="python")),
        "reused_targets": provenance,
        "new_review_target_count": sum(len(case["targets"]) for script in pending for case in script["cases"]),
        "case_assessments_reused": False, "script_assessments_reused": False}
    return pending, reused, record, full_targets


def review_targets_for_extension(original_plan: SyntheticValidationPlan, prior_review: ValidationPlanReview,
                                 extended_plan: SyntheticValidationPlan, *, baseline_context_digest: str,
                                 current_context_digest: str) -> list[dict]:
    """Every original/new case remains present, even if it has no new targets."""
    return _prepare(original_plan, prior_review, extended_plan,
        baseline_context_digest=baseline_context_digest, current_context_digest=current_context_digest)[0]


def review_scopes_for_extension(original_plan: SyntheticValidationPlan, prior_review: ValidationPlanReview,
                                extended_plan: SyntheticValidationPlan, *, baseline_context_digest: str,
                                current_context_digest: str) -> list[dict]:
    """Partition only pending targets without changing any existing T number."""
    pending = review_targets_for_extension(original_plan, prior_review, extended_plan,
        baseline_context_digest=baseline_context_digest, current_context_digest=current_context_digest)
    cases = {(script.script_path, case.id): case for script in extended_plan.scripts for case in script.cases}
    scopes = []
    for script in pending:
        path = script["script_path"]
        if not script["cases"]:
            scopes.append({"script_path": path, "case_ids": [], "target_ids": []})
        for case in script["cases"]:
            groups = defaultdict(list)
            for target in case["targets"]:
                groups[_canonical(_identity(path, cases[(path, case["case_id"])], target))].append(target["target_id"])
            ordered = [groups[key] for key in sorted(groups)]
            chunks = [ordered[start:start + REVIEW_TARGETS_PER_PART]
                      for start in range(0, len(ordered), REVIEW_TARGETS_PER_PART)] or [[]]
            for chunk in chunks:
                scopes.append({"script_path": path, "case_ids": [case["case_id"]],
                               "target_ids": [target for group in chunk for target in group]})
    return scopes


def merge_reused_oracle_review(original_plan: SyntheticValidationPlan, prior_review: ValidationPlanReview,
                               extended_plan: SyntheticValidationPlan, extension_review: ValidationPlanReview, *,
                               baseline_context_digest: str, current_context_digest: str) -> tuple[ValidationPlanReview, dict]:
    """Merge real new assessments with exact prior target assessments and audit."""
    pending, reused, record, full_targets = _prepare(original_plan, prior_review, extended_plan,
        baseline_context_digest=baseline_context_digest, current_context_digest=current_context_digest)
    _validate_review_targets(extension_review, pending)
    fresh = {(script.script_path, case.case_id, target.target_id): target
             for script in extension_review.scripts for case in script.cases for target in case.assertions}
    _require(not set(fresh).intersection(reused))
    targets_by_case = {(script["script_path"], case["case_id"]): case["targets"]
                       for script in full_targets for case in script["cases"]}
    merged = extension_review.model_copy(deep=True)
    for script in merged.scripts:
        for case in script.cases:
            restored = []
            for target in targets_by_case[(script.script_path, case.case_id)]:
                key = (script.script_path, case.case_id, target["target_id"])
                item = reused[key] if key in reused else fresh[key]
                restored.append(item.model_copy(deep=True))
            case.assertions = restored
    validate_oracle_review(merged, extended_plan)
    record["extension_review_sha256"] = _digest(extension_review.model_dump(mode="python"))
    record["merged_review_sha256"] = _digest(merged.model_dump(mode="python"))
    cases = {(script.script_path, case.id): case for script in extended_plan.scripts for case in script.cases}
    record["reviewed_targets"] = [{
        "script_path": script["script_path"], "case_id": case["case_id"],
        "target_id": target["target_id"],
        "target_identity_sha256": _digest(_identity(script["script_path"],
            cases[(script["script_path"], case["case_id"])], target)),
        "assessment_sha256": _digest(fresh[(script["script_path"], case["case_id"],
            target["target_id"])].model_dump(mode="python")),
    } for script in pending for case in script["cases"] for target in case["targets"]]
    return merged, record
