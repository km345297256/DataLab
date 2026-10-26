"""Complete host privacy scope permits only whole-string workflow leaf edits."""
from copy import deepcopy
import json

import pytest
from pydantic import ValidationError

from app.domain.services.skills.skill_creation.privacy import PrivacyContext, PrivateValue, scan_document
from app.domain.services.skills.skill_creation.schema_repair import SchemaFieldRepairs, SchemaRepairRejected
from app.domain.services.skills.skill_creation.workflow_authoring import WorkflowDraft, compile_workflow
from app.domain.services.skills.skill_creation.workflow_privacy_repair import (
    apply_workflow_privacy_repairs, build_workflow_privacy_repair_spec,
    workflow_privacy_response_schema,
)
from test_skill_workflow_authoring import contract, draft_payload


PRIVATE = "private-observations.csv"
PRIVACY = PrivacyContext((PrivateValue(PRIVATE, "source_filename"),))


def setup_repair(count=2):
    document = draft_payload(max(2, count))
    for item in document["steps"][:count]:
        item["action"] += " Read " + PRIVATE + " as the current input."
    draft = WorkflowDraft.model_validate(document)
    spec = build_workflow_privacy_repair_spec(draft, PRIVACY, stage="workflow")
    assert spec is not None
    return draft, spec


def at(value, path):
    for key in path:
        value = value[key]
    return value


def corrections(draft, spec):
    document = draft.model_dump(mode="json")
    return {"repairs": [{"field_id": item.field_id, "action": "set",
        "value": at(document, item.path).replace(PRIVATE, "the selected input")}
        for item in spec.fields]}


@pytest.mark.parametrize("count", [1, 16, 20, 32, 33, 67])
def test_complete_privacy_scan_builds_exact_dynamic_size_without_diagnostic_truncation(count):
    draft, spec = setup_repair(count)
    before = draft.model_dump(mode="json")
    assert len(spec.fields) == count
    assert len(PRIVACY.scan_document(draft)) == min(32, count)
    assert len(PRIVACY.scan_document(draft, limit=None)) == count
    assert scan_document(draft, PRIVACY, limit=None) == PRIVACY.scan_document(draft, limit=None)
    assert PRIVACY.scan_document(draft, limit=0) == []
    schema = workflow_privacy_response_schema(spec)
    assert schema.model_json_schema()["properties"]["repairs"]["minItems"] == count
    assert schema.model_json_schema()["properties"]["repairs"]["maxItems"] == count
    assert PRIVATE not in json.dumps(spec.public_fields())
    assert PRIVATE not in repr(spec)
    repaired = apply_workflow_privacy_repairs(draft, spec, schema.model_validate(corrections(draft, spec)))
    expected = deepcopy(before)
    for item in expected["steps"][:count]:
        item["action"] = item["action"].replace(PRIVATE, "the selected input")
    assert repaired == expected and draft.model_dump(mode="json") == before
    assert not PRIVACY.scan_document(repaired, limit=None)
    assert compile_workflow(WorkflowDraft.model_validate(repaired), contract()).coverage
    # This wider host-authorized path does not change generic schema repair size.
    if count > 16:
        with pytest.raises(ValidationError):
            SchemaFieldRepairs.model_validate(corrections(draft, spec))


@pytest.mark.parametrize("location", [
    ("description",), ("goal",), ("triggers", 0), ("applicability", 0),
    ("parameters", 0, "description"), ("parameters", 0, "constraints"),
    ("data_requirements", 0, "content"), ("dependencies", 0, "content"),
    ("steps", 0, "action"), ("steps", 0, "decision"), ("steps", 0, "verification"),
    ("outputs", 0, "name"), ("outputs", 0, "content"), ("outputs", 0, "acceptance"),
    ("validation_cases", 0, "input"), ("validation_cases", 0, "expected"),
    ("limitations", 0, "content"), ("implementation", "reason"),
])
def test_only_matching_natural_language_leaf_is_selected(location):
    document = draft_payload()
    at(document, location[:-1])[location[-1]] += " " + PRIVATE
    draft = WorkflowDraft.model_validate(document)
    spec = build_workflow_privacy_repair_spec(draft, PRIVACY, stage="workflow")
    assert spec is not None and [item.path for item in spec.fields] == [location]
    repaired = apply_workflow_privacy_repairs(draft, spec, corrections(draft, spec))
    before = draft.model_dump(mode="json")
    expected = deepcopy(before)
    at(expected, location[:-1])[location[-1]] = at(before, location).replace(PRIVATE, "the selected input")
    assert repaired == expected


