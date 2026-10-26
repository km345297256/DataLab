"""Output naming never grants privacy permission to input or identity fields."""
from copy import deepcopy
import json

from langchain.messages import AIMessage
import pytest

from app.domain.services.skills.skill_creation.gateway import RepairRequired
from app.domain.services.skills.skill_creation.models import SkillRecipe
from app.domain.services.skills.skill_creation.package import validate_portable_document
from app.domain.services.skills.skill_creation.privacy import PrivacyContext, PrivateValue, generation_privacy
from app.domain.services.skills.skill_creation.workflow_authoring import WorkflowDraft, compile_workflow
from app.domain.services.skills.skill_creation.workflow_outputs import normalize_generated_output_names
from app.domain.services.skills.skill_creation_error import SkillPackageValidationError
from test_skill_creation_gateway import make_gateway
from test_skill_workflow_authoring import contract, draft_payload, script_declaration


SOURCE = "historical-result.csv"


def authored_output(name=SOURCE):
    payload = draft_payload(step_count=2)
    payload["outputs"][0].update(name=name,
        content=f"Export HC3 covariance at alpha = 0.05 to `{name}`.",
        acceptance=f"Read `{name}` and verify the complete matrix and 0.0 boundary.")
    payload["steps"][0]["action"] += f" Write `{name}` after the complete calculation."
    payload["steps"][0]["verification"] += f" Inspect `{name}` without removing any coefficient."
    payload["validation_cases"][0]["expected"] += f" Verify `{name}` and retain 0.0 rather than null."
    payload["implementation"] = {"kind": "parameterized_scripts",
        "reason": "Preserve complete reproducible covariance calculation.", "scripts": [script_declaration()]}
    return WorkflowDraft.model_validate(payload)


def context_for(*names, extra=(), declared=True):
    return PrivacyContext(tuple(PrivateValue(name, "source_filename") for name in names) + tuple(extra),
        generated_output_filenames=frozenset(names if declared else ()))


def host_output_event(name=SOURCE):
    return {"type": "message", "role": "assistant", "message": "Artificial generated output.",
        "attachments": [{"file_id": "generated-artifact-one", "filename": name,
            "user_id": "synthetic-owner", "size": 1,
            "metadata": {"source": "sandbox_artifact", "session_id": "synthetic-session",
                         "artifact_size": 1, "artifact_sha256": "1" * 64}}]}


def collect(events):
    return PrivacyContext.collect(events, session_id="synthetic-session", user_id="synthetic-owner")


def assert_still_private(draft, privacy):
    original = draft.model_dump_json()
    normalized = normalize_generated_output_names(draft, privacy)
    assert draft.model_dump_json() == original
    assert normalized.model_dump_json() == original
    with pytest.raises(SkillPackageValidationError, match="来源名称或疑似敏感信息尚未处理完成"):
        validate_portable_document(normalized, privacy)


def test_only_filename_and_explicit_output_references_change_without_mutating_source():
    draft, privacy = authored_output(), context_for(SOURCE)
    before = draft.model_dump(mode="json")
    normalized = normalize_generated_output_names(draft, privacy)
    assert normalized is not draft
    assert draft.model_dump(mode="json") == before
    replacement = normalized.outputs[0].name
    assert replacement != SOURCE and replacement.endswith(".csv")
    validate_portable_document(normalized, privacy)
    assert normalize_generated_output_names(normalized, privacy).model_dump() == normalized.model_dump()
    # Reversing the single host-assigned label recovers every authored value;
    # numbers, method terms, requirement links and array positions are intact.
    assert json.loads(normalized.model_dump_json().replace(replacement, SOURCE)) == before
    for field in ("parameters", "data_requirements", "dependencies", "implementation"):
        assert normalized.model_dump()[field] == draft.model_dump()[field]
    assert compile_workflow(normalized, contract()).coverage == compile_workflow(draft, contract()).coverage
    assert all(item not in repr(privacy) for item in (SOURCE,))


def test_output_declaration_alone_cannot_launder_a_private_input_name():
    assert_still_private(authored_output(), context_for(SOURCE, declared=False))


