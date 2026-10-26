"""Validate structured edits before dispatch and preserve private API receipts."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.domain.models.tool_result import ToolResult
from app.domain.services.tools.file import FileToolkit
from app.domain.services.tools.tool_contract import ToolContractError, validate_tool_arguments
from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox


def call(**changes):
    args = {"file": "/home/ubuntu/scripts/analysis.py", "base_sha256": "a" * 64,
            "replacements": [{"old_str": "old", "new_str": "new", "expected_count": 1}],
            "syntax_check": "python", **changes}
    return {"id": "patch-1", "name": "file_apply_patch", "args": args}


@pytest.mark.asyncio
async def test_patch_tool_preserves_exact_structured_edits():
    sandbox = SimpleNamespace(file_apply_patch=AsyncMock(return_value=ToolResult(success=True)))
    tool = FileToolkit(sandbox).get_tool("file_apply_patch")
    result = await tool.ainvoke(call())
    assert result.status == "success"
    assert sandbox.file_apply_patch.await_args.kwargs == call()["args"]
    assert tool.execution_contract["effects"] == ["sandbox_write"]


@pytest.mark.parametrize("changes", [{"file": " "}, {"base_sha256": "partialhash"},
    {"replacements": []}, {"replacements": [{"old_str": "", "new_str": "x", "expected_count": 1}]},
    {"replacements": [{"old_str": "x", "new_str": "y", "expected_count": True}]},
    {"replacements": [{"old_str": "x", "new_str": "y", "expected_count": 0}]},
    {"syntax_check": "execute"}, {"sudo": True}])
def test_invalid_patch_rejected_before_dispatch(changes):
    tool = FileToolkit(SimpleNamespace()).get_tool("file_apply_patch")
    with pytest.raises(ToolContractError):
        validate_tool_arguments(tool, call(**changes))


@pytest.mark.asyncio
async def test_versioned_read_flag_is_forwarded_only_when_requested():
    sandbox = SimpleNamespace(file_read=AsyncMock(return_value=ToolResult(success=True)))
    tool = FileToolkit(sandbox).get_tool("file_read")
    await tool.ainvoke({"id": "read", "args": {"file": "/home/ubuntu/scripts/a.py", "include_version": True}})
    assert sandbox.file_read.await_args.kwargs["include_version"] is True
    await tool.ainvoke({"id": "read-old", "args": {"file": "/home/ubuntu/scripts/a.py"}})
    assert "include_version" not in sandbox.file_read.await_args.kwargs


@pytest.mark.asyncio
async def test_docker_wrapper_preserves_stale_receipt_without_retry():
    sandbox = object.__new__(DockerSandbox)
    sandbox.base_url = "http://sandbox"
    payload = {"success": False, "message": "File version changed",
               "data": {"error_code": "version_conflict", "changed": False, "side_effect_state": "not_started"}}
    sandbox.client = SimpleNamespace(post=AsyncMock(return_value=httpx.Response(409, json=payload)))
    result = await sandbox.file_apply_patch(**call()["args"])
    assert result.success is False and result.data == payload["data"]
    assert sandbox.client.post.await_count == 1
    assert sandbox.client.post.await_args.args == ("http://sandbox/api/v1/file/apply-patch",)


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', [True, False])
async def test_agent_native_patch_accounts_only_for_actual_modification(monkeypatch, changed):
    from app.domain.services.agents.execution import ExecutionAgent
    from app.interfaces.schemas.event import ToolSSEEvent
    from test_artifact_write_agent_flow import call as model_call, final, memory_files, completed, PROGRAM
    from test_tool_response_protocol_recovery import make_agent

    # Two individually non-identity replacements may still leave final bytes
    # unchanged. Only the native receipt can establish this after dispatch.
    replacements = [{'old_str': 'a', 'new_str': '', 'expected_count': 1},
                    {'old_str': 'b', 'new_str': 'ab' if not changed else 'abc', 'expected_count': 1}]
    agent, _, _, _ = make_agent(monkeypatch, [model_call('file_apply_patch', 'transaction',
        file=PROGRAM, base_sha256='a' * 64, replacements=replacements), final()], agent_type=ExecutionAgent)
    sandbox, _, _ = memory_files(agent, {PROGRAM: 'ab'})
    sandbox.file_apply_patch = AsyncMock(return_value=ToolResult(success=True, data={
        'file': PROGRAM, 'changed': changed, 'write_action': 'patch' if changed else 'reuse',
        'side_effect_state': 'completed' if changed else 'not_started'}))
    events = [event async for event in agent.execute('修正已有脚本中的两个位置')]
    observed = completed(events, 'transaction')
    assert sandbox.file_apply_patch.await_count == 1
    assert observed.dispatch_status == (None if changed else 'not_started')
    assert sum(state.revision for state in agent._artifact_writes._artifacts.values()) == int(changed)
    assert sum(state.revision for state in agent._analysis_progress.programs.values()) == int(changed)
    public = (await ToolSSEEvent.from_event_async(observed)).data
    assert public.write_action == (None if changed else 'reuse')
