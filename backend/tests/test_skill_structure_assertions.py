"""Exact output structure checks, separate from scientific output values."""
import copy
import json
import os
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.domain.services.skills.skill_creation_error import SkillPackageValidationError
from app.domain.services.skills.skill_validation import (
    CSVHeaderAssertion, JSONObjectKeysAssertion, SyntheticValidationCase, SyntheticValidationPlan,
    validate_validation_plan,
)
from app.infrastructure.external.sandbox import skill_validation_worker as worker
from test_skill_validation_worker import case, plan, runtime
from test_skill_validation_plan import interfaces, method_contract, plan_payload, sources

JSON_KEYS = {"type": "json_object_keys", "path": "report.json", "pointer": "", "keys": ["a", "b"]}
CSV_HEADER = {"type": "csv_header", "path": "report.csv", "columns": ["id", "value"]}


def compare(assertion, raw):
    document = case()
    document["assertions"] = [assertion]
    return worker._assertions(SyntheticValidationCase.model_validate(document),
                              {} if raw is None else {assertion["path"]: raw})


@pytest.mark.parametrize("model,document,field,value", [
    (JSONObjectKeysAssertion, JSON_KEYS, "keys", ["a", "a"]),
    (JSONObjectKeysAssertion, JSON_KEYS, "keys", [1]),
    (JSONObjectKeysAssertion, JSON_KEYS, "keys", [True]),
    (JSONObjectKeysAssertion, JSON_KEYS, "keys", [None]),
    (JSONObjectKeysAssertion, JSON_KEYS, "keys", "a"),
    (CSVHeaderAssertion, CSV_HEADER, "columns", []),
    (CSVHeaderAssertion, CSV_HEADER, "columns", ["id", "id"]),
    (CSVHeaderAssertion, CSV_HEADER, "columns", [""]),
    (CSVHeaderAssertion, CSV_HEADER, "columns", ["bad\x00name"]),
    (CSVHeaderAssertion, CSV_HEADER, "columns", [1]),
    (CSVHeaderAssertion, CSV_HEADER, "columns", [False]),
])
def test_expected_structure_is_strict_and_never_coerces_or_deduplicates(model, document, field, value):
    with pytest.raises(ValidationError):
        model.model_validate({**document, field: value})


@pytest.mark.parametrize("path", ["/report.json", "../report.json", "a/../report.json", "a//report.json", "{output}/report.json", "a\\report.json"])
@pytest.mark.parametrize("model,document", [(JSONObjectKeysAssertion, JSON_KEYS), (CSVHeaderAssertion, CSV_HEADER)])
def test_structure_paths_share_the_safe_relative_file_boundary(model, document, path):
    with pytest.raises(ValidationError):
        model.model_validate({**document, "path": path})


@pytest.mark.parametrize("pointer", ["report", "/a~", "/a~2"])
def test_object_key_pointer_is_rfc6901(pointer):
    with pytest.raises(ValidationError):
        JSONObjectKeysAssertion.model_validate({**JSON_KEYS, "pointer": pointer})


@pytest.mark.parametrize("document", [JSON_KEYS, CSV_HEADER])
def test_structural_checks_cannot_read_outputs_after_nonzero_rejection(document):
    raw = case("invalid", exit_code="nonzero")
    raw["assertions"] = [document]
    with pytest.raises(ValidationError, match="never scientific output"):
        SyntheticValidationCase.model_validate(raw)


@pytest.mark.parametrize("raw,passed,reason", [
    (b'{"b":{"private_value":123},"a":456}', True, None),
    (b'{"a":1}', False, None), (b'{"a":1,"b":2,"extra":3}', False, None),
    (b'{"a":1,"b":2,"a":3}', False, "duplicate_json_key"),
    (b'{"a":{"x":1,"x":2},"b":0}', False, "duplicate_json_key"),
    (b'["a","b"]', False, "json_object_type_mismatch"),
    (b'null', False, "json_object_type_mismatch"), (b'true', False, "json_object_type_mismatch"),
    (b'12', False, "json_object_type_mismatch"), (b'"object"', False, "json_object_type_mismatch"),
    (b'{"a":NaN,"b":0}', False, "nonfinite_json"),
    (b'{"a":1e999,"b":0}', False, "nonfinite_json"),
    (b'', False, "missing_or_invalid_output"), (None, False, "missing_or_invalid_output"),
    (b'\xff', False, "missing_or_invalid_output"),
])
def test_json_object_keys_exactness_and_host_parsing(raw, passed, reason):
    result, = compare(JSON_KEYS, raw)
    assert result["id"] == "assertion_1" and result["passed"] is passed
    if reason: assert result["reason"] == reason
    if passed:
        assert result == {"id": "assertion_1", "passed": True, "expected": ["a", "b"], "actual": ["a", "b"]}
        assert "private_value" not in json.dumps(result) and "123" not in json.dumps(result)


