"""Exact structure checks stay immutable across blind references and repair."""
from unittest.mock import AsyncMock

import pytest

from app.domain.services.skills.skill_creation_error import SkillCreationError, SkillPackageValidationError
from app.domain.services.skills.skill_reference_verification import (
    correct_reference_expectations, reference_generation_payload, verify_reference_plan,
)
from app.domain.services.skills.skill_validation_extension import (
    CoverageReferenceConfirmation, VerificationCoverageAmendment, apply_coverage_amendment,
)
from app.domain.services.skills.skill_validation_review import review_targets, review_validation_plan
from test_skill_reference_verification import generation as reference_generation, host_receipt, output_files
from test_skill_validation_plan import interfaces, method_contract, sources
from test_skill_validation_review import accepted_fixture_review, generation, typed


def additions(plan):
    return VerificationCoverageAmendment.model_validate({"assertion_additions": [
        {"script_path": plan.scripts[0].script_path, "case_id": plan.scripts[0].cases[0].id,
         "assertion": {"type": "json_object_keys", "path": "status.json", "pointer": "", "keys": ["status"]}},
        {"script_path": plan.scripts[0].script_path, "case_id": plan.scripts[0].cases[0].id,
         "assertion": {"type": "csv_header", "path": "summary.csv",
                       "columns": ["group", "mean", "valid_count", "missing_count", "missing_rate"]}}],
        "new_cases": []})


def coverage_rejection(plan):
    review = accepted_fixture_review(plan)
    review["scripts"][0]["cases"][0]["method_coverage"] = {
        "status": "rejected", "explanation": "Exact JSON keys and CSV header are not observed by this case."}
    return review


def fresh_structure_review(plan):
    review = accepted_fixture_review(plan)
    for script in review["scripts"]:
        for case in script["cases"]:
            case["assertions"] = [item for item in case["assertions"]
                                  if item["target_id"] in {"T007", "T008"}
                                  and case["case_id"] == plan.scripts[0].cases[0].id]
    return review


def test_structure_targets_are_exact_and_reference_does_not_receive_expected_names():
    plan = apply_coverage_amendment(typed(), additions(typed()))
    targets = review_targets(plan)[0]["cases"][0]["targets"][-2:]
    assert targets[0]["kind"] == "json_object_keys" and targets[0]["expected"] == ["status"]
    assert targets[1]["kind"] == "csv_header" and targets[1]["expected"][0] == "group"
    blind = reference_generation_payload(plan, method_contract(), sources(), interfaces())
    assert blind["blind_cases"][0]["cases"][0]["output_targets"][-2:] == [
        {"type": "json_object_keys", "path": "status.json", "pointer": ""},
        {"type": "csv_header", "path": "summary.csv"}]


@pytest.mark.parametrize("mutation", [None, "extra_key", "wrong_header", "missing_observation", "forged_pass"])
def test_reference_cannot_correct_a_required_structure_from_its_observation(mutation):
    plan = apply_coverage_amendment(typed(), additions(typed()))
    original = plan.model_dump_json()
    programs = {plan.scripts[0].script_path: "independent-reference"}
    def files(case):
        result = output_files(case)
        if mutation in {"extra_key", "forged_pass"}:
            result["status.json"] = b'{"status":"ok","extra":1}'
        elif mutation == "wrong_header":
            result["summary.csv"] = result["summary.csv"].replace(b"missing_rate", b"other_rate", 1)
        return result
    receipt = host_receipt(programs, plan, files=files)
    if mutation == "missing_observation":
        receipt["cases"][0]["checks"] = receipt["cases"][0]["checks"][:-1]
    if mutation == "forged_pass":
        for item in receipt["cases"][0]["checks"]:
            item["passed"] = True
    if mutation is None:
        corrected, corrections = correct_reference_expectations(plan, receipt, programs)
        assert corrections == [] and corrected.model_dump_json() == original
    else:
        with pytest.raises(SkillCreationError) as error:
            correct_reference_expectations(plan, receipt, programs)
        assert error.value.code == "reference_calculation_unconfirmed"
    assert plan.model_dump_json() == original


