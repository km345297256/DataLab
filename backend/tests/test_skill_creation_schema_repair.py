"""Local repairs preserve large independent oracles byte-for-byte elsewhere."""
from copy import deepcopy
import json

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from app.domain.services.skills.skill_creation.schema_repair import (
    SchemaFieldRepairs, SchemaRepairRejected, apply_schema_repairs, build_schema_repair_spec,
)
from app.domain.services.skills.skill_validation import SyntheticValidationPlan
from test_skill_validation_plan import plan_payload


def large_plan():
    plan = plan_payload()
    for case in plan["scripts"][0]["cases"][:2]:
        case["assertions"] = [{"type": "json_value", "path": "summary.json", "pointer": f"/metrics/value_{index}",
                               "expected": {"value": index + 0.25, "atol": 1e-9, "rtol": 0}}
                              for index in range(46)]
    SyntheticValidationPlan.model_validate(plan)
    return plan


def spec_for(candidate, schema=SyntheticValidationPlan):
    with pytest.raises(ValidationError) as failure:
        schema.model_validate(candidate)
    return build_schema_repair_spec(candidate, failure.value, schema)


def get(document, location):
    for part in location:
        document = document[part]
    return document


def damaged_plan():
    original = large_plan()
    candidate = deepcopy(original)
    for case_index, assertion_index in [(0, 17), (1, 31)]:
        assertion = candidate["scripts"][0]["cases"][case_index]["assertions"][assertion_index]
        assertion["path"] = assertion.pop("pointer")
    candidate["scripts"][0]["cases"][1].pop("source_ids")
    return original, candidate


def corrections(spec, correct):
    return SchemaFieldRepairs.model_validate({"repairs": [
        {"field_id": item["field_id"], "action": item["action"],
         **({"value": get(correct, item["location"])} if item["action"] == "set" else {})}
        for item in spec.public_fields()]})


def test_92_assertion_plan_repairs_only_union_fields_and_missing_source_ids():
    original, candidate = damaged_plan()
    before = deepcopy(candidate)
    spec = spec_for(candidate)
    assert spec is not None and len(spec.fields) == 5
    fields = spec.public_fields()
    assert [item["field_id"] for item in fields] == ["F001", "F002", "F003", "F004", "F005"]
    assert all("json_value" not in item["location"] for item in fields)
    assert {item["location"][-1] for item in fields} == {"path", "pointer", "source_ids"}
    repaired = apply_schema_repairs(candidate, spec, corrections(spec, original))
    assert repaired == original and candidate == before
    assert repaired is not candidate and repaired["scripts"] is not candidate["scripts"]
    assert sum(len(case["assertions"]) for case in repaired["scripts"][0]["cases"]) == 92
    SyntheticValidationPlan.model_validate(repaired)
    assert spec.public_fields() == spec_for(candidate).public_fields()


def test_fixed_pointer_and_path_hints_do_not_echo_invalid_pointer_values():
    _, candidate = damaged_plan()
    spec = spec_for(candidate)
    path = next(item for item in spec.public_fields() if item["location"][-1] == "path")
    pointer = next(item for item in spec.public_fields() if item["location"][-1] == "pointer")
    assert "relative output filename" in path["hint"]
    assert "RFC 6901" in pointer["hint"]
    assert "/metrics/value_17" not in json.dumps(spec.public_fields())


@pytest.mark.parametrize("problem", ["duplicate", "unknown", "missing", "remove_declared", "parent_object", "list", "missing_value"])
def test_field_patch_cannot_modify_other_fields_parent_objects_or_list_structure(problem):
    original, candidate = damaged_plan()
    spec = spec_for(candidate)
    repairs = corrections(spec, original).model_dump(exclude_unset=True)
    if problem == "duplicate":
        repairs["repairs"].append(deepcopy(repairs["repairs"][0]))
    elif problem == "unknown":
        repairs["repairs"][0]["field_id"] = "F999"
    elif problem == "missing":
        repairs["repairs"].pop()
    elif problem == "remove_declared":
        repairs["repairs"][0]["action"] = "remove"
    elif problem == "parent_object":
        repairs["repairs"][0]["value"] = {"expected": {"value": 0}, "pointer": ""}
    elif problem == "list":
        repairs["repairs"][0]["value"] = []
    else:
        repairs["repairs"][0].pop("value")
    before = deepcopy(candidate)
    with pytest.raises(SchemaRepairRejected):
        apply_schema_repairs(candidate, spec, SchemaFieldRepairs.model_validate(repairs))
    assert candidate == before


