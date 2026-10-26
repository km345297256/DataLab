"""Owned output repair explains bindings without guessing an input dataflow."""
import copy
import json

import pytest

from app.domain.services.skills.skill_creation.privacy import PrivacyContext
from app.domain.services.skills.skill_creation.workflow_authoring import WorkflowDraft
from app.domain.services.skills.skill_creation_error import SkillCreationError
from test_skill_creation_gateway import check_privacy, make_gateway, requests
from test_skill_workflow_output_normalization import example
from test_skill_workflow_privacy_repair_gateway import patch


def multistage_draft():
    draft, privacy = example()
    document = draft.model_dump(mode="json")
    document["implementation"] = {
        "kind": "parameterized_scripts", "reason": "Preserve the ordered reusable calculation stages.",
        "scripts": [{"path": "scripts/summary.py", "purpose": "Summarize the generated covariance table.",
            "invocation": "python scripts/summary.py --input table=<run_dir>/old_result.csv --output <output_dir>",
            "requirement_ids": ["R001"], "implementation_source_ids": []}],
    }
    return document, privacy


@pytest.mark.asyncio
async def test_portability_repair_receives_explicit_safe_output_binding_and_keeps_input_roles():
    original, privacy = multistage_draft()
    # This is the proposed author response, not an automatic host rewrite.
    revised = json.loads(json.dumps(original).replace("old_result.csv", "output_001.csv"))
    gateway, calls, _ = make_gateway([original, patch(original, revised, privacy)], privacy=privacy)
    result = await gateway.generate(WorkflowDraft, "workflow", "Preserve the complete method.", {},
                                    check_privacy(privacy))
    assert result.model_dump(mode="json") == revised
    request = requests(calls)[1]
    bindings = request["repair_feedback"]["details"]["portable_output_bindings"]
    assert len(bindings) == 1
    binding = bindings[0]
    assert binding["output_index"] == 0 and binding["portable_name"] == "output_001.csv"
    assert binding["current_reference"] == privacy.redact_text("old_result.csv")
    assert ["implementation", "scripts", 0, "invocation"] in binding["locations"]
    # The host identifies a filename, never equates independent directories or
    # silently changes the declared input role. The author must fix the form.
    previous_call = request["previous_candidate"]["implementation"]["scripts"][0]["invocation"]
    assert "--input table=<run_dir>/" in previous_call
    assert "--output <output_dir>" in previous_call
    assert "old_result.csv" not in json.dumps(request, ensure_ascii=False)
    assert privacy.scan_document(bindings) == []
    assert [(step.action, step.decision, step.requirement_ids) for step in result.steps] == [
        (step["action"], step["decision"], step["requirement_ids"]) for step in original["steps"]]
    assert original["outputs"][0]["name"] == "old_result.csv"


@pytest.mark.asyncio
async def test_binding_feedback_never_grants_permission_to_retain_private_references():
    original, privacy = multistage_draft()
    before = copy.deepcopy(original)
    gateway, calls, _ = make_gateway([original, patch(original, original, privacy)], privacy=privacy)
    with pytest.raises(SkillCreationError) as error:
        await gateway.generate(WorkflowDraft, "workflow", "Preserve the complete method.", {},
                               check_privacy(privacy))
    assert error.value.code == "private_package_content"
    assert len(calls) == 2
    assert original == before
    assert not any(event.status == "passed" for event in gateway.attempts)


@pytest.mark.asyncio
async def test_selected_input_filename_is_not_promoted_to_an_owned_output_binding():
    original, owned = multistage_draft()
    privacy = PrivacyContext(owned.entries)
    gateway, calls, _ = make_gateway([original, patch(original, original, privacy)], privacy=privacy)
    with pytest.raises(SkillCreationError) as error:
        await gateway.generate(WorkflowDraft, "workflow", "Preserve the complete method.", {},
                               check_privacy(privacy))
    assert error.value.code == "private_package_content"
    assert "portable_output_bindings" not in requests(calls)[1]["repair_feedback"]["details"]
