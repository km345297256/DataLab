"""Blind executable references establish answers before skill execution."""
import copy
import hashlib
import itertools
import json
from unittest.mock import AsyncMock

import pytest

from skill_review_fixtures import review_for_payload

from app.domain.services.skills.method_contract import CoverageError
from app.domain.services.skills.skill_creation_error import SkillCreationError
from app.domain.services.skills.skill_reference_verification import (
    REFERENCE_EXPLANATION, correct_reference_expectations, reference_generation_payload, reference_review_identity, verify_reference_plan,
)
from app.domain.services.skills.skill_validation import ExpectedValue, JSONValueAssertion, JSONArrayLengthAssertion
from app.infrastructure.external.sandbox import skill_validation_worker as worker
from test_session_skill_creator import helper_script
from test_skill_validation_plan import interfaces, method_contract, sources
from test_skill_validation_review import typed


def method_review(context, *, verdict="preserved"):
    return {"sources": [{"source_id": source["id"], "status": "preserved", "explanation": "Original method source remains fully represented."}
            for source in context["sources"]],
        "requirements": [{"requirement_id": item["id"], "status": verdict, "explanation": "Checked actual reference calculations against the original method."}
            for item in context["method_contract"]["requirements"]],
        "implementation_appropriate": verdict == "preserved", "implementation_reason": "Independent formulas and complete input handling are implemented."}


def generation(*, verdict="preserved"):
    calls = []
    async def generate(schema, stage, instruction, payload, check):
        calls.append((stage, copy.deepcopy(payload)))
        if stage == "synthetic_reference_implementation":
            document = {**payload["script_request"], "content": helper_script()["content"]}
        else:
            assert stage == "synthetic_reference_review"
            document = method_review(payload, verdict=verdict)
        value = schema.model_validate(document)
        check(value)
        return value
    return generate, calls


def output_files(case):
    normal = case.kind == "normal"
    return {"summary.csv": ("group,mean,valid_count,missing_count,missing_rate\nsynthetic-a,3,2,0,0\n" if normal else
                           "group,mean,valid_count,missing_count,missing_rate\nsynthetic-a,,0,2,1\n").encode(),
            "status.json": json.dumps({"status": "ok"}).encode()}


def host_receipt(programs, plan, *, files=output_files):
    """Trusted test fixture: use production host parser, never model verdicts."""
    cases = []
    for script in plan.scripts:
        if script.support == "unsupported":
            cases.append({"script_path": script.script_path, "id": "not_run", "kind": "unsupported", "status": "unsupported"})
            continue
        for case in script.cases:
            checks = worker._assertions(case, files(case))
            passed = all(check["passed"] for check in checks)
            cases.append({"script_path": script.script_path, "id": case.id, "kind": case.kind,
                "status": "passed" if passed else "failed", "reason": "expectations_matched" if passed else "assertion_mismatch",
                "expected_exit_code": case.expected_exit_code, "actual_exit_code": 2 if case.kind == "invalid" else 0,
                "input_unchanged": True, "checks": checks})
    return {"status": "failed" if any(case["status"] == "failed" for case in cases) else "unsupported" if any(case["status"] == "unsupported" for case in cases) else "passed",
        "scope": "synthetic", "cases": cases,
        "source_sha256": {path: hashlib.sha256(code.encode()).hexdigest() for path, code in programs.items()},
        "plan_sha256": hashlib.sha256(worker._json(plan.model_dump())).hexdigest()}


