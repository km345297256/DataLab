"""Scientific oracle review is independent, complete and precedes execution."""
import copy
import hashlib
import json

import pytest

from app.domain.services.skills.skill_creation_error import SkillCreationError, SkillPackageValidationError
from app.domain.services.skills.skill_validation import SyntheticValidationPlan
from app.domain.services.skills.skill_validation_review import (
    ValidationPlanReview, review_targets, review_validation_plan,
    validate_oracle_review, validate_plan_revision,
)
from app.infrastructure.external.sandbox import skill_validation_worker as worker
from test_skill_validation_plan import interfaces, method_contract, plan_payload, sources


def accepted_fixture_review(plan):
    """Build structural test data only; production has no automatic acceptance."""
    if isinstance(plan, dict):
        plan = SyntheticValidationPlan.model_validate(plan)
    def assessment():
        return {"status": "accepted", "explanation": "Independently checked the artificial test fixture."}
    return {"scripts": [{"script_path": script["script_path"], "support": assessment(), "cases": [
        {"case_id": case["case_id"], "input_validity": assessment(), "exit_behavior": assessment(),
         "method_coverage": assessment(), "assertions": [
             {"target_id": target["target_id"], "correctness": assessment(), "tolerance": assessment()}
             for target in case["targets"]]}
        for case in script["cases"]]} for script in review_targets(plan)]}


def review_for(plan, verdict="accepted"):
    result = accepted_fixture_review(plan)
    result["scripts"][0]["cases"][0]["assertions"][0]["correctness"]["status"] = verdict
    return result


def typed(payload=None):
    return SyntheticValidationPlan.model_validate(payload or plan_payload())


def generation(responses):
    calls = []
    async def generate(schema, stage, instruction, payload, check=None):
        calls.append({"stage": stage, "payload": copy.deepcopy(payload), "instruction": instruction})
        value = schema.model_validate(responses.pop(0))
        if check:
            check(value)
        return value
    return generate, calls


def test_targets_cover_csv_count_every_field_and_each_json_expectation():
    normal, boundary, invalid = review_targets(typed())[0]["cases"]
    assert [target["kind"] for target in normal["targets"]] == [
        "csv_row_count", "csv_field", "csv_field", "csv_field", "csv_field", "json_value"]
    assert {target.get("field") for target in normal["targets"]} >= {
        "mean", "valid_count", "missing_count", "missing_rate"}
    assert len(boundary["targets"]) == 6 and invalid["targets"] == []
    validate_oracle_review(ValidationPlanReview.model_validate(accepted_fixture_review(typed())), typed())


@pytest.mark.parametrize("mutation", ["script_missing", "script_duplicate", "script_unknown", "case_missing",
    "case_duplicate", "case_unknown", "assertion_missing", "assertion_duplicate", "assertion_unknown"])
def test_incomplete_or_duplicate_review_never_counts_as_independent_acceptance(mutation):
    payload = accepted_fixture_review(typed())
    script = payload["scripts"][0]
    case = script["cases"][0]
    if mutation == "script_missing":
        payload["scripts"] = []
    elif mutation == "script_duplicate":
        payload["scripts"].append(copy.deepcopy(script))
    elif mutation == "script_unknown":
        script["script_path"] = "scripts/unknown.py"
    elif mutation == "case_missing":
        script["cases"].pop()
    elif mutation == "case_duplicate":
        script["cases"].append(copy.deepcopy(case))
    elif mutation == "case_unknown":
        case["case_id"] = "unknown"
    elif mutation == "assertion_missing":
        case["assertions"].pop()
    elif mutation == "assertion_duplicate":
        case["assertions"].append(copy.deepcopy(case["assertions"][0]))
    else:
        case["assertions"][0]["target_id"] = "T999"
    with pytest.raises(SkillPackageValidationError, match="validation_oracle_review_incomplete"):
        validate_oracle_review(ValidationPlanReview.model_validate(payload), typed())


