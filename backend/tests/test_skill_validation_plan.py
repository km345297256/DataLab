import copy
import json

import pytest
from pydantic import ValidationError

from app.domain.services.skills.method_contract import MethodContract
from app.domain.services.skills.skill_validation import (
    CSVRowsAssertion, ExpectedValue, ScriptValidationInterface, SyntheticValidationPlan,
    VALIDATION_PLAN_PROMPT, build_validation_generation_payload, validate_validation_plan,
)


def method_contract():
    return MethodContract.model_validate({"requirements": [{"id": "R001", "kind": "method",
        "statement": "按组计算非缺失值的均值与缺失率；全缺失组均值保持缺失；无效数值必须拒绝。",
        "source_ids": ["S001"]}]})


def sources():
    return [{"id": "S001", "kind": "request", "text": method_contract().requirements[0].statement}]


def interfaces():
    return [{"script_path": "scripts/group_summary.py", "arguments": ["--input", "--output"],
             "input_contract": ["CSV columns group,value; null represents a missing measurement."],
             "output_contract": ["summary.csv: group,mean,valid_count,missing_count,missing_rate; status.json: status."],
             "validation_cases": [{"kind": "normal", "input": "Two finite values", "expected": "Arithmetic mean"}]}]


def case(kind):
    values = [2, 4] if kind == "normal" else [None, None] if kind == "boundary" else ["not-numeric"]
    total = len(values)
    return {"id": f"group-{kind}", "kind": kind,
        "description": "Use a newly synthesized tiny group to verify method semantics.",
        "requirement_ids": ["R001"], "source_ids": ["S001"],
        "oracle_explanation": "For two finite values 2 and 4, mean is (2+4)/2=3. With two missing values, mean is null and missing fraction is 2/2=1. Invalid numeric text must be rejected.",
        "argv": ["--input", "{input:table.csv}", "--output", "{output}"],
        "input_files": [{"path": "table.csv", "format": "csv", "columns": ["group", "value"],
                         "rows": [{"group": "synthetic-a", "value": value} for value in values]}],
        "expected_exit_code": 2 if kind == "invalid" else 0, "input_unchanged": True,
        "assertions": [] if kind == "invalid" else [
            {"type": "csv_rows", "path": "summary.csv", "match": {"group": "synthetic-a"}, "count": 1,
             "fields": {"mean": {"value": 3, "atol": 1e-12} if kind == "normal" else {"value": None},
                        "valid_count": {"value": total if kind == "normal" else 0},
                        "missing_count": {"value": 0 if kind == "normal" else total},
                        "missing_rate": {"value": 0 if kind == "normal" else 1, "atol": 1e-12}}},
            {"type": "json_value", "path": "status.json", "pointer": "/status", "expected": {"value": "ok"}},
        ]}


def plan_payload():
    return {"version": 1, "status": "not_run", "scripts": [{
        "script_path": "scripts/group_summary.py", "support": "supported",
        "reason": "Small tabular synthetic groups have independently calculable means and missing fractions.",
        "cases": [case(kind) for kind in ("normal", "boundary", "invalid")],
    }]}


def validate(payload):
    plan = SyntheticValidationPlan.model_validate(payload)
    validate_validation_plan(plan, method_contract(), sources(), interfaces())
    return plan


def test_declarative_oracle_preserves_zero_null_status_and_exit_expectations():
    plan = validate(plan_payload())
    normal, boundary, invalid = plan.scripts[0].cases
    assert normal.assertions[0].fields["mean"].value == 3
    assert boundary.assertions[0].fields["mean"].value is None
    assert boundary.assertions[0].fields["valid_count"].value == 0
    assert boundary.assertions[0].fields["missing_rate"].value == 1
    assert boundary.assertions[1].expected.value == "ok"
    assert invalid.expected_exit_code == 2 and invalid.assertions == []
    assert all(item.input_unchanged for item in plan.scripts[0].cases)
    assert SyntheticValidationPlan.model_validate_json(plan.model_dump_json()) == plan


def test_unspecified_rejection_code_is_a_semantic_contract_not_a_guessed_integer():
    payload = plan_payload()
    payload["scripts"][0]["cases"][2]["expected_exit_code"] = "nonzero"
    result = validate(payload)
    assert result.scripts[0].cases[2].expected_exit_code == "nonzero"
    assert SyntheticValidationPlan.model_validate_json(result.model_dump_json()) == result
    assert "不能猜测1或2" in VALIDATION_PLAN_PROMPT


@pytest.mark.parametrize("exit_expectation", [True, False, -1, 256, 1.0, "1", "any", "NONZERO"])
def test_exit_contract_is_strict(exit_expectation):
    payload = plan_payload()
    payload["scripts"][0]["cases"][2]["expected_exit_code"] = exit_expectation
    with pytest.raises(ValidationError):
        validate(payload)