def test_model_supplied_paths_and_expected_replacement_are_not_part_of_patch_schema():
    with pytest.raises(ValidationError):
        SchemaFieldRepairs.model_validate({"repairs": [{"field_id": "F001", "action": "set", "value": "summary.json",
                                                       "path": "/scripts/0/cases/0/assertions/0/expected/value"}]})
    with pytest.raises(ValidationError):
        SchemaFieldRepairs.model_validate({"repairs": [{"field_id": "F001", "action": "set", "value": "summary.json"}],
                                          "expected": {"value": 0}})


def test_spec_is_bound_to_original_candidate_including_unchanged_scientific_values():
    original, candidate = damaged_plan()
    spec = spec_for(candidate)
    candidate["scripts"][0]["cases"][0]["assertions"][0]["expected"]["value"] = 999
    with pytest.raises(SchemaRepairRejected) as failure:
        apply_schema_repairs(candidate, spec, corrections(spec, original))
    assert failure.value.reason == "candidate_changed"


def test_missing_scalar_list_can_be_added_but_cannot_add_nested_fixture_objects():
    original, candidate = damaged_plan()
    spec = spec_for(candidate)
    repairs = corrections(spec, original).model_dump(exclude_unset=True)
    source_field = next(item["field_id"] for item in spec.public_fields() if item["location"][-1] == "source_ids")
    next(item for item in repairs["repairs"] if item["field_id"] == source_field)["value"] = [{"expected": 0}]
    with pytest.raises(SchemaRepairRejected, match="Local schema"):
        apply_schema_repairs(candidate, spec, SchemaFieldRepairs.model_validate(repairs))


def test_extra_field_is_removed_by_opaque_id_without_exposing_private_key_or_error_input():
    candidate = large_plan()
    secret_key = "private_owner_and_credential_key"
    candidate["scripts"][0]["cases"][0][secret_key] = {"value": "private-sensitive-value"}
    spec = spec_for(candidate)
    assert spec is not None and len(spec.fields) == 1
    field = spec.public_fields()[0]
    assert field["location"][-1] == "<extra_field>" and field["action"] == "remove"
    assert secret_key not in json.dumps(spec.public_fields()) and secret_key not in repr(spec)
    assert "private-sensitive-value" not in json.dumps(spec.public_fields())
    result = apply_schema_repairs(candidate, spec, SchemaFieldRepairs.model_validate({"repairs": [{"field_id": "F001", "action": "remove"}]}))
    assert result == large_plan() and secret_key in candidate["scripts"][0]["cases"][0]


def test_custom_validation_message_and_context_are_never_exposed_in_field_spec():
    class PrivateMessage(BaseModel):
        path: str

        @field_validator("path")
        @classmethod
        def check(cls, value):
            raise ValueError("private exception input: " + value)

    spec = spec_for({"path": "/private/hidden-owner/data.csv"}, PrivateMessage)
    assert spec is not None
    rendered = json.dumps(spec.public_fields())
    assert "hidden-owner" not in rendered and "private exception" not in rendered
    assert "/private/" not in rendered


@pytest.mark.parametrize("problem", ["union_tag", "parent_error", "existing_list", "expected", "tolerance", "fixture", "too_many"])
def test_ambiguous_container_and_scientific_errors_use_existing_full_validation_path(problem):
    candidate = large_plan()
    case = candidate["scripts"][0]["cases"][0]
    if problem == "union_tag":
        case["assertions"][0]["type"] = "unrecognized"
    elif problem == "parent_error":
        case["expected_exit_code"] = 1
    elif problem == "existing_list":
        case["source_ids"] = []
    elif problem == "expected":
        case["assertions"][0]["expected"]["value"] = []
    elif problem == "tolerance":
        case["assertions"][0]["expected"]["atol"] = -1
    elif problem == "fixture":
        case["input_files"][0]["path"] = "/private/synthetic.csv"
    else:
        for assertion in case["assertions"]:
            assertion.pop("pointer")
    assert spec_for(candidate) is None