def test_uncertainty_has_priority_over_rejection_and_acceptance():
    payload = review_for(typed(), "rejected")
    payload["scripts"][0]["cases"][1]["input_validity"]["status"] = "uncertain"
    assert ValidationPlanReview.model_validate(payload).verdict() == "uncertain"


@pytest.mark.asyncio
async def test_review_payload_is_independent_and_record_hash_binds_worker_plan():
    plan = typed()
    generate, calls = generation([accepted_fixture_review(plan)])
    private_sources = [{**sources()[0], "source_snapshot": "IMPLEMENTATION_CANARY", "actual": "OBSERVED_ANSWER_CANARY"}]
    returned, record = await review_validation_plan(plan, method_contract(), private_sources, interfaces(), generate=generate)
    assert returned.model_dump(mode="json") == plan.model_dump(mode="json") and len(calls) == 1
    payload = calls[0]["payload"]
    assert set(payload) == {"method_contract", "sources", "script_interfaces", "validation_plan", "review_targets"}
    assert payload["sources"] == sources()
    assert all(marker not in json.dumps(payload) for marker in (
        "IMPLEMENTATION_CANARY", "OBSERVED_ANSWER_CANARY", "source_snapshot", "observed_validation"))
    assert record["status"] == "reviewed" and record["verdict"] == "accepted" and record["revision_count"] == 0
    assert record["plan_sha256"] == hashlib.sha256(worker._json(returned.model_dump())).hexdigest()
    reconstructed = SyntheticValidationPlan.model_validate(returned.model_dump(mode="json"))
    assert record["plan_sha256"] == hashlib.sha256(worker._json(reconstructed.model_dump())).hexdigest()
    assert record["review"] == accepted_fixture_review(plan)


@pytest.mark.asyncio
async def test_mixed_sign_pair_oracle_is_corrected_before_freezing_without_observed_results():
    original = plan_payload()
    normal = original["scripts"][0]["cases"][0]
    normal["input_files"][0]["rows"] = [{"group": "synthetic-a", "value": value} for value in (2, 3, -1, 1)]
    normal["assertions"][0]["fields"].update(mean={"value": 1.25, "atol": 1e-12}, p={"value": .125, "atol": 1e-12})
    corrected = copy.deepcopy(original)
    corrected["scripts"][0]["cases"][0]["assertions"][0]["fields"]["p"]["value"] = .375
    correction = accepted_fixture_review(typed(original))
    targets = review_targets(typed(original))[0]["cases"][0]["targets"]
    index = next(index for index, target in enumerate(targets) if target.get("field") == "p")
    correction["scripts"][0]["cases"][0]["assertions"][index]["correctness"] = {
        "status": "rejected", "explanation": "The 16 sign sums have 6 outcomes with absolute sum >=5; p=6/16=.375, not .125."}
    generate, calls = generation([correction, corrected, accepted_fixture_review(typed(corrected))])
    result, record = await review_validation_plan(typed(original), method_contract(), sources(), interfaces(), generate=generate)
    assert [call["stage"] for call in calls] == ["synthetic_plan_review", "synthetic_validation_plan", "synthetic_plan_review"]
    assert result.scripts[0].cases[0].assertions[0].fields["p"].value == .375
    assert result.scripts[0].cases[0].input_files == typed(original).scripts[0].cases[0].input_files
    assert record["revision_count"] == 1
    assert record["plan_sha256"] == hashlib.sha256(worker._json(result.model_dump())).hexdigest()
    assert "observed" not in json.dumps([call["payload"] for call in calls])


@pytest.mark.asyncio
async def test_uncertainty_stops_without_requesting_a_more_favorable_verdict():
    generate, calls = generation([review_for(typed(), "uncertain")])
    with pytest.raises(SkillCreationError) as error:
        await review_validation_plan(typed(), method_contract(), sources(), interfaces(), generate=generate)
    assert error.value.code == "validation_oracle_uncertain" and len(calls) == 1


