"""Exact, declarative array shape checks preserve scientific and privacy boundaries."""
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.domain.services.skills.skill_creation_error import SkillPackageValidationError
from app.domain.services.skills.skill_validation import (
    JSONArrayLengthAssertion, JSONValueAssertion, SyntheticValidationCase, SyntheticValidationPlan,
    validate_validation_plan,
)
from app.domain.services.skills.skill_validation_review import (
    ValidationPlanReview, review_targets, validate_oracle_review, validate_plan_revision,
)
from app.infrastructure.external.sandbox import skill_validation_worker as worker
from test_skill_creation_pipeline import PRIVATE, pipeline_for, check_plan
from test_skill_validation_plan import interfaces, method_contract, plan_payload, sources
from test_skill_validation_review import accepted_fixture_review
from test_skill_validation_worker import case, runtime


def assertion(**updates):
    return {"type": "json_array_length", "path": "summary.json", "pointer": "/groups", "length": 0, **updates}


def array_case(**updates):
    payload = case(output_type="json")
    payload["assertions"] = [assertion(**updates)]
    return SyntheticValidationCase.model_validate(payload)


@pytest.mark.parametrize("length", [0, 1, 25])
def test_exact_nonnegative_lengths_are_declarative(length):
    value = JSONArrayLengthAssertion.model_validate(assertion(length=length))
    assert value.length == length and type(value.length) is int
    assert JSONArrayLengthAssertion.model_validate_json(value.model_dump_json()) == value


@pytest.mark.parametrize("updates", [{"length": -1}, {"length": True}, {"length": 0.0}, {"length": "0"},
    {"length": None}, {"atol": 1}, {"expected": []}, {"path": "../summary.json"},
    {"path": "/private/summary.json"}, {"pointer": "$.groups"}, {"pointer": "/groups~7"}])
def test_array_lengths_do_not_expand_scalars_paths_or_expression_languages(updates):
    with pytest.raises(ValidationError):
        JSONArrayLengthAssertion.model_validate(assertion(**updates))
    with pytest.raises(ValidationError):
        JSONValueAssertion.model_validate({"type": "json_value", "path": "summary.json",
                                          "pointer": "/groups", "expected": {"value": []}})


@pytest.mark.parametrize("payload,length,passed", [
    (b'{"groups":[]}', 0, True), (b'{"groups":[1]}', 0, False),
    (b'{"groups":[]}', 1, False), (b'{"groups":[1,null,{}]}', 3, True),
    (b'{"groups":null}', 0, False), (b'{"groups":{}}', 0, False),
    (b'{"groups":""}', 0, False), (b'{"groups":0}', 0, False),
    (b'{"groups":false}', 0, False), (b'{}', 0, False),
])
def test_worker_distinguishes_actual_array_length_from_missing_null_and_other_types(payload, length, passed):
    checks = worker._assertions(array_case(length=length), {"summary.json": payload})
    assert len(checks) == 1 and checks[0]["passed"] is passed
    if passed:
        assert checks[0]["expected"] == checks[0]["actual"] == length


@pytest.mark.parametrize("pointer,payload,length,passed", [
    ("", b'[]', 0, True), ("/a~1b/~0nested/0/groups", b'{"a/b":{"~nested":[{"groups":[]}]}}', 0, True),
    ("/groups/0", b'{"groups":[[1,2]]}', 2, True),
    ("/groups/2", b'{"groups":[[]]}', 0, False),
    ("/groups/00", b'{"groups":[[]]}', 0, False),
])
def test_rfc6901_navigation_is_shared_for_nested_arrays(pointer, payload, length, passed):
    checks = worker._assertions(array_case(pointer=pointer, length=length), {"summary.json": payload})
    assert checks[0]["passed"] is passed


@pytest.mark.parametrize("payload,reason", [
    (b'{"groups":[],"groups":[]}', "duplicate_json_key"),
    (b'{"groups":[],"nested":{"x":1,"x":2}}', "duplicate_json_key"),
    (b'{"groups":[],"nested":[NaN]}', "nonfinite_json"),
    (b'{"groups":[],"nested":[Infinity]}', "nonfinite_json"),
    (b'{"groups":[],"nested":[1e400]}', "nonfinite_json"),
    (b'{"groups":[]', "missing_or_invalid_output"),
])
def test_array_shape_never_accepts_nonstandard_or_ambiguous_json(payload, reason):
    checks = worker._assertions(array_case(), {"summary.json": payload})
    assert checks == [{"id": "assertion_1", "passed": False, "reason": reason}]