@pytest.mark.asyncio
async def test_blind_generation_excludes_answers_observations_and_tested_code_and_corrects_only_expectations():
    plan = typed()
    normal = plan.scripts[0].cases[0]
    normal.assertions[0].fields["mean"].value = 314159.265
    normal.oracle_explanation = "SECRET_CANDIDATE_DERIVATION_314159"
    normal.description = "SECRET_PLAN_DESCRIPTION_314159"
    initial = plan.model_dump(mode="json")
    mixed_sources = [*sources(), {"id": "S999", "kind": "observed_summary", "text": "SECRET_OBSERVED_ANSWER"}]
    mixed_sources[0] = {**mixed_sources[0], "source_snapshot": "SECRET_TESTED_SOURCE", "actual": "SECRET_ACTUAL_RESULT"}
    declared = interfaces()
    declared[0]["validation_cases"][0]["expected"] = "SECRET_INTERFACE_EXPECTED"
    generate, calls = generation()
    executions = []
    async def execute(programs, candidate):
        executions.append((dict(programs), candidate.model_dump(mode="json")))
        return host_receipt(programs, candidate)
    corrected, record = await verify_reference_plan(plan, method_contract(), mixed_sources, declared,
        generate=generate, execute=execute)
    assert plan.model_dump(mode="json") == initial
    for stage, payload in calls:
        encoded = json.dumps(payload)
        assert not any(marker in encoded for marker in ("314159", "SECRET_", "observed_validation", "source_snapshot"))
        blind = payload["blind_cases"][0]["cases"][0]
        assert set(blind) == {"case_id", "input_files", "argv", "requirement_ids", "output_targets"}
        assert blind["input_files"] == normal.model_dump(mode="json")["input_files"]
        assert not {"expected", "count", "length", "atol", "rtol"} & set(blind["output_targets"][0])
    assert len(executions) == 2 and executions[0][0] == executions[1][0]
    assert [stage for stage, _ in calls] == ["synthetic_reference_implementation", "synthetic_reference_review"]
    changed = copy.deepcopy(initial)
    changed["scripts"][0]["cases"][0]["assertions"][0]["fields"]["mean"]["value"] = 3.0
    changed["scripts"][0]["cases"][0]["oracle_explanation"] = REFERENCE_EXPLANATION
    assert corrected.model_dump(mode="json") == changed
    assert record["corrections"] == [{"script_path": plan.scripts[0].script_path,
        "case_id": normal.id, "assertion_index": 0, "target_type": "csv_field", "field": "mean", "before": 314159.265, "after": 3.0}]
    assert record["status"] == "verified" and record["initial_execution"]["status"] == "failed"
    assert record["execution"]["status"] == "passed"
    assert record["corrected_plan_sha256"] == record["execution"]["plan_sha256"]
    assert record["original_plan_sha256"] != record["corrected_plan_sha256"]
    assert record["reference_source_sha256"] == record["execution"]["source_sha256"]
    assert record["original_explanations"] == [{"script_path": plan.scripts[0].script_path,
        "case_id": normal.id, "oracle_explanation": "SECRET_CANDIDATE_DERIVATION_314159"}]
    assert "SECRET_CANDIDATE_DERIVATION" not in corrected.model_dump_json()


@pytest.mark.asyncio
async def test_reference_with_no_corrections_is_valid_without_forcing_an_unchanged_revision():
    generate, calls = generation()
    execute = AsyncMock(side_effect=host_receipt)
    plan = typed()
    corrected, receipt = await verify_reference_plan(plan, method_contract(), sources(), interfaces(), generate=generate, execute=execute)
    assert corrected == plan and receipt["corrections"] == []
    execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_mixed_support_reviews_only_supported_requirements_and_never_claims_everything_verified():
    from app.domain.services.skills.skill_validation import ScriptValidationPlan
    from app.domain.services.skills.method_contract import MethodRequirement
    plan, contract, original_sources, declared = typed(), method_contract(), sources(), interfaces()
    plan.scripts.append(ScriptValidationPlan(script_path="scripts/unavailable.py", support="unsupported",
        reason="The separate method cannot be represented by these small tabular checks.", cases=[]))
    contract.requirements.append(MethodRequirement(id="R002", kind="method",
        statement="A separate scientific method needs specialized data unavailable in this verifier.", source_ids=["S002"]))
    original_sources.append({"id": "S002", "kind": "request", "text": contract.requirements[-1].statement})
    declared.append({**declared[0], "script_path": "scripts/unavailable.py"})
    generate, calls = generation()
    execute = AsyncMock(side_effect=host_receipt)
    corrected, record = await verify_reference_plan(plan, contract, original_sources, declared, generate=generate, execute=execute)
    assert record["status"] == "unsupported" and record["execution"]["status"] == "unsupported"
    assert corrected == plan and corrected.scripts[1].support == "unsupported"
    assert len(contract.requirements) == 2 and len(original_sources) == 2
    for _, payload in calls:
        assert [item["id"] for item in payload["method_contract"]["requirements"]] == ["R001"]
        assert [item["id"] for item in payload["sources"]] == ["S001"]
        assert [item["script_path"] for item in payload["script_interfaces"]] == ["scripts/group_summary.py"]
    execute.assert_awaited_once()
    assert record["execution"]["cases"][-1]["status"] == "unsupported"


