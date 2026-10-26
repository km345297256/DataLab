"""Host rejection-observation metadata explains capabilities, never votes."""
import copy
from unittest.mock import AsyncMock

import pytest

from app.domain.services.skills import skill_validation_review as reviews
from app.domain.services.skills.skill_creation_error import SkillCreationError
from app.domain.services.skills.skill_validation import SyntheticValidationPlan
from test_skill_validation_arguments import alias_rejection_payload
from test_skill_validation_plan import interfaces, method_contract, sources
from test_skill_validation_review import accepted_fixture_review, generation


def alias_plan():
    payload = alias_rejection_payload()
    payload["scripts"][0]["cases"][-1]["assertions"] = [
        {"type": "output_empty"}, {"type": "stderr_contains", "expected": "input"}]
    return SyntheticValidationPlan.model_validate(payload)


def declared_interfaces():
    declared = interfaces()
    declared[0]["output_mode"] = "file"
    return declared


def test_host_postcondition_targets_have_no_output_path_or_scientific_read():
    targets = reviews.review_targets(alias_plan())
    rejection = targets[0]["cases"][-1]["targets"]
    assert rejection == [
        {"target_id": "T001", "kind": "output_empty", "assertion_index": 0,
         "assertion_type": "output_empty", "expected": True,
         "observation_scope": "isolated_output_directory", "reads_scientific_output": False},
        {"target_id": "T002", "kind": "stderr_contains", "assertion_index": 1,
         "assertion_type": "stderr_contains", "expected": "input",
         "observation_scope": "ordinary_rejection_stderr", "reads_scientific_output": False},
    ]
    assert all("path" not in target and "pointer" not in target for target in rejection)
    assert all("observation_scope" not in target and "reads_scientific_output" not in target
               for target in targets[0]["cases"][0]["targets"])


def test_observation_descriptions_do_not_change_scientific_or_scope_decision_identity(monkeypatch):
    plan = alias_plan()
    scope = {"script_path": plan.scripts[0].script_path,
             "case_ids": [plan.scripts[0].cases[-1].id], "target_ids": ["T001", "T002"]}
    identity = reviews.oracle_decision_identity(plan)
    scoped = reviews.oracle_scope_identity(plan, scope)
    original = reviews.review_targets

    def old_targets(value):
        result = copy.deepcopy(original(value))
        for script in result:
            for case in script["cases"]:
                for target in case["targets"]:
                    target.pop("observation_scope", None)
                    target.pop("reads_scientific_output", None)
        return result

    monkeypatch.setattr(reviews, "review_targets", old_targets)
    assert reviews.oracle_decision_identity(plan) == identity
    assert reviews.oracle_scope_identity(plan, scope) == scoped


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["rejected", "uncertain"])
@pytest.mark.parametrize("target_index", [0, 1])
async def test_host_capabilities_do_not_replace_or_retry_an_adverse_observation_review(verdict, target_index):
    plan = alias_plan()
    document = accepted_fixture_review(plan)
    target = document["scripts"][0]["cases"][-1]["assertions"][target_index]
    target["correctness"] = {"status": verdict,
        "explanation": "The reviewer disputes this rejection observation; retain the actual decision."}
    generate, calls = generation([document])
    save = AsyncMock()
    expected_code = "validation_oracle_" + verdict
    with pytest.raises(SkillCreationError) as error:
        await reviews.review_validation_plan(plan, method_contract(), sources(), declared_interfaces(),
            generate=generate, save_revision=save, reference_evidence={"cases": [], "corrections": []})
    assert error.value.code == expected_code
    assert len(calls) == 1 and calls[0]["stage"] == "synthetic_plan_review"
    instruction = calls[0]["instruction"]
    assert "合法输入输出同址拒绝测试也可使用output_empty和stderr_contains" in instruction
    assert "不把输入文件当作产物" in instruction and "不要求--output绑定独立输出文件" in instruction
    assert "不得据此自动认可预期或忽略真实否决" in instruction
    assert calls[0]["payload"]["validation_plan"] == plan.model_dump(mode="json")
    assert calls[0]["payload"]["review_targets"] == reviews.review_targets(plan)
    save.assert_awaited_once()
    saved_plan, saved_review = save.await_args.args
    assert saved_plan.model_dump(mode="json") == plan.model_dump(mode="json")
    assert saved_review.model_dump(mode="json") == document

    # A saved rejection still cannot become a new vote just because its host
    # metadata is clearer. No valid substantive revision was supplied here.
    forbidden = AsyncMock(side_effect=AssertionError("An unchanged adverse review must not be re-voted"))
    with pytest.raises(SkillCreationError) as restored:
        await reviews.review_validation_plan(plan, method_contract(), sources(), declared_interfaces(),
            generate=forbidden,
            load_revision=lambda: {"plan": saved_plan.model_dump(), "review": saved_review.model_dump()},
            reference_evidence={"cases": [], "corrections": []})
    assert restored.value.code == expected_code
    forbidden.assert_not_awaited()
