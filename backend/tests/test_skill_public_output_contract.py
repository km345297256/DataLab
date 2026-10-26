"""Public JSON schema is observed separately from scientific test answers."""
import copy
import hashlib
import json
import os
from types import SimpleNamespace

import pytest

from app.domain.services.skills.skill_public_output_contract import (
    PublicOutputContractError, check_public_output, compile_public_output_contract,
    public_output_path, validate_public_contract_receipt,
)
from app.infrastructure.external.sandbox import skill_validation_worker as worker
from app.domain.services.skills.skill_validation import SyntheticValidationPlan
from test_skill_validation_worker import plan, runtime


PROGRAMS = {"scripts/summary.py": "# Exact synthetic source bytes for the isolated worker fixture.\n"}


def interfaces(shape=None, **output):
    return [{"script_path": "scripts/summary.py", "arguments": ["--input", "--output", "--report-name"],
        "input_contract": ["Synthetic CSV input."], "output_contract": ["summary.csv and public.json"],
        "json_outputs": [{"path": "@output/public.json", "shape": shape or {"type": "object", "properties": {
            "columns": {"type": "array", "items": {"type": "unknown"}, "column_name_prefix": ["id", "mean"]},
            "arbitrary": {"type": "array", "items": {"type": "unknown"}}}}, **output}]}]


def public_json(columns=None):
    return json.dumps({"columns": ["id", "mean", "dynamic_value"] if columns is None else columns,
                       "arbitrary": ["SECRET_SCIENTIFIC_VALUE", 314159.265]}).encode()


async def execute(runtime, *, candidate=None, declared=None):
    candidate = candidate or plan()
    contract = compile_public_output_contract(declared or interfaces())
    receipt = await worker.validate_skill_scripts(PROGRAMS, candidate, SimpleNamespace(sandbox_image="already-existing-image"),
        public_output_contract=contract)
    return candidate, contract, receipt


