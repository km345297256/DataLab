"""The opt-in real-model harness enforces a blind reference-authoring boundary."""
from copy import deepcopy
import hashlib
import json

import pytest

from acceptance.skill_creator_real_model import assert_oracle_isolation, assert_reference_isolation, assert_source_authoring_boundary, assert_reference_provenance, validated_replay_message, runtime_context
from app.domain.services.skills.skill_reference_verification import reference_generation_payload
from app.domain.services.skills.skill_creation.models import HelperScript
from app.domain.services.skills.skill_creation.source_response import source_authoring_capabilities
from app.domain.services.skills.skill_validation import SyntheticValidationPlan
from test_skill_validation_plan import interfaces, method_contract, plan_payload, sources


CANDIDATE = "def candidate_private_program():\n    return 'CANDIDATE_PROGRAM_CANARY'\n"
ORIGINAL = "def original_private_program():\n    return 'ORIGINAL_PROGRAM_CANARY'\n"


def blind_request():
    plan = plan_payload()
    case = plan["scripts"][0]["cases"][0]
    case["description"] = "OBSERVED_RESULT_CANARY"
    case["oracle_explanation"] = "CANDIDATE_EXPECTATION_EXPLANATION_CANARY"
    case["assertions"][0]["fields"]["mean"] = {"value": "EXPECTED_ANSWER_CANARY"}
    case["assertions"][0]["match"]["result"] = "MATCH_ANSWER_CANARY"
    source_evidence = [{**sources()[0], "source_snapshot": ORIGINAL, "observed": "OBSERVED_RESULT_CANARY"}]
    declared = deepcopy(interfaces())
    declared[0]["validation_cases"] = [{"condition": "case", "expected": "EXPECTED_ANSWER_CANARY"}]
    context = reference_generation_payload(SyntheticValidationPlan.model_validate(plan), method_contract(), source_evidence, declared)
    return {"stage": "synthetic_reference_implementation", **context,
        "script_authoring_capabilities": source_authoring_capabilities(HelperScript, "synthetic_reference_implementation"),
        "script_request": {"path": declared[0]["script_path"], "purpose": "Reference calculation", "invocation": "python helper.py --input INPUT --output OUTPUT"},
        "response_format": {"type": "python_source", "artifact": "script", "complete_source_required": True,
                            "host_metadata": {"path": declared[0]["script_path"]}}}


def test_real_reference_payload_excludes_original_candidate_and_observed_answers():
    request = blind_request()
    wire = json.dumps(request)
    for marker in ("EXPECTED_ANSWER_CANARY", "CANDIDATE_EXPECTATION_EXPLANATION_CANARY",
                   "OBSERVED_RESULT_CANARY", "ORIGINAL_PROGRAM_CANARY", "MATCH_ANSWER_CANARY"):
        assert marker not in wire
    assert_reference_isolation(request["stage"], request, candidate_sources=[CANDIDATE], original_sources=[ORIGINAL])
    assert_source_authoring_boundary(request["stage"], request)


@pytest.mark.parametrize("damage", ["missing", "extra", "imports", "source_form", "blocked_operations"])
def test_authoring_policy_cannot_hide_answers_or_expand_actual_capabilities(damage):
    request = blind_request()
    if damage == "missing":
        request.pop("script_authoring_capabilities")
    elif damage == "extra":
        request["script_authoring_capabilities"]["observed_answers"] = "SECRET_ANSWER"
    elif damage == "imports":
        request["script_authoring_capabilities"]["dependency_policy"]["admission"] = "restricted"
    elif damage == "blocked_operations":
        request["script_authoring_capabilities"]["blocked_operations"] = []
    else:
        request["script_authoring_capabilities"]["source_form"] = "function"
    with pytest.raises(AssertionError, match="host public policy"):
        assert_reference_isolation(request["stage"], request)