@pytest.mark.parametrize("location,private", [
    (("name",), "covariance-analysis"),
    (("parameters", 0, "name"), "input_path"),
    (("parameters", 0, "default"), PRIVATE),
    (("steps", 5, "requirement_ids", 0), "R001"),
    (("validation_cases", 0, "kind"), "normal"),
    (("implementation", "kind"), "tool_workflow"),
])
def test_private_identity_default_or_enum_prevents_partial_scope(location, private):
    document = draft_payload()
    document["steps"][0]["action"] += " " + PRIVATE
    at(document, location[:-1])[location[-1]] = private
    privacy = PrivacyContext((PrivateValue(PRIVATE, "source_filename"), PrivateValue(private, "identity")))
    assert build_workflow_privacy_repair_spec(WorkflowDraft.model_validate(document), privacy, stage="workflow") is None


def test_privacy_in_schema_key_cannot_turn_into_parent_or_key_mutation():
    draft, _ = setup_repair()
    privacy = PrivacyContext((PrivateValue("description", "identity"),))
    assert privacy.scan_document(draft, limit=None)
    assert build_workflow_privacy_repair_spec(draft, privacy, stage="workflow") is None


@pytest.mark.parametrize("stage", ["implementation", "method_review", "synthetic_validation_plan", "synthetic_plan_review"])
def test_other_stages_cannot_gain_workflow_string_authority(stage):
    draft, _ = setup_repair()
    assert build_workflow_privacy_repair_spec(draft, PRIVACY, stage=stage) is None


def test_untyped_subclass_or_corrupt_workflow_is_not_an_editable_candidate():
    draft, _ = setup_repair()
    class OtherWorkflow(WorkflowDraft):
        pass
    assert build_workflow_privacy_repair_spec(draft.model_dump(), PRIVACY, stage="workflow") is None
    assert build_workflow_privacy_repair_spec(OtherWorkflow.model_validate(draft.model_dump()), PRIVACY, stage="workflow") is None
    changed = draft.model_copy(deep=True)
    changed.parameters[0].required = 0
    assert build_workflow_privacy_repair_spec(changed, PRIVACY, stage="workflow") is None


@pytest.mark.parametrize("problem", ["missing", "extra", "duplicate", "unknown", "remove", "container", "null", "number", "bool", "path"])
def test_patch_is_exact_ids_and_complete_strings_only(problem):
    draft, spec = setup_repair()
    patch = corrections(draft, spec)
    if problem == "missing":
        patch["repairs"].pop()
    elif problem == "extra":
        patch["repairs"].append(deepcopy(patch["repairs"][0]))
    elif problem == "duplicate":
        patch["repairs"][1]["field_id"] = patch["repairs"][0]["field_id"]
    elif problem == "unknown":
        patch["repairs"][0]["field_id"] = "F999"
    elif problem == "remove":
        patch["repairs"][0]["action"] = "remove"
    elif problem == "path":
        patch["repairs"][0]["path"] = ["steps", 0]
    else:
        patch["repairs"][0]["value"] = {"container": {}, "null": None, "number": 3, "bool": False}[problem]
    with pytest.raises(SchemaRepairRejected):
        apply_workflow_privacy_repairs(draft, spec, patch)


def test_candidate_digest_rejects_an_unselected_field_change():
    draft, spec = setup_repair()
    changed = draft.model_dump(mode="json")
    changed["parameters"][0]["required"] = False
    with pytest.raises(SchemaRepairRejected, match="Local schema") as failure:
        apply_workflow_privacy_repairs(changed, spec, corrections(draft, spec))
    assert failure.value.reason == "candidate_changed"


def test_merging_is_not_privacy_or_scientific_approval():
    draft, spec = setup_repair()
    unchanged = {"repairs": [{"field_id": item.field_id, "action": "set",
                             "value": at(draft.model_dump(), item.path)} for item in spec.fields]}
    merged = apply_workflow_privacy_repairs(draft, spec, unchanged)
    assert PRIVACY.scan_document(merged, limit=None)
    assert merged == draft.model_dump(mode="json")
