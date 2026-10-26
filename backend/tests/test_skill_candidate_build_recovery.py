"""A program build failure carries no authority to change a scientific oracle."""
import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from app.domain.services.skills.skill_candidate_build_recovery import (
    CandidateBuildEvidenceError, collect_candidate_build_failures,
)
from app.domain.services.skills.skill_script_verification import _reference_corrections_confirmed
from app.domain.services.skills.skill_reference_verification import _reference_runtime_failures
from app.domain.services.skills.skill_public_output_contract import (
    PublicOutputContract, PublicPrefixRule, public_contract_receipt,
)
from app.infrastructure.external.sandbox import skill_validation_worker as worker
from test_skill_validation_worker import plan, rejection_plan, runtime


PROGRAMS = {"scripts/summary.py": "# frozen artificial candidate"}


def receipt(candidate=None):
    candidate = candidate or plan()
    result = {"scope": "synthetic", "status": "failed",
        "source_sha256": {path: hashlib.sha256(code.encode()).hexdigest() for path, code in PROGRAMS.items()},
        "plan_sha256": worker._digest(worker._json(candidate.model_dump(mode="json"))), "cases": []}
    for case in candidate.scripts[0].cases:
        result["cases"].append({"script_path": "scripts/summary.py", "id": case.id, "kind": case.kind,
            "status": "passed", "reason": "expectations_matched", "expected_exit_code": case.expected_exit_code,
            "actual_exit_code": 0 if case.expected_exit_code == 0 else 2, "input_unchanged": True,
            "checks": worker._assertions(case, {"summary.csv": b"group,value\nA,4\n"})})
    failed = result["cases"][0]
    failed.update(status="failed", reason="program_runtime_failure", actual_exit_code=1,
        runtime_error_type="KeyError", runtime_build_evidence={"version": 1,
            "origin": "host_uncaught_exception", "exception_type": "KeyError"})
    failed.pop("checks")
    return result


def test_correction_without_observation_authorizes_only_blind_build_repair():
    observed = receipt()
    correction = {"script_path": "scripts/summary.py", "case_id": "normal_case", "assertion_index": 0,
                  "target_type": "csv_field", "field": "value"}
    assert not _reference_corrections_confirmed(observed, [correction])
    observed["cases"][0]["diagnostic"] = "KeyError: PRIVATE_MESSAGE_AND_ANSWER_938271"
    failures = collect_candidate_build_failures(observed, plan(), PROGRAMS)
    assert [item.model_dump() for item in failures] == [{"script_path": "scripts/summary.py",
        "case_id": "normal_case", "exception_type": "KeyError"}]
    assert "938271" not in json.dumps([item.model_dump() for item in failures])
    assert _reference_runtime_failures(observed, plan(), PROGRAMS) == []


@pytest.mark.parametrize("damage", [
    "scientific_mismatch", "observed_failed_value", "observed_output", "unsafe_input", "timeout", "signal",
    "invalid_case", "semantic_exit", "interface_failure", "passed_check_missing", "duplicate_check",
    "changed_expected_exit_type", "second_failure_unknown",
])
def test_other_failures_cannot_be_reinterpreted_as_a_blind_build(damage):
    observed = receipt()
    failed, neighbour = observed["cases"][:2]
    if damage == "scientific_mismatch":
        neighbour.update(status="failed", reason="assertion_mismatch")
        neighbour["checks"][-1].update(passed=False, expected=.5, actual=.25)
    elif damage == "observed_failed_value": failed["checks"] = [{"id": "assertion_1", "passed": False, "actual": .25}]
    elif damage == "observed_output": failed["output_sha256"] = {"summary.csv": "a" * 64}
    elif damage == "unsafe_input": failed["input_unchanged"] = False
    elif damage == "timeout": failed.update(actual_exit_code=124, reason="program_timeout")
    elif damage == "signal": failed["actual_exit_code"] = -9
    elif damage == "invalid_case":
        invalid = observed["cases"][-1]
        invalid.update({key: copy.deepcopy(value) for key, value in failed.items() if key not in {"id", "kind"}})
        invalid.pop("checks", None)
    elif damage == "semantic_exit": failed["reason"] = "exit_code_mismatch"
    elif damage == "interface_failure": neighbour.update(status="failed", reason="public_output_contract_mismatch")
    elif damage == "passed_check_missing": neighbour["checks"].pop()
    elif damage == "duplicate_check": neighbour["checks"].append(copy.deepcopy(neighbour["checks"][0]))
    elif damage == "changed_expected_exit_type": failed["expected_exit_code"] = False
    elif damage == "second_failure_unknown": neighbour.update(status="failed", reason="output_missing:summary.csv")
    assert collect_candidate_build_failures(observed, plan(), PROGRAMS) == []