def test_blind_output_empty_target_has_no_path_or_mutable_expected_value():
    request = blind_request()
    target = {"type": "output_empty"}
    request["blind_cases"][0]["cases"][2]["output_targets"] = [target]
    assert_reference_isolation(request["stage"], request)
    for key, value in (("expected", True), ("path", "report.json"), ("actual", True)):
        target[key] = value
        with pytest.raises(AssertionError):
            assert_reference_isolation(request["stage"], request)
        del target[key]


def postcondition_request():
    from app.domain.services.skills.skill_validation import build_validation_generation_payload
    from app.domain.services.skills.skill_validation_extension import VerificationCoverageAmendment
    from test_skill_validation_review import accepted_fixture_review
    plan = SyntheticValidationPlan.model_validate(plan_payload())
    review = accepted_fixture_review(plan)
    review["scripts"][0]["cases"][2]["method_coverage"] = {
        "status": "rejected", "explanation": "The invalid input rejection lacks observable output-absence evidence."}
    return {"stage": "synthetic_validation_plan", "schema": VerificationCoverageAmendment.model_json_schema(),
        **build_validation_generation_payload(method_contract(), sources(), interfaces()),
        "previous_validation_plan": plan.model_dump(mode="json"), "validation_review": review,
        "eligible_cases": [{"script_path": plan.scripts[0].script_path, "case_id": plan.scripts[0].cases[2].id}],
        "required_cases": [{"script_path": plan.scripts[0].script_path, "case_id": plan.scripts[0].cases[2].id}]}


def test_postcondition_amendment_payload_remains_bound_to_original_adverse_cases():
    request = postcondition_request()
    assert_oracle_isolation(request["stage"], request)
    request["eligible_cases"][0]["case_id"] = "group-normal"
    with pytest.raises(AssertionError):
        assert_oracle_isolation(request["stage"], request)


def test_postcondition_required_cases_cannot_be_erased_or_replaced_by_optional_cases():
    request = postcondition_request()
    request["required_cases"] = []
    with pytest.raises(AssertionError):
        assert_oracle_isolation(request["stage"], request)


def test_postcondition_request_cannot_ask_model_to_rewrite_the_whole_scientific_plan():
    request = postcondition_request()
    request["schema"] = SyntheticValidationPlan.model_json_schema()
    with pytest.raises(AssertionError, match="replacement scientific-plan schema"):
        assert_oracle_isolation(request["stage"], request)


@pytest.mark.parametrize("extra", ["expected", "candidate_source", "observed_candidate"])
def test_postcondition_amendment_does_not_open_an_extra_value_channel(extra):
    request = postcondition_request()
    request["eligible_cases"][0][extra] = "FORBIDDEN_MATERIAL"
    with pytest.raises(AssertionError):
        assert_oracle_isolation(request["stage"], request)


@pytest.mark.parametrize("leak", ["expected", "match_value", "observed", "candidate", "source_snapshot", "case_prose", "interface_expected", "original_text"])
def test_reference_acceptance_rejects_candidate_answer_or_source_leaks(leak):
    request = blind_request()
    if leak == "expected":
        request["blind_cases"][0]["cases"][0]["output_targets"][0]["expected"] = 3
    elif leak == "match_value":
        request["blind_cases"][0]["cases"][0]["output_targets"][0]["match"] = {"result": "MATCH_ANSWER_CANARY"}
    elif leak == "observed":
        request["observed_validation"] = {"actual": 3}
    elif leak == "candidate":
        request["script"] = {"content": CANDIDATE}
    elif leak == "source_snapshot":
        request["sources"][0]["source_snapshot"] = ORIGINAL
    elif leak == "case_prose":
        request["blind_cases"][0]["cases"][0]["oracle_explanation"] = "Expected answer 3."
    elif leak == "interface_expected":
        request["script_interfaces"][0]["validation_cases"] = [{"expected": "3"}]
    else:
        request["sources"][0]["text"] = ORIGINAL
    with pytest.raises(AssertionError):
        assert_reference_isolation(request["stage"], request, candidate_sources=[CANDIDATE], original_sources=[ORIGINAL])


