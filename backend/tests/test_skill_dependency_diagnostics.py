"""Real import failures are environment evidence, never algorithm-repair input."""
import copy
import hashlib
import json
import os
from types import SimpleNamespace

import pytest

from app.domain.services.skills.skill_candidate_build_recovery import collect_candidate_build_failures
from app.domain.services.skills.skill_creation_error import SkillCreationError
from app.domain.services.skills.skill_dependency_diagnostics import DependencyEvidenceError, has_dependency_unavailable
from app.domain.services.skills.skill_public_output_contract import PublicOutputContract, PublicPrefixRule, public_contract_receipt
from app.infrastructure.external.sandbox import skill_validation_worker as worker
from test_skill_candidate_build_recovery import run_launcher
from test_skill_validation_worker import plan, rejection_plan, runtime


PROGRAMS = {"scripts/summary.py": "import datalab_artificial_missing_dependency_0739\n"}


def receipt():
    value = plan()
    return {"scope": "synthetic", "status": "failed",
        "source_sha256": {path: hashlib.sha256(source.encode()).hexdigest() for path, source in PROGRAMS.items()},
        "plan_sha256": worker._digest(worker._json(value.model_dump(mode="json"))),
        "cases": [{"script_path": "scripts/summary.py", "id": case.id, "kind": case.kind,
            "expected_exit_code": case.expected_exit_code, "actual_exit_code": 1,
            "status": "failed", "reason": "dependency_unavailable", "input_unchanged": True,
            "runtime_dependency_evidence": {"version": 1, "origin": "host_import_exception",
                                            "exception_type": "ModuleNotFoundError"}}
            for case in value.scripts[0].cases]}


@pytest.mark.parametrize("source,kind", [
    ("import datalab_artificial_missing_dependency_0739", "ModuleNotFoundError"),
    ("from math import datalab_artificial_missing_symbol_0739", "ImportError"),
])
def test_actual_import_opcode_supplies_type_only_without_module_or_message(tmp_path, source, kind):
    code, evidence, _, stderr = run_launcher(tmp_path, source)
    assert code == 1 and b"datalab_artificial_missing_" in stderr
    assert evidence == {"nonce": "fixture-nonce", "exception_type": None, "dependency_exception": kind}
    assert "0739" not in json.dumps(evidence)


@pytest.mark.parametrize("source", [
    "raise ImportError('PRIVATE_BUSINESS_VALUE')",
    "raise ModuleNotFoundError('PRIVATE_BUSINESS_VALUE', name='fake_package')",
    "class ModuleNotFoundError(Exception): pass\nraise ModuleNotFoundError('private')",
    "class Custom(ImportError): pass\nraise Custom('private')",
    "import sys\nprint('ModuleNotFoundError: private', file=sys.stderr)\nsys.exit(1)",
    "import sys\nprint('Traceback (most recent call last):\\nImportError: private',file=sys.stderr)\nsys.exit(1)",
    "try:\n import datalab_artificial_missing_dependency_0739\nexcept ImportError:\n raise SystemExit(2)",
    "import importlib\nimportlib.import_module('datalab_artificial_missing_dependency_0739')",
])
def test_business_or_custom_or_printed_error_does_not_prove_import_operation(tmp_path, source):
    _, evidence, _, _ = run_launcher(tmp_path, source)
    assert evidence == {"nonce": "fixture-nonce", "exception_type": None}


def test_library_business_error_at_raise_is_not_misclassified_as_missing_dependency(tmp_path):
    (tmp_path / "business_library.py").write_text("raise ImportError('input is not a supported business format')")
    _, evidence, _, _ = run_launcher(tmp_path, "import business_library")
    assert "dependency_exception" not in evidence


def test_transitive_missing_import_is_a_real_dependency_failure(tmp_path):
    (tmp_path / "wrapper_library.py").write_text("import datalab_artificial_missing_dependency_0739")
    _, evidence, _, _ = run_launcher(tmp_path, "import wrapper_library")
    assert evidence["dependency_exception"] == "ModuleNotFoundError"