@pytest.mark.asyncio
async def test_negative_reference_method_review_never_runs_code_or_becomes_tested_method_repair():
    generate, calls = generation(verdict="changed")
    execute = AsyncMock()
    with pytest.raises(SkillCreationError) as failure:
        await verify_reference_plan(typed(), method_contract(), sources(), interfaces(), generate=generate, execute=execute)
    assert (failure.value.stage, failure.value.code) == ("synthetic_reference_review", "reference_calculation_unconfirmed")
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_incomplete_positive_reference_review_keeps_repairable_structural_diagnostic():
    good, _ = generation()
    async def generate(schema, stage, instruction, payload, check):
        if stage != "synthetic_reference_review":
            return await good(schema, stage, instruction, payload, check)
        data = method_review(payload)
        data["requirements"] = []
        value = schema.model_validate(data)
        check(value)
        return value
    with pytest.raises(CoverageError, match="incomplete_method_review"):
        await verify_reference_plan(typed(), method_contract(), sources(), interfaces(), generate=generate, execute=AsyncMock())


def test_input_fields_named_expected_and_public_target_locators_remain_intact():
    plan = typed()
    payload = reference_generation_payload(plan, method_contract(), sources(), interfaces())
    assert payload["blind_cases"][0]["cases"][0]["output_targets"] == [
        {"type": "csv_rows", "path": "summary.csv", "match_fields": ["group"],
         "field_names": ["mean", "missing_count", "missing_rate", "valid_count"]},
        {"type": "json_value", "path": "status.json", "pointer": "/status"}]
    from app.domain.services.skills.skill_validation import SyntheticJSONInput
    plan.scripts[0].cases[0].input_files = [SyntheticJSONInput(format="json", path="table.json", data={"expected": "actual scientific input"})]
    assert reference_generation_payload(plan, method_contract(), sources(), interfaces())["blind_cases"][0]["cases"][0]["input_files"][0]["data"] == {"expected": "actual scientific input"}


def test_computed_csv_selector_values_do_not_leak_candidate_answers_to_reference():
    from app.domain.services.skills.skill_validation import validate_validation_plan
    plan = typed()
    selector = plan.scripts[0].cases[0].assertions[0].match
    selector["mean"] = 314159.265
    validate_validation_plan(plan, method_contract(), sources(), interfaces())
    payload = reference_generation_payload(plan, method_contract(), sources(), interfaces())
    target = payload["blind_cases"][0]["cases"][0]["output_targets"][0]
    assert target["match_fields"] == ["group", "mean"]
    assert "match" not in target
    assert "314159.265" not in json.dumps(payload)
    assert selector == {"group": "synthetic-a", "mean": 314159.265}
    # Host comparison keeps the original selector. A reference that cannot
    # satisfy it cannot replace it or invent a row to force a correction.
    programs = {"scripts/group_summary.py": "reference-program"}
    with pytest.raises(SkillCreationError):
        correct_reference_expectations(plan, host_receipt(programs, plan), programs)


def test_semantic_reference_identity_ignores_comments_docstrings_metadata_and_target_order():
    plan = typed()
    base = {**reference_generation_payload(plan, method_contract(), sources(), interfaces()),
        "reference_scripts": [{**helper_script(), "content": "def compute(values):\n    return sum(values)\n"}]}
    changed = copy.deepcopy(base)
    changed["reference_scripts"][0].update(content='"""New commentary."""\n# harmless\ndef compute(values):\n    """Explain arithmetic."""\n    return sum(values)\n', purpose="Rewritten prose")
    changed["blind_cases"][0]["cases"].reverse()
    for case in changed["blind_cases"][0]["cases"]:
        case["output_targets"].reverse()
    assert reference_review_identity(base) == reference_review_identity(changed)
    changed["reference_scripts"][0]["content"] = "def compute(values):\n    return len(values)\n"
    assert reference_review_identity(base) != reference_review_identity(changed)


