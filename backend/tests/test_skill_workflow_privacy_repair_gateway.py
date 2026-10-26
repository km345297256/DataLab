"""Workflow privacy corrections cannot replace the rest of the authored method."""
from copy import deepcopy
import json

import pytest
from langchain.messages import AIMessage

from app.domain.services.skills.method_contract import validate_coverage
from app.domain.services.skills.skill_creation.gateway import RepairRequired
from app.domain.services.skills.skill_creation.package import validate_recipe
from app.domain.services.skills.skill_creation.privacy import PrivacyContext, PrivateValue
from app.domain.services.skills.skill_creation.workflow_authoring import WorkflowDraft, check_workflow_draft
from app.domain.services.skills.skill_creation.workflow_privacy_repair import build_workflow_privacy_repair_spec
from app.domain.services.skills.skill_creation_error import SkillCreationError
from test_skill_creation_gateway import make_gateway, requests
from test_skill_workflow_authoring import contract, draft_payload


PRIVATE = "artificial-private-owner"


def fixture(count=1):
    clean = draft_payload(step_count=max(count, 6))
    original = deepcopy(clean)
    for item in original["steps"][:count]:
        item["verification"] += " Owned by " + PRIVATE
    return (WorkflowDraft.model_validate(original).model_dump(mode="json"),
            WorkflowDraft.model_validate(clean).model_dump(mode="json"),
            PrivacyContext((PrivateValue(PRIVATE, "identity"),)))


def patch(original, revised, privacy):
    spec = build_workflow_privacy_repair_spec(WorkflowDraft.model_validate(original), privacy, stage="workflow")
    assert spec is not None
    changes = []
    for field in spec.fields:
        value = revised
        for part in field.path:
            value = value[part]
        changes.append({"field_id": field.field_id, "action": "set", "value": value})
    return {"repairs": changes}


def checker(privacy, seen):
    def validate(value):
        seen.append(value.model_dump(mode="json"))
        def recipe_check(recipe):
            validate_recipe(recipe, privacy)
            validate_coverage(contract(), recipe.model_dump(mode="json"))
        check_workflow_draft(value, contract(), recipe_check)
    return validate


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 17, 33])
async def test_exact_all_privacy_leaves_merge_and_full_workflow_checks_run(count, caplog):
    original, clean, privacy = fixture(count)
    before = deepcopy(original)
    changes = patch(original, clean, privacy)
    gateway, calls, _ = make_gateway([original, changes], privacy=privacy)
    seen = []
    result = await gateway.generate(WorkflowDraft, "workflow", "Keep the method and conditions.",
                                    {"method_contract": contract().model_dump(mode="json")}, checker(privacy, seen))
    req = requests(calls)[1]
    assert result.model_dump(mode="json") == clean and original == before
    assert seen == [original, clean]
    assert len(req["repair_feedback"]["field_repairs"]) == count
    assert req["schema"]["title"] == "WorkflowPrivacyFieldRepairs"
    assert req["schema"]["properties"]["repairs"]["minItems"] == count
    assert req["schema"]["properties"]["repairs"]["maxItems"] == count
    assert req["repair_feedback"]["document_schema"] == WorkflowDraft.model_json_schema()
    assert PRIVATE not in json.dumps(requests(calls)) and PRIVATE not in caplog.text
    assert [x["field_id"] for x in req["repair_feedback"]["field_repairs"]] == [f"F{i:03d}" for i in range(1, count + 1)]
    assert "不重新生成整份工作流" in calls[1][0].content


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["unparsed", "truncated", "missing", "duplicate", "unknown", "remove", "container", "full_document", "empty_value"])
async def test_invalid_patch_retains_exact_original_and_field_scope(damage):
    original, clean, privacy = fixture(2)
    good = patch(original, clean, privacy)
    bad = deepcopy(good)
    if damage == "unparsed":
        bad = AIMessage(content=json.dumps(good) + "}")
    elif damage == "truncated":
        bad = AIMessage(content=json.dumps(good), response_metadata={"finish_reason": "length"})
    elif damage == "missing":
        bad["repairs"].pop()
    elif damage == "duplicate":
        bad["repairs"][1] = deepcopy(bad["repairs"][0])
    elif damage == "unknown":
        bad["repairs"][0]["field_id"] = "F999"
    elif damage == "remove":
        bad["repairs"][0]["action"] = "remove"
    elif damage == "container":
        bad["repairs"][0]["value"] = {"text": "Different node"}
    elif damage == "full_document":
        bad = clean
    else:
        bad["repairs"][0]["value"] = ""
    gateway, calls, _ = make_gateway([original, bad, good], privacy=privacy)
    result = await gateway.generate(WorkflowDraft, "workflow", "Keep methods.", {}, checker(privacy, []))
    first, second = requests(calls)[1:]
    assert result.model_dump(mode="json") == clean
    assert first["previous_candidate"] == second["previous_candidate"]
    assert first["schema"] == second["schema"]
    assert first["repair_feedback"]["field_repairs"] == second["repair_feedback"]["field_repairs"]
    assert "prior_unrechecked_feedback" not in second["repair_feedback"]
    assert PRIVATE not in json.dumps(requests(calls))


