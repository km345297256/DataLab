"""An invalid command must be repairable before source metadata is frozen."""
from copy import deepcopy

import pytest

from app.domain.services.skills.skill_creation.package import validate_implementation
from app.domain.services.skills.skill_creation.script_policy import SkillScriptValidationError
from app.domain.services.skills.skill_creation.workflow_authoring import (
    WorkflowDraft, check_workflow_draft, compile_workflow,
)
from test_skill_creation_gateway import make_gateway, requests
from test_skill_workflow_authoring import contract, draft_payload


def script_draft(invocation):
    payload = draft_payload()
    payload["implementation"] = {
        "kind": "parameterized_scripts",
        "reason": "Use a reusable program with current input and output parameters.",
        "scripts": [{"path": "scripts/compute.py", "purpose": "Compute the declared covariance matrix.",
                     "invocation": invocation, "requirement_ids": ["R001"],
                     "implementation_source_ids": []}],
    }
    return payload


@pytest.mark.parametrize("invocation", [
    "python scripts/different.py --input data.csv --output output",
    "python scripts/compute.py --input data.csv",
    "python scripts/compute.py --input data.csv --output",
    "python scripts/compute.py --input 'unclosed --output output",
])
def test_invalid_declaration_fails_before_any_source_generation(invocation):
    draft = WorkflowDraft.model_validate(script_draft(invocation))
    recipe = compile_workflow(draft, contract())
    before = recipe.model_dump(mode="json")
    assert recipe.scripts == []
    with pytest.raises(SkillScriptValidationError) as failure:
        validate_implementation(recipe, contract(), [])
    assert failure.value.code == "script_invocation_invalid"
    assert failure.value.details == {"invocation_issues": [{
        "location": ["implementation", "scripts", 0, "invocation"],
        "code": "script_invocation_invalid",
    }]}
    assert recipe.model_dump(mode="json") == before
    assert invocation not in str(failure.value.details)


@pytest.mark.parametrize("invocation", [
    "python scripts/compute.py --input data.csv --output output",
    "python3 scripts/compute.py --input=data.csv --output=output --column score",
    "uv run scripts/compute.py --input 'selected input.csv' --output 'new output'",
])
def test_valid_declared_commands_keep_existing_parser_independence(invocation):
    draft = WorkflowDraft.model_validate(script_draft(invocation))
    recipe = compile_workflow(draft, contract())
    validate_implementation(recipe, contract(), [])
    assert recipe.implementation.scripts[0].invocation == invocation


@pytest.mark.asyncio
async def test_workflow_retry_corrects_the_interface_before_it_is_frozen():
    invalid = script_draft("python scripts/wrong.py --input data.csv --output output")
    valid = deepcopy(invalid)
    valid["implementation"]["scripts"][0]["invocation"] = (
        "python scripts/compute.py --input data.csv --output output")
    gateway, calls, _ = make_gateway([invalid, valid])

    def check(value):
        return check_workflow_draft(value, contract(),
            lambda recipe: validate_implementation(recipe, contract(), []))

    draft = await gateway.generate(WorkflowDraft, "workflow", "Design a reusable workflow.", {}, check)
    repair = requests(calls)[1]["repair_feedback"]
    assert repair["reason"] == "script_invocation_invalid"
    assert repair["fields"] != ["content"]
    assert repair["details"]["invocation_issues"][0]["location"] == [
        "implementation", "scripts", 0, "invocation"]
    assert draft.model_dump(mode="json") == WorkflowDraft.model_validate(valid).model_dump(mode="json")
    assert len(calls) == 2