@pytest.mark.parametrize("pointer,raw,keys", [
    ("", b'{}', []), ("/rows/0/a~1b/~0", b'{"rows":[{"a/b":{"~":{"":1,"id":2}}}]}', ["id", ""]),
])
def test_object_keys_allow_empty_objects_and_exact_escaped_nested_pointers(pointer, raw, keys):
    assert compare({**JSON_KEYS, "pointer": pointer, "keys": keys}, raw)[0]["passed"]


@pytest.mark.parametrize("pointer", ["/missing", "/rows/2", "/rows/00", "/rows/-", "/rows/0/missing"])
def test_missing_objects_and_invalid_array_indices_fail(pointer):
    assert compare({**JSON_KEYS, "pointer": pointer, "keys": []}, b'{"rows":[{}]}')[0]["passed"] is False


@pytest.mark.parametrize("raw,passed,reason", [
    (b'id,value\n', True, None), (b'\xef\xbb\xbfid,value\r\n1,private_value\r\n', True, None),
    (b'value,id\n', False, None), (b'id\n', False, None), (b'id,value,extra\n', False, None),
    (b'id,value,value\n', False, "missing_or_duplicate_column"),
    (b'id,\n', False, "missing_or_duplicate_column"), (b'id,va\x00lue\n', False, "missing_or_duplicate_column"),
    (b'', False, "missing_or_duplicate_column"), (b'\n', False, "missing_or_duplicate_column"),
    (None, False, "missing_or_invalid_output"), (b'"id,value\n', False, "missing_or_invalid_output"),
])
def test_csv_header_is_complete_ordered_nonempty_and_never_includes_records(raw, passed, reason):
    result, = compare(CSV_HEADER, raw)
    assert result["passed"] is passed
    if reason: assert result["reason"] == reason
    assert "private_value" not in json.dumps(result)
    if passed: assert result["expected"] == result["actual"] == ["id", "value"]


def test_csv_header_supports_csv_quoting_and_unicode():
    assert compare({**CSV_HEADER, "columns": ["标识", "value,unit"]}, '标识,"value,unit"\r\n'.encode())[0]["passed"]


def test_structures_and_existing_scientific_assertions_are_checked_independently():
    raw = case()
    raw["assertions"] = [CSV_HEADER,
        {"type": "csv_rows", "path": "report.csv", "match": {"id": "A"}, "count": 1, "fields": {"value": {"value": 4}}},
        {**JSON_KEYS, "keys": ["value"]},
        {"type": "json_value", "path": "report.json", "pointer": "/value", "expected": {"value": 4}}]
    receipt = worker._assertions(SyntheticValidationCase.model_validate(raw),
                                {"report.csv": b'id,value,extra\nA,4,anything\n', "report.json": b'{"value":4}'})
    assert receipt[0]["passed"] is False and all(item["passed"] for item in receipt[1:])
    assert [item["id"] for item in receipt] == ["assertion_1", "assertion_2.count", "assertion_2.row_1.value", "assertion_3", "assertion_4"]


@pytest.mark.parametrize("keys,additional,accepted", [(["a", "b"], False, True), (["b", "a"], False, True),
    (["a"], False, False), (["a", "b", "extra"], False, False), (["a", "b", "extra"], True, True), (["a"], True, False)])
def test_known_public_json_shape_cannot_be_weakened_by_a_structural_oracle(keys, additional, accepted):
    raw = plan_payload()
    raw["scripts"][0]["cases"][0]["assertions"] = [{**JSON_KEYS, "keys": keys}]
    interface = interfaces()[0]
    interface["json_outputs"] = [{"path": "@output/report.json", "shape": {"type": "object",
        "properties": {"a": {"type": "unknown"}, "b": {"type": "unknown"}}, "additional_keys": additional}}]
    args = (SyntheticValidationPlan.model_validate(raw), method_contract(), sources(), [interface])
    if accepted: validate_validation_plan(*args)
    else:
        with pytest.raises(SkillPackageValidationError, match="validation_output_field_unknown"):
            validate_validation_plan(*args)


def test_open_public_object_does_not_guess_nested_dynamic_property_shape():
    raw = plan_payload()
    raw["scripts"][0]["cases"][0]["assertions"] = [{**JSON_KEYS, "pointer": "/dynamic", "keys": ["child"]}]
    interface = interfaces()[0]
    interface["json_outputs"] = [{"path": "@output/report.json", "shape": {"type": "object",
        "properties": {"fixed": {"type": "unknown"}}, "additional_keys": True}}]
    validate_validation_plan(SyntheticValidationPlan.model_validate(raw), method_contract(), sources(), [interface])


