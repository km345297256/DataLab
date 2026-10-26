"""Only observed pre-comparison build failures can justify reference revision."""
from types import SimpleNamespace

import pytest

from app.infrastructure.external.sandbox import skill_validation_worker as worker
from test_skill_validation_worker import plan, rejection_plan, runtime


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["TypeError", "NameError", "UnboundLocalError", "AttributeError"])
async def test_expected_success_runtime_error_has_readonly_evidence_but_no_scientific_observations(runtime, monkeypatch, kind):
    original = runtime.receipt
    def broken(container, request, cancelled, deadline):
        result = original(container, request, cancelled, deadline)
        return {**result, "exit_code": 1, "stderr": f"{kind}: synthetic implementation error"}
    monkeypatch.setattr(worker, "_capture_receipt", broken)
    receipt = await worker.validate_skill_scripts({"scripts/summary.py": "# Immutable artificial code"}, plan(),
        SimpleNamespace(sandbox_image="existing:sandbox"))
    assert receipt["status"] == "failed"
    for case in receipt["cases"]:
        assert case["reason"] == "program_runtime_failure"
        assert case["runtime_error_type"] == kind and case["input_unchanged"] is True
        assert case["actual_exit_code"] == 1 and case["expected_exit_code"] == 0
        assert "checks" not in case and "output_sha256" not in case


@pytest.mark.asyncio
async def test_runtime_error_cannot_claim_safe_inputs_when_readback_changed(runtime, monkeypatch):
    runtime.changed_input = True
    initial = runtime.receipt
    def broken(container, request, cancelled, deadline):
        return {**initial(container, request, cancelled, deadline), "exit_code": 1, "stderr": "TypeError: synthetic failure"}
    monkeypatch.setattr(worker, "_capture_receipt", broken)
    receipt = await worker.validate_skill_scripts({"scripts/summary.py": "# Immutable artificial code"}, plan(),
        SimpleNamespace(sandbox_image="existing:sandbox"))
    assert all(case["reason"] == "synthetic_input_changed" for case in receipt["cases"])
    assert all("runtime_error_type" not in case and "input_unchanged" not in case for case in receipt["cases"])


@pytest.mark.parametrize("kind", ["ImportError", "ModuleNotFoundError", "SyntaxError", "MemoryError", "ValueError", "RuntimeError"])
def test_environment_resource_and_semantic_exceptions_do_not_authorize_build_repair(kind):
    assert worker._repairable_runtime_error_type(plan().scripts[0].cases[0], 1, f"{kind}: failure") is None


@pytest.mark.parametrize("exit_code", [0, -9, 124, 125, 126, 127, 137])
def test_success_signals_timeouts_and_launch_errors_have_no_build_repair_authority(exit_code):
    assert worker._repairable_runtime_error_type(plan().scripts[0].cases[0], exit_code, "TypeError: failure") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["TypeError", "NameError"])
async def test_invalid_input_rejection_semantics_are_unchanged_and_have_no_typed_repair_authority(runtime, monkeypatch, kind):
    initial = runtime.receipt
    def broken(container, request, cancelled, deadline):
        result = initial(container, request, cancelled, deadline)
        if "s0-c2" in container.config["name"]:
            result.update(exit_code=1, stderr=f"{kind}: invalid input")
        return result
    monkeypatch.setattr(worker, "_capture_receipt", broken)
    receipt = await worker.validate_skill_scripts({"scripts/summary.py": "# Immutable artificial code"}, rejection_plan(),
        SimpleNamespace(sandbox_image="existing:sandbox"))
    invalid = receipt["cases"][-1]
    assert invalid["input_unchanged"] is True and "runtime_error_type" not in invalid
    assert invalid["status"] == ("passed" if kind == "TypeError" else "failed")
    if kind == "NameError":
        assert invalid["reason"] == "program_runtime_failure"


@pytest.mark.asyncio
async def test_scientific_mismatch_is_not_reclassified_as_a_build_failure(runtime):
    receipt = await worker.validate_skill_scripts({"scripts/summary.py": "# Immutable artificial code"}, plan(expected=5),
        SimpleNamespace(sandbox_image="existing:sandbox"))
    assert all(case["reason"] == "assertion_mismatch" and case["checks"] for case in receipt["cases"])
    assert all("runtime_error_type" not in case for case in receipt["cases"])