def test_missing_dependency_is_not_candidate_algorithm_repair():
    observed = receipt()
    assert has_dependency_unavailable(observed, plan(), PROGRAMS)
    assert collect_candidate_build_failures(observed, plan(), PROGRAMS) == []
    public = SkillCreationError("synthetic_execution", "dependency_unavailable")
    assert "依赖安装和版本兼容性" in public.public_message
    assert "参数化" not in public.public_message and "只读" not in public.public_message
    assert "0739" not in public.public_message and "ModuleNotFoundError" not in public.public_message


@pytest.mark.parametrize("damage", ["source", "plan", "missing_case", "extra_case", "duplicate_case", "origin",
    "bool_version", "type", "extra_private_field", "exit", "signal", "reason", "input", "kind", "expected_type",
    "scientific_checks", "output", "build_proof", "diagnostic", "passed_overall", "public_contract_omitted"])
def test_claimed_dependency_failure_requires_complete_bound_host_evidence(damage):
    observed = receipt()
    item = observed["cases"][0]
    if damage == "source": observed["source_sha256"] = {}
    elif damage == "plan": observed["plan_sha256"] = "f" * 64
    elif damage == "missing_case": observed["cases"].pop()
    elif damage == "extra_case": observed["cases"].append({**copy.deepcopy(item), "id": "unknown"})
    elif damage == "duplicate_case": observed["cases"].append(copy.deepcopy(item))
    elif damage == "origin": item["runtime_dependency_evidence"]["origin"] = "stderr"
    elif damage == "bool_version": item["runtime_dependency_evidence"]["version"] = True
    elif damage == "type": item["runtime_dependency_evidence"]["exception_type"] = "TypeError"
    elif damage == "extra_private_field": item["runtime_dependency_evidence"]["module"] = "PRIVATE_NAME"
    elif damage == "exit": item["actual_exit_code"] = 0
    elif damage == "signal": item["actual_exit_code"] = 137
    elif damage == "reason": item["reason"] = "program_timeout"
    elif damage == "input": item["input_unchanged"] = False
    elif damage == "kind": item["kind"] = "invalid"
    elif damage == "expected_type": item["expected_exit_code"] = False
    elif damage == "scientific_checks": item["checks"] = [{"id": "assertion_1", "actual": 1, "passed": False}]
    elif damage == "output": item["output_sha256"] = {"summary.csv": "0" * 64}
    elif damage == "build_proof": item["runtime_build_evidence"] = {"exception_type": "TypeError"}
    elif damage == "diagnostic": item["diagnostic"] = "ModuleNotFoundError: PRIVATE_NAME"
    elif damage == "passed_overall": observed["status"] = "passed"
    elif damage == "public_contract_omitted": observed["public_output_verification"] = {"status": "failed"}
    with pytest.raises(DependencyEvidenceError, match="^dependency_evidence_unconfirmed$"):
        has_dependency_unavailable(observed, plan(), PROGRAMS)


def test_stderr_or_legacy_error_type_alone_does_not_gain_environment_authority():
    observed = receipt()
    observed.pop("source_sha256")
    for item in observed["cases"]:
        item.pop("runtime_dependency_evidence")
        item.update(diagnostic="ModuleNotFoundError: fake", runtime_error_type="ImportError")
    assert not has_dependency_unavailable(observed, plan(), PROGRAMS)


@pytest.mark.parametrize("reason", ["assertion_mismatch", "public_output_contract_mismatch", "exit_code_mismatch",
                                    "program_timeout", "program_runtime_failure", "output_missing"])
def test_environment_failure_does_not_hide_a_different_failure(reason):
    observed = receipt()
    item = observed["cases"][1]
    item.pop("runtime_dependency_evidence")
    item["reason"] = reason
    assert not has_dependency_unavailable(observed, plan(), PROGRAMS)