def test_reference_review_can_only_see_its_independently_authored_program():
    request = blind_request()
    metadata = request.pop("script_request")
    request.pop("response_format")
    request.pop("script_authoring_capabilities")
    request["stage"] = "synthetic_reference_review"
    own_source = "def reference_calculation(data):\n    return sum(data)\n"
    request["reference_scripts"] = [{**metadata, "content": own_source}]
    authored = {metadata["path"]: own_source}
    assert_reference_isolation(request["stage"], request, candidate_sources=[CANDIDATE],
                               original_sources=[ORIGINAL], reference_sources=authored)
    request["reference_scripts"][0]["content"] = CANDIDATE
    with pytest.raises(AssertionError):
        assert_reference_isolation(request["stage"], request, candidate_sources=[CANDIDATE],
                                   original_sources=[ORIGINAL], reference_sources=authored)


def test_reference_acceptance_allows_proven_column_names_without_scalar_values():
    request = blind_request()
    shape = {"type": "object", "properties": {"columns": {
        "type": "array", "items": {"type": "unknown"}, "column_name_prefix": ["id"]}}}
    request["script_interfaces"][0]["json_outputs"] = [{"path": "report.json", "shape": shape}]
    assert_reference_isolation(request["stage"], request)
    shape["properties"]["columns"]["items"]["value"] = "CANDIDATE_ANSWER_CANARY"
    with pytest.raises(AssertionError):
        assert_reference_isolation(request["stage"], request)


def reference_revision_request():
    request = blind_request()
    metadata = request["script_request"]
    prior = {"reference_scripts": [{**metadata, "content": "def independent_reference(data):\n    return None\n"}],
        "review": {"sources": [{"source_id": "S001", "status": "changed",
                                  "explanation": "The reference incorrectly replaces a defined zero statistic with null."}],
            "requirements": [{"requirement_id": "R001", "status": "changed",
                              "explanation": "A defined zero standard error must retain its value under the unchanged formula."}],
            "implementation_appropriate": True,
            "implementation_reason": "A deterministic Python calculation remains appropriate, but the zero branch must preserve the method."}}
    request["reference_revision"] = deepcopy(prior)
    return request, prior


def test_reference_revision_only_contains_its_own_program_and_exact_prior_review():
    request, prior = reference_revision_request()
    assert_reference_isolation(request["stage"], request, candidate_sources=[CANDIDATE],
                               original_sources=[ORIGINAL], reference_revision_context=prior)


@pytest.mark.parametrize("status,allowed", [("preserved", True), ("valid_exclusion", False), ("changed", True)])
def test_reference_revision_uses_excluded_source_scope(status, allowed):
    request, prior = reference_revision_request()
    for item in prior["review"]["sources"] + prior["review"]["requirements"]:
        item["status"] = "preserved"
    request["method_contract"]["excluded_sources"].append({
        "source_id": "S002", "reason": "duplicate", "explanation": "Duplicate evidence is excluded from reusable requirements."})
    request["sources"].append({"id": "S002", "kind": "method", "text": "Duplicate source evidence."})
    prior["review"]["sources"].append({"source_id": "S002", "status": status,
        "explanation": "The duplicate source is assessed independently against the frozen exclusion decision."})
    request["reference_revision"] = deepcopy(prior)
    original = deepcopy(request)
    if allowed:
        assert_reference_isolation(request["stage"], request, reference_revision_context=prior)
    else:
        with pytest.raises(AssertionError, match="concrete adverse"):
            assert_reference_isolation(request["stage"], request, reference_revision_context=prior)
    assert request == original


@pytest.mark.parametrize("collection,key,value", [("sources", "source_id", "S999"),
                                                 ("requirements", "requirement_id", "R999")])
def test_reference_revision_unknown_targets_are_not_an_adverse_method_judgment(collection, key, value):
    request, prior = reference_revision_request()
    prior["review"][collection][0][key] = value
    request["reference_revision"] = deepcopy(prior)
    with pytest.raises(AssertionError, match="scope must remain complete"):
        assert_reference_isolation(request["stage"], request, reference_revision_context=prior)


