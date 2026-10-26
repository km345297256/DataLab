"""Input-location contracts must reach authors and both independent reviewers."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.services.skills.skill_creation.workflow_authoring import WorkflowDraft
from app.domain.services.skills.skill_script_verification import verify_skill_script_cases
from app.domain.services.skills.skill_validation_review import review_validation_plan
from test_session_skill_creator import stage_response
from test_skill_creation_pipeline import pipeline_for
from test_skill_script_verification import recipe
from test_skill_validation_plan import interfaces, method_contract, plan_payload, sources
from test_skill_validation_review import typed


class ReachedModel(Exception):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["method_requirements", "workflow", "implementation", "method_review",
                                   "synthetic_validation_plan", "synthetic_plan_review"])
async def test_input_location_contract_rule_reaches_each_responsible_model_stage(stage):
    calls = []
    async def generate(schema, actual, instruction, payload, check=None, **kwargs):
        calls.append((actual, instruction))
        if stage == "implementation" and actual == "workflow":
            from test_session_skill_creator import recipe_payload, helper_script
            authored = recipe_payload()
            authored["scripts"] = [helper_script()]
            return WorkflowDraft.model_validate(stage_response(
                [None, SimpleNamespace(content=json.dumps({"stage": actual, **payload}))], authored))
        raise ReachedModel()

    pipeline = pipeline_for({})
    pipeline.gateway.generate = generate
    pipeline.context.evidence["successful_tool_capabilities"] = []
    with pytest.raises(ReachedModel):
        if stage == "method_requirements":
            await pipeline.extract_requirements()
        elif stage in {"workflow", "implementation"}:
            await pipeline.design_package()
        elif stage == "method_review":
            await pipeline.review(recipe())
        elif stage == "synthetic_validation_plan":
            await verify_skill_script_cases(recipe(), method_contract(), sources(), generate=generate,
                                            revalidate=lambda value: None, review=AsyncMock())
        else:
            await review_validation_plan(typed(), method_contract(), sources(), interfaces(), generate=generate)
    assert calls[-1][0] == stage
    instruction = calls[-1][1]
    assert "原输入契约必须保真" in instruction
    assert "CLI" in instruction and ("位置" in instruction or "载体" in instruction)


@pytest.mark.asyncio
async def test_fixed_hand_mapping_fixture_never_adds_compensating_cli(monkeypatch):
    from acceptance import skill_creator_real_model as acceptance
    from app.infrastructure.external.sandbox import skill_validation_worker
    from copy import deepcopy

    original = deepcopy(plan_payload()["scripts"][0])
    normal = deepcopy(original["cases"][0])
    normal["argv"] = ["--input", "{input:input.json}", "--output", "{output}/result.json"]
    normal["input_files"] = [{"path": "input.json", "format": "json", "data": {"pairs": []}}]
    normal["assertions"] = [{"type": "json_value", "path": "result.json", "pointer": "/n", "expected": {"value": 2}}]
    original["cases"] = [normal]
    observed = []
    async def inspect_fixed_oracles(scripts, plan):
        observed.append(plan)
        return {"status": "passed", "scope": "synthetic", "cases": [
            {"status": "passed", "id": case.id} for case in plan.scripts[0].cases]}
    monkeypatch.setattr(skill_validation_worker, "validate_skill_scripts", inspect_fixed_oracles)
    await acceptance.independently_verify_paired({"scripts": {original["script_path"]: "NOT_EXECUTED"},
        "references": {"references/synthetic-validation.json": json.dumps({"scripts": [original]})}})
    cases = observed[0].scripts[0].cases
    assert len(cases) == 8 and sum(len(case.assertions) for case in cases) == 32
    mapped = next(case for case in cases if case.id == "fixed-mapped")
    assert mapped.argv == normal["argv"]
    assert mapped.input_files[0].data == {"fields": {"id": "unit", "before": "pre", "after": "post"},
        "pairs": [{"unit": "m1", "pre": 0, "post": 1}, {"unit": "m2", "pre": 0, "post": 3}]}
    values = {assertion.pointer: assertion.expected.value for assertion in mapped.assertions}
    assert values["/mean_change"] == 2 and values["/p_value"] == .5
    mixed = next(case for case in cases if case.id == "fixed-mixed")
    assert next(a.expected.value for a in mixed.assertions if a.pointer == "/p_value") == .375
