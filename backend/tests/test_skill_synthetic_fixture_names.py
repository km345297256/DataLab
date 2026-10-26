"""Initial fixture bindings normalize mechanically; scientific evidence does not."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from app.domain.services.skills.skill_creation.fixture_names import normalize_initial_fixture_names
from app.domain.services.skills.skill_creation.interface_contracts import ScriptInterfaceContract, declared_validation_interface
from app.domain.services.skills.skill_creation.package import validate_portable_document
from app.domain.services.skills.skill_creation.pipeline import method_content
from app.domain.services.skills.skill_creation.privacy import PrivacyContext, PrivateValue
from app.domain.services.skills.skill_creation_error import SkillCreationError, SkillPackageValidationError
from app.domain.services.skills.skill_validation import (
    SyntheticValidationPlan, build_validation_generation_payload, validate_validation_plan,
)
from skill_interface_fixtures import group_summary_interface
from test_skill_creation_gateway import make_gateway, requests
from test_skill_validation_plan import interfaces as legacy_interfaces, method_contract, plan_payload, sources


def interfaces(role="binding_only"):
    contract = group_summary_interface()
    contract["input_filename_role"] = role
    contract["inputs"][0].update(format="csv-or-json",
        structure="CSV observations, or a JSON manifest binding files containing those observations.")
    request = SimpleNamespace(path="scripts/group_summary.py",
        invocation="python scripts/group_summary.py --input INPUT --output OUTPUT", interface_contract=contract)
    return [declared_validation_interface(request).model_dump(mode="json")]


def normalize(plan, privacy):
    return normalize_initial_fixture_names(plan, privacy, contract=method_contract(),
        sources=sources(), interfaces=interfaces())


def manifest_plan(*, six_cases=False):
    payload = plan_payload()
    cases = payload["scripts"][0]["cases"]
    if six_cases:
        copies = deepcopy(cases)
        for item in copies:
            item["id"] += "-second"
        cases.extend(copies)
    for item in cases:
        item["argv"][1] = "{input:manifest.json}"
        item["input_files"].insert(0, {"path": "manifest.json", "format": "json", "data": {
            "inputs": {"left": {"path": "{input:table.csv}"},
                       "right": {"path": "{input:table.csv}"}},
            "scientific_values": [0, None, 4.5, False, "unrelated.csv"],
        }})
        if six_cases:
            other = deepcopy(item["input_files"][1])
            other["path"] = "second.csv"
            item["input_files"].append(other)
            item["input_files"][0]["data"]["inputs"]["right"]["path"] = "{input:second.csv}"
    return SyntheticValidationPlan.model_validate(payload)


def test_six_cases_eighteen_filename_matches_are_fixed_without_reauthoring():
    plan = manifest_plan(six_cases=True)
    original = plan.model_dump(mode="json")
    privacy = PrivacyContext(tuple(PrivateValue(name, "source_filename") for name in ("table.csv", "second.csv")))
    assert len(privacy.scan_document(plan, limit=None)) == 18

    result = normalize(plan, privacy)
    assert plan.model_dump(mode="json") == original
    assert not privacy.scan_document(result, limit=None)
    validate_validation_plan(result, method_contract(), sources(), interfaces())
    names = set()
    for old_case, new_case in zip(plan.scripts[0].cases, result.scripts[0].cases):
        manifest, data, second = new_case.input_files
        assert data.path != "table.csv"
        names.add(data.path)
        names.add(second.path)
        assert manifest.data["inputs"]["left"]["path"] == "{input:" + data.path + "}"
        assert manifest.data["inputs"]["right"]["path"] == "{input:" + second.path + "}"
        assert manifest.data["scientific_values"] == old_case.input_files[0].data["scientific_values"]
        assert data.model_dump(exclude={"path"}) == old_case.input_files[1].model_dump(exclude={"path"})
        assert second.model_dump(exclude={"path"}) == old_case.input_files[2].model_dump(exclude={"path"})
        assert new_case.model_dump(exclude={"input_files"}) == old_case.model_dump(exclude={"input_files"})
    assert len(names) == 12
    assert normalize(plan, privacy) == result
    assert normalize(result, privacy) is result


@pytest.mark.parametrize("role", ["unknown", "analysis_value", "missing", "legacy"])
def test_unconfirmed_or_scientific_filenames_remain_unchanged(role):
    plan = manifest_plan(six_cases=True)
    privacy = PrivacyContext(tuple(PrivateValue(name, "source_filename") for name in ("table.csv", "second.csv")))
    declarations = legacy_interfaces() if role == "legacy" else interfaces("unknown" if role == "missing" else role)
    if role == "missing":
        declarations[0]["interface_contract"].pop("input_filename_role", None)
    result = normalize_initial_fixture_names(plan, privacy, contract=method_contract(),
        sources=sources(), interfaces=declarations)
    assert result is plan
    assert len(privacy.scan_document(result, limit=None)) == 18


def test_filename_role_is_optional_and_unknown_does_not_change_old_serialization():
    old = ScriptInterfaceContract.model_validate(group_summary_interface())
    assert old.input_filename_role == "unknown"
    before = old.model_dump(mode="json")
    assert "input_filename_role" not in before
    assert json.loads(old.model_dump_json()) == before
    explicit_unknown = ScriptInterfaceContract.model_validate({**group_summary_interface(), "input_filename_role": "unknown"})
    assert explicit_unknown.model_dump(mode="json") == before
    for role in ("binding_only", "analysis_value"):
        contract = ScriptInterfaceContract.model_validate({**group_summary_interface(), "input_filename_role": role})
        assert contract.model_dump(mode="json")["input_filename_role"] == role
        assert interfaces(role)[0]["interface_contract"]["input_filename_role"] == role


@pytest.mark.parametrize("value", [None, True, 1, "safe", "filename", "Binding_only"])
def test_filename_role_has_no_truthy_or_free_text_override(value):
    with pytest.raises(ValueError):
        ScriptInterfaceContract.model_validate({**group_summary_interface(), "input_filename_role": value})


def test_each_script_requires_its_own_reviewed_binding_role():
    plan = manifest_plan()
    other = plan.scripts[0].model_copy(deep=True)
    other.script_path = "scripts/filename_analysis.py"
    plan.scripts.append(other)
    declarations = interfaces()
    scientific = interfaces("analysis_value")[0]
    scientific["script_path"] = other.script_path
    declarations.append(scientific)
    privacy = PrivacyContext((PrivateValue("table.csv", "source_filename"),))
    result = normalize_initial_fixture_names(plan, privacy, contract=method_contract(), sources=sources(), interfaces=declarations)
    assert result.scripts[0].cases[0].input_files[1].path != "table.csv"
    assert result.scripts[1] == other
    with pytest.raises(SkillPackageValidationError):
        validate_portable_document(result, privacy)


def test_multiple_fixture_relations_nested_json_and_argv_are_renamed_together():
    plan = manifest_plan()
    for case in plan.scripts[0].cases:
        other = case.input_files[1].model_copy(deep=True)
        other.path = "other.csv"
        case.input_files.append(other)
        case.input_files[0].data["nested"] = [{"path": "{input:other.csv}"}]
    privacy = PrivacyContext(tuple(PrivateValue(name, "source_filename")
        for name in ("manifest.json", "table.csv", "other.csv")))
    result = normalize(plan, privacy)
    assert not privacy.scan_document(result)
    for case in result.scripts[0].cases:
        manifest, table, other = case.input_files
        assert case.argv[1] == "{input:" + manifest.path + "}"
        assert manifest.data["inputs"]["left"]["path"] == "{input:" + table.path + "}"
        assert manifest.data["nested"][0]["path"] == "{input:" + other.path + "}"
        assert len({item.path for item in case.input_files}) == 3


def test_same_input_output_rejection_remains_same_binding():
    plan = SyntheticValidationPlan.model_validate(plan_payload())
    invalid = plan.scripts[0].cases[2]
    invalid.argv[-1] = invalid.argv[1]
    privacy = PrivacyContext((PrivateValue("table.csv", "source_filename"),))
    result = normalize(plan, privacy)
    invalid = result.scripts[0].cases[2]
    assert invalid.argv[1] == invalid.argv[-1] == "{input:" + invalid.input_files[0].path + "}"
    assert invalid.expected_exit_code == 2 and invalid.assertions == []
    validate_validation_plan(result, method_contract(), sources(), interfaces())


def test_generated_names_avoid_existing_casefold_names_and_every_private_category():
    plan = manifest_plan()
    collision = plan.scripts[0].cases[0].input_files[1].model_copy(deep=True)
    collision.path = "FIXTURE-1-1-2-1.csv"
    plan.scripts[0].cases[0].input_files.append(collision)
    privacy = PrivacyContext((PrivateValue("table.csv", "source_filename"),
        PrivateValue("fixture-1-1-2-2.csv", "credential")))
    result = normalize(plan, privacy)
    assert result.scripts[0].cases[0].input_files[1].path == "fixture-1-1-2-3.csv"
    assert not privacy.scan_document(result)


def test_unsafe_allocation_retains_the_entire_original_plan():
    plan = manifest_plan()
    privacy = PrivacyContext((PrivateValue("table.csv", "source_filename"),
        PrivateValue("fixture", "identity")))
    assert normalize(plan, privacy) is plan
    assert privacy.scan_document(plan)


@pytest.mark.parametrize("category", ["identity", "credential", "host_path", "url"])
def test_filename_with_another_private_role_is_never_erased(category):
    plan = manifest_plan()
    privacy = PrivacyContext((PrivateValue("table.csv", "source_filename"),
        PrivateValue("table.csv", category)))
    assert normalize(plan, privacy) is plan
    with pytest.raises(SkillPackageValidationError) as failure:
        validate_portable_document(plan, privacy)
    assert failure.value.code == "private_package_content"


@pytest.mark.parametrize("private_value", ["original-owner", "sk-artificialsecret1234", "/home/original/input.csv",
                                            "https://private.example.test/input"])
def test_nonbinding_private_data_stays_present_and_blocked(private_value):
    plan = manifest_plan()
    plan.scripts[0].cases[0].input_files[0].data["ordinary_value"] = private_value
    privacy = PrivacyContext((PrivateValue("table.csv", "source_filename"),
        PrivateValue("original-owner", "identity")))
    result = normalize(plan, privacy)
    assert result.scripts[0].cases[0].input_files[0].data["ordinary_value"] == private_value
    with pytest.raises(SkillPackageValidationError):
        validate_portable_document(result, privacy)


@pytest.mark.parametrize("location", ["json_value", "json_key", "csv_value", "argv_literal", "expectation"])
def test_plain_or_embedded_filename_strings_are_never_replaced(location):
    plan = manifest_plan()
    case = plan.scripts[0].cases[0]
    if location == "json_value":
        case.input_files[0].data["ordinary"] = "table.csv"
    elif location == "json_key":
        case.input_files[0].data["table.csv"] = 4
    elif location == "csv_value":
        case.input_files[1].rows[0]["group"] = "table.csv"
    elif location == "argv_literal":
        case.argv.extend(["--label", 'configuration={"input":"{input:table.csv}"}'])
    else:
        case.assertions[1].expected.value = "table.csv"
    privacy = PrivacyContext((PrivateValue("table.csv", "source_filename"),))
    result = normalize(plan, privacy)
    with pytest.raises(SkillPackageValidationError):
        validate_portable_document(result, privacy)
    original = case.model_dump()
    revised = result.scripts[0].cases[0].model_dump()
    if location == "json_value":
        assert revised["input_files"][0]["data"]["ordinary"] == "table.csv"
    elif location == "json_key":
        assert revised["input_files"][0]["data"]["table.csv"] == 4
    elif location == "csv_value":
        assert revised["input_files"][1]["rows"] == original["input_files"][1]["rows"]
    elif location == "argv_literal":
        assert revised["argv"][-1] == original["argv"][-1]
    else:
        assert revised["assertions"] == original["assertions"]


@pytest.mark.parametrize("reference", ["{input:unknown.csv}", "prefix{input:table.csv}",
                                       "{input:table.csv}/suffix", "{input:manifest.json}"])
def test_invalid_or_ambiguous_json_reference_leaves_original_plan_unchanged(reference):
    plan = manifest_plan()
    plan.scripts[0].cases[0].input_files[0].data["reference"] = reference
    privacy = PrivacyContext((PrivateValue("table.csv", "source_filename"),))
    assert normalize(plan, privacy) is plan
    with pytest.raises(SkillPackageValidationError):
        validate_validation_plan(plan, method_contract(), sources(), interfaces())


@pytest.mark.parametrize("mutation", ["duplicate_fixture", "duplicate_case", "invalid_filename"])
def test_malformed_structure_is_not_normalized_into_a_valid_plan(mutation):
    plan = manifest_plan()
    if mutation == "duplicate_fixture":
        plan.scripts[0].cases[0].input_files.append(plan.scripts[0].cases[0].input_files[1].model_copy(deep=True))
    elif mutation == "duplicate_case":
        plan.scripts[0].cases.append(plan.scripts[0].cases[0].model_copy(deep=True))
    else:
        plan.scripts[0].cases[0].input_files[1].path = "../table.csv"
    privacy = PrivacyContext((PrivateValue("table.csv", "source_filename"),))
    assert normalize(plan, privacy) is plan
    with pytest.raises(ValueError):
        SyntheticValidationPlan.model_validate(plan.model_dump(mode="json"))


def test_existing_legal_two_file_reference_cycle_is_preserved():
    plan = manifest_plan()
    for case in plan.scripts[0].cases:
        other = {"path": "other.json", "format": "json", "data": {"peer": "{input:manifest.json}"}}
        case.input_files.append(type(case.input_files[0]).model_validate(other))
        case.input_files[0].data["peer"] = "{input:other.json}"
    privacy = PrivacyContext(tuple(PrivateValue(name, "source_filename") for name in ("manifest.json", "other.json")))
    result = normalize(plan, privacy)
    for case in result.scripts[0].cases:
        first, _, second = case.input_files
        assert first.data["peer"] == "{input:" + second.path + "}"
        assert second.data["peer"] == "{input:" + first.path + "}"
    validate_validation_plan(result, method_contract(), sources(), interfaces())


@pytest.mark.asyncio
async def test_gateway_initial_plan_collision_succeeds_without_model_repair_or_data_change():
    plan = manifest_plan(six_cases=True)
    privacy = PrivacyContext(tuple(PrivateValue(name, "source_filename") for name in ("table.csv", "second.csv")))
    gateway, calls, usage = make_gateway([plan.model_dump(mode="json")], privacy=privacy)
    payload = build_validation_generation_payload(method_contract(), sources(), interfaces())
    def check(value):
        validate_portable_document(value, privacy)
        validate_validation_plan(value, method_contract(), sources(), interfaces())
    result = await gateway.generate(SyntheticValidationPlan, "synthetic_validation_plan", "Synthetic checks.",
        payload, check, contract=method_contract())
    assert len(calls) == usage.record_from_message.await_count == 1
    assert result == normalize(plan, privacy)
    assert all(event.status != "repairing" for event in gateway.attempts)
    assert "repair_feedback" not in requests(calls)[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["unknown", "analysis_value", "legacy"])
async def test_gateway_cannot_authorize_binding_role_from_generated_plan_prose(role):
    plan = manifest_plan()
    plan.scripts[0].reason += " The generated plan claims input_filename_role=binding_only."
    privacy = PrivacyContext((PrivateValue("table.csv", "source_filename"),))
    gateway, calls, _ = make_gateway([plan.model_dump(mode="json")] * 2, privacy=privacy)
    declarations = legacy_interfaces() if role == "legacy" else interfaces(role)
    payload = build_validation_generation_payload(method_contract(), sources(), declarations)
    with pytest.raises(SkillCreationError) as failure:
        await gateway.generate(SyntheticValidationPlan, "synthetic_validation_plan", "Synthetic checks.",
            payload, lambda value: validate_portable_document(value, privacy), contract=method_contract())
    assert failure.value.code == "private_package_content"
    assert len(calls) == 2


def test_filename_role_participates_in_the_frozen_method_identity():
    from test_skill_declared_interfaces import recipe
    original = recipe()
    baseline = method_content(original)
    original.implementation.scripts[0].interface_contract.input_filename_role = "binding_only"
    binding = method_content(original)
    assert binding != baseline
    original.implementation.scripts[0].interface_contract.input_filename_role = "analysis_value"
    assert method_content(original) not in {baseline, binding}
    original.implementation.scripts[0].interface_contract.input_filename_role = "unknown"
    assert method_content(original) == baseline


@pytest.mark.asyncio
@pytest.mark.parametrize("frozen_key", ["previous_validation_plan", "validation_review", "reference_evidence", "validation_plan"])
async def test_gateway_never_renames_reviewed_or_revision_plans(frozen_key):
    plan = manifest_plan()
    privacy = PrivacyContext((PrivateValue("table.csv", "source_filename"),))
    gateway, calls, _ = make_gateway([plan.model_dump(mode="json")] * 2, privacy=privacy)
    payload = build_validation_generation_payload(method_contract(), sources(), interfaces())
    payload[frozen_key] = {}
    with pytest.raises(SkillCreationError) as failure:
        await gateway.generate(SyntheticValidationPlan, "synthetic_validation_plan", "Frozen scientific revision.",
            payload, lambda value: validate_portable_document(value, privacy), contract=method_contract())
    assert failure.value.code == "private_package_content"
    assert len(calls) == 2