@pytest.mark.parametrize("damage", ["extra_answer", "changed_source", "changed_review", "missing_provenance", "no_negative"])
def test_reference_revision_cannot_smuggle_material_or_revote_a_review(damage):
    request, prior = reference_revision_request()
    if damage == "extra_answer":
        request["reference_revision"]["candidate_expected"] = 0
    elif damage == "changed_source":
        request["reference_revision"]["reference_scripts"][0]["content"] = CANDIDATE
    elif damage == "changed_review":
        request["reference_revision"]["review"]["requirements"][0]["status"] = "preserved"
    elif damage == "missing_provenance":
        prior = None
    else:
        for collection in ("sources", "requirements"):
            request["reference_revision"]["review"][collection][0]["status"] = "preserved"
        prior = deepcopy(request["reference_revision"])
    with pytest.raises(AssertionError):
        assert_reference_isolation(request["stage"], request, candidate_sources=[CANDIDATE],
                                   original_sources=[ORIGINAL], reference_revision_context=prior)


def test_replayed_reference_source_is_validated_and_tracked_before_blind_review():
    from app.domain.services.skills.skill_creation.models import HelperScript
    from app.domain.services.skills.skill_creation.source_response import source_response_adapter
    request = blind_request()
    raw = "```python\ndef reference_calculation(data):\n    return sum(data)\n```"
    adapter = source_response_adapter(HelperScript, request["stage"], request)
    authored = {}

    def validate_and_track(message):
        value = HelperScript.model_validate(adapter.document(message.content))
        authored[value.path] = value.content

    message = validated_replay_message(raw, [validate_and_track])
    assert message.content == raw
    metadata = request.pop("script_request")
    request.pop("response_format")
    request.pop("script_authoring_capabilities")
    request["stage"] = "synthetic_reference_review"
    request["reference_scripts"] = [{**metadata, "content": authored[metadata["path"]]}]
    assert_reference_isolation(request["stage"], request, candidate_sources=[CANDIDATE],
                               original_sources=[ORIGINAL], reference_sources=authored)


def test_replayed_response_cannot_bypass_missing_or_rejecting_validator():
    with pytest.raises(AssertionError, match="response-validation"):
        validated_replay_message("saved bytes", [])
    error = ValueError("production validation rejected saved candidate")

    def reject(message):
        raise error

    with pytest.raises(ValueError) as captured:
        validated_replay_message("saved bytes", [reject])
    assert captured.value is error


def provenance_fixture():
    corrected = SyntheticValidationPlan.model_validate(
        SyntheticValidationPlan.model_validate(plan_payload()).model_dump()).model_dump()
    original = deepcopy(corrected)
    original["scripts"][0]["cases"][0]["assertions"][0]["fields"]["mean"]["value"] = 999
    corrected["scripts"][0]["cases"][0]["oracle_explanation"] = "Computed by the independent reference, without tested-program outputs."
    path = corrected["scripts"][0]["script_path"]
    reference = {path: "# independently generated structural-test fixture\n"}
    digest = lambda value: hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()
    hashes = {path: hashlib.sha256(reference[path].encode()).hexdigest()}
    execution = {"status": "passed", "scope": "synthetic", "plan_sha256": digest(corrected), "source_sha256": hashes,
        "cases": [{"script_path": path, "id": case["id"], "status": "passed", "input_unchanged": True, "checks": []}
                  for case in corrected["scripts"][0]["cases"]]}
    receipt = {"status": "verified", "scope": "synthetic_reference", "original_plan_sha256": digest(original),
        "corrected_plan_sha256": digest(corrected), "reference_source_sha256": hashes, "execution": execution,
        "initial_execution": {"plan_sha256": digest(original), "source_sha256": hashes},
        "original_explanations": [{"script_path": path, "case_id": original["scripts"][0]["cases"][0]["id"],
                                   "oracle_explanation": original["scripts"][0]["cases"][0]["oracle_explanation"]}],
        "corrections": [{"script_path": path, "case_id": corrected["scripts"][0]["cases"][0]["id"],
            "assertion_index": 0, "target_type": "csv_field", "field": "mean", "before": 999, "after": 3}]}
    return corrected, receipt, reference