@pytest.mark.parametrize("damage", ["wrong_source", "wrong_plan", "missing_case", "duplicate_case", "extra_case",
    "bad_proof", "bool_version", "missing_type", "environment_type", "public_contract_omitted"])
def test_claimed_build_authority_requires_complete_bound_receipt(damage):
    observed = receipt()
    failed = observed["cases"][0]
    if damage == "wrong_source": observed["source_sha256"] = {}
    elif damage == "wrong_plan": observed["plan_sha256"] = "f" * 64
    elif damage == "missing_case": observed["cases"].pop()
    elif damage == "duplicate_case": observed["cases"].append(copy.deepcopy(failed))
    elif damage == "extra_case": observed["cases"].append({**failed, "id": "unknown_case"})
    elif damage == "bad_proof": failed["runtime_build_evidence"]["origin"] = "stderr"
    elif damage == "bool_version": failed["runtime_build_evidence"]["version"] = True
    elif damage == "missing_type": failed.pop("runtime_error_type")
    elif damage == "environment_type":
        failed["runtime_error_type"] = failed["runtime_build_evidence"]["exception_type"] = "ImportError"
    elif damage == "public_contract_omitted": observed["public_output_verification"] = {"status": "passed", "cases": []}
    with pytest.raises(CandidateBuildEvidenceError, match="^candidate_build_evidence_unconfirmed$"):
        collect_candidate_build_failures(observed, plan(), PROGRAMS)


def test_legacy_stderr_failure_keeps_its_original_route_without_new_authority():
    observed = receipt()
    observed.pop("source_sha256")
    observed["cases"][0].pop("runtime_build_evidence")
    assert collect_candidate_build_failures(observed, plan(), PROGRAMS) == []


@pytest.mark.parametrize("neighbour_failure", [False, True])
def test_public_contract_neighbour_cannot_hide_a_real_structure_disagreement(neighbour_failure):
    candidate = plan()
    observed = receipt(candidate)
    contract = PublicOutputContract(rules=[PublicPrefixRule(id="C001", script_path="scripts/summary.py",
        output_path="@output/schema.json", steps=[], prefix=["id"])])
    public_cases = []
    for index, case in enumerate(candidate.scripts[0].cases):
        failed = index == 0 or index == 1 and neighbour_failure
        check = {"rule_id": "C001", "status": "failed" if failed else "passed", "observations": 0 if failed else 1}
        if failed:
            check["reason"] = "public_execution_unconfirmed" if index == 0 else "public_prefix_mismatch"
        public_cases.append({"script_path": "scripts/summary.py", "id": case.id,
            "status": check["status"], "checks": [check]})
    observed["public_output_verification"] = public_contract_receipt(contract, candidate, public_cases)
    failures = collect_candidate_build_failures(observed, candidate, PROGRAMS, public_output_contract=contract)
    assert bool(failures) is not neighbour_failure


def run_launcher(tmp_path, body):
    path = tmp_path / "artificial.py"
    path.write_text(body)
    read, write = os.pipe()
    try:
        process = subprocess.Popen([sys.executable, "-B", "-c", worker._CHILD_RUNNER,
            str(write), "fixture-nonce", str(path), "--input", "artificial.csv"],
            pass_fds=(write,), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        os.close(write)
        write = None
        stdout, stderr = process.communicate(timeout=20)
        data = os.read(read, 2048)
        return process.returncode, json.loads(data), stdout, stderr
    finally:
        os.close(read)
        if write is not None:
            os.close(write)


@pytest.mark.parametrize(("body", "kind"), [
    ("{}['missing']", "KeyError"), ("[][0]", "IndexError"),
    ("1 + 'bad'", "TypeError"), ("undefined_value", "NameError"),
    ("object().missing", "AttributeError"),
    ("def f():\n x += 1\nf()", "UnboundLocalError"),
    pytest.param("import pandas as pd\npd.DataFrame({'x':[1]})[['missing']]", "KeyError",
                 marks=pytest.mark.skipif(importlib.util.find_spec("pandas") is None,
                     reason="Pandas is installed in the isolated sandbox; covered by its real-worker test")),
])
def test_launcher_classifies_real_builtin_exception_without_message(tmp_path, body, kind):
    code, evidence, _, stderr = run_launcher(tmp_path, body)
    assert code == 1 and b"Traceback" in stderr
    assert evidence == {"nonce": "fixture-nonce", "exception_type": kind}


@pytest.mark.parametrize("body", [
    "import sys\nprint('KeyError: secret',file=sys.stderr)\nsys.exit(1)",
    "import sys\nprint('Traceback (most recent call last):\\n  File \\\"fake.py\\\", line 1, in main\\nKeyError: secret',file=sys.stderr)\nsys.exit(1)",
    "class KeyError(Exception): pass\nraise KeyError('private')",
    "class ChildKeyError(KeyError): pass\nraise ChildKeyError('private')",
    "try:\n {}['missing']\nexcept KeyError:\n raise SystemExit(2)",
    "raise ValueError('semantic rejection')", "raise ImportError('environment')", "raise SystemExit(0)",
    "print('ordinary completion')",
])
def test_printed_or_caught_or_custom_exceptions_cannot_forge_build_evidence(tmp_path, body):
    _, evidence, _, _ = run_launcher(tmp_path, body)
    assert evidence == {"nonce": "fixture-nonce", "exception_type": None}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["KeyError", "IndexError", "TypeError"])