@pytest.mark.parametrize("defect", ["missing_actual", "missing_check", "extra_check", "duplicate_check", "missing_case",
    "changed_input", "wrong_plan", "wrong_program", "runtime_error", "exit_mismatch", "truncated_string", "non_scalar"])
def test_incomplete_ambiguous_or_wrong_run_evidence_cannot_correct_or_approve_an_oracle(defect):
    plan, programs = typed(), {"scripts/group_summary.py": "reference-program"}
    receipt = host_receipt(programs, plan)
    case, check = receipt["cases"][0], receipt["cases"][0]["checks"][0]
    if defect == "missing_actual": check.pop("actual")
    elif defect == "missing_check": case["checks"].pop()
    elif defect == "extra_check": case["checks"].append({"id": "foreign", "actual": 0})
    elif defect == "duplicate_check": case["checks"].append(copy.deepcopy(check))
    elif defect == "missing_case": receipt["cases"].pop()
    elif defect == "changed_input": case["input_unchanged"] = False
    elif defect == "wrong_plan": receipt["plan_sha256"] = "0" * 64
    elif defect == "wrong_program": receipt["source_sha256"] = {"scripts/group_summary.py": "0" * 64}
    elif defect == "runtime_error": case["reason"] = "program_runtime_failure"
    elif defect == "exit_mismatch": case["actual_exit_code"] = 2
    elif defect == "truncated_string": case["checks"][-1]["actual"] = "x" * 160
    else: case["checks"][-1]["actual"] = "non_scalar_or_nonfinite"
    with pytest.raises(SkillCreationError) as failure:
        correct_reference_expectations(plan, receipt, programs)
    assert failure.value.code == "reference_calculation_unconfirmed"


def test_missing_or_wrong_typed_json_targets_are_not_replaced_with_guessed_scalars():
    plan, programs = typed(), {"scripts/group_summary.py": "reference-program"}
    broken = host_receipt(programs, plan, files=lambda case: {"summary.csv": output_files(case)["summary.csv"], "status.json": b'{"other":"ok"}'})
    with pytest.raises(SkillCreationError, match="独立参考计算"):
        correct_reference_expectations(plan, broken, programs)


def test_conflicting_csv_rows_cannot_be_collapsed_into_one_guessed_expected_value():
    plan, programs = typed(), {"scripts/group_summary.py": "reference-program"}
    def files(case):
        values = output_files(case)
        if case.kind == "normal":
            values["summary.csv"] += b"synthetic-a,7,2,0,0\n"
        return values
    with pytest.raises(SkillCreationError):
        correct_reference_expectations(plan, host_receipt(programs, plan, files=files), programs)


@pytest.mark.parametrize("field,value", [("mean", None), ("valid_count", "not-a-number"), ("valid_count", "True")])
def test_csv_semantic_type_or_missing_convention_is_not_guessed(field, value):
    plan, programs = typed(), {"scripts/group_summary.py": "reference-program"}
    receipt = host_receipt(programs, plan)
    target = next(check for check in receipt["cases"][0]["checks"] if check["id"].endswith("." + field))
    target["actual"] = "" if value is None else value
    with pytest.raises(SkillCreationError):
        correct_reference_expectations(plan, receipt, programs)


def test_csv_int_and_float_are_one_numeric_semantic_type_without_guessing_boolean_string_or_null():
    from app.domain.services.skills.skill_reference_verification import _scalar
    assert _scalar("2.5", 0, csv_value=True) == 2.5
    assert _scalar("2", 0.0, csv_value=True) == 2.0
    assert _scalar("0", "old", csv_value=True) == "0"
    assert _scalar("True", "old", csv_value=True) == "True"
    assert _scalar("False", True, csv_value=True) is False
    assert _scalar("", None, csv_value=True) is None
    for actual, expected in [("True", 0), ("0", True), ("0", None), ("", 0), (True, "old"),
                             ("NaN", 0), ("Infinity", 0), ("1e9999", 0.0), ("1e999999999", 0)]:
        with pytest.raises(SkillCreationError):
            _scalar(actual, expected, csv_value=True)
    plan, programs = typed(), {"scripts/group_summary.py": "independent-reference"}
    plan.scripts[0].cases[0].assertions[0].fields["mean"].value = 0
    def files(case):
        values = output_files(case)
        if case.kind == "normal":
            values["summary.csv"] = values["summary.csv"].replace(b",3,", b",2.5,")
        return values
    corrected, changes = correct_reference_expectations(plan, host_receipt(programs, plan, files=files), programs)
    assert corrected.scripts[0].cases[0].assertions[0].fields["mean"].value == 2.5
    assert changes[0]["before"] == 0 and changes[0]["after"] == 2.5