def test_reference_provenance_reconstructs_only_numerical_correction():
    assert_reference_provenance(*provenance_fixture())


def extended_provenance_fixture():
    plan, base_receipt, reference = provenance_fixture()
    extended = deepcopy(plan)
    extended["scripts"][0]["cases"][2]["assertions"].append({"type": "output_empty"})
    digest = lambda value: hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()
    execution = deepcopy(base_receipt["execution"])
    execution["plan_sha256"] = digest(extended)
    execution["cases"][2]["checks"] = [{"id": "assertion_1", "passed": True, "expected": True, "actual": True}]
    receipt = {**deepcopy(base_receipt), "corrected_plan_sha256": digest(extended),
        "initial_execution": deepcopy(execution), "execution": execution,
        "postcondition_extension": {"base_plan_sha256": digest(plan),
            "base_reference_verification": deepcopy(base_receipt),
            "previous_plan_sha256": digest(plan), "proposed_plan_sha256": digest(extended),
            "confirmed_plan_sha256": digest(extended), "initial_execution": deepcopy(execution),
            "execution": deepcopy(execution), "corrections": [], "original_explanations": [],
            "previous_confirmations": []}}
    records = [{"scripts": reference, "plan": plan, "execution": base_receipt["execution"]},
               {"scripts": reference, "plan": extended, "execution": execution}]
    return extended, receipt, reference, records


def test_reference_postcondition_provenance_binds_new_execution_and_preserves_original_corrections():
    plan, receipt, reference, records = extended_provenance_fixture()
    before = deepcopy((plan, receipt, records))
    assert_reference_provenance(plan, receipt, reference, execution_records=records)
    assert (plan, receipt, records) == before


