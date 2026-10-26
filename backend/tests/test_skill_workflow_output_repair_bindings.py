"""Repair metadata names host-proven outputs without rewriting any invocation."""
from copy import deepcopy
import json

import pytest

from app.domain.services.skills.skill_creation.privacy import PrivacyContext, PrivateValue
from app.domain.services.skills.skill_creation.workflow_authoring import WorkflowDraft
from app.domain.services.skills.skill_creation.workflow_outputs import (
    normalize_generated_output_names, portable_output_repair_bindings,
)
from test_skill_workflow_output_normalization_adversarial import (
    SOURCE, authored_output, collect, context_for, host_output_event,
)


def test_reused_intermediate_outputs_have_one_binding_with_all_fixed_locations():
    draft = authored_output()
    second = deepcopy(draft.outputs[0])
    second.name = "historical-model.json"
    second.content = "Export the complete reusable model summary."
    second.acceptance = "Verify the declared model diagnostics."
    draft.outputs.append(second)
    producer = draft.implementation.scripts[0]
    producer.invocation = f"python {producer.path} --input INPUT --output <output_dir>"
    for index in range(2):
        consumer = producer.model_copy(deep=True)
        consumer.path = f"scripts/consumer_{index}.py"
        consumer.invocation = (f"python {consumer.path} --input "
            f"table=<run_dir>/{SOURCE} model=<run_dir>/{second.name} --output <other_dir>")
        draft.implementation.scripts.append(consumer)
    privacy = context_for(SOURCE, second.name)
    before = draft.model_dump_json()
    result = portable_output_repair_bindings(draft, privacy)
    assert len(result) == 2
    for index, binding in enumerate(result):
        original = draft.outputs[index].name
        assert set(binding) == {"output_index", "portable_name", "current_reference", "locations"}
        assert binding["output_index"] == index
        assert binding["portable_name"] == f"output_{index + 1:03d}.{original.rsplit('.', 1)[1]}"
        assert binding["current_reference"] == privacy.redact_text(original)
        assert ["outputs", index, "name"] in binding["locations"]
        assert binding["locations"][-2:] == [
            ["implementation", "scripts", 1, "invocation"],
            ["implementation", "scripts", 2, "invocation"],
        ]
        assert ["implementation", "scripts", 0, "invocation"] not in binding["locations"]
    encoded = json.dumps(result, ensure_ascii=False)
    assert SOURCE not in encoded and second.name not in encoded
    assert "run_dir" not in encoded and "other_dir" not in encoded
    assert not privacy.scan_document(result)
    assert draft.model_dump_json() == before
    # Declared intermediate input references remain untouched and private;
    # metadata does not authorize the host to infer equal output directories.
    assert normalize_generated_output_names(draft, privacy) is draft


def test_binding_and_normalizer_share_collision_allocation_and_original_indexes():
    draft = authored_output()
    public = deepcopy(draft.outputs[0])
    public.name, public.content, public.acceptance = "output_001.csv", "A separate output.", "Validate the other result."
    draft.outputs.insert(0, public)
    privacy = context_for(SOURCE, "output_002.csv")
    bindings = portable_output_repair_bindings(draft, privacy)
    normalized = normalize_generated_output_names(draft, privacy)
    assert len(bindings) == 1
    assert bindings[0]["output_index"] == 1
    assert bindings[0]["portable_name"] == normalized.outputs[1].name == "output_003.csv"
    assert normalized.outputs[0].name == "output_001.csv"


@pytest.mark.parametrize("duplicate", [SOURCE, SOURCE.upper()])
def test_duplicate_or_case_ambiguous_declarations_have_no_binding(duplicate):
    draft = authored_output()
    draft.outputs.append(draft.outputs[0].model_copy(update={"name": duplicate}, deep=True))
    assert portable_output_repair_bindings(draft, context_for(SOURCE)) == []