@pytest.mark.asyncio
async def test_structural_assertions_use_existing_readonly_worker_and_file_hashes(runtime):
    candidate = plan()
    for item in candidate.scripts[0].cases:
        item.assertions = [CSVHeaderAssertion(type="csv_header", path="summary.csv", columns=["group", "value"]),
                           JSONObjectKeysAssertion(type="json_object_keys", path="summary.json", pointer="", keys=["value"])]
    before = candidate.model_dump(mode="json")
    receipt = await worker.validate_skill_scripts({"scripts/summary.py": "# artificial fixture"}, candidate,
                                                 SimpleNamespace(sandbox_image="existing:sandbox"))
    assert receipt["status"] == "passed" and candidate.model_dump(mode="json") == before
    assert all(item["input_unchanged"] and set(item["output_sha256"]) == {"summary.csv", "summary.json"} for item in receipt["cases"])
    assert all(container.removed for container in runtime.created)


@pytest.mark.skipif(os.environ.get("DATALAB_RUN_SYNTHETIC_DOCKER_TEST") != "1", reason="Opt-in disposable sandbox integration")
@pytest.mark.asyncio
async def test_real_worker_checks_fixed_json_keys_and_empty_table_header_without_changing_science(monkeypatch):
    monkeypatch.setattr(worker, "helper_security_options", lambda: ["no-new-privileges:true"])
    document = plan().model_dump(mode="json")
    normal, boundary, invalid = document["scripts"][0]["cases"]
    normal["assertions"] = [{**JSON_KEYS, "keys": ["value", "metadata"]}, CSV_HEADER,
        {"type": "json_value", "path": "report.json", "pointer": "/value", "expected": {"value": 4}},
        {"type": "csv_rows", "path": "report.csv", "match": {"id": "A"}, "count": 1, "fields": {"value": {"value": 4}}}]
    boundary["input_files"][0]["rows"] = []
    boundary["assertions"] = [{**JSON_KEYS, "keys": []}, {**CSV_HEADER, "columns": ["id"]},
        {"type": "csv_rows", "path": "report.csv", "match": {}, "count": 0, "fields": {}}]
    invalid["input_files"][0]["rows"][0]["value"] = "bad"
    invalid.update(expected_exit_code="nonzero", assertions=[{"type": "output_empty"}])
    candidate = SyntheticValidationPlan.model_validate(document)
    original = copy.deepcopy(candidate.model_dump())
    source = '''import argparse,csv,json
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--input');p.add_argument('--output');a=p.parse_args()
rows=list(csv.DictReader(open(a.input)))
try:
    total=sum(float(row['value']) for row in rows)
except ValueError:
    raise SystemExit(2)
report={'value':total,'metadata':{}} if rows else {}
columns=['id','value'] if rows else ['id']
# CHANGE_STRUCTURE
Path(a.output,'report.json').write_text(json.dumps(report))
with Path(a.output,'report.csv').open('w',newline='') as handle:
    writer=csv.writer(handle);writer.writerow(columns)
    if rows: writer.writerow(['A',total])
'''
    settings = SimpleNamespace(sandbox_image=os.environ.get("DATALAB_SYNTHETIC_TEST_IMAGE", "datalab-sandbox:analysis-repair-20261023"))
    for mutation, expected in [("pass", True), ("report['extra']=1", False),
                               ("columns.append('extra') if not rows else None", False)]:
        receipt = await worker.validate_skill_scripts({"scripts/summary.py": source.replace("# CHANGE_STRUCTURE", mutation)}, candidate, settings)
        assert receipt["status"] == ("passed" if expected else "failed"), receipt
        assert candidate.model_dump() == original and all(item["input_unchanged"] for item in receipt["cases"])
        assert receipt["cases"][-1]["status"] == "passed"
        numerical = [item for item in receipt["cases"][0]["checks"] if item["id"].startswith(('assertion_3', 'assertion_4'))]
        assert numerical and all(item["passed"] for item in numerical)


def test_rfc6901_empty_property_token_is_preserved_by_public_shape_and_actual_reader():
    assertion = {**JSON_KEYS, "pointer": "//id", "keys": ["nested"]}
    raw = plan_payload()
    raw["scripts"][0]["cases"][0]["assertions"] = [assertion]
    interface = interfaces()[0]
    interface["json_outputs"] = [{"path": "@output/report.json", "shape": {"type": "object", "properties": {
        "": {"type": "object", "properties": {"id": {"type": "object", "properties": {"nested": {"type": "unknown"}}}}}}}}]
    validate_validation_plan(SyntheticValidationPlan.model_validate(raw), method_contract(), sources(), [interface])
    assert compare(assertion, b'{"":{"id":{"nested":0}}}')[0]["passed"]


@pytest.mark.parametrize("document", [JSON_KEYS, CSV_HEADER])
@pytest.mark.parametrize("extra,value", [("expected", []), ("atol", 1), ("rtol", 1)])
def test_exact_structure_does_not_accept_alternative_answers_or_tolerance(document, extra, value):
    model = JSONObjectKeysAssertion if document["type"] == "json_object_keys" else CSVHeaderAssertion
    with pytest.raises(ValidationError):
        model.model_validate({**document, extra: value})
