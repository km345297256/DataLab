import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.domain.models.tool_result import ToolResult
from app.domain.services.tools.shell import ShellToolkit
from app.domain.services.tools.tool_contract import resolved_tool_is_read_only


def observation(module="scipy.stats"):
    return {"schema": "python-api-inspection/v1", "module": module,
            "state": "observed", "members": [{"name": "ttest_ind", "state": "observed"}]}


def sandbox_result(payload):
    return ToolResult(success=True, data={"status": "completed", "returncode": 0,
                                         "output": json.dumps(payload)})


def toolkit(result):
    sandbox = SimpleNamespace(exec_program=AsyncMock(return_value=result),
                              exec_command=AsyncMock(), kill_process=AsyncMock())
    return ShellToolkit(sandbox), sandbox


@pytest.mark.asyncio
async def test_inspects_via_saved_program_and_literal_arguments_without_shell():
    kit, sandbox = toolkit(sandbox_result(observation()))
    result = await ShellToolkit.python_inspect.coroutine(
        kit, id="api", module="scipy.stats", names=["ttest_ind", "bootstrap"])
    assert result.success and result.data["inspection"] == observation()
    args = sandbox.exec_program.call_args.args
    assert args[:3] == ("api", "/home/ubuntu", "/usr/local/lib/ai-dataseek/python_inspect.py")
    assert json.loads(args[3][0]) == {"module": "scipy.stats", "names": ["ttest_ind", "bootstrap"]}
    sandbox.exec_command.assert_not_awaited()


def test_import_initialization_is_not_classified_as_read_only():
    kit, _ = toolkit(sandbox_result(observation()))
    tool = kit.get_tool("python_inspect")
    assert not resolved_tool_is_read_only(tool)
    assert set(tool.execution_contract["effects"]) == {"sandbox_read", "sandbox_write", "network"}
    assert tool.execution_contract["cancellable"] is True
    for module in ["custom_science.engine", "not_yet_installed", "statsmodels.regression.linear_model"]:
        assert tool.args_schema.model_validate({"id": "api", "module": module}).module == module
    with pytest.raises(ValidationError):
        tool.args_schema.model_validate({"id": "api", "module": "os; exit()"})
    with pytest.raises(ValidationError):
        tool.args_schema.model_validate({"id": "api", "module": "math", "names": ["sin"] * 9})


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["__import__('os')", "sin()", "../../data", "a\nb", "x" * 257])
async def test_expression_or_invalid_attribute_never_starts_a_process(name):
    kit, sandbox = toolkit(sandbox_result(observation()))
    result = await ShellToolkit.python_inspect.coroutine(kit, id="api", module="math", names=[name])
    assert not result.success and result.data["side_effect_state"] == "not_started"
    sandbox.exec_program.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("output", ["prefix\n{}", "[... omitted ...]", "x" * 30001,
                                     json.dumps(observation("different_module"))],
                         ids=["polluted", "truncated", "oversized", "wrong-module"])
async def test_incomplete_or_wrong_observations_are_never_accepted(output):
    kit, _ = toolkit(ToolResult(success=True, data={"status": "completed", "returncode": 0, "output": output}))
    result = await ShellToolkit.python_inspect.coroutine(kit, id="api", module="scipy.stats")
    assert not result.success and result.data["error_code"] == "api_inspection_unavailable"


@pytest.mark.asyncio
async def test_missing_import_is_an_observation_not_a_failed_analysis():
    report = {**observation(), "state": "import_failed", "error_type": "ImportError", "members": []}
    kit, _ = toolkit(sandbox_result(report))
    result = await ShellToolkit.python_inspect.coroutine(kit, id="api", module="scipy.stats")
    assert result.success and result.data["inspection"]["state"] == "import_failed"


@pytest.mark.asyncio
async def test_unconfirmed_execution_is_preserved_without_relaunch():
    pending = ToolResult(success=False, data={"status": "unknown", "returncode": None})
    kit, sandbox = toolkit(pending)
    result = await ShellToolkit.python_inspect.coroutine(kit, id="api", module="math")
    assert result is pending
    assert sandbox.exec_program.await_count == 1


@pytest.mark.asyncio
async def test_existing_sandbox_without_helper_gets_scoped_fallback_guidance():
    kit, sandbox = toolkit(ToolResult(success=False, data={"program_preparation": {
        "version": 1, "phase": "not_started", "reason": "script_missing"}}))
    result = await ShellToolkit.python_inspect.coroutine(kit, id="api", module="math")
    assert result.data["error_code"] == "api_inspection_not_installed"
    assert "writable task space" in result.message
    assert sandbox.exec_program.await_count == 1


@pytest.mark.asyncio
async def test_probe_timeout_cancels_process_instead_of_relaunching(monkeypatch):
    kit, sandbox = toolkit(None)
    async def pending(*args):
        await asyncio.Future()
    sandbox.exec_program.side_effect = pending
    original_timeout = asyncio.timeout
    monkeypatch.setattr(asyncio, "timeout", lambda seconds: original_timeout(0.01))
    result = await ShellToolkit.python_inspect.coroutine(kit, id="api", module="math")
    assert result.data["error_code"] == "api_inspection_timeout"
    assert sandbox.exec_program.await_count == 1
    sandbox.kill_process.assert_awaited_once_with("api")


@pytest.mark.asyncio
async def test_cancellation_kills_same_inspection_and_propagates():
    kit, sandbox = toolkit(None)
    entered = asyncio.Event()
    async def pending(*args):
        entered.set()
        await asyncio.Future()
    sandbox.exec_program.side_effect = pending
    task = asyncio.create_task(ShellToolkit.python_inspect.coroutine(kit, id="api", module="math"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    sandbox.kill_process.assert_awaited_once_with("api")