@pytest.mark.asyncio
async def test_contract_reads_unasserted_json_in_same_execution_without_changing_scientific_plan(runtime):
    candidate = plan()
    before = candidate.model_dump()
    runtime.outputs[worker.OUTPUT_ROOT + "/public.json"] = public_json()
    _, contract, receipt = await execute(runtime, candidate=candidate)
    assert receipt["status"] == "passed"
    assert candidate.model_dump() == before
    canonical = SyntheticValidationPlan.model_validate(before).model_dump()
    assert receipt["plan_sha256"] == hashlib.sha256(worker._json(canonical)).hexdigest()
    assert len(runtime.requests) == len(candidate.scripts[0].cases)
    assert len(runtime.created) == len(candidate.scripts[0].cases) + 1
    assert all(len(case["checks"]) == 2 for case in receipt["cases"])
    public = validate_public_contract_receipt(receipt, contract, PROGRAMS, candidate)
    assert public["status"] == "passed" and len(public["cases"]) == 3
    assert all(case["checks"] == [{"rule_id": "C001", "status": "passed", "observations": 1}]
               for case in public["cases"])
    assert "SECRET_SCIENTIFIC_VALUE" not in json.dumps(public) and "314159.265" not in json.dumps(public)
    assert "arbitrary" not in contract.model_dump_json()
    assert all(worker.OUTPUT_ROOT + "/public.json" not in json.dumps(request) for request in runtime.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("data,reason", [(b'{"other":[]}', "public_property_missing"),
    (b'{"columns":"id"}', "public_prefix_not_array"), (b'{"columns":[]}', "public_prefix_mismatch"),
    (b'{"columns":["mean","id"]}', "public_prefix_mismatch"),
    (b'{"columns":["id",null]}', "public_prefix_mismatch"),
    (b'{"columns":["id","mean"],"columns":[]}', "public_json_invalid"),
    (b'{"columns":["id","mean"],"number":NaN}', "public_json_invalid"),
    (b'not-json', "public_json_invalid"), (None, "public_output_unavailable")])
async def test_public_contract_failure_is_not_a_scientific_expectation_correction(runtime, data, reason):
    if data is not None:
        runtime.outputs[worker.OUTPUT_ROOT + "/public.json"] = data
    candidate, contract, receipt = await execute(runtime)
    assert receipt["status"] == "failed"
    assert all(all(check["passed"] for check in case["checks"]) for case in receipt["cases"])
    public = validate_public_contract_receipt(receipt, contract, PROGRAMS, candidate, require_passed=False)
    assert public["status"] == "failed"
    assert {check["reason"] for case in public["cases"] for check in case["checks"]} == {reason}
    with pytest.raises(PublicOutputContractError):
        validate_public_contract_receipt(receipt, contract, PROGRAMS, candidate)


@pytest.mark.asyncio
async def test_filename_override_and_file_output_binding_are_host_resolved(runtime):
    candidate = plan()
    for case in candidate.scripts[0].cases:
        case.argv.extend(["--report-name", "renamed.json"])
    runtime.outputs[worker.OUTPUT_ROOT + "/renamed.json"] = public_json()
    candidate, contract, receipt = await execute(runtime, candidate=candidate,
        declared=interfaces(filename_argument="--report-name"))
    assert validate_public_contract_receipt(receipt, contract, PROGRAMS, candidate)["status"] == "passed"
    file_contract = compile_public_output_contract(interfaces(path="@output"))
    case = candidate.scripts[0].cases[0].model_copy(deep=True)
    case.argv[case.argv.index("--output") + 1] = "{output}/report.json"
    assert public_output_path(file_contract.rules[0], case) == "report.json"


@pytest.mark.parametrize("binding", ["{input:table.csv}", "/tmp/private.json", "{output}/../outside.json", "{output}"])
def test_unknown_or_unsafe_file_binding_never_grants_access(binding):
    contract = compile_public_output_contract(interfaces(path="@output"))
    case = plan().scripts[0].cases[0]
    case.argv[case.argv.index("--output") + 1] = binding
    with pytest.raises(PublicOutputContractError):
        public_output_path(contract.rules[0], case)


def nested_interfaces():
    return interfaces({"type": "object", "properties": {"*": {"type": "array", "items": {
        "type": "object", "properties": {"columns": {"type": "array", "items": {"type": "unknown"},
                                                              "column_name_prefix": ["id"]}}}}}})


def test_nested_items_are_explicit_and_literal_star_is_an_ordinary_property():
    contract = compile_public_output_contract(nested_interfaces())
    rule = contract.rules[0]
    assert [step.model_dump(exclude_none=True) for step in rule.steps] == [
        {"kind": "property", "name": "*"}, {"kind": "items"}, {"kind": "property", "name": "columns"}]
    assert check_public_output(rule, b'{"*":[{"columns":["id"]},{"columns":["id","value"]}]}')["observations"] == 2
    assert check_public_output(rule, b'{"wrong":[{"columns":["id"]}]}')["status"] == "failed"
    assert check_public_output(rule, b'{"*":[{"columns":["id"]},{"wrong":[]}]}')["status"] == "failed"
    assert check_public_output(rule, b'{"*":[]}') == {"rule_id": "C001", "status": "not_observed", "observations": 0}


@pytest.mark.asyncio
async def test_all_empty_parent_arrays_cannot_claim_the_prefix_was_observed(runtime):
    runtime.outputs[worker.OUTPUT_ROOT + "/public.json"] = b'{"*":[]}'
    candidate, contract, receipt = await execute(runtime, declared=nested_interfaces())
    assert receipt["status"] == "failed"
    public = validate_public_contract_receipt(receipt, contract, PROGRAMS, candidate, require_passed=False)
    assert public["status"] == "failed" and all(case["status"] == "not_observed" for case in public["cases"])
    with pytest.raises(PublicOutputContractError):
        validate_public_contract_receipt(receipt, contract, PROGRAMS, candidate)


@pytest.mark.asyncio
async def test_one_nonempty_case_confirms_prefix_without_falsely_claiming_empty_cases(runtime, monkeypatch):
    initial = runtime.receipt
    def receipt(container, request, cancelled, deadline):
        runtime.outputs[worker.OUTPUT_ROOT + "/public.json"] = (b'{"*":[{"columns":["id"]}]}'
            if len(runtime.requests) == 0 else b'{"*":[]}')
        return initial(container, request, cancelled, deadline)
    monkeypatch.setattr(worker, "_capture_receipt", receipt)
    candidate, contract, receipt = await execute(runtime, declared=nested_interfaces())
    public = validate_public_contract_receipt(receipt, contract, PROGRAMS, candidate)
    assert public["status"] == "passed"
    assert [case["status"] for case in public["cases"]] == ["passed", "not_observed", "not_observed"]


@pytest.mark.asyncio
async def test_rejection_cases_are_not_forced_to_emit_success_schema(runtime, monkeypatch):
    candidate = plan()
    candidate.scripts[0].cases[-1].expected_exit_code = 2
    candidate.scripts[0].cases[-1].assertions = []
    runtime.outputs[worker.OUTPUT_ROOT + "/public.json"] = public_json()
    initial = runtime.receipt
    def receipt(container, request, cancelled, deadline):
        if len(runtime.requests) == 2:
            runtime.exit_code = 2
            del runtime.outputs[worker.OUTPUT_ROOT + "/public.json"]
        return initial(container, request, cancelled, deadline)
    monkeypatch.setattr(worker, "_capture_receipt", receipt)
    candidate, contract, receipt = await execute(runtime, candidate=candidate)
    assert len(receipt["public_output_verification"]["cases"]) == 2
    assert receipt["status"] == "passed"
    assert validate_public_contract_receipt(receipt, contract, PROGRAMS, candidate)["status"] == "passed"


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing_receipt", "contract_hash", "source_hash", "plan_hash", "missing_case",
    "duplicate_case", "missing_check", "duplicate_check", "unobserved_pass", "unknown_reason", "foreign_rule",
    "public_data", "missing_execution", "wrong_exit", "input_changed", "hidden_failure"])