@pytest.mark.parametrize("public_failed", [False, True])
def test_passed_neighbour_must_also_have_matching_public_output_evidence(public_failed):
    observed = receipt()
    case = plan().scripts[0].cases[1]
    item = observed["cases"][1]
    item.pop("runtime_dependency_evidence")
    item.update(status="passed", reason="expectations_matched", actual_exit_code=0,
                checks=worker._assertions(case, {"summary.csv": b"group,value\nA,4\n"}))
    contract = PublicOutputContract(rules=[PublicPrefixRule(id="C001", script_path="scripts/summary.py",
        output_path="@output/schema.json", steps=[], prefix=["id"])])
    public_cases = []
    for index, item in enumerate(observed["cases"]):
        check = {"rule_id": "C001", "status": "failed", "observations": 0, "reason": "public_execution_unconfirmed"}
        if index == 1:
            check = {"rule_id": "C001", "status": "failed" if public_failed else "passed", "observations": 1}
            if public_failed: check["reason"] = "public_prefix_mismatch"
        public_cases.append({"script_path": item["script_path"], "id": item["id"], "status": check["status"], "checks": [check]})
    observed["public_output_verification"] = public_contract_receipt(contract, plan(), public_cases)
    assert has_dependency_unavailable(observed, plan(), PROGRAMS, public_output_contract=contract) is not public_failed


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_input", [False, True])
async def test_worker_confirms_inputs_before_type_only_dependency_receipt(runtime, monkeypatch, changed_input):
    runtime.changed_input = changed_input
    original = runtime.receipt
    monkeypatch.setattr(worker, "_capture_receipt", lambda *args: {**original(*args), "exit_code": 1,
        "runtime_exception": None, "dependency_exception": "ModuleNotFoundError", "stderr": "ModuleNotFoundError: PRIVATE_DEPENDENCY"})
    observed = await worker.validate_skill_scripts(PROGRAMS, rejection_plan(), SimpleNamespace(sandbox_image="existing:sandbox"))
    assert observed["status"] == "failed"
    if changed_input:
        assert all(item["reason"] == "synthetic_input_changed" and "runtime_dependency_evidence" not in item
                   for item in observed["cases"])
    else:
        assert has_dependency_unavailable(observed, rejection_plan(), PROGRAMS)
        assert all(item["reason"] == "dependency_unavailable" and item["input_unchanged"] for item in observed["cases"])
    assert all("runtime_build_evidence" not in item and "checks" not in item for item in observed["cases"])
    assert "PRIVATE_DEPENDENCY" not in json.dumps(observed)


@pytest.mark.skipif(os.environ.get("DATALAB_RUN_SYNTHETIC_DOCKER_TEST") != "1", reason="Opt-in isolated artificial worker")
@pytest.mark.asyncio
@pytest.mark.parametrize("source,expected", [
    ("import datalab_artificial_missing_dependency_0739", True),
    ("from math import datalab_artificial_missing_symbol_0739", True),
    ("raise ImportError('PRIVATE_BUSINESS_VALUE')", False),
    ("import sys\nprint('ModuleNotFoundError: PRIVATE_FAKE',file=sys.stderr)\nsys.exit(1)", False),
])
async def test_real_worker_dependency_classification_uses_host_import_channel(monkeypatch, source, expected):
    monkeypatch.setattr(worker, "helper_security_options", lambda: ["no-new-privileges:true"])
    programs = {"scripts/summary.py": source + "\n"}
    observed = await worker.validate_skill_scripts(programs, rejection_plan(), SimpleNamespace(
        sandbox_image=os.environ.get("DATALAB_SYNTHETIC_TEST_IMAGE", "datalab-sandbox:file-delivery-20261025")))
    assert observed["status"] == "failed"
    assert has_dependency_unavailable(observed, rejection_plan(), programs) is expected
    assert collect_candidate_build_failures(observed, rejection_plan(), programs) == []
    if expected:
        assert all(item["reason"] == "dependency_unavailable" for item in observed["cases"])
        assert "0739" not in json.dumps(observed) and "diagnostic" not in json.dumps(observed)