def test_real_field_named_like_union_tag_is_not_stripped_without_a_discriminator():
    class Inner(BaseModel):
        title: str
    class Outer(BaseModel):
        json_value: Inner
    spec = spec_for({"json_value": {}}, Outer)
    assert spec.public_fields()[0]["location"] == ["json_value", "title"]
    repaired = apply_schema_repairs({"json_value": {}}, spec, SchemaFieldRepairs.model_validate({"repairs": [{"field_id": "F001", "action": "set", "value": "/result"}]}))
    assert Outer.model_validate(repaired).json_value.title == "/result"


def test_ordinary_union_branch_errors_are_ambiguous_and_not_guessed():
    class A(BaseModel):
        model_config = ConfigDict(extra="forbid")
        title: str
    class B(BaseModel):
        model_config = ConfigDict(extra="forbid")
        name: str
    class Both(BaseModel):
        result: A | B
    assert spec_for({"result": {}}, Both) is None


def test_patch_application_does_not_claim_schema_or_domain_acceptance():
    original, candidate = damaged_plan()
    spec = spec_for(candidate)
    repairs = corrections(spec, original).model_dump(exclude_unset=True)
    repairs["repairs"][0]["value"] = "/still/not/a/relative/output/file"
    patched = apply_schema_repairs(candidate, spec, SchemaFieldRepairs.model_validate(repairs))
    with pytest.raises(ValidationError):
        SyntheticValidationPlan.model_validate(patched)
    assert patched["scripts"][0]["cases"][0]["assertions"][0]["expected"] == original["scripts"][0]["cases"][0]["assertions"][0]["expected"]


@pytest.mark.parametrize("original_pointer,replacement", [
    ("/paired/1/n", "/paired/1/excluded"),
    ("/paired/1/n", "/paired/0/n"),
    ("/paired/a~1b/n", "/paired/a/b/n"),
])
def test_misplaced_pointer_cannot_change_original_assertion_target(original_pointer, replacement):
    correct = large_plan()
    assertion = correct["scripts"][0]["cases"][0]["assertions"][0]
    assertion.update(path="report.json", pointer=original_pointer)
    candidate = deepcopy(correct)
    bad = candidate["scripts"][0]["cases"][0]["assertions"][0]
    bad["path"] = bad.pop("pointer")
    before = deepcopy(candidate)
    spec = spec_for(candidate)
    assert spec is not None
    pointer_field = next(item for item in spec.public_fields() if item["location"][-1] == "pointer")
    assert "byte-for-byte" in pointer_field["hint"]
    assert original_pointer not in json.dumps(spec.public_fields()) and original_pointer not in repr(spec)
    repairs = corrections(spec, correct).model_dump(exclude_unset=True)
    next(item for item in repairs["repairs"] if item["field_id"] == pointer_field["field_id"])["value"] = replacement
    with pytest.raises(SchemaRepairRejected) as failure:
        apply_schema_repairs(candidate, spec, SchemaFieldRepairs.model_validate(repairs))
    assert failure.value.reason == "pointer_target_must_be_preserved"
    assert original_pointer not in str(failure.value) and replacement not in str(failure.value)
    assert candidate == before
    recovered = apply_schema_repairs(candidate, spec, corrections(spec, correct))
    assert recovered == correct  # Includes every unchanged expectation and all other 91 assertions.
    SyntheticValidationPlan.model_validate(recovered)


@pytest.mark.parametrize("path", ["report.json", "", "/paired/~2/n", "paired/1/n", None])
def test_missing_pointer_without_unambiguous_misplaced_target_returns_to_full_design(path):
    candidate = large_plan()
    assertion = candidate["scripts"][0]["cases"][0]["assertions"][0]
    assertion["path"] = path
    assertion.pop("pointer")
    assert spec_for(candidate) is None


def test_missing_path_and_pointer_cannot_invent_an_assertion_target_via_local_repair():
    candidate = large_plan()
    assertion = candidate["scripts"][0]["cases"][0]["assertions"][0]
    assertion.pop("path")
    assertion.pop("pointer")
    assert spec_for(candidate) is None