def test_exclusion_bookkeeping_does_not_leak_observed_result_explanations():
    from app.domain.services.skills.method_contract import SourceExclusion
    contract = method_contract()
    contract.excluded_sources = [SourceExclusion(source_id="S999", reason="observed_result",
        explanation="SECRET_OBSERVED_ANSWER_999 and session-specific calculation")]
    payload = reference_generation_payload(typed(), contract,
        [*sources(), {"id": "S999", "kind": "observed_summary", "text": "SECRET_OBSERVED_ANSWER_999"}], interfaces())
    assert "SECRET_OBSERVED_ANSWER_999" not in json.dumps(payload)
    assert payload["method_contract"]["excluded_sources"][0]["source_id"] == "S999"
    assert payload["method_contract"]["excluded_sources"][0]["reason"] == "observed_result"


def test_real_permutation_and_regression_arithmetic_corrects_wrong_expected_values_without_implementation_outputs():
    plan, programs = typed(), {"scripts/group_summary.py": "independent-reference"}
    # Hand-owned test calculations are independent of candidate expecteds.
    pool = [1, 2, 2, 3]
    observed = abs((1 + 2) / 2 - (2 + 3) / 2)
    differences = []
    for selected in itertools.combinations(range(4), 2):
        left = [pool[i] for i in selected]
        right = [pool[i] for i in range(4) if i not in selected]
        differences.append(abs(sum(left) / 2 - sum(right) / 2))
    permutation_p = sum(value >= observed for value in differences) / len(differences)
    x, y = [1, 2, 2, 3], [2, 4, 5, 7]
    mx, my = sum(x) / len(x), sum(y) / len(y)
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    covariance = sxy / (len(x) - 1)
    slope = sxy / sum((a - mx) ** 2 for a in x)
    expected = {"p": 1 / 3, "Sxy": 0.0, "cov": 0.0, "slope": 0.0}
    calculated = {"p": permutation_p, "Sxy": sxy, "cov": covariance, "slope": slope}
    assert calculated == {"p": 4 / 6, "Sxy": 5, "cov": 5 / 3, "slope": 2.5}
    plan.scripts[0].cases[0].assertions = [JSONValueAssertion(type="json_value", path="numeric.json", pointer="/" + key,
        expected=ExpectedValue(value=value, atol=1e-12)) for key, value in expected.items()]
    def files(case):
        return {**output_files(case), "numeric.json": json.dumps(calculated).encode()}
    corrected, changes = correct_reference_expectations(plan, host_receipt(programs, plan, files=files), programs)
    assert len(changes) == 4
    assert [item.expected.value for item in corrected.scripts[0].cases[0].assertions] == list(calculated.values())
    assert all(item.expected.atol == 1e-12 for item in corrected.scripts[0].cases[0].assertions)
    assert [item.expected.value for item in plan.scripts[0].cases[0].assertions] == list(expected.values())


def test_csv_count_column_and_row_count_have_distinct_correction_provenance():
    plan, programs = typed(), {"scripts/group_summary.py": "independent-reference"}
    normal = plan.scripts[0].cases[0]
    normal.assertions[0].fields = {"count": ExpectedValue(value=1)}
    def files(case):
        values = output_files(case)
        if case.kind == "normal":
            values["summary.csv"] = b"group,count\nsynthetic-a,8\nsynthetic-a,8\n"
        return values
    corrected, changes = correct_reference_expectations(plan, host_receipt(programs, plan, files=files), programs)
    assert [(change["target_type"], change["field"], change["after"]) for change in changes] == [
        ("csv_row_count", "count", 2), ("csv_field", "count", 8)]
    assert corrected.scripts[0].cases[0].assertions[0].count == 2
    assert corrected.scripts[0].cases[0].assertions[0].fields["count"].value == 8