@pytest.mark.parametrize("category", ["identity", "credential", "host_path", "url"])
def test_a_filename_also_marked_as_another_private_category_cannot_be_renamed(category):
    assert_still_private(authored_output(), context_for(SOURCE, extra=(PrivateValue(SOURCE, category),)))


@pytest.mark.parametrize("place", ["parameter_description", "parameter_default", "parameter_constraints",
    "data_requirements", "dependency", "limitation", "goal", "description", "trigger",
    "output_format", "validation_input", "script_path", "script_purpose", "script_invocation"])
def test_old_filename_in_any_unapproved_field_rejects_the_entire_normalization(place):
    payload = authored_output().model_dump()
    if place.startswith("parameter_"):
        field = place.removeprefix("parameter_")
        payload["parameters"][0][field] = SOURCE if field == "default" else f"Use {SOURCE} as historical input."
    elif place in {"data_requirements", "dependency", "limitation"}:
        root = {"dependency": "dependencies", "limitation": "limitations"}.get(place, place)
        payload[root][0]["content"] += f" Read {SOURCE} as an original input."
    elif place in {"goal", "description"}:
        payload[place] += f" Use {SOURCE} as an original input."
    elif place == "trigger":
        payload["triggers"][0] = f"Analyze {SOURCE}"
    elif place == "output_format":
        payload["outputs"][0]["format"] = SOURCE
    elif place == "validation_input":
        payload["validation_cases"][0]["input"] += f" Read {SOURCE}."
    else:
        field = place.removeprefix("script_")
        if field == "path":
            # Another permitted script basename can still be a private input;
            # never rewrite a script's identity to make privacy checks pass.
            payload["implementation"]["scripts"][0]["path"] = "scripts/historical-result.py"
            privacy = context_for(SOURCE, "historical-result.py")
            assert_still_private(WorkflowDraft.model_validate(payload), privacy)
            return
        payload["implementation"]["scripts"][0][field] += f" {SOURCE}"
    assert_still_private(WorkflowDraft.model_validate(payload), context_for(SOURCE))


@pytest.mark.parametrize("unsafe_reference", [
    "/private/results/historical-result.csv", r"C:\results\historical-result.csv",
    "https://example.invalid/historical-result.csv", "historical-result.csv.backup",
    "folder/historical-result.csv", "role:historical-result.csv",
])
def test_full_filename_tokens_never_match_inside_paths_urls_or_compound_names(unsafe_reference):
    draft = authored_output()
    draft.steps[0].decision += f" Read {unsafe_reference}."
    assert_still_private(draft, context_for(SOURCE))


def test_once_only_mapping_avoids_old_private_and_existing_public_name_collisions():
    draft = authored_output()
    second = deepcopy(draft.outputs[0])
    second.name, second.content, second.acceptance = "output_001.csv", "Preserve a second distinct result.", "Check the second complete output."
    third = deepcopy(second)
    third.name = "output_002.csv"  # Already public and must remain unchanged.
    draft.outputs.extend([second, third])
    draft.steps[0].decision += " Compare `historical-result.csv`, `output_001.csv` and `output_002.csv`."
    privacy = context_for(SOURCE, "output_001.csv")
    normalized = normalize_generated_output_names(draft, privacy)
    names = [item.name for item in normalized.outputs]
    assert len(set(names)) == 3
    assert names[2] == "output_002.csv"
    assert not {SOURCE, "output_001.csv"} & set(names)
    assert f"Compare `{names[0]}`, `{names[1]}` and `output_002.csv`." in normalized.steps[0].decision
    validate_portable_document(normalized, privacy)
    assert normalize_generated_output_names(normalized, privacy).model_dump_json() == normalized.model_dump_json()


def test_unrelated_filename_substrings_keep_their_exact_original_text():
    draft = authored_output()
    unrelated = "prefixhistorical-result.csv and historical-result.csv2"
    draft.steps[0].decision += f" Keep {unrelated} unchanged."
    normalized = normalize_generated_output_names(draft, context_for(SOURCE))
    assert unrelated in normalized.steps[0].decision
    assert normalized.outputs[0].name != SOURCE
    validate_portable_document(normalized, context_for(SOURCE))


@pytest.mark.parametrize("composite", [f"历史{SOURCE}", f"{SOURCE}患者", f"α{SOURCE}", f"{SOURCE}١",
    f"{SOURCE}\u0301", f"other+{SOURCE}", f"{SOURCE}@backup"])