def test_missing_array_output_file_fails_closed():
    assert worker._assertions(array_case(), {})[0]["passed"] is False


@pytest.mark.asyncio
async def test_array_length_runs_through_real_host_worker_boundary(runtime):
    payload = plan_payload()
    payload["scripts"][0]["script_path"] = "scripts/summary.py"
    for candidate in payload["scripts"][0]["cases"]:
        candidate["assertions"] = [assertion()]
        candidate["expected_exit_code"] = 0
    plan = SyntheticValidationPlan.model_validate(payload)
    runtime.outputs[worker.OUTPUT_ROOT + "/summary.json"] = b'{"groups":[]}'
    result = await worker.validate_skill_scripts({"scripts/summary.py": "print('synthetic')"}, plan,
                                                SimpleNamespace(sandbox_image="existing:sandbox"))
    assert result["status"] == "passed" and len(result["cases"]) == 3
    assert all(check["passed"] for item in result["cases"] for check in item["checks"])


def test_array_pointer_obeys_declared_public_output_fields():
    payload = plan_payload()
    payload["scripts"][0]["cases"][0]["assertions"] = [assertion(path="status.json")]
    declared = interfaces()
    declared[0]["json_outputs"] = [{"path": "@output/status.json", "shape": {
        "type": "object", "properties": {"groups": {"type": "array", "items": {"type": "unknown"}},
                                            "status": {"type": "string"}}}}]
    validate_validation_plan(SyntheticValidationPlan.model_validate(payload), method_contract(), sources(), declared)
    payload["scripts"][0]["cases"][0]["assertions"][0]["pointer"] = "/invented"
    with pytest.raises(SkillPackageValidationError, match="validation_output_field_unknown"):
        validate_validation_plan(SyntheticValidationPlan.model_validate(payload), method_contract(), sources(), declared)


def test_revision_can_add_required_empty_array_check_and_review_must_cover_it():
    original = SyntheticValidationPlan.model_validate(plan_payload())
    revised = original.model_copy(deep=True)
    revised.scripts[0].cases[1].assertions.append(JSONArrayLengthAssertion.model_validate(assertion()))
    validate_plan_revision(original, revised)
    target = review_targets(revised)[0]["cases"][1]["targets"][-1]
    assert target["kind"] == "json_array_length" and target["expected"] == 0
    review = ValidationPlanReview.model_validate(accepted_fixture_review(revised))
    validate_oracle_review(review, revised)
    review.scripts[0].cases[1].assertions.pop()
    with pytest.raises(SkillPackageValidationError, match="validation_oracle_review_incomplete"):
        validate_oracle_review(review, revised)


def test_revision_may_correct_array_length_but_cannot_retarget_or_remove_check():
    original = SyntheticValidationPlan.model_validate(plan_payload())
    original.scripts[0].cases[1].assertions.append(JSONArrayLengthAssertion.model_validate(assertion(length=1)))
    revised = original.model_copy(deep=True)
    revised.scripts[0].cases[1].assertions[-1].length = 0
    validate_plan_revision(original, revised)
    for key, value in [("path", "other.json"), ("pointer", "/other")]:
        changed = revised.model_copy(deep=True)
        setattr(changed.scripts[0].cases[1].assertions[-1], key, value)
        with pytest.raises(SkillPackageValidationError, match="validation_oracle_revision_invalid"):
            validate_plan_revision(original, changed)
    revised.scripts[0].cases[1].assertions.pop()
    with pytest.raises(SkillPackageValidationError, match="validation_oracle_revision_invalid"):
        validate_plan_revision(original, revised)


@pytest.mark.asyncio
@pytest.mark.parametrize("private", [False, True])
async def test_array_pointer_has_contract_syntax_but_retains_known_private_value_checks(private):
    payload = plan_payload()
    pointer = "/summary/" + (PRIVATE if private else "groups")
    payload["scripts"][0]["cases"][1]["assertions"].append(assertion(path="status.json", pointer=pointer))
    pipeline = pipeline_for(payload)
    if private:
        with pytest.raises(SkillPackageValidationError) as error:
            await pipeline.generate(SyntheticValidationPlan, "synthetic_validation_plan", "Plan synthetic checks.", {}, check_plan)
        assert error.value.code == "private_package_content"
        assert PRIVATE not in json.dumps(error.value.details)
    else:
        result = await pipeline.generate(SyntheticValidationPlan, "synthetic_validation_plan", "Plan synthetic checks.", {}, check_plan)
        assert result.scripts[0].cases[1].assertions[-1].length == 0
        assert result.scripts[0].cases[1].assertions[-1].pointer == pointer
