"""Literal rejection diagnostics are observable without retaining error text."""
import json
import os
import threading
import time
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.domain.services.skills.skill_creation.privacy import PrivacyContext
from app.domain.services.skills.skill_validation import (
    StderrContainsAssertion, SyntheticValidationCase, SyntheticValidationPlan, is_input_alias_rejection_case,
)
from app.infrastructure.external.sandbox import skill_validation_worker as worker
from test_skill_output_empty_assertion import absence_runtime, empty_plan
from test_skill_validation_worker import case, runtime


def diagnostic_plan():
    document = empty_plan().model_dump(mode="json")
    document["scripts"][0]["cases"][-1]["assertions"].append({"type": "stderr_contains", "expected": "group"})
    return SyntheticValidationPlan.model_validate(document)


@pytest.mark.parametrize("expected", [None, 1, True, {}, [], "", " ", " group", "group ",
    "group\nname", "group\rname", "group\tname", "group\x00name", "group\x1bname", "group\u202ename", "x" * 161])
def test_diagnostic_is_short_strict_printable_literal(expected):
    with pytest.raises(ValidationError):
        StderrContainsAssertion.model_validate({"type": "stderr_contains", "expected": expected})


@pytest.mark.parametrize("field,value", [("path", "error.log"), ("regex", "group.*"), ("atol", 1), ("pointer", "/group")])
def test_diagnostic_has_no_path_regex_or_scientific_tolerance(field, value):
    with pytest.raises(ValidationError):
        StderrContainsAssertion.model_validate({"type": "stderr_contains", "expected": "group", field: value})


@pytest.mark.parametrize("kind,exit_code", [("normal", 0), ("boundary", 0), ("invalid", 0),
    ("boundary", "nonzero"), ("invalid", 124), ("invalid", 137), ("invalid", 255)])
def test_diagnostic_only_observes_ordinary_invalid_rejection(kind, exit_code):
    document = case(kind, exit_code=exit_code)
    document["assertions"] = [{"type": "stderr_contains", "expected": "group"}]
    with pytest.raises(ValidationError):
        SyntheticValidationCase.model_validate(document)


def test_different_required_diagnostics_can_coexist_with_empty_output_but_duplicates_cannot():
    document = diagnostic_plan().scripts[0].cases[-1].model_dump(mode="json")
    document["assertions"].append({"type": "stderr_contains", "expected": "missing"})
    document["argv"][-1] = document["argv"][1]
    assert is_input_alias_rejection_case(SyntheticValidationCase.model_validate(document))
    document["assertions"].append({"type": "stderr_contains", "expected": "group"})
    with pytest.raises(ValidationError, match="only once"):
        SyntheticValidationCase.model_validate(document)


@pytest.mark.parametrize("expected", ["/home/private/input.csv", "sk-privatecredential1234", "known-private-user"])
def test_diagnostic_values_still_cross_existing_privacy_scan(expected):
    assertion = StderrContainsAssertion(type="stderr_contains", expected=expected)
    privacy = PrivacyContext.from_legacy(["known-private-user"])
    assert privacy.scan_document(assertion)


@pytest.mark.parametrize("expected,stderr,matched", [
    ("group", "missing required role: group\n", True),
    ("group", "missing required role: Group\n", False),
    ("group.*", "group missing", False), ("group.*", "literal group.* is missing", True),
    ("组别", "缺少角色：组别\n", True), ("group", "", False),
])
def test_literal_host_comparison_never_returns_error_stream(expected, stderr, matched):
    item = diagnostic_plan().scripts[0].cases[-1]
    item.assertions = [StderrContainsAssertion(type="stderr_contains", expected=expected)]
    result = worker._assertions(item, {}, stderr=stderr)
    assert result == [{"id": "assertion_1", "passed": matched, "expected": expected, "actual": matched}]


async def execute_probe():
    plan = diagnostic_plan()
    original = plan.model_dump(mode="json")
    receipt = await worker.validate_skill_scripts({"scripts/summary.py": "# fixed artificial source"}, plan,
        SimpleNamespace(sandbox_image="existing:sandbox"))
    assert plan.model_dump(mode="json") == original
    return receipt


@pytest.mark.asyncio
@pytest.mark.parametrize("stderr,matched", [("private payload 938216; missing group\n", True),
    ("private payload 938216; missing value\n", False)])
async def test_rejection_reports_only_literal_expectation_and_boolean_after_input_check(absence_runtime, stderr, matched):
    absence_runtime.rejection = {"exit_code": 2, "stderr": stderr, "stderr_truncated": False}
    receipt = await execute_probe()
    invalid = receipt["cases"][-1]
    assert invalid["input_unchanged"] is True and invalid["output_sha256"] == {}
    assert invalid["checks"] == [
        {"id": "assertion_1", "passed": True, "expected": True, "actual": True},
        {"id": "assertion_2", "passed": matched, "expected": "group", "actual": matched}]
    assert invalid["status"] == ("passed" if matched else "failed")
    assert "938216" not in json.dumps(receipt) and "private payload" not in json.dumps(receipt)
    assert not any("s0-c2" in name for name in absence_runtime.scientific_reads)
    assert "diagnostic" not in invalid and "stderr" not in invalid and "runtime_error_type" not in invalid