@pytest.mark.asyncio
async def test_coverage_extension_reexecutes_frozen_reference_before_another_review():
    plan = typed()
    amendment = additions(plan)
    extended = apply_coverage_amendment(plan, amendment)
    responses = [coverage_rejection(plan), amendment.model_dump(mode="json"), fresh_structure_review(extended)]
    base_generate, calls = generation(responses)
    events = []
    async def generate(schema, stage, instruction, payload, check=None):
        events.append(stage)
        if stage == "synthetic_validation_plan":
            assert schema is VerificationCoverageAmendment
            assert payload["previous_validation_plan"] == plan.model_dump(mode="json")
            assert payload["validation_review"] == coverage_rejection(plan)
        elif "actual_reference_reexecution" in events:
            assert [len(case["targets"]) for case in payload["review_targets"][0]["cases"]] == [2, 0, 0]
        return await base_generate(schema, stage, instruction, payload, check)
    async def confirm(value):
        assert value.model_dump_json() == extended.model_dump_json()
        events.append("actual_reference_reexecution")
        return {"cases": [], "corrections": []}
    result, receipt = await review_validation_plan(plan, method_contract(), sources(), interfaces(),
        generate=generate, reference_evidence={"cases": [], "corrections": []}, confirm_postconditions=confirm)
    assert events == ["synthetic_plan_review", "synthetic_validation_plan", "actual_reference_reexecution", "synthetic_plan_review"]
    assert result.model_dump_json() == extended.model_dump_json()
    assert receipt["verdict"] == "accepted" and receipt["revision_count"] == 1
    assert receipt["review"] == accepted_fixture_review(extended)
    assert len(receipt["reused_target_reviews"]) == 1


@pytest.mark.asyncio
async def test_reference_failure_during_extension_cannot_request_another_acceptance():
    plan = typed()
    generate, calls = generation([coverage_rejection(plan), additions(plan).model_dump(mode="json")])
    confirm = AsyncMock(side_effect=SkillCreationError("synthetic_reference_execution", "reference_calculation_unconfirmed"))
    with pytest.raises(SkillCreationError) as error:
        await review_validation_plan(plan, method_contract(), sources(), interfaces(), generate=generate,
            reference_evidence={"cases": []}, confirm_postconditions=confirm)
    assert error.value.code == "reference_calculation_unconfirmed"
    assert [item["stage"] for item in calls] == ["synthetic_plan_review", "synthetic_validation_plan"]
    confirm.assert_awaited_once()


@pytest.mark.asyncio
async def test_unrelated_added_observations_cannot_reopen_rejected_case():
    plan = typed()
    amendment = additions(plan).model_dump(mode="json")
    for item in amendment["assertion_additions"]:
        item["case_id"] = plan.scripts[0].cases[1].id
    generate, calls = generation([coverage_rejection(plan), amendment])
    confirm = AsyncMock()
    with pytest.raises(SkillPackageValidationError, match="validation_oracle_revision_invalid"):
        await review_validation_plan(plan, method_contract(), sources(), interfaces(), generate=generate,
            reference_evidence={"cases": []}, confirm_postconditions=confirm)
    confirm.assert_not_awaited()
    assert len(calls) == 2


@pytest.mark.parametrize("field", ["input_validity", "exit_behavior", "correctness", "tolerance"])
@pytest.mark.asyncio
async def test_scientific_or_input_negative_does_not_authorize_structural_extension(field):
    plan = typed()
    negative = coverage_rejection(plan)
    case = negative["scripts"][0]["cases"][0]
    if field in {"correctness", "tolerance"}:
        case = case["assertions"][0]
    case[field]["status"] = "rejected"
    generate, calls = generation([negative])
    with pytest.raises(SkillCreationError) as error:
        await review_validation_plan(plan, method_contract(), sources(), interfaces(), generate=generate,
            reference_evidence={"cases": []}, confirm_postconditions=AsyncMock())
    assert error.value.code == "validation_oracle_rejected"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_real_reference_closure_reuses_source_and_checks_new_structure():
    plan = typed()
    generate, model_calls = reference_generation()
    execute = AsyncMock(side_effect=host_receipt)
    callbacks = []
    original, receipt = await verify_reference_plan(plan, method_contract(), sources(), interfaces(),
        generate=generate, execute=execute, on_verified=callbacks.append)
    extended = apply_coverage_amendment(original, additions(original))
    confirmed = await callbacks[0](extended)
    assert len(model_calls) == 2 and execute.await_count == 2
    assert execute.await_args_list[0].args[0] == execute.await_args_list[1].args[0]
    assert confirmed.plan.model_dump_json() == extended.model_dump_json()
    assert confirmed.evidence["reference_source_sha256"] == receipt["reference_source_sha256"]
    assert confirmed.evidence["corrections"] == [] and confirmed.evidence["execution"]["status"] == "passed"
    assert confirmed.evidence["corrected_plan_sha256"] != receipt["corrected_plan_sha256"]