def test_complete_json_lengths_and_scalars_are_corrected_without_changing_locations_or_tolerances():
    plan, programs = typed(), {"scripts/group_summary.py": "independent-reference"}
    assertions = [
        JSONArrayLengthAssertion(type="json_array_length", path="status.json", pointer="/rows", length=9),
        JSONValueAssertion(type="json_value", path="status.json", pointer="/status", expected=ExpectedValue(value="wrong")),
        JSONValueAssertion(type="json_value", path="status.json", pointer="/valid", expected=ExpectedValue(value=False)),
        JSONValueAssertion(type="json_value", path="status.json", pointer="/missing", expected=ExpectedValue(value=None)),
    ]
    plan.scripts[0].cases[0].assertions = assertions
    def files(case):
        values = output_files(case)
        if case.kind == "normal":
            values["status.json"] = b'{"rows":[1,2],"status":"ok","valid":true,"missing":null}'
        return values
    corrected, changes = correct_reference_expectations(plan, host_receipt(programs, plan, files=files), programs)
    assert [(change["target_type"], change["after"]) for change in changes] == [
        ("json_array_length", 2), ("json_value", "ok"), ("json_value", True)]
    for before, after in zip(assertions, corrected.scripts[0].cases[0].assertions):
        assert (before.type, before.path, before.pointer) == (after.type, after.path, after.pointer)
        if before.type == "json_value":
            assert before.expected.atol == after.expected.atol and before.expected.rtol == after.expected.rtol


@pytest.mark.parametrize("expected,actual", [(0, None), (None, 0), (2.5, None), (None, 2.5),
    (0, False), (True, 1), (0, "0"), ("0", 0), (None, ""), ("", None), (False, "False"), ("true", True)])
def test_json_semantic_type_changes_cannot_be_promoted_to_verified_expectations(expected, actual):
    plan, programs = typed(), {"scripts/group_summary.py": "independent-reference"}
    plan.scripts[0].cases[0].assertions = [JSONValueAssertion(type="json_value", path="status.json", pointer="/value",
        expected=ExpectedValue(value=expected))]
    def files(case):
        values = output_files(case)
        if case.kind == "normal":
            values["status.json"] = json.dumps({"value": actual}).encode()
        return values
    original = plan.model_dump(mode="json")
    with pytest.raises(SkillCreationError) as failure:
        correct_reference_expectations(plan, host_receipt(programs, plan, files=files), programs)
    assert failure.value.code == "reference_calculation_unconfirmed"
    assert plan.model_dump(mode="json") == original


@pytest.mark.parametrize("expected,actual", [(0, 2.5), (2.5, 0), (False, True), ("wrong", "right"), (None, None)])
def test_json_correction_preserves_semantic_family(expected, actual):
    from app.domain.services.skills.skill_reference_verification import _scalar
    assert _scalar(actual, expected) == actual


@pytest.mark.asyncio
async def test_unavailable_reference_environment_cannot_produce_a_verified_plan():
    generate, _ = generation()
    execute = AsyncMock(return_value={"status": "unavailable", "reason": "sandbox_image_not_configured"})
    with pytest.raises(SkillCreationError) as failure:
        await verify_reference_plan(typed(), method_contract(), sources(), interfaces(), generate=generate, execute=execute)
    assert (failure.value.stage, failure.value.code) == ("synthetic_reference_execution", "reference_execution_unavailable")


@pytest.mark.asyncio
async def test_reference_is_frozen_when_repeat_execution_disagrees_again():
    generate, calls = generation()
    count = 0
    async def execute(programs, plan):
        nonlocal count
        count += 1
        def files(case):
            value = output_files(case)
            if case.kind == "normal":
                value["summary.csv"] = value["summary.csv"].replace(b",3,", b",4," if count == 1 else b",5,")
            return value
        return host_receipt(programs, plan, files=files)
    with pytest.raises(SkillCreationError) as failure:
        await verify_reference_plan(typed(), method_contract(), sources(), interfaces(), generate=generate, execute=execute)
    assert failure.value.code == "reference_calculation_unconfirmed"
    assert [stage for stage, _ in calls].count("synthetic_reference_implementation") == 1 and count == 2