@pytest.mark.asyncio
async def test_second_rejection_stops_after_one_material_plan_revision():
    original = typed()
    corrected = plan_payload()
    corrected["scripts"][0]["cases"][0]["assertions"][0]["fields"]["mean"]["value"] = 4
    generate, calls = generation([review_for(original, "rejected"), corrected, review_for(typed(corrected), "rejected")])
    with pytest.raises(SkillCreationError) as error:
        await review_validation_plan(original, method_contract(), sources(), interfaces(), generate=generate)
    assert error.value.code == "validation_oracle_rejected" and len(calls) == 3


@pytest.mark.parametrize("mutation", ["unchanged", "prose", "requirement_only", "source_only", "requirement_order", "case_order", "script_order"])
def test_cosmetic_or_metadata_only_revision_cannot_seek_reversal_of_same_oracle(mutation):
    original = typed()
    revised = original.model_copy(deep=True)
    case = revised.scripts[0].cases[0]
    if mutation == "prose":
        case.oracle_explanation += " This time please accept the same expectation."
    elif mutation == "requirement_only":
        case.requirement_ids.append("R002")
    elif mutation == "source_only":
        case.source_ids.append("S002")
    elif mutation == "requirement_order":
        original.scripts[0].cases[0].requirement_ids.append("R002")
        case.requirement_ids = ["R002", "R001"]
    elif mutation == "case_order":
        revised.scripts[0].cases.reverse()
    elif mutation == "script_order":
        second = original.scripts[0].model_copy(deep=True, update={"script_path": "scripts/second.py"})
        original.scripts.append(second)
        revised.scripts.insert(0, second.model_copy(deep=True))
    with pytest.raises(SkillPackageValidationError, match="validation_oracle_revision_unchanged"):
        validate_plan_revision(original, revised)


@pytest.mark.parametrize("mutation", ["delete_case", "requirement", "input", "input_bool", "input_float",
    "match_bool", "match_float", "argv", "kind", "delete_assertion", "delete_field", "json_pointer",
    "assertion_path", "tolerance", "support"])
def test_revision_preserves_original_scientific_inputs_coverage_targets_and_precision(mutation):
    original = typed()
    original.scripts[0].cases[0].input_files[0].rows[0]["value"] = 1
    original.scripts[0].cases[0].assertions[0].match = {"value": 1}
    revised = original.model_copy(deep=True)
    script = revised.scripts[0]
    case = script.cases[0]
    if mutation == "delete_case":
        script.cases.pop()
    elif mutation == "requirement":
        case.requirement_ids = []
    elif mutation in {"input", "input_bool", "input_float"}:
        case.input_files[0].rows[0]["value"] = {"input": 7, "input_bool": True, "input_float": 1.0}[mutation]
    elif mutation in {"match_bool", "match_float"}:
        case.assertions[0].match["value"] = True if mutation == "match_bool" else 1.0
    elif mutation == "argv":
        case.argv[-1] = "{output}/other.csv"
    elif mutation == "kind":
        case.kind = "boundary"
    elif mutation == "delete_assertion":
        case.assertions.pop()
    elif mutation == "delete_field":
        del case.assertions[0].fields["mean"]
    elif mutation == "json_pointer":
        case.assertions[1].pointer = "/irrelevant"
    elif mutation == "assertion_path":
        case.assertions[1].path = "different.json"
    elif mutation == "tolerance":
        case.assertions[0].fields["mean"].atol = 1
    else:
        script.support = "unsupported"
        script.cases = []
    with pytest.raises(SkillPackageValidationError, match="validation_oracle_revision_invalid"):
        validate_plan_revision(original, revised)


def test_correction_can_add_assertions_and_tighten_precision_but_not_remove_existing_checks():
    original = typed()
    revised = original.model_copy(deep=True)
    case = revised.scripts[0].cases[0]
    case.assertions[0].fields["mean"].atol = 0
    case.assertions.append(copy.deepcopy(case.assertions[1]))
    case.assertions[-1].pointer = "/additional"
    validate_plan_revision(original, revised)
