"""Bound independent review work to complete host-owned target partitions."""
import copy
import json
from collections import Counter

import pytest
from pydantic import ValidationError

from app.domain.services.skills.skill_creation.checkpoints import BuildCheckpoints
from app.domain.services.skills.skill_creation_error import SkillCreationError, SkillPackageValidationError
from app.domain.services.skills.skill_validation import ExpectedValue, JSONValueAssertion
from app.domain.services.skills.skill_validation_review import (
    OracleAssessment, ValidationPlanReview, _review_candidate, _review_parts,
    oracle_decision_identity, oracle_scope_identity, review_targets, validate_oracle_review,
)
from test_skill_creation_checkpoints import MemoryCache, scope
from test_skill_validation_plan import interfaces, method_contract, sources
from test_skill_validation_review import typed


def large_plan():
    plan = typed()
    for case, count in zip(plan.scripts[0].cases, (196, 134)):
        case.assertions = [JSONValueAssertion(type="json_value", path="summary.json",
            pointer=f"/{case.kind}/value_{number:03d}", expected=ExpectedValue(value=number))
            for number in range(count)]
    original_invalid = plan.scripts[0].cases[-1]
    for number in range(5):
        plan.scripts[0].cases.append(original_invalid.model_copy(update={"id": f"invalid-extra-{number}"}, deep=True))
    return plan


def context():
    return {"method_contract": method_contract().model_dump(), "sources": sources(),
            "script_interfaces": interfaces()}


def part_response(targets):
    """Structural fake only; no automatic scientific acceptance in production."""
    def assessment():
        return {"status": "accepted", "explanation": "独立输入计算结果一致。"}
    return {"scripts": [{"script_path": script["script_path"], "support": assessment(), "cases": [
        {"case_id": case["case_id"], "input_validity": assessment(), "exit_behavior": assessment(),
         "method_coverage": assessment(), "assertions": [
             {"target_id": target["target_id"], "correctness": assessment(), "tolerance": assessment()}
             for target in case["targets"]]} for case in script["cases"]]} for script in targets]}


@pytest.mark.parametrize("text", ["数组长度精确比较。", "均值4/2=2。", "整数精确比较。", "应为3。", "null 精确比较。", "2+2=4", "Δ=2"])
def test_complete_short_explanations_are_valid_without_language_specific_padding(text):
    assessment = OracleAssessment(status="accepted", explanation="  " + text + "\n")
    assert assessment.explanation == text
    assert OracleAssessment.model_json_schema()["properties"]["explanation"]["minLength"] == 1


@pytest.mark.parametrize("text", ["", "  \n\t", "...", "---", "。；！", "✓"])
def test_empty_or_punctuation_only_explanations_are_not_evidence(text):
    with pytest.raises(ValidationError):
        OracleAssessment(status="accepted", explanation=text)


@pytest.mark.asyncio
async def test_all_330_targets_and_every_case_are_reviewed_with_complete_context():
    plan, calls = large_plan(), []
    async def generate(schema, stage, instruction, payload, check):
        calls.append(copy.deepcopy(payload))
        assert stage == "synthetic_plan_review"
        assert payload["validation_plan"] == plan.model_dump(mode="json")
        assert all(payload[key] == value for key, value in context().items())
        value = schema.model_validate(part_response(payload["review_targets"]))
        check(value)
        return value
    result = await _review_candidate(plan, context(), generate)
    validate_oracle_review(result, plan)
    assert len(calls) == 13  # 4 normal + 3 boundary + 6 invalid-input cases
    actual, expected = Counter(), Counter()
    for payload in calls:
        targets = payload["review_targets"]
        assert len(targets) == 1 and len(targets[0]["cases"]) == 1
        case = targets[0]["cases"][0]
        assert len(case["targets"]) <= 64
        for target in case["targets"]:
            actual[(targets[0]["script_path"], case["case_id"], target["target_id"])] += 1
    for script in review_targets(plan):
        for case in script["cases"]:
            for target in case["targets"]:
                expected[(script["script_path"], case["case_id"], target["target_id"])] += 1
    assert actual == expected and sum(actual.values()) == 330
    assert result.verdict() == "accepted"
    assert len(result.scripts[0].cases) == 8


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["missing_target", "extra_target", "duplicate_target", "wrong_case", "wrong_script"])
async def test_fragment_cannot_omit_add_duplicate_or_reassign_host_targets(mutation):
    async def generate(schema, stage, instruction, payload, check):
        document = part_response(payload["review_targets"])
        script, case = document["scripts"][0], document["scripts"][0]["cases"][0]
        if mutation == "missing_target": case["assertions"].pop()
        elif mutation == "extra_target": case["assertions"].append({**case["assertions"][0], "target_id": "T999"})
        elif mutation == "duplicate_target": case["assertions"].append(copy.deepcopy(case["assertions"][0]))
        elif mutation == "wrong_case": case["case_id"] = "foreign-case"
        else: script["script_path"] = "scripts/foreign.py"
        value = schema.model_validate(document)
        # Deliberately skip an injected generator's callback: host still checks.
        return value
    with pytest.raises(SkillPackageValidationError, match="validation_oracle_review_incomplete"):
        await _review_candidate(large_plan(), context(), generate)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["support", "input_validity", "exit_behavior", "method_coverage", "correctness", "tolerance"])
