"""New successful probes learn only from the frozen independent reference."""
import copy
import json

import pytest

from app.domain.services.skills.skill_creation_error import SkillCreationError, SkillPackageValidationError
from app.domain.services.skills.skill_reference_verification import (
    correct_reference_expectations, reference_generation_payload, verify_reference_plan,
)
from app.domain.services.skills.skill_validation import StderrContainsAssertion
from app.domain.services.skills.skill_validation_extension import CoverageReferenceConfirmation
from app.infrastructure.external.sandbox import skill_validation_worker as worker
from test_skill_reference_verification import generation, host_receipt, output_files
from test_skill_validation_plan import interfaces, method_contract, sources
from test_skill_validation_review import typed


def canonical(plan):
    return json.dumps(plan.model_dump(mode="python"), sort_keys=True, allow_nan=False)


def add_normal(plan, *, identifier="group-zero", value=0, expected=99):
    proposed = plan.model_copy(deep=True)
    case = plan.scripts[0].cases[0].model_copy(deep=True)
    case.id = identifier
    case.input_files[0].rows[0]["value"] = value
    case.assertions[0].fields["mean"].value = expected
    proposed.scripts[0].cases.append(case)
    return proposed


def calculated_files(case):
    if case.kind != "normal":
        return output_files(case)
    values = [item["value"] for item in case.input_files[0].rows]
    mean = sum(values) / len(values)
    return {"summary.csv": f"group,mean,valid_count,missing_count,missing_rate\nsynthetic-a,{mean},2,0,0\n".encode(),
            "status.json": b'{"status":"ok"}'}


async def retained_reference(execute=None):
    generate, calls = generation()
    runs, callbacks = [], []
    async def runner(programs, plan):
        runs.append((copy.deepcopy(programs), plan.model_copy(deep=True)))
        if execute is not None:
            return execute(programs, plan, len(runs))
        return host_receipt(programs, plan, files=calculated_files)
    plan, receipt = await verify_reference_plan(typed(), method_contract(), sources(), interfaces(),
        generate=generate, execute=runner, on_verified=callbacks.append)
    return plan, receipt, callbacks[0], runs, calls


@pytest.mark.asyncio
async def test_new_numeric_case_is_corrected_then_reexecuted_without_touching_frozen_cases_or_source():
    plan, base, confirm, runs, calls = await retained_reference()
    proposed = add_normal(plan)
    frozen = canonical(plan)
    proposed_before = canonical(proposed)
    result = await confirm(proposed)
    assert isinstance(result, CoverageReferenceConfirmation)
    assert canonical(plan) == frozen and canonical(proposed) == proposed_before
    assert [canonical(run) for _, run in runs][:2] == [frozen, proposed_before]
    assert len(runs) == 3 and all(programs == runs[0][0] for programs, _ in runs)
    assert len(calls) == 2
    assert result.plan.scripts[0].cases[-1].assertions[0].fields["mean"].value == 2
    assert [item.model_dump() for item in result.plan.scripts[0].cases[:-1]] == [item.model_dump() for item in plan.scripts[0].cases]
    record = result.evidence
    assert record["initial_execution"]["status"] == "failed" and record["execution"]["status"] == "passed"
    assert record["corrected_plan_sha256"] == record["execution"]["plan_sha256"]
    extension = record["postcondition_extension"]
    assert extension["proposed_plan_sha256"] == record["initial_execution"]["plan_sha256"]
    assert extension["confirmed_plan_sha256"] == record["corrected_plan_sha256"]
    assert extension["base_reference_verification"] == base
    assert extension["previous_confirmations"] == []
    assert extension["corrections"] == record["corrections"]
    assert record["corrections"][0]["case_id"] == "group-zero"