@pytest.mark.parametrize("damage", ["unobserved", "no_base", "changed_science", "changed_input", "changed_source", "dropped_correction"])
def test_postcondition_extension_does_not_authorize_scientific_changes_or_fake_execution(damage):
    from app.domain.services.skills.skill_creation_error import SkillPackageValidationError
    plan, receipt, reference, records = extended_provenance_fixture()
    if damage == "unobserved":
        records.pop()
    elif damage == "no_base":
        records.pop(0)
    elif damage == "changed_source":
        records[-1] = deepcopy(records[-1])
        records[-1]["scripts"][next(iter(reference))] += "# changed source"
    elif damage == "dropped_correction":
        receipt["corrections"] = []
    else:
        plan = deepcopy(plan)
        if damage == "changed_science":
            plan["scripts"][0]["cases"][0]["assertions"][0]["fields"]["mean"]["value"] = 50
        else:
            plan["scripts"][0]["cases"][0]["input_files"][0]["rows"][0]["value"] = 50
        digest = hashlib.sha256(json.dumps(plan, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()
        receipt["corrected_plan_sha256"] = digest
        receipt["execution"]["plan_sha256"] = digest
        receipt["initial_execution"]["plan_sha256"] = digest
    with pytest.raises((AssertionError, SkillPackageValidationError)):
        assert_reference_provenance(plan, receipt, reference, execution_records=records)


def numerical_extension_fixture():
    from test_skill_validation_extension import success_case
    plan, previous_receipt, reference, records = extended_provenance_fixture()
    proposed = deepcopy(plan)
    value = success_case()["case"]
    value["assertions"][0]["fields"]["mean"]["value"] = 999
    proposed["scripts"][0]["cases"].append(value)
    proposed = SyntheticValidationPlan.model_validate(SyntheticValidationPlan.model_validate(proposed).model_dump()).model_dump()
    confirmed = deepcopy(proposed)
    case = confirmed["scripts"][0]["cases"][-1]
    original_explanation = case["oracle_explanation"]
    case["assertions"][0]["fields"]["mean"]["value"] = 5
    case["oracle_explanation"] = "Independent reference execution calculated this new probe without candidate observations."
    digest = lambda value: hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()
    initial = deepcopy(previous_receipt["execution"])
    initial.update(plan_sha256=digest(proposed), status="failed")
    initial["cases"].append({"script_path": "scripts/group_summary.py", "id": "new-success", "status": "failed",
        "input_unchanged": True, "checks": [{"id": "assertion_1_mean", "expected": 999, "actual": 5, "passed": False}]})
    execution = deepcopy(initial)
    execution.update(plan_sha256=digest(confirmed), status="passed")
    execution["cases"][-1].update(status="passed", checks=[{"id": "assertion_1_mean", "expected": 5, "actual": 5, "passed": True}])
    correction = {"script_path": "scripts/group_summary.py", "case_id": "new-success", "assertion_index": 0,
        "target_type": "csv_field", "field": "mean", "before": 999, "after": 5}
    explanation = {"script_path": "scripts/group_summary.py", "case_id": "new-success",
        "oracle_explanation": original_explanation}
    old_extension = previous_receipt["postcondition_extension"]
    transition_keys = {"previous_plan_sha256", "proposed_plan_sha256", "confirmed_plan_sha256",
        "initial_execution", "execution", "corrections", "original_explanations"}
    extension = {"base_plan_sha256": old_extension["base_plan_sha256"],
        "base_reference_verification": deepcopy(old_extension["base_reference_verification"]),
        "previous_plan_sha256": digest(plan), "proposed_plan_sha256": digest(proposed),
        "confirmed_plan_sha256": digest(confirmed), "initial_execution": initial, "execution": execution,
        "corrections": [correction], "original_explanations": [explanation],
        "previous_confirmations": [{key: deepcopy(old_extension[key]) for key in transition_keys}]}
    receipt = {**deepcopy(previous_receipt), "corrected_plan_sha256": digest(confirmed),
        "initial_execution": deepcopy(initial), "execution": deepcopy(execution), "postcondition_extension": extension,
        "corrections": previous_receipt["corrections"] + [correction],
        "original_explanations": previous_receipt["original_explanations"] + [explanation]}
    records += [{"scripts": reference, "plan": proposed, "execution": deepcopy(initial)},
                {"scripts": reference, "plan": confirmed, "execution": deepcopy(execution)}]
    return confirmed, receipt, reference, records


def test_cumulative_reference_confirmation_keeps_flat_history_and_audits_new_numeric_correction():
    plan, receipt, reference, records = numerical_extension_fixture()
    before = deepcopy((plan, receipt, records))
    assert_reference_provenance(plan, receipt, reference, execution_records=records)
    assert (plan, receipt, records) == before


@pytest.mark.parametrize("damage", ["missing_initial", "missing_confirmation", "history_deleted", "history_reordered",
    "wrong_previous", "old_value", "proposed_input", "correction_value", "missing_explanation", "duplicate_correction",
    "new_source", "faked_top_execution"])
def test_new_numeric_confirmation_cannot_erase_history_or_substitute_values_or_executions(damage):
    from app.domain.services.skills.skill_creation_error import SkillPackageValidationError
    plan, receipt, reference, records = numerical_extension_fixture()
    extension = receipt["postcondition_extension"]
    if damage == "missing_initial":
        records.pop(-2)
    elif damage == "missing_confirmation":
        records.pop()
    elif damage == "history_deleted":
        extension["previous_confirmations"] = []
    elif damage == "history_reordered":
        extension["previous_confirmations"] *= 2
    elif damage == "wrong_previous":
        extension["previous_plan_sha256"] = "0" * 64
    elif damage == "old_value":
        plan["scripts"][0]["cases"][0]["assertions"][0]["fields"]["mean"]["value"] = 8
    elif damage == "proposed_input":
        records[-2]["plan"]["scripts"][0]["cases"][-1]["input_files"][0]["rows"][0]["value"] = 8
    elif damage == "correction_value":
        extension["corrections"][0]["after"] = 6
    elif damage == "missing_explanation":
        extension["original_explanations"] = []
    elif damage == "duplicate_correction":
        receipt["corrections"].append(deepcopy(receipt["corrections"][-1]))
    elif damage == "new_source":
        records[-1]["scripts"] = {key: value + "# replaced\n" for key, value in reference.items()}
    else:
        receipt["execution"]["unobserved"] = "a fabricated host record"
    with pytest.raises((AssertionError, SkillPackageValidationError)):
        assert_reference_provenance(plan, receipt, reference, execution_records=records)


def test_blind_reference_diagnostic_target_never_contains_expected_literal():
    request = blind_request()
    request["blind_cases"][0]["cases"][0]["output_targets"].append({"type": "stderr_contains"})
    assert_reference_isolation(request["stage"], request)
    request["blind_cases"][0]["cases"][0]["output_targets"][-1]["expected"] = "PRIVATE_EXPECTED_ERROR_CANARY"
    with pytest.raises(AssertionError):
        assert_reference_isolation(request["stage"], request)


def public_provenance_fixture():
    from app.domain.services.skills.skill_public_output_contract import (
        compile_public_output_contract, public_contract_receipt,
    )
    plan, receipt, reference = provenance_fixture()
    declared = interfaces()
    declared[0]["json_outputs"] = [{"path": "@output/status.json", "shape": {
        "type": "object", "properties": {"columns": {
            "type": "array", "items": {"type": "unknown"}, "column_name_prefix": ["group"]}}}}]
    contract = compile_public_output_contract(declared)
    typed = SyntheticValidationPlan.model_validate(plan)
    proof = public_contract_receipt(contract, typed, [{
        "script_path": typed.scripts[0].script_path, "id": case.id, "status": "passed",
        "checks": [{"rule_id": contract.rules[0].id, "status": "passed", "observations": 1}],
    } for case in typed.scripts[0].cases if case.expected_exit_code == 0])
    for item, case in zip(receipt["execution"]["cases"], typed.scripts[0].cases):
        item.update(actual_exit_code=case.expected_exit_code,
                    expected_exit_code=case.expected_exit_code, reason="expectations_matched")
    receipt["execution"]["public_output_verification"] = proof
    receipt["initial_execution"] = {**deepcopy(receipt["execution"]),
        "plan_sha256": receipt["original_plan_sha256"]}
    return plan, receipt, reference, declared


def test_acceptance_verifies_public_contract_from_original_interface_in_both_executions():
    plan, receipt, reference, declared = public_provenance_fixture()
    before = deepcopy((plan, receipt, reference, declared))
    assert_reference_provenance(plan, receipt, reference, interfaces=declared)
    assert (plan, receipt, reference, declared) == before


@pytest.mark.parametrize("execution", ["initial_execution", "execution"])
@pytest.mark.parametrize("damage", ["missing_evidence", "different_contract", "no_observation"])
def test_acceptance_rejects_self_reported_or_missing_public_contract_evidence(execution, damage):
    from app.domain.services.skills.skill_public_output_contract import PublicOutputContractError
    plan, receipt, reference, declared = public_provenance_fixture()
    proof = receipt[execution]["public_output_verification"]
    if damage == "missing_evidence":
        del receipt[execution]["public_output_verification"]
    elif damage == "different_contract":
        proof["contract_sha256"] = "0" * 64
    else:
        for case in proof["cases"]:
            case["status"] = "not_observed"
            case["checks"][0].update(status="not_observed", observations=0)
    with pytest.raises(PublicOutputContractError):
        assert_reference_provenance(plan, receipt, reference, interfaces=declared)


@pytest.mark.parametrize("damage", ["input", "tolerance", "wrong_source", "missing_case", "wrong_after", "wrong_target_type"])
def test_reference_provenance_rejects_unaccounted_change_or_forged_receipt(damage):
    plan, receipt, reference = provenance_fixture()
    if damage == "input":
        plan["scripts"][0]["cases"][0]["input_files"][0]["rows"][0]["value"] = 999
    elif damage == "tolerance":
        plan["scripts"][0]["cases"][0]["assertions"][0]["fields"]["mean"]["atol"] = 1000
    elif damage == "wrong_source":
        reference[next(iter(reference))] += "# different bytes"
    elif damage == "missing_case":
        receipt["execution"]["cases"].pop()
    elif damage == "wrong_after":
        receipt["corrections"][0]["after"] = 4
    else:
        receipt["corrections"][0]["target_type"] = "csv_row_count"
    with pytest.raises(AssertionError):
        assert_reference_provenance(plan, receipt, reference)


def runtime_fixture():
    request = blind_request()
    plan, receipt, reference, _ = public_provenance_fixture()
    metadata = request["script_request"]
    scripts = [{**metadata, "content": reference[metadata["path"]]}]
    execution = deepcopy(receipt["execution"])
    execution["status"] = "failed"
    execution["cases"][0].update(status="failed", reason="program_runtime_failure", actual_exit_code=1,
        runtime_error_type="TypeError", diagnostic="TypeError: PRIVATE_ERROR_VALUE_CANARY", checks=[])
    public = execution["public_output_verification"]
    public["status"] = "failed"
    public["cases"][0].update(status="failed", checks=[{"rule_id": "C001", "status": "failed",
        "observations": 0, "reason": "public_execution_failed"}])
    record = {"scripts": reference, "plan": plan, "execution": execution}
    return request, scripts, record


def test_runtime_repair_transmits_only_own_source_and_host_typed_crash_categories():
    request, scripts, record = runtime_fixture()
    repair = runtime_context(scripts, record)
    request["runtime_repair"] = repair
    assert set(repair) == {"reference_scripts", "failures"}
    assert repair["failures"] == [{"script_path": scripts[0]["path"], "case_id": "group-normal", "exception_type": "TypeError"}]
    assert "PRIVATE_ERROR_VALUE_CANARY" not in json.dumps(repair)
    assert_reference_isolation(request["stage"], request, runtime_repair_context=repair)


@pytest.mark.parametrize("damage", ["observed_value", "changed_input", "scientific_mismatch", "untyped_error", "exit_mismatch", "wrong_source", "interface_observed"])
def test_runtime_context_cannot_turn_scientific_or_unsafe_failures_into_build_repairs(damage):
    _, scripts, record = runtime_fixture()
    case = record["execution"]["cases"][0]
    if damage == "observed_value":
        case["checks"] = [{"passed": False, "expected": 3, "actual": 99}]
    elif damage == "changed_input":
        case["input_unchanged"] = False
    elif damage == "scientific_mismatch":
        case["reason"] = "assertion_mismatch"
    elif damage == "untyped_error":
        case["runtime_error_type"] = "ValueError"
    elif damage == "exit_mismatch":
        record["execution"]["cases"][1]["actual_exit_code"] = 2
    elif damage == "wrong_source":
        record["scripts"][scripts[0]["path"]] += "# changed"
    else:
        record["execution"]["public_output_verification"]["cases"][0]["checks"][0]["observations"] = 1
    with pytest.raises(AssertionError):
        runtime_context(scripts, record)


@pytest.mark.parametrize("field", ["observed", "expected", "stderr", "review"])
def test_runtime_request_rejects_extra_material_even_when_context_matches(field):
    request, scripts, record = runtime_fixture()
    repair = runtime_context(scripts, record)
    repair[field] = "UNEXPECTED_MATERIAL"
    request["runtime_repair"] = repair
    with pytest.raises(AssertionError):
        assert_reference_isolation(request["stage"], request, runtime_repair_context=repair)