@pytest.mark.parametrize("change_old_value", [False, True])
@pytest.mark.asyncio
async def test_new_success_case_confirms_only_new_answers_then_reviews_collective_coverage(change_old_value):
    plan = typed()
    new_case = plan.scripts[0].cases[0].model_copy(deep=True)
    new_case.id = "additional_success"
    new_case.input_files[0].rows[0]["value"] = 4
    new_case.input_files[0].rows[1]["value"] = 8
    # Deliberately wrong new answer: the independent confirmer must correct it.
    new_case.assertions[0].fields["mean"].value = 1
    amendment = VerificationCoverageAmendment.model_validate({
        "new_cases": [{"script_path": plan.scripts[0].script_path, "case": new_case.model_dump(mode="python")}],
        "remediations": [{"script_path": plan.scripts[0].script_path,
                          "case_id": plan.scripts[0].cases[0].id,
                          "new_case_ids": [new_case.id]}]})
    proposed = apply_coverage_amendment(plan, amendment)
    confirmed = proposed.model_copy(deep=True)
    confirmed.scripts[0].cases[-1].assertions[0].fields["mean"].value = 6
    if change_old_value:
        confirmed.scripts[0].cases[0].assertions[0].fields["mean"].value = 9
    calls = []
    async def generate(schema, stage, instruction, payload, check=None):
        calls.append(stage)
        if stage == "synthetic_validation_plan":
            value = amendment
        else:
            answer = (coverage_rejection(plan) if len(calls) == 1
                      else accepted_fixture_review(confirmed))
            requested = {(item["script_path"], case["case_id"]): {target["target_id"] for target in case["targets"]}
                         for item in payload["review_targets"] for case in item["cases"]}
            for script in answer["scripts"]:
                for case in script["cases"]:
                    case["assertions"] = [item for item in case["assertions"]
                        if item["target_id"] in requested[(script["script_path"], case["case_id"])]]
            value = schema.model_validate(answer)
        if check:
            check(value)
        return value
    confirm = AsyncMock(return_value=CoverageReferenceConfirmation(
        plan=confirmed, evidence={"status": "verified", "confirmation_fixture": True}))
    if change_old_value:
        with pytest.raises(SkillPackageValidationError, match="validation_oracle_revision_invalid"):
            await review_validation_plan(plan, method_contract(), sources(), interfaces(), generate=generate,
                reference_evidence={"status": "verified"}, confirm_postconditions=confirm)
        assert calls == ["synthetic_plan_review", "synthetic_validation_plan"]
    else:
        result, receipt = await review_validation_plan(plan, method_contract(), sources(), interfaces(), generate=generate,
            reference_evidence={"status": "verified"}, confirm_postconditions=confirm)
        assert result.scripts[0].cases[-1].assertions[0].fields["mean"].value == 6
        assert result.scripts[0].cases[0].assertions[0].fields["mean"].value == 3
        assert receipt["verdict"] == "accepted" and len(receipt["reused_target_reviews"]) == 1
        assert calls == ["synthetic_plan_review", "synthetic_validation_plan", "synthetic_plan_review"]
    assert proposed.scripts[0].cases[-1].assertions[0].fields["mean"].value == 1
    assert plan.scripts[0].cases[0].assertions[0].fields["mean"].value == 3

