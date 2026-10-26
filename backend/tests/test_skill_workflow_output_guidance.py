"""Display labels need safe naming guidance without guessing historical outputs."""
from copy import deepcopy
import json

import pytest
from langchain.messages import AIMessage

from app.domain.services.skills.skill_creation.privacy import PrivacyContext, PrivateValue
from app.domain.services.skills.skill_creation.workflow_authoring import WorkflowDraft
from app.domain.services.skills.skill_creation_error import SkillCreationError
from test_skill_creation_gateway import check_privacy, make_gateway, requests
from test_skill_workflow_authoring import draft_payload
from test_skill_workflow_output_repair_gateway import multistage_draft
from test_skill_workflow_privacy_repair_gateway import checker, patch


NAMES = ("historical-first.csv", "historical-second.csv", "historical-summary.json")


def display_labels():
    original = draft_payload()
    original["outputs"] = [{"name": f"Analysis product {index + 1}",
        "format": "JSON" if name.endswith(".json") else "CSV",
        "content": f"Export complete method output to `{name}` at alpha = 0.05.",
        "acceptance": f"Verify every declared result in `{name}` including the 0.0 boundary."}
        for index, name in enumerate(NAMES)]
    original["validation_cases"][0]["expected"] += f" Validate `{NAMES[2]}` without removing any requirement."
    # A concrete occupied name elsewhere is not an editable output label.
    original["steps"][0]["decision"] += " Retain existing artifact_001.csv unchanged."
    original = WorkflowDraft.model_validate(original).model_dump(mode="json")
    privacy = PrivacyContext(tuple(PrivateValue(name, "source_filename") for name in NAMES),
        generated_output_filenames=frozenset(NAMES))
    return original, privacy


def suggested_document(original, privacy):
    references = privacy.output_repair_references(original)
    names = {privacy.redact_text(name): name for name in NAMES}
    text = json.dumps(original)
    for reference in references:
        text = text.replace(names[reference["reference"]], reference["suggested_portable_name"])
    return json.loads(text)


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed_first", [False, True])
async def test_display_only_outputs_get_host_suggestions_for_every_private_leaf(malformed_first, caplog):
    original, privacy = display_labels()
    before = deepcopy(original)
    revised = suggested_document(original, privacy)
    corrections = patch(original, revised, privacy)
    responses = [original]
    if malformed_first:
        responses.append(AIMessage(content='{"repairs":'))
    responses.append(corrections)
    gateway, calls, _ = make_gateway(responses, privacy=privacy)
    seen = []
    result = await gateway.generate(WorkflowDraft, "workflow", "Preserve source methods and outputs.",
                                   {}, checker(privacy, seen))
    request = requests(calls)[1]
    feedback = request["repair_feedback"]
    assert len(feedback["field_repairs"]) == 7
    assert "portable_output_bindings" not in feedback["details"]
    references = feedback["details"]["output_repair_references"]
    assert references == privacy.output_repair_references(original)
    assert len(references) == 3
    assert all(row["role"] == "generated_output" and row["suggested_portable_name"] for row in references)
    assert all(row["suggested_portable_name"] != "artifact_001.csv" for row in references)
    assert all("suggested_portable_name" in row["hint"] for row in feedback["field_repairs"])
    assert "可采用 output_repair_references" in calls[1][0].content
    assert "不得捏造具体文件名" not in calls[1][0].content
    assert "Do not invent a basename" not in json.dumps(feedback)
    assert result.model_dump(mode="json") == revised and original == before
    assert seen == [original, revised]
    assert [item.name for item in result.outputs] == [row["name"] for row in original["outputs"]]
    for root in ("parameters", "steps", "dependencies", "implementation"):
        assert result.model_dump(mode="json")[root] == original[root]
    assert all(name not in json.dumps(requests(calls)) and name not in caplog.text for name in NAMES)
    if malformed_first:
        last = requests(calls)[2]["repair_feedback"]
        assert last["privacy_repair_context"] == feedback["privacy_repair_context"]
        assert last["privacy_repair_context"]["feedback"]["details"]["output_repair_references"] == references
        assert last["field_repairs"] == feedback["field_repairs"]
        assert last["reason"] == "invalid_json"