def test_semantic_rejection_does_not_allow_successful_or_input_overwriting_cases():
    payload = plan_payload()
    payload["scripts"][0]["cases"][0]["expected_exit_code"] = "nonzero"
    with pytest.raises(ValidationError):
        validate(payload)
    payload = plan_payload()
    invalid = payload["scripts"][0]["cases"][2]
    invalid.update(expected_exit_code="nonzero")
    invalid["argv"][-1] = invalid["argv"][1]
    validate(payload)
    invalid["expected_exit_code"] = 0
    with pytest.raises(ValidationError):
        validate(payload)


def test_generation_payload_is_independent_of_tested_implementation_and_observations():
    independent_sources = [{**sources()[0], "source_snapshot": "private implementation", "actual": "wrong result"}]
    payload = build_validation_generation_payload(method_contract(), independent_sources, interfaces())
    assert payload["sources"] == sources()
    assert "source_snapshot" not in json.dumps(payload) and "wrong result" not in json.dumps(payload)
    assert payload["method_contract"] == method_contract().model_dump()
    with pytest.raises(ValidationError, match="extra_forbidden"):
        build_validation_generation_payload(method_contract(), sources(), [{**interfaces()[0], "content": "print('tested code')"}])
    assert "不得请求、读取或猜测被测实现" in VALIDATION_PLAN_PROMPT
    assert "不能将 0 当作缺失" in VALIDATION_PLAN_PROMPT
    assert "成功退出码不等于科学验证通过" in VALIDATION_PLAN_PROMPT


@pytest.mark.parametrize("mutation", ["missing_script", "duplicate_script", "unknown_script", "missing_kind", "duplicate_case", "no_observation", "normal_rejection", "changed_input", "already_passed"])
def test_incomplete_or_falsely_successful_plans_fail_closed(mutation):
    payload = plan_payload()
    item = payload["scripts"][0]
    if mutation == "missing_script":
        payload["scripts"] = []
    elif mutation == "duplicate_script":
        payload["scripts"].append(copy.deepcopy(item))
    elif mutation == "unknown_script":
        item["script_path"] = "scripts/unknown.py"
    elif mutation == "missing_kind":
        item["cases"].pop()
    elif mutation == "duplicate_case":
        item["cases"].append(copy.deepcopy(item["cases"][0]))
    elif mutation == "no_observation":
        item["cases"][0]["assertions"] = []
    elif mutation == "normal_rejection":
        item["cases"][0].update(expected_exit_code=2, assertions=[])
    elif mutation == "changed_input":
        item["cases"][0]["input_unchanged"] = False
    elif mutation == "already_passed":
        payload["status"] = "passed"
    with pytest.raises(ValueError):
        validate(payload)


@pytest.mark.parametrize("mutation", ["unknown_requirement", "duplicate_requirement", "unknown_source", "unlinked_source", "duplicate_source"])
def test_oracle_has_known_linked_method_and_source_mapping(mutation):
    payload = plan_payload()
    item = payload["scripts"][0]["cases"][0]
    if mutation == "unknown_requirement":
        item["requirement_ids"] = ["R099"]
    elif mutation == "duplicate_requirement":
        item["requirement_ids"] *= 2
    elif mutation in {"unknown_source", "unlinked_source"}:
        item["source_ids"] = ["S099"]
    elif mutation == "duplicate_source":
        item["source_ids"] *= 2
    with pytest.raises(ValueError):
        plan = SyntheticValidationPlan.model_validate(payload)
        source_data = sources() + ([{"id": "S099", "kind": "request", "text": "A different unrelated requirement."}] if mutation == "unlinked_source" else [])
        validate_validation_plan(plan, method_contract(), source_data, interfaces())


@pytest.mark.parametrize("mutation", ["python", "expression", "shell", "path_traversal", "absolute_output", "absolute_input", "unknown_input", "undeclared_flag", "short_interpreter_option", "missing_input_flag", "duplicate_input", "nonrectangular_csv", "nan_input", "nan_expectation", "infinite_tolerance", "bad_pointer"])
def test_model_tests_are_only_data_with_safe_paths_and_finite_expectations(mutation):
    payload = plan_payload()
    item = payload["scripts"][0]["cases"][0]
    if mutation == "python":
        item["test_code"] = "assert 1 == 1"
    elif mutation == "expression":
        item["assertions"] = [{"type": "expression", "expression": "eval(output)"}]
    elif mutation == "shell":
        item["command"] = "python script.py; echo success"
    elif mutation == "path_traversal":
        item["assertions"][0]["path"] = "../outside.csv"
    elif mutation == "absolute_output":
        item["assertions"][0]["path"] = "/etc/passwd"
    elif mutation == "absolute_input":
        item["argv"][1] = "/home/ubuntu/input.csv"
    elif mutation == "unknown_input":
        item["argv"][1] = "{input:unknown.csv}"
    elif mutation == "undeclared_flag":
        item["argv"] += ["--install", "package"]
    elif mutation == "short_interpreter_option":
        item["argv"] += ["-c", "print(123)"]
    elif mutation == "missing_input_flag":
        item["argv"][0] = "--different"
    elif mutation == "duplicate_input":
        item["argv"] += ["--input", "{input:table.csv}"]
    elif mutation == "nonrectangular_csv":
        del item["input_files"][0]["rows"][0]["value"]
    elif mutation == "nan_input":
        item["input_files"][0]["rows"][0]["value"] = float("nan")
    elif mutation == "nan_expectation":
        item["assertions"][0]["fields"]["mean"]["value"] = float("nan")
    elif mutation == "infinite_tolerance":
        item["assertions"][0]["fields"]["mean"]["atol"] = float("inf")
    elif mutation == "bad_pointer":
        item["assertions"][1]["pointer"] = "$.status"
    with pytest.raises(ValueError):
        validate(payload)