async def test_worker_checks_inputs_and_stops_before_output_reads_on_real_channel_evidence(runtime, monkeypatch, kind):
    original = runtime.receipt
    def broken(container, request, cancelled, deadline):
        return {**original(container, request, cancelled, deadline), "exit_code": 1,
                "stderr": "irrelevant diagnostic", "runtime_exception": kind}
    monkeypatch.setattr(worker, "_capture_receipt", broken)
    observed = await worker.validate_skill_scripts(PROGRAMS, rejection_plan(), SimpleNamespace(sandbox_image="existing:sandbox"))
    for case in observed["cases"][:2]:
        assert case["input_unchanged"] is True and case["reason"] == "program_runtime_failure"
        assert case["runtime_build_evidence"] == {"version": 1, "origin": "host_uncaught_exception", "exception_type": kind}
        assert "checks" not in case and "output_sha256" not in case
    assert "runtime_build_evidence" not in observed["cases"][-1]
    assert observed["cases"][-1]["status"] == "failed"
    assert collect_candidate_build_failures(observed, rejection_plan(), PROGRAMS) == []


@pytest.mark.asyncio
async def test_changed_inputs_prevent_even_real_channel_build_evidence(runtime, monkeypatch):
    runtime.changed_input = True
    original = runtime.receipt
    monkeypatch.setattr(worker, "_capture_receipt", lambda *args: {
        **original(*args), "exit_code": 1, "stderr": "", "runtime_exception": "KeyError"})
    observed = await worker.validate_skill_scripts(PROGRAMS, plan(), SimpleNamespace(sandbox_image="existing:sandbox"))
    assert all(case["reason"] == "synthetic_input_changed" and "runtime_build_evidence" not in case for case in observed["cases"])


@pytest.mark.skipif(os.environ.get("DATALAB_RUN_SYNTHETIC_DOCKER_TEST") != "1", reason="Opt-in disposable sandbox integration")
@pytest.mark.asyncio
@pytest.mark.parametrize(("action", "kind"), [
    ("{}['missing']", "KeyError"), ("[][0]", "IndexError"),
    ("import pandas as pd\npd.DataFrame(rows)[['missing']]", "KeyError"),
    ("print('KeyError: synthetic text', file=sys.stderr)\nsys.exit(1)", None),
    ("class KeyError(Exception): pass\nraise KeyError('synthetic text')", None),
])
async def test_real_isolated_worker_lookup_exception_has_bound_blind_evidence(monkeypatch, action, kind):
    monkeypatch.setattr(worker, "helper_security_options", lambda: ["no-new-privileges:true"])
    candidate = rejection_plan()
    candidate.scripts[0].cases[-1].input_files[0].rows = []
    source = """import argparse, csv, sys
from pathlib import Path
p=argparse.ArgumentParser()
p.add_argument('--input', required=True)
p.add_argument('--output', required=True)
a=p.parse_args()
if Path(a.input).resolve()==Path(a.output).resolve(): raise ValueError('input conflict')
rows=list(csv.DictReader(open(a.input)))
if not rows: raise SystemExit(2)
""" + action + "\n"
    programs = {"scripts/summary.py": source}
    observed = await worker.validate_skill_scripts(programs, candidate, SimpleNamespace(
        sandbox_image=os.environ.get("DATALAB_SYNTHETIC_TEST_IMAGE", "datalab-sandbox:execution-reliability-20261025")))
    assert observed["status"] == "failed"
    if kind is not None:
        assert all(case["reason"] == "program_runtime_failure" for case in observed["cases"][:2])
        assert all(case.get("runtime_build_evidence", {}).get("exception_type") == kind for case in observed["cases"][:2])
    else:
        assert all("runtime_build_evidence" not in case for case in observed["cases"])
    assert "runtime_build_evidence" not in observed["cases"][-1]
    assert observed["cases"][-1]["status"] == "passed"
    failures = collect_candidate_build_failures(observed, candidate, programs)
    assert [item.exception_type for item in failures] == ([kind, kind] if kind else [])