@pytest.mark.asyncio
async def test_second_extension_freezes_first_and_keeps_flat_execution_history_and_defensive_copies():
    plan, _, confirm, runs, calls = await retained_reference()
    first = await confirm(add_normal(plan))
    stable_first = first.plan.model_copy(deep=True)
    first_record = copy.deepcopy(first.evidence)
    # Review/caller mutations cannot change the closure's frozen baseline.
    first.plan.scripts[0].cases[-1].input_files[0].rows[0]["value"] = 888
    first.evidence["corrections"].clear()
    second = await confirm(add_normal(stable_first, identifier="group-negative", value=-4))
    ext = second.evidence["postcondition_extension"]
    assert len(second.evidence["corrections"]) == 2
    assert len(ext["previous_confirmations"]) == 1
    previous = ext["previous_confirmations"][0]
    assert previous["execution"] == first_record["execution"]
    assert "base_reference_verification" not in previous and "previous_confirmations" not in previous
    assert ext["previous_plan_sha256"] == first_record["corrected_plan_sha256"]
    repeated = await confirm(second.plan)
    assert repeated.evidence["postcondition_extension"] == ext
    assert repeated.evidence["corrections"] == second.evidence["corrections"]
    assert len(runs) == 6 and len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", ["old_numeric", "string", "null", "boolean", "wrong_source", "wrong_plan", "runtime"])
async def test_extension_failure_cannot_relearn_old_or_nonnumeric_expectations_and_does_not_advance_baseline(defect):
    corrupt = True
    def execute(programs, plan, number):
        result = host_receipt(programs, plan, files=calculated_files)
        if number > 1 and corrupt:
            if defect == "wrong_source": result["source_sha256"] = {path: "0" * 64 for path in programs}
            elif defect == "wrong_plan": result["plan_sha256"] = "0" * 64
            else:
                row = result["cases"][0 if defect == "old_numeric" else -1]
                if defect == "runtime":
                    row.update(status="failed", reason="program_runtime_failure", actual_exit_code=1, checks=[])
                else:
                    check = row["checks"][1 if defect == "old_numeric" else -1]
                    check.update(actual="4" if defect == "old_numeric" else {"string": "different", "null": None, "boolean": True}[defect], passed=False)
                    row.update(status="failed", reason="assertion_mismatch")
                result["status"] = "failed"
        return result
    plan, _, confirm, runs, calls = await retained_reference(execute)
    proposed = add_normal(plan)
    with pytest.raises((SkillCreationError, SkillPackageValidationError)):
        await confirm(proposed)
    assert len(runs) == 2 and len(calls) == 2
    corrupt = False
    accepted = await confirm(proposed)
    assert accepted.evidence["execution"]["status"] == "passed"
    assert len(runs) == 4 and len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("mutate", ["input", "expected", "tolerance", "target"])
async def test_later_extension_cannot_change_a_previously_confirmed_new_case(mutate):
    plan, _, confirm, runs, _ = await retained_reference()
    accepted = await confirm(add_normal(plan))
    proposed = add_normal(accepted.plan, identifier="group-next", value=6)
    old = proposed.scripts[0].cases[-2]
    if mutate == "input": old.input_files[0].rows[0]["value"] = 3
    elif mutate == "expected": old.assertions[0].fields["mean"].value = 50
    elif mutate == "tolerance": old.assertions[0].fields["mean"].rtol = 0.5
    else: old.assertions[0].path = "other.csv"
    with pytest.raises(SkillPackageValidationError, match="validation_oracle_revision_invalid"):
        await confirm(proposed)
    assert len(runs) == 3


@pytest.mark.asyncio
async def test_same_plan_reexecution_cannot_correct_a_previously_confirmed_answer():
    corrupt = False
    def execute(programs, plan, number):
        result = host_receipt(programs, plan, files=calculated_files)
        if corrupt:
            row = result["cases"][-1]
            row["checks"][1].update(actual="8", passed=False)
            row.update(status="failed", reason="assertion_mismatch")
            result["status"] = "failed"
        return result
    plan, _, confirm, runs, _ = await retained_reference(execute)
    first = await confirm(add_normal(plan))
    corrupt = True
    with pytest.raises(SkillCreationError):
        await confirm(first.plan)
    assert len(runs) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("defect", ["changed_value", "wrong_source", "wrong_plan"])
async def test_numeric_correction_confirmation_is_bound_to_same_source_and_final_plan(defect):
    def execute(programs, plan, number):
        result = host_receipt(programs, plan, files=calculated_files)
        if number == 3:
            if defect == "wrong_source": result["source_sha256"] = {path: "0" * 64 for path in programs}
            elif defect == "wrong_plan": result["plan_sha256"] = "0" * 64
            else:
                row = result["cases"][-1]
                row["checks"][1].update(actual="4", passed=False)
                row.update(status="failed", reason="assertion_mismatch")
                result["status"] = "failed"
        return result
    plan, _, confirm, runs, calls = await retained_reference(execute)
    with pytest.raises(SkillCreationError):
        await confirm(add_normal(plan))
    assert len(runs) == 3 and len(calls) == 2