def test_unicode_filename_components_are_not_partial_output_references(composite):
    draft = authored_output()
    draft.steps[0].decision += f" Read the separately named file `{composite}`."
    assert_still_private(draft, context_for(SOURCE))


def test_collector_requires_host_owned_generated_artifact_metadata():
    context = collect([host_output_event()])
    assert context.generated_output_filenames == frozenset({SOURCE})
    # The original privacy marker is retained; the role is not an exemption.
    assert PrivateValue(SOURCE, "source_filename") in context.entries
    assert context.scan_document({"description": SOURCE})
    scoped = generation_privacy(context, WorkflowDraft, "workflow", {})
    assert scoped.generated_output_filenames == context.generated_output_filenames


@pytest.mark.parametrize("mutation", ["user_role", "tool_type", "no_metadata", "wrong_source",
    "no_owner", "wrong_owner", "no_session", "other_session", "metadata_other_owner",
    "missing_hash", "invalid_hash", "missing_size", "bool_size", "negative_size", "missing_id", "empty_id"])
def test_unverified_artifact_metadata_never_authorizes_private_name_replacement(mutation):
    event = host_output_event()
    attachment = event["attachments"][0]
    metadata = attachment["metadata"]
    if mutation == "user_role": event["role"] = "user"
    elif mutation == "tool_type": event["type"] = "tool"
    elif mutation == "no_metadata": attachment.pop("metadata")
    elif mutation == "wrong_source": metadata["source"] = "upload"
    elif mutation == "no_owner": attachment.pop("user_id")
    elif mutation == "wrong_owner": attachment["user_id"] = "different-owner"
    elif mutation == "no_session": metadata.pop("session_id")
    elif mutation == "other_session": metadata["session_id"] = "different-session"
    elif mutation == "metadata_other_owner": metadata["user_id"] = "different-owner"
    elif mutation == "missing_hash": metadata.pop("artifact_sha256")
    elif mutation == "invalid_hash": metadata["artifact_sha256"] = "not-a-verified-hash"
    elif mutation == "missing_size": metadata.pop("artifact_size")
    elif mutation == "bool_size": metadata["artifact_size"] = True
    elif mutation == "missing_id": attachment.pop("file_id")
    elif mutation == "empty_id": attachment["file_id"] = ""
    else: metadata["artifact_size"] = -1
    privacy = collect([event])
    assert not privacy.generated_output_filenames
    assert_still_private(authored_output(), privacy)


@pytest.mark.parametrize("role", ["attachment_same_name", "attachment_casefold", "selected_input_id", "requested_input_id"])
def test_generated_output_reused_as_an_input_loses_rename_eligibility(role):
    output = host_output_event()
    user = {"type": "message", "role": "user", "message": "Analyze the selected current input.", "metadata": {}}
    if role.startswith("attachment_"):
        name = SOURCE.upper() if role.endswith("casefold") else SOURCE
        user["attachments"] = [{"file_id": "other-input-id", "filename": name, "user_id": "synthetic-owner"}]
    else:
        key = "analysis_input_file_ids" if role == "selected_input_id" else "requested_input_file_ids"
        user["metadata"][key] = [output["attachments"][0]["file_id"]]
    privacy = collect([output, user])
    assert not privacy.generated_output_filenames
    assert_still_private(authored_output(), privacy)


@pytest.mark.parametrize("mode", ["workflow_schema_wrong_stage", "recipe_schema_workflow_stage"])
def test_gateway_does_not_apply_output_normalization_to_other_protocols(mode):
    privacy, draft = context_for(SOURCE), authored_output()
    gateway, _, _ = make_gateway([], privacy=privacy)
    if mode == "workflow_schema_wrong_stage":
        schema, stage, value = WorkflowDraft, "implementation", draft
    else:
        schema, stage, value = SkillRecipe, "workflow", compile_workflow(draft, contract())
    with pytest.raises(RepairRequired) as error:
        gateway._validate(AIMessage(content=value.model_dump_json()), schema, stage,
            lambda candidate: validate_portable_document(candidate, privacy), contract(), privacy=privacy)
    assert error.value.feedback["reason"] == "private_package_content"
