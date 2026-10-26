"""An exact CSV cardinality is observable without fabricated column values."""
import copy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.domain.services.skills.skill_validation import (
    CSVRowsAssertion, ScriptValidationInterface, SyntheticValidationCase, SyntheticValidationPlan,
    VALIDATION_PLAN_PROMPT, validation_generation_schema,
)
from app.domain.services.skills.skill_validation_review import review_targets, validate_oracle_review, ValidationPlanReview
from app.domain.services.skills.skill_creation_error import SkillPackageValidationError
from app.infrastructure.external.sandbox import skill_validation_worker as worker
from test_skill_validation_review import accepted_fixture_review
from test_skill_validation_worker import case, plan, runtime


def count_assertion(count=2, **changes):
    return {"type": "csv_rows", "path": "wide.csv", "match": {}, "count": count, "fields": {}, **changes}


@pytest.mark.parametrize("count", [0, 1, 2, 50])
def test_explicit_cardinality_without_fields_is_preserved_exactly(count):
    original = count_assertion(count)
    typed = CSVRowsAssertion.model_validate(original)
    assert typed.model_dump(mode="json") == original


def test_model_facing_schema_and_prompt_explain_csv_header_and_data_row_boundary():
    schema = validation_generation_schema()["$defs"]["CSVRowsAssertion"]
    assert "csv.DictReader" in schema["description"]
    properties = schema["properties"]
    assert "never on the header" in properties["match"]["description"]
    assert "excluding the CSV header" in properties["count"]["description"]
    assert "count=0 and fields={}" in properties["count"]["description"]
    assert "not a valid empty table" in properties["count"]["description"]
    assert "never header names or column structure" in properties["fields"]["description"]
    assert "script_interfaces.output_contract" in properties["fields"]["description"]
    output_contract = ScriptValidationInterface.model_json_schema()["properties"]["output_contract"]
    assert "not data rows" in output_contract["description"]
    assert "csv.DictReader" in VALIDATION_PLAN_PROMPT and "不包含表头" in VALIDATION_PLAN_PROMPT
    assert "match={}、count=0、fields={}" in VALIDATION_PLAN_PROMPT
    assert "把表头伪装成一行数据" in VALIDATION_PLAN_PROMPT


@pytest.mark.parametrize("output", [b"group,value\n", b"group,value\r\n", b"group,value\n\n"])
def test_header_only_csv_has_zero_real_data_rows(output):
    item = SyntheticValidationCase.model_validate({**case(), "assertions": [count_assertion(0)]})
    assert worker._assertions(item, {"wide.csv": output}) == [
        {"id": "assertion_1.count", "passed": True, "expected": 0, "actual": 0}]


@pytest.mark.parametrize("output", [b"", b"\n"])
def test_file_without_a_header_cannot_pass_as_an_empty_csv_table(output):
    item = SyntheticValidationCase.model_validate({**case(), "assertions": [count_assertion(0)]})
    assert worker._assertions(item, {"wide.csv": output}) == [
        {"id": "assertion_1", "passed": False, "reason": "missing_or_duplicate_column"}]


@pytest.mark.parametrize("output,passed,actual", [
    (b"group,value\n", False, 0),
    # A genuine data record may happen to equal its header. Do not reject it
    # with a column-name heuristic; only csv.DictReader's records are counted.
    (b"group,value\ngroup,value\n", True, 1),
])
def test_header_name_filters_only_match_genuine_data_records(output, passed, actual):
    item = SyntheticValidationCase.model_validate({**case(), "assertions": [
        count_assertion(1, match={"group": "group"}, fields={"value": {"value": "value"}})]})
    checks = worker._assertions(item, {"wide.csv": output})
    assert checks[0] == {"id": "assertion_1.count", "passed": passed, "expected": 1, "actual": actual}
    assert len(checks) == 1 + actual
    assert all(check["passed"] for check in checks) is passed


@pytest.mark.parametrize("changes", [
    {"count": -1}, {"count": True}, {"count": "2"}, {"count": 2.0},
    {"path": "/private/wide.csv"}, {"path": "../wide.csv"},
    {"count": 0, "fields": {"value": {"value": 0}}},
])
def test_count_only_does_not_weaken_types_paths_or_zero_row_semantics(changes):
    with pytest.raises(ValidationError):
        CSVRowsAssertion.model_validate(count_assertion(**changes))