@pytest.mark.asyncio
async def test_explicit_portable_binding_suppresses_competing_suggestion_for_same_alias():
    original, privacy = multistage_draft()
    revised = json.loads(json.dumps(original).replace("old_result.csv", "output_001.csv"))
    gateway, calls, _ = make_gateway([original, patch(original, revised, privacy)], privacy=privacy)
    result = await gateway.generate(WorkflowDraft, "workflow", "Preserve all methods.", {}, check_privacy(privacy))
    details = requests(calls)[1]["repair_feedback"]["details"]
    binding = details["portable_output_bindings"][0]
    assert details["output_repair_references"] == [{"reference": binding["current_reference"], "role": "generated_output"}]
    assert binding["portable_name"] == "output_001.csv"
    assert result.model_dump(mode="json") == revised


@pytest.mark.asyncio
async def test_unknown_input_names_never_receive_generated_output_suggestions():
    original, owned = display_labels()
    privacy = PrivacyContext(owned.entries)
    unchanged = patch(original, original, privacy)
    gateway, calls, _ = make_gateway([original, unchanged], privacy=privacy)
    with pytest.raises(SkillCreationError) as error:
        await gateway.generate(WorkflowDraft, "workflow", "Preserve methods.", {}, checker(privacy, []))
    assert error.value.code == "private_package_content" and len(calls) == 2
    details = requests(calls)[1]["repair_feedback"]["details"]
    assert "output_repair_references" not in details and "portable_output_bindings" not in details
    assert not any("role" in issue for issue in details["privacy_issues"])


@pytest.mark.asyncio
async def test_suggestion_does_not_exempt_old_name_or_expand_same_error_budget():
    original, privacy = display_labels()
    gateway, calls, _ = make_gateway([original, patch(original, original, privacy)], privacy=privacy)
    with pytest.raises(SkillCreationError) as error:
        await gateway.generate(WorkflowDraft, "workflow", "Preserve methods.", {}, checker(privacy, []))
    assert error.value.code == "private_package_content" and len(calls) == 2
    assert not any(item.status == "passed" for item in gateway.attempts)
    assert requests(calls)[1]["repair_feedback"]["details"]["output_repair_references"]


@pytest.mark.asyncio
async def test_protected_default_full_authoring_also_gets_same_role_guidance():
    original, privacy = display_labels()
    original["parameters"][0]["default"] = NAMES[0]
    revised = suggested_document(original, privacy)
    gateway, calls, _ = make_gateway([original, revised], privacy=privacy)
    result = await gateway.generate(WorkflowDraft, "workflow", "Preserve methods.", {}, checker(privacy, []))
    feedback = requests(calls)[1]["repair_feedback"]
    assert "field_repairs" not in feedback
    assert feedback["details"]["output_repair_references"] == privacy.output_repair_references(original)
    assert result.model_dump(mode="json") == revised


@pytest.mark.asyncio
async def test_display_label_and_historical_reference_in_description_preserve_other_fields():
    original = draft_payload()
    original["description"] += f" The declared output was `{NAMES[0]}`."
    original = WorkflowDraft.model_validate(original).model_dump(mode="json")
    privacy = PrivacyContext((PrivateValue(NAMES[0], "source_filename"),),
        generated_output_filenames=frozenset({NAMES[0]}))
    reference = privacy.output_repair_references(original)[0]
    revised = deepcopy(original)
    revised["description"] = revised["description"].replace(NAMES[0], reference["suggested_portable_name"])
    gateway, calls, _ = make_gateway([original, patch(original, revised, privacy)], privacy=privacy)
    seen = []
    result = await gateway.generate(WorkflowDraft, "workflow", "Preserve all methods.", {}, checker(privacy, seen))
    feedback = requests(calls)[1]["repair_feedback"]
    assert feedback["details"]["output_repair_references"] == [reference]
    assert feedback["field_repairs"][0]["location"] == ["description"]
    assert len(feedback["field_repairs"]) == 1
    assert seen == [original, revised]
    final = result.model_dump(mode="json")
    assert final == revised
    assert {key: value for key, value in final.items() if key != "description"} == {
        key: value for key, value in original.items() if key != "description"}