@pytest.mark.asyncio
async def test_full_verifier_computes_and_confirms_reference_before_review_or_any_tested_program_execution(monkeypatch):
    from app.domain.services.skills.skill_script_verification import verify_skill_script_cases
    from test_skill_script_verification import recipe, revalidate
    from test_skill_validation_review import accepted_fixture_review
    original = recipe()
    plan = typed()
    plan.scripts[0].cases[0].assertions[0].fields["mean"].value = 999
    plan.scripts[0].cases[0].oracle_explanation = "OLD_UNEXECUTED_DERIVATION_999"
    generated, executions, model_calls = generation()[0], [], []
    async def generate(schema, stage, instruction, payload, check):
        model_calls.append(stage)
        if stage == "synthetic_validation_plan":
            value = plan.model_copy(deep=True)
        elif stage == "synthetic_plan_review":
            assert len(executions) == 2
            assert payload["reference_evidence"]["corrections"][0]["after"] == 3
            assert "OLD_UNEXECUTED_DERIVATION_999" not in json.dumps(payload)
            assert "IMPLEMENTATION_MARKER_NOT_FOR_ORACLE" not in json.dumps(payload)
            assert payload["validation_plan"]["scripts"][0]["cases"][0]["oracle_explanation"] == REFERENCE_EXPLANATION
            value = schema.model_validate(review_for_payload(accepted_fixture_review(payload["validation_plan"]), payload))
        else:
            return await generated(schema, stage, instruction, payload, check)
        check(value)
        return value
    async def execute(programs, candidate):
        executions.append((dict(programs), candidate.model_copy(deep=True)))
        if len(executions) < 3:
            assert "IMPLEMENTATION_MARKER_NOT_FOR_ORACLE" not in "\n".join(programs.values())
        else:
            assert programs == {script.path: script.content for script in original.scripts}
        return host_receipt(programs, candidate)
    monkeypatch.setattr(worker, "validate_skill_scripts", execute)
    final_review = AsyncMock()
    result, frozen, evidence = await verify_skill_script_cases(original, method_contract(), sources(),
        generate=generate, revalidate=revalidate, review=final_review)
    assert len(executions) == 3 and model_calls == ["synthetic_validation_plan", "synthetic_reference_implementation",
        "synthetic_reference_review", "synthetic_plan_review"]
    assert result == original and frozen == executions[-1][1].model_dump(mode="json")
    assert evidence["reference_verification"]["corrected_plan_sha256"] == evidence["plan_sha256"]
    assert evidence["reference_verification"]["original_explanations"][0]["oracle_explanation"] == "OLD_UNEXECUTED_DERIVATION_999"
    final_review.assert_not_awaited()


@pytest.mark.asyncio
async def test_text_reviewer_cannot_override_executed_reference_or_start_script_repair(monkeypatch):
    from app.domain.services.skills.skill_script_verification import verify_skill_script_cases
    from test_skill_script_verification import recipe, revalidate
    from test_skill_validation_review import review_for
    generated, executions, stages = generation()[0], [], []
    async def generate(schema, stage, instruction, payload, check):
        stages.append(stage)
        if stage == "synthetic_validation_plan":
            value = typed()
        elif stage == "synthetic_plan_review":
            value = schema.model_validate(review_for_payload(review_for(payload["validation_plan"], "rejected"), payload))
        else:
            return await generated(schema, stage, instruction, payload, check)
        check(value)
        return value
    async def execute(programs, plan):
        executions.append(dict(programs))
        assert "IMPLEMENTATION_MARKER_NOT_FOR_ORACLE" not in "\n".join(programs.values())
        return host_receipt(programs, plan)
    monkeypatch.setattr(worker, "validate_skill_scripts", execute)
    with pytest.raises(SkillCreationError) as failure:
        await verify_skill_script_cases(recipe(), method_contract(), sources(),
            generate=generate, revalidate=revalidate, review=AsyncMock())
    assert failure.value.code == "validation_oracle_rejected"
    assert len(executions) == 1 and stages.count("synthetic_plan_review") == 1
    assert "synthetic_script_repair" not in stages and stages.count("synthetic_validation_plan") == 1