@pytest.mark.asyncio
async def test_patch_does_not_invoke_output_name_normalization_again(monkeypatch):
    from app.domain.services.skills.skill_creation import workflow_outputs
    original, clean, privacy = fixture()
    normalized = []
    def normalize(value, context):
        normalized.append(value.model_dump(mode="json"))
        return value
    monkeypatch.setattr(workflow_outputs, "normalize_generated_output_names", normalize)
    gateway, _, _ = make_gateway([original, patch(original, clean, privacy)], privacy=privacy)
    result = await gateway.generate(WorkflowDraft, "workflow", "Keep methods.", {}, checker(privacy, []))
    assert result.model_dump(mode="json") == clean
    assert normalized == [original]


@pytest.mark.asyncio
async def test_privacy_patch_cannot_turn_method_rejection_into_repair():
    original, clean, privacy = fixture()
    from app.domain.services.skills.method_contract import CoverageError
    def check(value):
        checker(privacy, [])(value)
        raise CoverageError("method_review_rejected", ["R001"])
    gateway, calls, _ = make_gateway([original, patch(original, clean, privacy), clean], privacy=privacy)
    with pytest.raises(SkillCreationError) as failure:
        await gateway.generate(WorkflowDraft, "workflow", "Keep methods.", {}, check)
    assert failure.value.code == "method_review_rejected" and len(calls) == 2


@pytest.mark.asyncio
async def test_repeated_patch_privacy_failure_keeps_retry_budget():
    original, clean, privacy = fixture()
    gateway, calls, _ = make_gateway([original, patch(original, original, privacy), patch(original, clean, privacy)], privacy=privacy)
    with pytest.raises(SkillCreationError) as failure:
        await gateway.generate(WorkflowDraft, "workflow", "Keep methods.", {}, checker(privacy, []))
    assert failure.value.code == "private_package_content" and len(calls) == 2


@pytest.mark.asyncio
async def test_protected_default_uses_complete_authoring_protocol_not_partial_privacy_patch():
    original, clean, privacy = fixture()
    original["parameters"][0]["default"] = PRIVATE
    gateway, calls, _ = make_gateway([original, clean], privacy=privacy)
    result = await gateway.generate(WorkflowDraft, "workflow", "Keep methods.", {}, checker(privacy, []))
    assert result.model_dump(mode="json") == clean
    assert requests(calls)[1]["schema"]["title"] == "WorkflowDraft"
    assert "field_repairs" not in requests(calls)[1]["repair_feedback"]


@pytest.mark.asyncio
async def test_malformed_patch_preserves_portable_binding_in_one_safe_scope_context():
    from test_skill_workflow_output_repair_gateway import multistage_draft
    original, privacy = multistage_draft()
    clean = json.loads(json.dumps(original).replace("old_result.csv", "output_001.csv"))
    good = patch(original, clean, privacy)
    from test_skill_creation_gateway import check_privacy
    gateway, calls, _ = make_gateway([original, AIMessage(content=json.dumps(good) + "}"), good], privacy=privacy)
    result = await gateway.generate(WorkflowDraft, "workflow", "Preserve all existing roles.", {}, check_privacy(privacy))
    first, second = requests(calls)[1:]
    context = first["repair_feedback"]["privacy_repair_context"]
    assert second["repair_feedback"]["privacy_repair_context"] == context
    assert context["basis"] == "original_check_of_bound_candidate"
    assert context["feedback"]["reason"] == "private_package_content"
    assert context["feedback"]["details"]["portable_output_bindings"][0]["portable_name"] == "output_001.csv"
    assert "privacy_repair_context" not in context["feedback"]
    assert second["repair_feedback"]["reason"] == "invalid_json"
    assert first["repair_feedback"]["field_repairs"] == second["repair_feedback"]["field_repairs"]
    assert "old_result.csv" not in json.dumps(requests(calls))
    assert result.model_dump(mode="json") == clean
