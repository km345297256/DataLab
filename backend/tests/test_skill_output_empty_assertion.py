"""Rejected input must not leave partial artifacts in isolated output space."""
import copy
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.domain.services.skills.skill_validation import (
    OutputEmptyAssertion, SyntheticValidationCase, SyntheticValidationPlan, is_input_alias_rejection_case,
)
from app.infrastructure.external.sandbox import skill_validation_worker as worker
from test_skill_validation_worker import FakeContainer, case, plan, rejection_plan, runtime


def empty_plan():
    document = rejection_plan().model_dump(mode="json")
    document["scripts"][0]["cases"][-1]["assertions"] = [{"type": "output_empty"}]
    return SyntheticValidationPlan.model_validate(document)


@pytest.mark.parametrize("field,value", [("path", "../secret"), ("expected", False), ("pointer", "/value")])
def test_absence_postcondition_does_not_accept_model_paths_or_expected_answers(field, value):
    with pytest.raises(ValidationError):
        OutputEmptyAssertion.model_validate({"type": "output_empty", field: value})


@pytest.mark.parametrize("kind,exit_code", [("normal", 0), ("boundary", 0), ("invalid", 0), ("boundary", "nonzero")])
def test_only_invalid_input_rejection_can_assert_empty_output(kind, exit_code):
    document = case(kind, exit_code=exit_code)
    document["assertions"] = [{"type": "output_empty"}]
    with pytest.raises(ValidationError):
        SyntheticValidationCase.model_validate(document)


@pytest.mark.parametrize("exit_code", [124, 125, 126, 127, 137, 255])
def test_exact_signal_or_timeout_codes_do_not_prove_normal_rejection(exit_code):
    document = case("invalid", exit_code=exit_code)
    document["assertions"] = [{"type": "output_empty"}]
    with pytest.raises(ValidationError, match="ordinary rejection code"):
        SyntheticValidationCase.model_validate(document)


@pytest.mark.parametrize("assertion", [
    {"type": "csv_rows", "path": "report.csv", "match": {}, "count": 0, "fields": {}},
    {"type": "json_value", "path": "report.json", "pointer": "/value", "expected": {"value": 0}},
    {"type": "json_array_length", "path": "report.json", "pointer": "/rows", "length": 0},
])
def test_rejection_cannot_be_used_to_read_scientific_output(assertion):
    document = case("invalid", exit_code="nonzero")
    document["assertions"] = [{"type": "output_empty"}, assertion]
    with pytest.raises(ValidationError, match="never scientific output"):
        SyntheticValidationCase.model_validate(document)


def test_same_input_output_safety_probe_can_check_isolated_output_without_scanning_inputs():
    document = empty_plan().scripts[0].cases[-1].model_dump(mode="json")
    document["argv"][-1] = document["argv"][1]
    value = SyntheticValidationCase.model_validate(document)
    assert is_input_alias_rejection_case(value)
    assert value.assertions == [OutputEmptyAssertion(type="output_empty")]
    document["assertions"] *= 2
    with pytest.raises(ValidationError, match="only once"):
        SyntheticValidationCase.model_validate(document)


@pytest.fixture
def absence_runtime(runtime, monkeypatch):
    original_exec, original_receipt = FakeContainer.exec_run, runtime.receipt
    runtime.presence_result = SimpleNamespace(exit_code=0, output=(b"empty\n", None))
    runtime.presence_reads, runtime.input_reads, runtime.scientific_reads = [], [], []
    runtime.rejection = {"exit_code": 2, "stderr": ""}
    def observed(container, request, cancelled, deadline):
        result = original_receipt(container, request, cancelled, deadline)
        if "s0-c2" in container.config["name"]:
            result.update(runtime.rejection)
        return result
    def read(container, command, **kwargs):
        if command[3] == worker._OUTPUT_EMPTY:
            assert command == ["/app/.venv/bin/python", "-I", "-c", worker._OUTPUT_EMPTY, worker.OUTPUT_ROOT]
            assert kwargs == {"user": worker.RUNTIME_USER, "stdout": True, "stderr": True, "demux": True}
            assert any("s0-c2" in name for name in runtime.input_reads)
            runtime.presence_reads.append(container.config["name"])
            return runtime.presence_result
        target = runtime.input_reads if command[4].startswith("/validation") else runtime.scientific_reads
        target.append(container.config["name"])
        return original_exec(container, command, **kwargs)
    monkeypatch.setattr(worker, "_capture_receipt", observed)
    monkeypatch.setattr(FakeContainer, "exec_run", read)
    return runtime


