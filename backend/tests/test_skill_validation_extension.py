"""Coverage can grow without weakening or rewriting a frozen scientific plan."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.domain.services.skills.skill_creation_error import SkillPackageValidationError
from app.domain.services.skills.skill_validation import SyntheticValidationPlan
from app.domain.services.skills.skill_validation_extension import (
    VerificationCoverageAmendment, apply_coverage_amendment, validate_coverage_extension,
    CoverageReferenceConfirmation, validate_reference_coverage_confirmation,
    validate_required_remediations,
)
from test_skill_validation_plan import plan_payload


def original():
    return SyntheticValidationPlan.model_validate(plan_payload())


def additions():
    path = "scripts/group_summary.py"
    return {"assertion_additions": [
        {"script_path": path, "case_id": "group-normal", "assertion": {
            "type": "json_object_keys", "path": "status.json", "pointer": "", "keys": ["status"]}},
        {"script_path": path, "case_id": "group-boundary", "assertion": {
            "type": "csv_header", "path": "summary.csv", "columns": [
                "group", "mean", "valid_count", "missing_count", "missing_rate"]}},
        {"script_path": path, "case_id": "group-invalid", "assertion": {"type": "output_empty"}},
    ], "new_cases": []}


def new_case(identifier="group-invalid-extra"):
    value = deepcopy(plan_payload()["scripts"][0]["cases"][2])
    value.update(id=identifier, expected_exit_code="nonzero", assertions=[{"type": "output_empty"}])
    value["input_files"][0]["rows"][0]["value"] = "another-invalid-value"
    return {"script_path": "scripts/group_summary.py", "case": value}


def amend(plan, value):
    return apply_coverage_amendment(plan, VerificationCoverageAmendment.model_validate(value))


def test_appends_structure_and_rejection_without_touching_original_bytes():
    before = original()
    old_json = before.model_dump_json()
    amendment = additions()
    amendment["new_cases"] = [new_case()]
    revised = amend(before, amendment)
    assert before.model_dump_json() == old_json
    assert revised.scripts[0].cases[:3] != before.scripts[0].cases
    assert [case.id for case in revised.scripts[0].cases] == [
        "group-normal", "group-boundary", "group-invalid", "group-invalid-extra"]
    validate_coverage_extension(before, revised)
    assert type(before.scripts[0].cases[0].assertions[0].fields["valid_count"].atol) is float
    assert type(revised.scripts[0].cases[0].assertions[0].fields["valid_count"].atol) is float


def test_cumulative_extension_validates_against_its_original_frozen_basis():
    before = original()
    first = amend(before, {"assertion_additions": additions()["assertion_additions"]})
    second = amend(first, {"new_cases": [new_case()]})
    validate_coverage_extension(before, second)
    validate_coverage_extension(first, second)


@pytest.mark.parametrize("code", ["nonzero", 1, 2, 123])
def test_new_rejection_case_accepts_only_normal_rejection_exit_contracts(code):
    value = new_case()
    value["case"]["expected_exit_code"] = code
    assert amend(original(), {"new_cases": [value]}).scripts[0].cases[-1].expected_exit_code == code


@pytest.mark.parametrize("code", [0, 124, 137, 255, -1, True, "any"])
def test_new_rejection_cannot_be_success_timeout_signal_or_coerced_exit(code):
    value = new_case()
    value["case"].update(expected_exit_code=code, assertions=[])
    with pytest.raises(ValidationError):
        VerificationCoverageAmendment.model_validate({"new_cases": [value]})


def success_case(kind="normal", identifier="new-success"):
    value = {"script_path": "scripts/group_summary.py", "case": deepcopy(plan_payload()["scripts"][0]["cases"][0])}
    value["case"].update(kind=kind, id=identifier)
    value["case"]["input_files"][0]["rows"][0]["value"] = 6
    value["case"]["assertions"][0]["fields"]["mean"]["value"] = 5
    return value


@pytest.mark.parametrize("kind", ["normal", "boundary"])
def test_new_successful_probe_keeps_all_old_values_and_needs_separate_reference_confirmation(kind):
    before = original()
    revised = amend(before, {"new_cases": [success_case(kind)]})
    assert revised.scripts[0].cases[:-1] == before.scripts[0].cases
    assert revised.scripts[0].cases[-1].assertions[0].fields["mean"].value == 5
    validate_coverage_extension(before, revised)


@pytest.mark.parametrize("assertion", [
    {"type": "json_value", "path": "status.json", "pointer": "/value", "expected": {"value": 4}},
    {"type": "json_array_length", "path": "status.json", "pointer": "/values", "length": 4},
    {"type": "csv_rows", "path": "summary.csv", "match": {}, "count": 1, "fields": {}},
])
def test_amendment_schema_never_accepts_new_scientific_values(assertion):
    with pytest.raises(ValidationError):
        VerificationCoverageAmendment.model_validate({"assertion_additions": [
            {"script_path": "scripts/group_summary.py", "case_id": "group-normal", "assertion": assertion}]})


@pytest.mark.parametrize("damage", ["input", "argv", "kind", "exit", "requirements", "sources", "description",
    "explanation", "value", "tolerance", "scalar_type", "bool_type", "assertion_order", "case_order",
    "script_path", "support", "reason", "version", "status", "remove_assertion", "remove_case"])
def test_cumulative_validator_rejects_every_old_contract_mutation(damage):
    before = original()
    revised = amend(before, additions())
    script = revised.scripts[0]
    case = script.cases[0]
    if damage == "input":
        case.input_files[0].rows[0]["value"] = 100
    elif damage == "argv":
        case.argv[-1] = "{output}/renamed"
    elif damage == "kind":
        case.kind = "boundary"
    elif damage == "exit":
        case.expected_exit_code = 1
    elif damage == "requirements":
        case.requirement_ids = ["R999"]
    elif damage == "sources":
        case.source_ids = ["S999"]
    elif damage == "description":
        case.description = "A rewritten old description."
    elif damage == "explanation":
        case.oracle_explanation = "A rewritten original independent explanation."
    elif damage == "value":
        case.assertions[0].fields["mean"].value = 100
    elif damage == "tolerance":
        case.assertions[0].fields["mean"].atol = 10
    elif damage == "scalar_type":
        case.assertions[0].fields["valid_count"].atol = 0
    elif damage == "bool_type":
        case.input_files[0].rows[0]["value"] = True
    elif damage == "assertion_order":
        case.assertions[0], case.assertions[1] = case.assertions[1], case.assertions[0]
    elif damage == "case_order":
        script.cases[0], script.cases[1] = script.cases[1], script.cases[0]
    elif damage == "script_path":
        script.script_path = "scripts/replacement.py"
    elif damage == "support":
        script.support = "unsupported"
    elif damage == "reason":
        script.reason = "A different declared support reason."
    elif damage == "version":
        revised.version = 2
    elif damage == "status":
        revised.status = "passed"
    elif damage == "remove_assertion":
        case.assertions.pop(0)
    else:
        script.cases.pop()
    with pytest.raises(SkillPackageValidationError):
        validate_coverage_extension(before, revised)


@pytest.mark.parametrize("damage", ["unknown_script", "unknown_case", "duplicate_addition", "duplicate_old",
    "json_missing_old_key", "json_scalar_target", "json_descends_scalar", "csv_missing_old_column",
    "csv_json_file", "json_csv_file", "shape_on_invalid", "absence_on_success"])
def test_appended_structure_must_be_new_consistent_and_on_a_success_case(damage):
    before = original()
    item = additions()["assertion_additions"][0]
    payload = {"assertion_additions": [item]}
    if damage == "unknown_script":
        item["script_path"] = "scripts/unknown.py"
    elif damage == "unknown_case":
        item["case_id"] = "unknown-case"
    elif damage == "duplicate_addition":
        payload["assertion_additions"].append(deepcopy(item))
    elif damage == "duplicate_old":
        before = amend(before, payload)
    elif damage == "json_missing_old_key":
        item["assertion"]["keys"] = []
    elif damage == "json_scalar_target":
        item["assertion"]["pointer"] = "/status"
    elif damage == "json_descends_scalar":
        item["assertion"]["pointer"] = "/status/nested"
    elif damage == "csv_missing_old_column":
        item["assertion"] = {"type": "csv_header", "path": "summary.csv", "columns": ["mean"]}
    elif damage == "csv_json_file":
        item["assertion"] = {"type": "csv_header", "path": "status.json", "columns": ["status"]}
    elif damage == "json_csv_file":
        item["assertion"]["path"] = "summary.csv"
    elif damage == "shape_on_invalid":
        item["case_id"] = "group-invalid"
    else:
        item["assertion"] = {"type": "output_empty"}
    with pytest.raises(SkillPackageValidationError):
        amend(before, payload)


@pytest.mark.parametrize("damage", ["duplicate_id", "duplicate_new_id", "unknown_script", "renamed_old_case"])
def test_rejection_cases_cannot_replace_old_ids_or_fake_new_coverage(damage):
    value = new_case()
    payload = {"new_cases": [value]}
    if damage == "duplicate_id":
        value["case"]["id"] = "group-invalid"
    elif damage == "duplicate_new_id":
        second = deepcopy(value)
        second["case"]["input_files"][0]["rows"][0]["value"] = "different-invalid"
        payload["new_cases"].append(second)
    elif damage == "unknown_script":
        value["script_path"] = "scripts/unknown.py"
    else:
        value["case"] = deepcopy(plan_payload()["scripts"][0]["cases"][2])
        value["case"]["id"] = "just-renamed"
        value["case"]["description"] = "Merely another explanation and identifier."
    with pytest.raises(SkillPackageValidationError):
        amend(original(), payload)


def test_empty_extension_is_not_a_new_vote_for_the_same_rejected_plan():
    with pytest.raises(ValidationError):
        VerificationCoverageAmendment.model_validate({})
    before = original()
    with pytest.raises(SkillPackageValidationError) as captured:
        validate_coverage_extension(before, before.model_copy(deep=True))
    assert captured.value.code == "validation_oracle_revision_unchanged"


def test_unknown_response_fields_cannot_smuggle_a_replacement_plan_or_expected_answers():
    for key in ("plan", "expected", "source_code", "status"):
        with pytest.raises(ValidationError):
            VerificationCoverageAmendment.model_validate({**additions(), key: "not allowed"})


def test_source_ids_remain_required_for_every_new_rejection_case():
    value = new_case()
    del value["case"]["source_ids"]
    with pytest.raises(ValidationError):
        VerificationCoverageAmendment.model_validate({"new_cases": [value]})


def test_supported_script_scope_and_order_cannot_change_in_a_cumulative_extension():
    payload = plan_payload()
    payload["scripts"].append({"script_path": "scripts/unsupported.py", "support": "unsupported",
        "reason": "This script cannot be observed by the permitted validation boundary.", "cases": []})
    before = SyntheticValidationPlan.model_validate(payload)
    value = new_case()
    value["script_path"] = "scripts/unsupported.py"
    with pytest.raises(SkillPackageValidationError):
        amend(before, {"new_cases": [value]})
    revised = amend(before, additions())
    revised.scripts.reverse()
    with pytest.raises(SkillPackageValidationError):
        validate_coverage_extension(before, revised)


def test_new_case_cannot_repeat_original_input_under_a_different_exit_spelling():
    before = original()
    value = new_case()
    value["case"]["input_files"] = deepcopy(plan_payload()["scripts"][0]["cases"][2]["input_files"])
    with pytest.raises(SkillPackageValidationError):
        amend(before, {"new_cases": [value]})


@pytest.mark.parametrize("old_pointer,new_pointer,keys,allowed", [
    ("/a~1b/status", "/a~1b", ["status"], True),
    ("/a~1b/status", "/a~1b", ["a/b"], False),
    ("/a~0b/status", "/a~0b", ["status"], True),
    ("/status", "", [], False),
])
def test_json_shape_relations_use_decoded_rfc6901_segments(old_pointer, new_pointer, keys, allowed):
    payload = plan_payload()
    payload["scripts"][0]["cases"][0]["assertions"][1]["pointer"] = old_pointer
    before = SyntheticValidationPlan.model_validate(payload)
    item = additions()["assertion_additions"][0]
    item["assertion"].update(pointer=new_pointer, keys=keys)
    if allowed:
        amend(before, {"assertion_additions": [item]})
    else:
        with pytest.raises(SkillPackageValidationError):
            amend(before, {"assertion_additions": [item]})


@pytest.mark.parametrize("pointer,allowed", [("/items/0", True), ("/items/1", False),
    ("/items/no-index", False), ("/items/00", False), ("/items/" + "9" * 5000, False)])
def test_structure_within_a_frozen_array_respects_its_existing_length(pointer, allowed):
    payload = plan_payload()
    payload["scripts"][0]["cases"][0]["assertions"].append({
        "type": "json_array_length", "path": "status.json", "pointer": "/items", "length": 1})
    before = SyntheticValidationPlan.model_validate(payload)
    item = additions()["assertion_additions"][0]
    item["assertion"].update(pointer=pointer, keys=[])
    if allowed:
        amend(before, {"assertion_additions": [item]})
    else:
        with pytest.raises(SkillPackageValidationError):
            amend(before, {"assertion_additions": [item]})


def test_cumulative_structure_cannot_contradict_an_earlier_added_object_keys_contract():
    before = original()
    first = amend(before, {"assertion_additions": [additions()["assertion_additions"][0]]})
    item = additions()["assertion_additions"][0]
    item["assertion"].update(pointer="/not-an-original-key", keys=[])
    with pytest.raises(SkillPackageValidationError):
        amend(first, {"assertion_additions": [item]})


def test_scalar_valued_data_type_cannot_be_rewritten_even_when_numerically_equal():
    before = original()
    revised = amend(before, additions())
    revised.scripts[0].cases[0].input_files[0].rows[0]["value"] = 2.0
    with pytest.raises(SkillPackageValidationError):
        validate_coverage_extension(before, revised)


@pytest.mark.parametrize("kind", ["normal", "boundary"])
def test_new_probe_cannot_rename_existing_inputs_by_changing_kind_or_description(kind):
    value = success_case(kind)
    value["case"]["input_files"] = deepcopy(plan_payload()["scripts"][0]["cases"][0]["input_files"])
    value["case"]["description"] = "A different narrative cannot create a different input experiment."
    with pytest.raises(SkillPackageValidationError):
        amend(original(), {"new_cases": [value]})


def test_same_data_with_different_actual_arguments_is_a_distinct_probe():
    value = success_case()
    value["case"]["input_files"] = deepcopy(plan_payload()["scripts"][0]["cases"][0]["input_files"])
    value["case"]["argv"] += ["--threshold", "2"]
    # Interface-specific allowed arguments are validated by the caller against
    # the frozen public interface, not guessed by this append-only boundary.
    amend(original(), {"new_cases": [value]})


@pytest.mark.parametrize("type", ["output_empty", "stderr_contains"])
def test_new_successful_case_cannot_smuggle_rejection_assertions(type):
    value = success_case()
    value["case"]["assertions"] = [{"type": type, **({"expected": "missing group"} if type == "stderr_contains" else {})}]
    with pytest.raises(ValidationError):
        VerificationCoverageAmendment.model_validate({"new_cases": [value]})


def diagnostic(case="group-invalid", text="missing group"):
    return {"script_path": "scripts/group_summary.py", "case_id": case,
            "assertion": {"type": "stderr_contains", "expected": text}}


def test_invalid_case_can_observe_exact_diagnostic_and_no_partial_outputs():
    before = original()
    revised = amend(before, {"assertion_additions": [diagnostic(), additions()["assertion_additions"][2]]})
    assert [item.type for item in revised.scripts[0].cases[2].assertions] == ["stderr_contains", "output_empty"]
    assert revised.scripts[0].cases[2].assertions[0].expected == "missing group"
    validate_coverage_extension(before, revised)
    # A different explicitly required literal is a new observable condition.
    second = amend(revised, {"assertion_additions": [diagnostic(text="column")]})
    validate_coverage_extension(before, second)


@pytest.mark.parametrize("case", ["group-normal", "group-boundary"])
def test_diagnostic_assertion_cannot_be_added_to_successful_case(case):
    with pytest.raises(SkillPackageValidationError):
        amend(original(), {"assertion_additions": [diagnostic(case)]})


def test_diagnostic_literal_cannot_duplicate_or_change_a_frozen_literal():
    before = amend(original(), {"assertion_additions": [diagnostic()]})
    with pytest.raises(SkillPackageValidationError):
        amend(before, {"assertion_additions": [diagnostic()]})
    revised = amend(before, {"new_cases": [new_case()]})
    revised.scripts[0].cases[2].assertions[0].expected = "different diagnostic"
    with pytest.raises(SkillPackageValidationError):
        validate_coverage_extension(before, revised)


def remediation_payload():
    return {"new_cases": [success_case()], "remediations": [{
        "script_path": "scripts/group_summary.py", "case_id": "group-normal", "new_case_ids": ["new-success"]}]}


def test_required_gap_needs_a_real_added_assertion_or_related_new_probe():
    before = original()
    payload = remediation_payload()
    payload["assertion_additions"] = [diagnostic()]
    amendment = VerificationCoverageAmendment.model_validate(payload)
    revised = apply_coverage_amendment(before, amendment)
    validate_required_remediations(before, revised, amendment, {
        ("scripts/group_summary.py", "group-normal"), ("scripts/group_summary.py", "group-invalid")})
    assert amendment.remediations[0].new_case_ids == ["new-success"]
    # This helper proves only actual coverage additions, never an accepted
    # oracle verdict. The complete independent review remains required.


@pytest.mark.parametrize("damage", ["missing_mapping", "unknown_old", "existing_new_id", "unknown_new_id",
    "duplicate_mapping", "duplicate_new_id", "different_script", "unrelated_requirement", "unrelated_source",
    "revised_differs", "required_unknown"])
def test_remediation_cannot_claim_unrelated_replaced_or_unobserved_cases(damage):
    before = original()
    payload = remediation_payload()
    item = payload["remediations"][0]
    if damage == "missing_mapping":
        payload["remediations"] = []
    elif damage == "unknown_old":
        item["case_id"] = "not-original"
    elif damage == "existing_new_id":
        item["new_case_ids"] = ["group-boundary"]
    elif damage == "unknown_new_id":
        item["new_case_ids"] = ["never-added"]
    elif damage == "duplicate_mapping":
        payload["remediations"].append(deepcopy(item))
    elif damage == "duplicate_new_id":
        item["new_case_ids"] *= 2
    elif damage == "different_script":
        item["script_path"] = "scripts/other.py"
    elif damage == "unrelated_requirement":
        payload["new_cases"][0]["case"]["requirement_ids"] = ["R999"]
    elif damage == "unrelated_source":
        payload["new_cases"][0]["case"]["source_ids"] = ["S999"]
    with pytest.raises((ValidationError, SkillPackageValidationError)):
        amendment = VerificationCoverageAmendment.model_validate(payload)
        revised = apply_coverage_amendment(before, amendment)
        if damage == "revised_differs":
            revised.scripts[0].cases[-1].assertions[0].fields["mean"].value = 99
        required = "never-original" if damage == "required_unknown" else "group-normal"
        validate_required_remediations(before, revised, amendment, {("scripts/group_summary.py", required)})


def test_legacy_amendments_default_empty_remediations_and_exact_scalar_types():
    amendment = VerificationCoverageAmendment.model_validate(additions())
    assert amendment.remediations == []
    before = original()
    revised = apply_coverage_amendment(before, amendment)
    validate_required_remediations(before, revised, amendment, {("scripts/group_summary.py", "group-invalid")})
    revised.scripts[0].cases[0].assertions[0].fields["valid_count"].atol = 0
    with pytest.raises(SkillPackageValidationError):
        validate_required_remediations(before, revised, amendment, set())


def proposed_reference_plan():
    value = success_case()
    value["case"]["assertions"] += [
        {"type": "json_value", "path": "result.json", "pointer": "/mean", "expected": {"value": 5, "atol": 1e-12}},
        {"type": "json_array_length", "path": "result.json", "pointer": "/rows", "length": 2},
        {"type": "json_object_keys", "path": "result.json", "pointer": "", "keys": ["mean", "rows"]},
        {"type": "csv_header", "path": "summary.csv", "columns": ["group", "mean", "valid_count", "missing_count", "missing_rate"]},
    ]
    return amend(original(), {"new_cases": [value]})


def test_blind_reference_can_correct_only_new_finite_numeric_expectations():
    before, proposed = original(), proposed_reference_plan()
    confirmed = proposed.model_copy(deep=True)
    case = confirmed.scripts[0].cases[-1]
    case.assertions[0].fields["mean"].value = 5.5
    case.assertions[0].count = 2
    case.assertions[2].expected.value = 5.5
    case.assertions[3].length = 3
    case.oracle_explanation = "Independent fixed reference execution established these new numeric targets."
    validate_reference_coverage_confirmation(before, proposed, confirmed)
    result = CoverageReferenceConfirmation(plan=confirmed, evidence={"scope": "unit-test-only"})
    assert result.plan is confirmed
    assert proposed.scripts[0].cases[-1].assertions[2].expected.value == 5


@pytest.mark.parametrize("damage", ["old_value", "old_scalar_type", "new_input", "new_argv", "new_kind",
    "new_requirement", "new_source", "new_id", "new_description", "new_tolerance", "new_target", "new_path",
    "new_order", "new_assertion", "string", "null", "bool", "infinite", "nan", "json_keys", "csv_header",
    "explanation_only", "invalid_diagnostic"])
def test_reference_confirmation_cannot_rewrite_any_other_old_or_new_contract(damage):
    before, proposed = original(), proposed_reference_plan()
    proposed = amend(proposed, {"new_cases": [new_case()]})
    proposed.scripts[0].cases[-1].assertions.append(
        VerificationCoverageAmendment.model_validate({"assertion_additions": [diagnostic()]}).assertion_additions[0].assertion)
    confirmed = proposed.model_copy(deep=True)
    case = confirmed.scripts[0].cases[-2]
    if damage == "old_value":
        confirmed.scripts[0].cases[0].assertions[0].fields["mean"].value = 5
    elif damage == "old_scalar_type":
        confirmed.scripts[0].cases[0].assertions[0].fields["valid_count"].atol = 0
    elif damage == "new_input":
        case.input_files[0].rows[0]["value"] = 8
    elif damage == "new_argv":
        case.argv[-1] = "{output}/other"
    elif damage == "new_kind":
        case.kind = "boundary"
    elif damage == "new_requirement":
        case.requirement_ids = ["R999"]
    elif damage == "new_source":
        case.source_ids = ["S999"]
    elif damage == "new_id":
        case.id = "renamed-case"
    elif damage == "new_description":
        case.description = "A replacement description is not a numerical correction."
    elif damage == "new_tolerance":
        case.assertions[2].expected.atol = 1
    elif damage == "new_target":
        case.assertions[2].pointer = "/changed"
    elif damage == "new_path":
        case.assertions[2].path = "elsewhere.json"
    elif damage == "new_order":
        case.assertions.reverse()
    elif damage == "new_assertion":
        case.assertions.pop()
    elif damage in {"string", "null", "bool", "infinite", "nan"}:
        case.assertions[2].expected.value = {"string": "5", "null": None, "bool": True,
            "infinite": float("inf"), "nan": float("nan")}[damage]
    elif damage == "json_keys":
        case.assertions[4].keys.append("surprise")
    elif damage == "csv_header":
        case.assertions[5].columns.reverse()
    elif damage == "invalid_diagnostic":
        confirmed.scripts[0].cases[-1].assertions[-1].expected = "wrong role"
    else:
        case.oracle_explanation = "No numerical target changed, so rewriting this explanation is not warranted."
    with pytest.raises((SkillPackageValidationError, ValueError)):
        validate_reference_coverage_confirmation(before, proposed, confirmed)


def test_repeated_confirmation_cannot_revise_an_already_confirmed_new_case():
    confirmed = proposed_reference_plan()
    validate_reference_coverage_confirmation(confirmed, confirmed, confirmed.model_copy(deep=True))
    changed = confirmed.model_copy(deep=True)
    changed.scripts[0].cases[-1].assertions[2].expected.value = 99
    with pytest.raises(SkillPackageValidationError):
        validate_reference_coverage_confirmation(confirmed, confirmed, changed)


def test_later_extension_preserves_every_previous_confirmed_probe_and_adds_only_new_answers():
    first = proposed_reference_plan()
    value = success_case("boundary", "next-probe")
    value["case"]["input_files"][0]["rows"][0]["value"] = 10
    proposed = amend(first, {"new_cases": [value]})
    confirmed = proposed.model_copy(deep=True)
    confirmed.scripts[0].cases[-1].assertions[0].fields["mean"].value = 7
    validate_reference_coverage_confirmation(first, proposed, confirmed)
    confirmed.scripts[0].cases[-2].assertions[2].expected.value = 50
    with pytest.raises(SkillPackageValidationError):
        validate_reference_coverage_confirmation(first, proposed, confirmed)
