from unittest.mock import AsyncMock

import pytest

from app.domain.models.tool_result import ToolResult
from app.domain.services.program_execution import (
    consume_program_feedback, program_preparation_failure, trusted_program_prerequisites,
)
from app.domain.services.tools.shell import ShellToolkit
from test_program_prelaunch_recovery import make_sandbox, run_call


def dependencies(*, missing=True):
    return {"version": 1, "check": "static_import_resolution", "state": "missing" if missing else "ready",
            "required_modules": ["sklearn"], "missing_modules": ["sklearn"] if missing else [],
            "optional_modules": ["optional_backend"], "optional_missing_modules": ["optional_backend"],
            "dynamic_imports": False, "runtime": {"python_version": "3.10.12", "implementation": "CPython",
                "packages": [{"module": "sklearn", "distribution": "scikit-learn",
                              "version": None if missing else "1.7.2", "available": not missing}],
                "json_writer": "datalab_json.write_json"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [True, False])
async def test_dependency_preflight_is_consumed_and_reports_real_capabilities(missing):
    sandbox = make_sandbox()
    tool = next(item for item in ShellToolkit(sandbox).tools if item.name == "program_run")
    data = {"version": 1, "status": "blocked" if missing else "ready", "ready": not missing,
            "prerequisite_digest": "a" * 64, "cwd": {"state": "ready"},
            "source": {"state": "ready", "source_digest": "b" * 64},
            "syntax": {"state": "ready"}, "dependencies": dependencies(missing=missing)}
    sandbox.program_preflight = AsyncMock(return_value=ToolResult(success=True, data=data))
    observed = await trusted_program_prerequisites(tool, run_call())
    assert observed["ready"] is (not missing)
    result = program_preparation_failure(tool, run_call(), observed)
    if missing:
        assert result["error_code"] == "missing_dependencies" and result["side_effect_state"] == "not_started"
        assert result["program_preflight"]["dependencies"]["missing_modules"] == ["sklearn"]
        issue = result["program_preflight"]["issues"][0]
        assert issue["action"] == "prepare_runtime_dependencies" and issue["tool"] == "shell_run"
        assert "All libraries and APIs are admitted" in issue["instruction"]
        assert "Do not install packages" not in issue["instruction"]
    else:
        assert result is None  # Optional import failures never block an installed primary implementation.


@pytest.mark.asyncio
async def test_malformed_dependency_report_cannot_claim_ready():
    sandbox = make_sandbox()
    tool = next(item for item in ShellToolkit(sandbox).tools if item.name == "program_run")
    deps = dependencies()
    deps["state"] = "ready"
    sandbox.program_preflight = AsyncMock(return_value=ToolResult(success=True, data={
        "version": 1, "status": "ready", "ready": True, "prerequisite_digest": "a" * 64,
        "cwd": {"state": "ready"}, "source": {"state": "ready", "source_digest": "b" * 64},
        "syntax": {"state": "ready"}, "dependencies": deps}))
    assert await trusted_program_prerequisites(tool, run_call()) is None


def test_launch_feedback_accepts_bounded_runtime_report_and_still_strips_private_source():
    from types import SimpleNamespace
    attempt = SimpleNamespace(program_execution=None)
    result = ToolResult(success=True, data={"program_execution": {
        "version": 1, "script_path": "/home/ubuntu/scripts/example.py", "source_digest": "a" * 64,
        "returncode": 0, "failure_fingerprint": None, "diagnostic": None, "output_truncated": False,
        "source_snapshot": {"content": "PRIVATE"}, "dependencies": dependencies(missing=False)}})
    public = consume_program_feedback(result, attempt)
    assert attempt.program_execution["dependencies"]["runtime"]["packages"][0]["version"] == "1.7.2"
    assert "PRIVATE" not in public.model_dump_json()
