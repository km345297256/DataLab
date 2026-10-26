"""Coverage repairs receive exact safe bounds, without changing scientific gates."""
import copy
import json

import pytest

from app.domain.services.skills.method_contract import (
    COVERAGE_FIELDS_BY_ROOT, COVERAGE_ROOTS, CoverageError, MethodContract, RequirementCoverage, validate_coverage,
)
from test_skill_creation_gateway import SmallRecipe, VALID, make_gateway, requests


def contract(kind="method"):
    return MethodContract(requirements=[{"id": "R001", "kind": kind,
        "statement": "Compute HC3 covariance for the declared model.", "source_ids": ["S001"]}])


def document(pointer="/steps/6/action"):
    return {"steps": [{"action": "Compute HC3 covariance for the declared model.",
                       "decision": "Preserve the complete scientific method.",
                       "verification": "Verify the covariance calculation."} for _ in range(6)],
            "coverage": [{"requirement_id": "R001", "locations": [pointer]}]}


def diagnostics(value, method=None, **kwargs):
    with pytest.raises(CoverageError) as failure:
        validate_coverage(method or contract(), value, **kwargs)
    return failure.value


def test_zero_based_bounds_locate_sixth_step_without_automatically_remapping_pointer():
    value = document()
    before = copy.deepcopy(value)
    failure = diagnostics(value)
    assert failure.code == "unknown_coverage_location"
    assert failure.details["coverage_issues"][0]["location"] == ["coverage", 0, "locations", 0]
    assert failure.details["available_locations"]["/steps"] == {
        "item_count": 6, "valid_index_range": [0, 5], "fields": ["action", "decision", "verification"]}
    assert value == before
    value["coverage"][0]["locations"] = ["/steps/5/action"]
    validate_coverage(contract(), value)


def test_inventory_is_fixed_roots_fields_and_counts_without_private_keys_pointer_or_values():
    canary = "synthetic_private_marker"
    value = document("/steps/6/" + canary)
    value[canary] = [canary] * 20
    value["steps"][0][canary] = canary
    value["steps"][0]["action"] = canary
    value["outputs"] = [{"name": canary}]
    value["parameters"] = []
    # This known root is deliberately malformed; its arbitrary dict keys and
    # length must not become an advertised array or leak into the inventory.
    value["dependencies"] = {canary: canary}
    failure = diagnostics(value)
    inventory = failure.details["available_locations"]
    assert set(inventory) == {"/" + root for root in COVERAGE_ROOTS}
    assert inventory["/outputs"]["item_count"] == 1
    assert inventory["/outputs"]["valid_index_range"] == [0, 0]
    assert inventory["/parameters"]["valid_index_range"] is None
    assert inventory["/dependencies"]["item_count"] == 0
    assert inventory["/dependencies"]["fields"] == []
    assert canary not in json.dumps(failure.details)
    for root in COVERAGE_ROOTS:
        assert inventory["/" + root]["fields"] == list(COVERAGE_FIELDS_BY_ROOT[root])


def test_pending_script_inventory_uses_real_declared_slots_and_fixed_public_fields():
    declaration = {"path": "scripts/model.py", "purpose": "Compute the declared HC3 covariance.",
        "invocation": "python scripts/model.py --input INPUT --output OUTPUT", "requirement_ids": ["R001"]}
    value = document("/scripts/1/content")
    value["scripts"] = []
    failure = diagnostics(value, pending_scripts=[declaration])
    assert failure.details["available_locations"]["/scripts"] == {
        "item_count": 1, "valid_index_range": [0, 0], "fields": ["path", "purpose", "invocation", "content"]}
    value["coverage"][0]["locations"] = ["/scripts/0/content"]
    validate_coverage(contract(), value, pending_scripts=[declaration])
    assert diagnostics(value).code == "unknown_coverage_location"


def test_valid_pointer_still_requires_actual_method_content_and_required_section():
    value = document("/steps/5/action")
    value["steps"][5]["action"] = "Compute only an unspecified generic result."
    assert diagnostics(value).code == "method_identifiers_lost"
    value = document("/steps/5/action")
    assert diagnostics(value, contract("output")).code == "requirement_wrong_section"


def test_schema_explains_zero_based_pointer_indices():
    description = RequirementCoverage.model_json_schema()["properties"]["locations"]["description"]
    assert "zero-based" in description and "N-1" in description


@pytest.mark.asyncio
async def test_gateway_repair_preserves_safe_bounds_and_explicit_zero_based_instructions():
    gateway, calls, _ = make_gateway([VALID, VALID])
    failure = diagnostics(document())
    checks = iter([failure, None])
    def check(_):
        error = next(checks)
        if error:
            raise error
    await gateway.generate(SmallRecipe, "workflow", "Preserve the complete method.", {}, check)
    feedback = requests(calls)[1]["repair_feedback"]
    assert feedback["details"]["available_locations"]["/steps"]["valid_index_range"] == [0, 5]
    assert "available_locations" in feedback["hint"] and "从0开始" in feedback["hint"]
    assert "/steps/6/action" not in json.dumps(feedback)