async def run_probe(runtime):
    candidate = empty_plan()
    original = candidate.model_dump(mode="json")
    receipt = await worker.validate_skill_scripts({"scripts/summary.py": "# fixed artificial source"}, candidate,
        SimpleNamespace(sandbox_image="existing:sandbox"))
    assert candidate.model_dump(mode="json") == original
    return receipt


@pytest.mark.asyncio
@pytest.mark.parametrize("raw,expected", [(b"empty\n", True), (b"nonempty\n", False)])
async def test_normal_rejection_checks_output_metadata_after_input_integrity(absence_runtime, raw, expected):
    absence_runtime.presence_result.output = raw, None
    receipt = await run_probe(absence_runtime)
    invalid = receipt["cases"][-1]
    assert (receipt["status"] == "passed") is expected
    assert invalid["checks"] == [{"id": "assertion_1", "passed": expected, "expected": True, "actual": expected}]
    assert invalid["input_unchanged"] is True
    assert invalid["output_sha256"] == {}
    assert "runtime_error_type" not in invalid
    assert len(absence_runtime.presence_reads) == 1
    assert not any("s0-c2" in name for name in absence_runtime.scientific_reads)
    if not expected:
        assert invalid["reason"] == "assertion_mismatch"


@pytest.mark.asyncio
@pytest.mark.parametrize("actual,stderr,reason", [
    (0, "", "exit_code_mismatch"), (-9, "", "exit_code_mismatch"),
    (124, "", "exit_code_mismatch"), (127, "", "exit_code_mismatch"),
    (1, "NameError: broken source", "program_runtime_failure"),
    (1, "ModuleNotFoundError: unavailable library", "program_runtime_failure"),
])
async def test_wrong_exit_or_interpreter_failure_never_checks_output_absence(absence_runtime, actual, stderr, reason):
    absence_runtime.rejection = {"exit_code": actual, "stderr": stderr}
    receipt = await run_probe(absence_runtime)
    invalid = receipt["cases"][-1]
    assert invalid["status"] == "failed" and invalid["reason"] == reason
    assert not invalid.get("checks") and not absence_runtime.presence_reads


@pytest.mark.asyncio
async def test_timeout_and_changed_input_never_claim_empty_output(absence_runtime):
    absence_runtime.rejection = {"exit_code": 137, "reason": "program_timeout"}
    receipt = await run_probe(absence_runtime)
    assert receipt["cases"][-1]["reason"] == "program_timeout"
    assert not absence_runtime.presence_reads
    absence_runtime.rejection = {"exit_code": 2, "stderr": ""}
    absence_runtime.changed_input = True
    receipt = await run_probe(absence_runtime)
    assert receipt["cases"][-1]["reason"] == "synthetic_input_changed"
    assert not absence_runtime.presence_reads


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_code,stdout,stderr", [(2, None, b"private error"),
    (0, b"empty\n", b"warning"), (0, b"true", None), (0, b"empty\nprivate filename", None), (0, None, None)])