@pytest.mark.parametrize("role", ["unknown", "user", "selected", "casefold_input"])
def test_unknown_and_real_input_roles_never_provide_a_mapping(role):
    event = host_output_event()
    events = [event]
    if role == "unknown":
        event["attachments"][0].pop("metadata")
    elif role == "user":
        event["role"] = "user"
    elif role == "selected":
        events.append({"type": "message", "role": "user", "metadata": {
            "analysis_input_file_ids": [event["attachments"][0]["file_id"]]}})
    else:
        events.append({"type": "message", "role": "user", "attachments": [
            {"filename": SOURCE.upper(), "file_id": "selected-input", "user_id": "synthetic-owner"}]})
    assert portable_output_repair_bindings(authored_output(), collect(events)) == []


@pytest.mark.parametrize("name", ["/private/analysis/table.csv", "nested/table.csv", "result", "name with space.csv"])
def test_only_declared_basename_candidates_are_mapped(name):
    assert portable_output_repair_bindings(authored_output(name), context_for(name)) == []


@pytest.mark.parametrize("category", ["credential", "identity", "host_path", "url"])
def test_other_private_role_cannot_be_laundered_into_an_output_binding(category):
    privacy = context_for(SOURCE, extra=(PrivateValue(SOURCE, category),))
    assert portable_output_repair_bindings(authored_output(), privacy) == []


@pytest.mark.parametrize("redacted", [SOURCE, "unmarked", "{input_file_0}", "partial_{input_file_1}", "{redacted}"])
def test_unredacted_partial_or_non_filename_reference_is_never_exposed(monkeypatch, redacted):
    monkeypatch.setattr(PrivacyContext, "redact_text", lambda self, value: redacted)
    assert portable_output_repair_bindings(authored_output(), context_for(SOURCE)) == []


def test_alias_still_matching_private_content_is_not_exposed(monkeypatch):
    privacy = context_for(SOURCE, extra=(PrivateValue("{input_file_9}", "credential"),))
    monkeypatch.setattr(PrivacyContext, "redact_text", lambda self, value: "{input_file_9}")
    assert portable_output_repair_bindings(authored_output(), privacy) == []


@pytest.mark.parametrize("reference,located", [
    (SOURCE, True), (f"<run_dir>/{SOURCE}", True), (f"table={SOURCE}", True),
    (f'"<run_dir>/{SOURCE}"', True), (f"prefix{SOURCE}", False),
    (f"{SOURCE}.backup", False), (f"{SOURCE}/child", False),
    (f"other+{SOURCE}", False), (f"历史{SOURCE}", False),
])
def test_invocation_coordinate_matches_exact_basename_references_only(reference, located):
    draft = authored_output()
    draft.implementation.scripts[0].invocation += f" --extra {reference}"
    result = portable_output_repair_bindings(draft, context_for(SOURCE))
    location = ["implementation", "scripts", 0, "invocation"]
    assert (location in result[0]["locations"]) is located


def test_feedback_has_only_allowlisted_coordinates_and_no_other_private_content():
    draft = authored_output()
    draft.goal += " Do not expose private-owner-marker."
    draft.parameters[0].description += f" A literal mentioning {SOURCE} is not a rewrite surface."
    draft.implementation.scripts[0].purpose += f" Do not expose {SOURCE} as source text."
    draft.outputs[0].content += f" ```python\nprint('{SOURCE}')\n```"
    privacy = context_for(SOURCE, extra=(PrivateValue("private-owner-marker", "identity"),))
    before = draft.model_dump_json()
    result = portable_output_repair_bindings(draft, privacy)
    assert result and not privacy.scan_document(result)
    assert all(location[0] in {"outputs", "steps", "validation_cases", "implementation"}
               for location in result[0]["locations"])
    assert ["outputs", 0, "content"] not in result[0]["locations"]
    assert "private-owner-marker" not in json.dumps(result)
    assert draft.model_dump_json() == before
    assert normalize_generated_output_names(draft, privacy) is draft


def test_non_draft_and_undeclared_output_names_do_not_create_a_binding():
    draft = authored_output("public-table.csv")
    assert portable_output_repair_bindings(draft, context_for(SOURCE)) == []
    assert portable_output_repair_bindings({}, context_for(SOURCE)) == []