@pytest.mark.parametrize("adverse", ["rejected", "uncertain"])
async def test_later_accepted_fragments_never_override_an_adverse_assessment(field, adverse):
    calls = 0
    async def generate(schema, stage, instruction, payload, check):
        nonlocal calls
        data = part_response(payload["review_targets"])
        if calls == 0:
            script, case = data["scripts"][0], data["scripts"][0]["cases"][0]
            target = script["support"] if field == "support" else (
                case["assertions"][0][field] if field in {"correctness", "tolerance"} else case[field])
            target.update(status=adverse, explanation="独立计算发现该项不能接受。")
        calls += 1
        value = schema.model_validate(data)
        check(value)
        return value
    result = await _review_candidate(large_plan(), context(), generate)
    assert result.verdict() == adverse and calls == 13
    encoded = json.dumps(result.model_dump(), ensure_ascii=False)
    assert "独立计算发现该项不能接受。" in encoded


@pytest.mark.asyncio
async def test_uncertainty_takes_priority_and_preserves_other_rejection_reason():
    calls = 0
    async def generate(schema, stage, instruction, payload, check):
        nonlocal calls
        data = part_response(payload["review_targets"])
        if calls < 2:
            data["scripts"][0]["support"].update(status=["rejected", "uncertain"][calls],
                explanation=["原方法覆盖不完整。", "独立证据不足。 "][calls])
        calls += 1
        value = schema.model_validate(data)
        check(value)
        return value
    result = await _review_candidate(large_plan(), context(), generate)
    assert result.scripts[0].support.status == "uncertain"
    assert all(text in result.scripts[0].support.explanation for text in ("原方法覆盖不完整。", "独立证据不足。"))


def test_semantic_partitions_and_scope_identity_survive_target_reordering():
    plan = large_plan()
    reordered = plan.model_copy(deep=True)
    for case in reordered.scripts[0].cases:
        case.assertions.reverse()
    assert oracle_decision_identity(plan) == oracle_decision_identity(reordered)
    original_parts, changed_parts = _review_parts(plan), _review_parts(reordered)
    assert len(original_parts) == len(changed_parts)
    for (_, original), (_, changed) in zip(original_parts, changed_parts):
        assert oracle_scope_identity(plan, original) == oracle_scope_identity(reordered, changed)
    assert original_parts[0][1]["target_ids"] != changed_parts[0][1]["target_ids"]


def test_same_positional_t_number_cannot_identify_a_different_scientific_assertion():
    plan = typed()
    scope_value = {"script_path": plan.scripts[0].script_path,
                   "case_ids": [plan.scripts[0].cases[0].id], "target_ids": ["T001"]}
    reordered = plan.model_copy(deep=True)
    reordered.scripts[0].cases[0].assertions.reverse()
    assert oracle_scope_identity(plan, scope_value) != oracle_scope_identity(reordered, scope_value)


def test_repeated_equivalent_assertions_do_not_create_fresh_semantic_partitions():
    plan = large_plan()
    duplicated = plan.model_copy(deep=True)
    for case in duplicated.scripts[0].cases:
        if case.assertions:
            case.assertions = [case.assertions[0].model_copy(deep=True) for _ in range(80)] + case.assertions
    assert oracle_decision_identity(plan) == oracle_decision_identity(duplicated)
    original_parts, duplicated_parts = _review_parts(plan), _review_parts(duplicated)
    assert len(original_parts) == len(duplicated_parts)
    for (_, original), (_, repeated) in zip(original_parts, duplicated_parts):
        assert oracle_scope_identity(plan, original) == oracle_scope_identity(duplicated, repeated)
    expected = Counter((script["script_path"], case["case_id"], target["target_id"])
        for script in review_targets(duplicated) for case in script["cases"] for target in case["targets"])
    actual = Counter((script["script_path"], case["case_id"], target["target_id"])
        for targets, _ in duplicated_parts for script in targets for case in script["cases"] for target in case["targets"])
    assert actual == expected and sum(actual.values()) == 490


def test_repeated_equivalent_targets_do_not_change_single_review_identity_boundary():
    plan = typed()
    duplicated = plan.model_copy(deep=True)
    duplicated.scripts[0].cases[0].assertions *= 100
    assert oracle_decision_identity(plan) == oracle_decision_identity(duplicated)
    assert _review_parts(plan) is None and _review_parts(duplicated) is None


@pytest.mark.asyncio
async def test_retry_resumes_completed_parts_and_never_repeats_them():
    plan, cache, requests, should_fail = large_plan(), MemoryCache(), [], True
    async def run():
        build = BuildCheckpoints(cache, scope())
        async def generate(schema, stage, instruction, payload, check):
            async def produce():
                nonlocal should_fail
                requests.append(payload["review_scope"])
                if len(requests) == 3 and should_fail:
                    should_fail = False
                    raise SkillCreationError(stage, "model_unavailable")
                return schema.model_validate(part_response(payload["review_targets"]))
            return await build.run(schema, stage, instruction, payload, check, produce,
                decision_identity={"plan": oracle_decision_identity(plan), "scope": oracle_scope_identity(plan, payload["review_scope"])})
        return await _review_candidate(plan, context(), generate)
    with pytest.raises(SkillCreationError, match="模型服务暂时不可用"):
        await run()
    review = await run()
    validate_oracle_review(review, plan)
    counts = Counter(json.dumps(value, sort_keys=True) for value in requests)
    assert len(requests) == 14 and sorted(counts.values()) == [1] * 12 + [2]