@pytest.mark.parametrize("defect", [None, "false", "expected", "integer_actual", "integer_passed", "missing"])
def test_stderr_contains_remains_an_exact_host_observation_and_never_an_oracle_correction(defect):
    plan = typed()
    invalid = plan.scripts[0].cases[-1]
    invalid.assertions.append(StderrContainsAssertion(type="stderr_contains", expected="ROLE_DIAGNOSTIC_CANARY"))
    programs = {plan.scripts[0].script_path: "# independently reviewed reference"}
    receipt = host_receipt(programs, plan)
    row = receipt["cases"][-1]
    row.update(status="passed", reason="expectations_matched",
        checks=worker._assertions(invalid, {}, stderr="Invalid ROLE_DIAGNOSTIC_CANARY"))
    receipt["status"] = "passed"
    check = row["checks"][0]
    if defect == "false": check.update(actual=False, passed=False)
    elif defect == "expected": check["expected"] = "different"
    elif defect == "integer_actual": check["actual"] = 1
    elif defect == "integer_passed": check["passed"] = 1
    elif defect == "missing": row["checks"] = []
    if defect:
        with pytest.raises(SkillCreationError):
            correct_reference_expectations(plan, receipt, programs)
    else:
        corrected, changes = correct_reference_expectations(plan, receipt, programs)
        assert changes == [] and canonical(corrected) == canonical(plan)
    blind = reference_generation_payload(plan, method_contract(), sources(), interfaces())
    assert blind["blind_cases"][0]["cases"][-1]["output_targets"] == [{"type": "stderr_contains"}]
    assert "ROLE_DIAGNOSTIC_CANARY" not in json.dumps(blind)


@pytest.mark.asyncio
@pytest.mark.parametrize("matching", [True, False])
async def test_existing_invalid_case_can_add_diagnostic_without_exposing_it_or_rebuilding_reference(matching):
    def execute(programs, plan, number):
        result = host_receipt(programs, plan, files=calculated_files)
        invalid = plan.scripts[0].cases[-1]
        if invalid.assertions:
            row = result["cases"][-1]
            row["checks"] = worker._assertions(invalid, {}, stderr="missing role group" if matching else "wrong format")
            passed = all(item["passed"] for item in row["checks"])
            row.update(status="passed" if passed else "failed", reason="expectations_matched" if passed else "assertion_mismatch")
            result["status"] = row["status"]
        return result
    plan, base, confirm, runs, calls = await retained_reference(execute)
    proposed = plan.model_copy(deep=True)
    proposed.scripts[0].cases[-1].assertions.append(StderrContainsAssertion(type="stderr_contains", expected="group"))
    if matching:
        result = await confirm(proposed)
        assert canonical(result.plan) == canonical(proposed)
        assert result.evidence["corrections"] == []
    else:
        with pytest.raises(SkillCreationError):
            await confirm(proposed)
    assert len(runs) == 2 and len(calls) == 2


@pytest.mark.asyncio
async def test_new_case_count_and_array_length_can_be_corrected_with_unchanged_targets_and_tolerances():
    from app.domain.services.skills.skill_validation import JSONArrayLengthAssertion
    def files(case):
        return {**calculated_files(case), "length.json": b'{"items":[2,4]}'}
    def execute(programs, plan, number):
        return host_receipt(programs, plan, files=files)
    plan, _, confirm, runs, _ = await retained_reference(execute)
    proposed = add_normal(plan, expected=2)
    new = proposed.scripts[0].cases[-1]
    new.assertions[0].count = 8
    new.assertions.append(JSONArrayLengthAssertion(type="json_array_length", path="length.json", pointer="/items", length=7))
    result = await confirm(proposed)
    corrected = result.plan.scripts[0].cases[-1]
    assert corrected.assertions[0].count == 1 and corrected.assertions[-1].length == 2
    assert {item["target_type"] for item in result.evidence["corrections"]} == {"csv_row_count", "json_array_length"}
    assert len(runs) == 3