async def test_missing_or_malformed_directory_evidence_is_not_an_empty_directory(absence_runtime, exit_code, stdout, stderr):
    absence_runtime.presence_result = SimpleNamespace(exit_code=exit_code, output=(stdout, stderr))
    receipt = await run_probe(absence_runtime)
    invalid = receipt["cases"][-1]
    assert invalid["status"] == "failed" and invalid["reason"] == "output_presence_unconfirmed"
    assert not invalid.get("checks") and "private" not in json.dumps(receipt)


@pytest.mark.parametrize("entry", ["empty", "file", "hidden", "directory", "symlink"])
def test_fixed_reader_observes_only_presence_and_does_not_follow_or_disclose_entries(tmp_path, entry):
    directory = tmp_path / "output"
    directory.mkdir()
    if entry == "file": (directory / "scientific_answer.json").write_text('{"value":314159}')
    elif entry == "hidden": (directory / ".secret").write_text("private")
    elif entry == "directory": (directory / "partial").mkdir()
    elif entry == "symlink": (directory / "link").symlink_to(tmp_path / "missing-outside")
    result = subprocess.run([sys.executable, "-I", "-c", worker._OUTPUT_EMPTY, str(directory)], capture_output=True)
    assert result.returncode == 0 and result.stderr == b""
    assert result.stdout == (b"empty\n" if entry == "empty" else b"nonempty\n")


@pytest.mark.parametrize("root_kind", ["missing", "file", "symlink"])
def test_fixed_reader_rejects_missing_non_directory_or_linked_output_root(tmp_path, root_kind):
    root = tmp_path / "output"
    if root_kind == "file": root.write_text("private")
    elif root_kind == "symlink": root.symlink_to(tmp_path, target_is_directory=True)
    result = subprocess.run([sys.executable, "-I", "-c", worker._OUTPUT_EMPTY, str(root)], capture_output=True)
    assert result.returncode == 2 and result.stdout == b""
    assert result.stderr == b"output_presence_unconfirmed\n"


@pytest.mark.skipif(os.environ.get("DATALAB_RUN_SYNTHETIC_DOCKER_TEST") != "1", reason="Opt-in disposable sandbox integration")
@pytest.mark.asyncio
async def test_real_worker_distinguishes_clean_rejection_from_partial_hidden_output(monkeypatch):
    monkeypatch.setattr(worker, "helper_security_options", lambda: ["no-new-privileges:true"])
    document = empty_plan().model_dump(mode="json")
    document["scripts"][0]["cases"][-1]["input_files"][0]["rows"] = []
    for item in document["scripts"][0]["cases"][:2]:
        item["assertions"] = [{"type": "json_value", "path": "summary.json", "pointer": "/value", "expected": {"value": 4}}]
    candidate = SyntheticValidationPlan.model_validate(document)
    source = '''import argparse,csv,json
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--input');p.add_argument('--output');a=p.parse_args()
rows=list(csv.DictReader(open(a.input)))
if not rows:
    # REJECTION_ACTION
    raise SystemExit(2)
Path(a.output,'summary.json').write_text(json.dumps({'value':sum(float(row['value']) for row in rows)}))
'''
    settings = SimpleNamespace(sandbox_image=os.environ.get("DATALAB_SYNTHETIC_TEST_IMAGE", "datalab-sandbox:analysis-repair-20261023"))
    for action, expected in [("pass", True), ("Path(a.output,'.partial').write_text('private artifact')", False)]:
        original = copy.deepcopy(candidate.model_dump())
        receipt = await worker.validate_skill_scripts({"scripts/summary.py": source.replace("# REJECTION_ACTION", action)}, candidate, settings)
        assert receipt["status"] == ("passed" if expected else "failed"), receipt
        assert receipt["cases"][-1]["checks"] == [{"id": "assertion_1", "passed": expected, "expected": True, "actual": expected}]
        assert receipt["cases"][-1]["input_unchanged"] is True
        assert all(check["passed"] for item in receipt["cases"][:2] for check in item["checks"])
        assert candidate.model_dump() == original and "private artifact" not in json.dumps(receipt)