async def test_public_receipt_cannot_be_spoofed_or_incomplete_even_when_failures_are_allowed(runtime, damage):
    runtime.outputs[worker.OUTPUT_ROOT + "/public.json"] = public_json()
    candidate, contract, receipt = await execute(runtime)
    public = receipt["public_output_verification"]
    check = public["cases"][0]["checks"][0]
    if damage == "missing_receipt": receipt.pop("public_output_verification")
    elif damage == "contract_hash": public["contract_sha256"] = "0" * 64
    elif damage == "source_hash": receipt["source_sha256"] = {}
    elif damage == "plan_hash": receipt["plan_sha256"] = "0" * 64
    elif damage == "missing_case": public["cases"].pop()
    elif damage == "duplicate_case": public["cases"].append(copy.deepcopy(public["cases"][0]))
    elif damage == "missing_check": public["cases"][0]["checks"] = []
    elif damage == "duplicate_check": public["cases"][0]["checks"].append(copy.deepcopy(check))
    elif damage == "unobserved_pass": check["observations"] = 0
    elif damage == "unknown_reason": check.update(status="failed", reason="model_instruction")
    elif damage == "foreign_rule": check["rule_id"] = "C999"
    elif damage == "public_data": check["actual"] = "SECRET_SCIENTIFIC_VALUE"
    elif damage == "missing_execution": receipt["cases"].pop()
    elif damage == "wrong_exit": receipt["cases"][0]["actual_exit_code"] = 2
    elif damage == "input_changed": receipt["cases"][0]["input_unchanged"] = False
    else:
        check.update(status="failed", reason="public_prefix_mismatch")
        public["cases"][0]["status"] = public["status"] = "failed"
    with pytest.raises(PublicOutputContractError):
        validate_public_contract_receipt(receipt, contract, PROGRAMS, candidate, require_passed=False)


@pytest.mark.asyncio
async def test_unsupported_public_rules_preserve_unsupported_status_without_starting_docker(runtime):
    candidate = plan()
    candidate.scripts[0].support = "unsupported"
    candidate.scripts[0].reason = "The interface cannot currently be validated with synthetic inputs."
    candidate.scripts[0].cases = []
    candidate, contract, receipt = await execute(runtime, candidate=candidate)
    assert receipt["status"] == "unsupported" and runtime.created == []
    assert validate_public_contract_receipt(receipt, contract, PROGRAMS, candidate)["status"] == "unsupported"


@pytest.mark.skipif(os.environ.get("DATALAB_RUN_SYNTHETIC_DOCKER_TEST") != "1", reason="Opt-in disposable sandbox integration")
@pytest.mark.asyncio
async def test_real_worker_checks_json_bound_inside_try_without_rewriting_source(monkeypatch):
    monkeypatch.setattr(worker, "helper_security_options", lambda: ["no-new-privileges:true"])
    source = '''import argparse,csv,json
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--input');p.add_argument('--output');a=p.parse_args()
if Path(a.input).resolve()==Path(a.output).resolve(): raise ValueError('Input is read only')
rows=list(csv.DictReader(open(a.input)));total=sum(float(row['value']) for row in rows)
Path(a.output,'summary.json').write_text(json.dumps({'value':total}))
try:
    document=json.dumps({'columns':['id','mean'], 'private_calculation':total})
except (TypeError,ValueError):
    raise ValueError('Serialization failed')
Path(a.output,'public.json').write_text(document)
'''
    contract = compile_public_output_contract(interfaces())
    candidate = plan(output_type="json")
    settings = SimpleNamespace(sandbox_image=os.environ.get("DATALAB_SYNTHETIC_TEST_IMAGE", "datalab-sandbox:analysis-recovery-20261022"))
    for code, expected in [(source, "passed"), (source.replace("['id','mean']", "['mean','id']"), "failed")]:
        scripts = {"scripts/summary.py": code}
        receipt = await worker.validate_skill_scripts(scripts, candidate, settings, public_output_contract=contract)
        assert receipt["status"] == expected, json.dumps(receipt)
        assert validate_public_contract_receipt(receipt, contract, scripts, candidate, require_passed=False)["status"] == expected
        assert all(case["checks"][0]["passed"] for case in receipt["cases"])