def test_multi_input_json_role_mapping_references_only_declared_fixtures():
    payload = plan_payload()
    item = payload["scripts"][0]["cases"][0]
    item["input_files"].append({"path": "roles.json", "format": "json", "data": {"measurements": "{input:table.csv}"}})
    item["argv"][1] = "{input:roles.json}"
    item["argv"][3] = "{output}/summary.csv"
    assert validate(payload).scripts[0].cases[0].input_files[1].data == {"measurements": "{input:table.csv}"}
    item["input_files"][1]["data"]["measurements"] = "{input:private.csv}"
    with pytest.raises(ValueError, match="invalid_validation_fixture_reference"):
        validate(payload)


def test_unsupported_non_tabular_script_has_no_fabricated_cases_or_pass_status():
    payload = plan_payload()
    payload["scripts"][0].update(support="unsupported", reason="This method requires an interactive visualization unavailable to CSV/JSON assertions.", cases=[])
    plan = validate(payload)
    assert plan.status == "not_run" and plan.scripts[0].support == "unsupported"
    payload["scripts"][0]["cases"] = [case("normal")]
    with pytest.raises(ValueError, match="must not invent"):
        validate(payload)


def test_zero_match_assertion_is_explicit_and_tolerance_is_numeric_only():
    assertion = CSVRowsAssertion.model_validate({"type": "csv_rows", "path": "out.csv", "match": {"status": "invalid"}, "count": 0, "fields": {}})
    assert assertion.count == 0
    with pytest.raises(ValueError, match="Only numeric"):
        ExpectedValue(value=None, atol=1)
    with pytest.raises(ValueError, match="Only numeric"):
        ExpectedValue(value="ok", rtol=0.1)
    assert type(ExpectedValue(value=True).value) is bool
    with pytest.raises(ValueError, match="long CLI flags"):
        ScriptValidationInterface.model_validate({**interfaces()[0], "arguments": ["python", "--input", "--output"]})


@pytest.mark.parametrize("existing_sources", [None, [], ["S001"], ["S999"]])
def test_host_derives_sources_from_frozen_requirements_without_changing_scientific_cases(existing_sources):
    from app.domain.services.skills.skill_validation import derive_validation_sources
    contract = method_contract()
    contract.requirements.append(contract.requirements[0].model_copy(update={"id": "R013", "source_ids": ["S004", "S005"]}))
    payload = plan_payload()
    invalid = payload["scripts"][0]["cases"][2]
    invalid["requirement_ids"] = ["R001", "R013"]
    if existing_sources is None:
        invalid.pop("source_ids")
    else:
        invalid["source_ids"] = existing_sources
    snapshot = copy.deepcopy(payload)
    normalized = derive_validation_sources(payload, contract)
    assert payload == snapshot
    expected = copy.deepcopy(snapshot)
    expected["scripts"][0]["cases"][2]["source_ids"] = ["S001", "S004", "S005"]
    assert normalized == expected
    parsed = SyntheticValidationPlan.model_validate(normalized)
    validate_validation_plan(parsed, contract, [*sources(), {"id": "S004"}, {"id": "S005"}], interfaces())


@pytest.mark.parametrize("requirement_ids", [["R999"], ["R001", "R001"], [], [None]])
def test_source_derivation_never_fabricates_missing_or_unknown_requirements(requirement_ids):
    from app.domain.services.skills.skill_validation import derive_validation_sources
    payload = plan_payload()
    payload["scripts"][0]["cases"][0]["requirement_ids"] = requirement_ids
    normalized = derive_validation_sources(payload, method_contract())
    assert normalized == payload
    with pytest.raises(ValueError):
        validate(normalized)


def test_generation_allows_omitted_derived_sources_but_durable_plan_requires_them():
    from app.domain.services.skills.skill_validation import validation_generation_schema
    schema = validation_generation_schema()
    assert "source_ids" not in schema["$defs"]["SyntheticValidationCase"]["required"]
    assert "source_ids" in SyntheticValidationPlan.model_json_schema()["$defs"]["SyntheticValidationCase"]["required"]