@pytest.mark.asyncio
async def test_diagnostic_does_not_hide_partial_outputs(absence_runtime):
    absence_runtime.rejection = {"exit_code": 2, "stderr": "missing group\n"}
    absence_runtime.presence_result.output = b"nonempty\n", None
    invalid = (await execute_probe())["cases"][-1]
    assert invalid["status"] == "failed" and invalid["reason"] == "assertion_mismatch"
    assert invalid["checks"][0]["passed"] is False and invalid["checks"][1]["passed"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("rejection,reason", [
    ({"exit_code": 0, "stderr": "missing group"}, "exit_code_mismatch"),
    ({"exit_code": 124, "stderr": "missing group"}, "exit_code_mismatch"),
    ({"exit_code": 137, "stderr": "missing group"}, "exit_code_mismatch"),
    ({"exit_code": 1, "stderr": "NameError: group private938216"}, "program_runtime_failure"),
    ({"exit_code": 1, "stderr": "Traceback (most recent call last):\n  private938216\nValueError: group"}, "program_runtime_failure"),
    ({"exit_code": 1, "stderr": "Exception Group Traceback (most recent call last):\nTypeError: group"}, "program_runtime_failure"),
    ({"exit_code": 2, "stderr": "private938216 missing group", "stderr_truncated": True}, "rejection_diagnostic_unconfirmed"),
    ({"exit_code": 137, "stderr": "group private938216", "reason": "program_timeout"}, "program_timeout"),
])
async def test_crash_timeout_wrong_exit_and_incomplete_stream_cannot_satisfy_diagnostic(absence_runtime, rejection, reason):
    absence_runtime.rejection = rejection
    invalid = (await execute_probe())["cases"][-1]
    assert invalid["status"] == "failed" and invalid["reason"] == reason
    assert not invalid.get("checks") and not absence_runtime.presence_reads
    assert "diagnostic" not in invalid and "private938216" not in json.dumps(invalid)


@pytest.mark.asyncio
async def test_modified_input_cannot_pass_rejection_diagnostic(absence_runtime):
    absence_runtime.rejection = {"exit_code": 2, "stderr": "missing group"}
    absence_runtime.changed_input = True
    invalid = (await execute_probe())["cases"][-1]
    assert invalid["reason"] == "synthetic_input_changed" and not invalid.get("checks")
    assert not absence_runtime.presence_reads


class ReceiptSocket:
    def __init__(self, document):
        data = (json.dumps(document) + "\n").encode()
        self.wire = bytearray(b"\x01\0\0\0" + len(data).to_bytes(4, "big") + data)
    def settimeout(self, value): pass
    def send(self, data): return len(data)
    def shutdown(self, value): pass
    def recv(self, length):
        result = bytes(self.wire[:length]); del self.wire[:length]
        return result
    def close(self): pass


@pytest.mark.parametrize("truncated", [False, True, None, 1, "false", "missing"])
def test_fixed_supervisor_receipt_requires_typed_stream_completeness(truncated):
    document = {"nonce": "n", "exit_code": 2, "reason": None, "stderr": "missing group"}
    if truncated != "missing": document["stderr_truncated"] = truncated
    attached = ReceiptSocket(document)
    container = SimpleNamespace(attach_socket=lambda **kwargs: attached, start=lambda: None)
    args = (container, {"nonce": "n", "script": "artificial.py", "argv": []}, threading.Event(), time.monotonic() + 10)
    if type(truncated) is bool:
        assert worker._capture_receipt(*args) == document
    else:
        with pytest.raises(worker._Failed, match="validation_receipt_invalid"):
            worker._capture_receipt(*args)


@pytest.mark.skipif(os.environ.get("DATALAB_RUN_SYNTHETIC_DOCKER_TEST") != "1", reason="Opt-in disposable sandbox integration")
@pytest.mark.asyncio
async def test_real_worker_checks_role_diagnostic_without_leaking_stream_or_accepting_crash(monkeypatch):
    monkeypatch.setattr(worker, "helper_security_options", lambda: ["no-new-privileges:true"])
    document = diagnostic_plan().model_dump(mode="json")
    document["scripts"][0]["cases"][-1]["input_files"][0]["rows"] = []
    for item in document["scripts"][0]["cases"][:2]:
        item["assertions"] = [{"type": "json_value", "path": "summary.json", "pointer": "/value", "expected": {"value": 4}}]
    plan = SyntheticValidationPlan.model_validate(document)
    original = plan.model_dump(mode="json")
    source = '''import argparse,csv,json,sys
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--input');p.add_argument('--output');a=p.parse_args()
rows=list(csv.DictReader(open(a.input)))
if not rows:
    REJECTION_ACTION
    raise SystemExit(2)
Path(a.output,'summary.json').write_text(json.dumps({'value':sum(float(row['value']) for row in rows)}))
'''
    settings = SimpleNamespace(sandbox_image=os.environ.get("DATALAB_SYNTHETIC_TEST_IMAGE", "datalab-sandbox:analysis-repair-20261023"))
    actions = [
        ("print('private938216 missing group', file=sys.stderr)", "passed", "expectations_matched"),
        ("print('private938216 missing value', file=sys.stderr)", "failed", "assertion_mismatch"),
        ("raise ValueError('private938216 missing group')", "failed", "program_runtime_failure"),
        ("raise ValueError('private938216'+'x'*2200+' group')", "failed", "rejection_diagnostic_unconfirmed"),
        ("print('group',file=sys.stderr);Path(a.output,'.partial').write_text('private938216')", "failed", "assertion_mismatch"),
    ]
    for action, status, reason in actions:
        receipt = await worker.validate_skill_scripts({"scripts/summary.py": source.replace("REJECTION_ACTION", action)}, plan, settings)
        invalid = receipt["cases"][-1]
        assert receipt["status"] == invalid["status"] == status, receipt
        assert invalid["reason"] == reason and invalid["input_unchanged"] is True
        assert all(item["status"] == "passed" for item in receipt["cases"][:2])
        assert "private938216" not in json.dumps(receipt) and "diagnostic" not in invalid
        assert plan.model_dump(mode="json") == original
