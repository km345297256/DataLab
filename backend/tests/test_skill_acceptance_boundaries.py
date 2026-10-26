"""The opt-in acceptance wrapper accepts only host-declared oracle slices."""
from copy import deepcopy

import pytest

from acceptance.skill_creator_real_model import assert_oracle_isolation
from app.domain.services.skills.skill_validation import SyntheticValidationPlan
from app.domain.services.skills.skill_validation_review import _review_parts
from test_skill_validation_plan import plan_payload


def scoped_request():
    plan = plan_payload()
    # Use a real host partition, not a hand-picked target subset that the
    # production partitioner would never request.
    plan["scripts"][0]["cases"][0]["assertions"].extend([
        {"type": "json_value", "path": "status.json", "pointer": f"/probe{index}", "expected": {"value": index}}
        for index in range(65)])
    targets, scope = _review_parts(SyntheticValidationPlan.model_validate(plan))[0]
    return {"stage": "synthetic_plan_review", "sources": [], "script_interfaces": [],
        "validation_plan": plan, "review_targets": deepcopy(targets), "review_scope": deepcopy(scope)}


def test_acceptance_accepts_exact_host_oracle_slice():
    assert_oracle_isolation("synthetic_plan_review", scoped_request())


@pytest.mark.parametrize("corruption", ["extra_scope_data", "unknown_target", "unknown_case", "changed_expectation", "incomplete_slice"])
def test_acceptance_rejects_oracle_slice_drift(corruption):
    request = scoped_request()
    if corruption == "extra_scope_data":
        request["review_scope"]["implementation"] = "forbidden"
    elif corruption == "unknown_target":
        request["review_scope"]["target_ids"] = ["T999"]
    elif corruption == "unknown_case":
        request["review_scope"]["case_ids"] = ["unknown-case"]
    elif corruption == "changed_expectation":
        request["review_targets"][0]["cases"][0]["targets"][0]["expected"] = "changed"
    else:
        request["review_targets"][0]["cases"][0]["targets"].pop()
        request["review_scope"]["target_ids"].pop()
    with pytest.raises(AssertionError):
        assert_oracle_isolation("synthetic_plan_review", request)