@pytest.mark.parametrize("match,count,passed,actual", [
    ({}, 2, True, 2), ({}, 1, False, 2), ({}, 3, False, 2),
    ({"group": "A"}, 1, True, 1), ({"group": "A"}, 2, False, 1),
    ({"group": "missing"}, 0, True, 0),
])
def test_worker_compares_exact_selected_count_even_with_no_fields(match, count, passed, actual):
    item = SyntheticValidationCase.model_validate({**case(),
        "assertions": [count_assertion(count, match=match)]})
    checks = worker._assertions(item, {"wide.csv": b"group,value\nA,2\nB,4\n"})
    assert checks == [{"id": "assertion_1.count", "passed": passed, "expected": count, "actual": actual}]


def test_worker_still_checks_values_when_count_and_fields_are_declared():
    item = SyntheticValidationCase.model_validate({**case(),
        "assertions": [count_assertion(2, fields={"value": {"value": 2}})]})
    checks = worker._assertions(item, {"wide.csv": b"group,value\nA,2\nB,4\n"})
    assert [check["passed"] for check in checks] == [True, True, False]
    assert checks[-1]["expected"] == 2 and checks[-1]["actual"] == "4"


@pytest.mark.parametrize("output", [b"group,group\nA,A\nB,B\n", b"group,value\nA\nB,4\n"])
def test_count_only_still_rejects_duplicate_columns_and_irregular_csv(output):
    item = SyntheticValidationCase.model_validate({**case(), "assertions": [count_assertion()]})
    assert any(not check["passed"] for check in worker._assertions(item, {"wide.csv": output}))


def count_plan(count=2):
    value = plan().model_dump(mode="json")
    for item in value["scripts"][0]["cases"]:
        item["assertions"] = [count_assertion(count)]
    return SyntheticValidationPlan.model_validate(value)


def test_independent_review_must_cover_count_target_without_column_targets():
    value = count_plan()
    targets = review_targets(value)[0]["cases"]
    assert all(item["targets"] == [{"target_id": "T001", "kind": "csv_row_count",
                "assertion_index": 0, "assertion_type": "csv_rows", "path": "wide.csv",
                "match": {}, "row_count": 2, "expected": 2}] for item in targets)
    review = accepted_fixture_review(value)
    validate_oracle_review(ValidationPlanReview.model_validate(review), value)
    omitted = copy.deepcopy(review)
    omitted["scripts"][0]["cases"][0]["assertions"] = []
    with pytest.raises(SkillPackageValidationError):
        validate_oracle_review(ValidationPlanReview.model_validate(omitted), value)


@pytest.mark.asyncio
@pytest.mark.parametrize("expected,status", [(2, "passed"), (1, "failed"), (3, "failed")])
async def test_full_host_worker_never_passes_incorrect_count(runtime, expected, status):
    runtime.outputs[worker.OUTPUT_ROOT + "/wide.csv"] = b"group,value\nA,2\nB,4\n"
    result = await worker.validate_skill_scripts({"scripts/summary.py": "print('count checked by host')"},
        count_plan(expected), SimpleNamespace(sandbox_image="existing:sandbox"))
    assert result["status"] == status
    assert all(item["checks"] == [{"id": "assertion_1.count", "passed": expected == 2,
                "expected": expected, "actual": 2}] for item in result["cases"])


@pytest.mark.asyncio
@pytest.mark.parametrize("expected,status", [(0, "passed"), (1, "failed")])
async def test_full_host_worker_excludes_header_from_empty_table_count(runtime, expected, status):
    runtime.outputs[worker.OUTPUT_ROOT + "/wide.csv"] = b"group,value\n"
    result = await worker.validate_skill_scripts({"scripts/summary.py": "print('header is not data')"},
        count_plan(expected), SimpleNamespace(sandbox_image="existing:sandbox"))
    assert result["status"] == status
    assert all(item["checks"] == [{"id": "assertion_1.count", "passed": expected == 0,
                "expected": expected, "actual": 0}] for item in result["cases"])
