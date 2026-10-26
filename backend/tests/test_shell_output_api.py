"""Shell pagination remains a session-authorized, read-only observation."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.application.services.agent_service import AgentService
from app.domain.models.tool_result import ToolResult
from app.interfaces.api.session_routes import view_shell
from app.interfaces.schemas.session import ShellViewRequest, ShellViewResponse


@pytest.mark.parametrize("fields", [
    {"output_id": "a" * 32}, {"cursor": 0}, {"output_id": "invalid", "cursor": 0},
    {"output_id": "a" * 32, "cursor": -1}, {"output_id": "a" * 32, "cursor": True},
    {"max_bytes": 3}, {"max_bytes": 16385}, {"max_bytes": True},
])
def test_pagination_rejects_incomplete_identity_or_unbounded_reads(fields):
    with pytest.raises(ValidationError):
        ShellViewRequest(session_id="shell", **fields)


@pytest.mark.asyncio
async def test_shell_route_forwards_bound_cursor_and_authenticated_owner():
    service = SimpleNamespace(shell_view=AsyncMock(return_value=ShellViewResponse(output="page", session_id="shell")))
    request = ShellViewRequest(session_id="shell", output_id="a" * 32, cursor=8192, max_bytes=4096)
    await view_shell("conversation", request, SimpleNamespace(id="owner"), service)
    service.shell_view.assert_awaited_once_with(
        "conversation", "shell", "owner", output_id="a" * 32, cursor=8192, max_bytes=4096,
    )


@pytest.mark.asyncio
async def test_shell_page_preserves_explicit_status_and_excludes_private_execution_receipts():
    service = AgentService.__new__(AgentService)
    session = SimpleNamespace(id="conversation")
    service._session_repository = SimpleNamespace(find_by_id_and_user_id=AsyncMock(return_value=session))
    payload = {
        "output": "page", "session_id": "shell", "console_truncated": True,
        "console": [{"ps1": "$", "command": "python task.py", "output": "page", "output_truncated": True,
                     "status": "completed", "returncode": 1}],
        "output_metadata": {"output_id": "a" * 32, "total_bytes": 9000, "stream_complete": True},
        "output_page": {"cursor": 8192, "start": 8192, "next_cursor": 9000, "lossy": False, "source": "spool", "eof": True},
        "program_execution": {"private_source": "never expose"},
    }
    sandbox = SimpleNamespace(view_shell=AsyncMock(return_value=ToolResult(success=True, data=payload)))
    service._restore_session_sandbox = AsyncMock(return_value=sandbox)
    result = await service.shell_view("conversation", "shell", "owner", output_id="a" * 32, cursor=8192)
    service._session_repository.find_by_id_and_user_id.assert_awaited_once_with("conversation", "owner")
    sandbox.view_shell.assert_awaited_once_with("shell", console=True, output_id="a" * 32, cursor=8192, max_bytes=8192)
    assert result.output_page["next_cursor"] == 9000
    assert result.output_metadata["output_id"] == "a" * 32
    assert result.console_truncated and result.console[0].output_truncated
    assert result.console[0].returncode == 1
    assert "private_source" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_shell_page_cannot_restore_another_users_session():
    service = AgentService.__new__(AgentService)
    service._session_repository = SimpleNamespace(find_by_id_and_user_id=AsyncMock(return_value=None))
    service._restore_session_sandbox = AsyncMock()
    with pytest.raises(RuntimeError, match="Session not found"):
        await service.shell_view("conversation", "shell", "other", output_id="a" * 32, cursor=0)
    service._restore_session_sandbox.assert_not_awaited()


def test_console_without_an_explicit_exit_code_does_not_invent_success():
    response = ShellViewResponse(output="text", session_id="shell", console=[{"ps1": "$", "command": "run", "output": "text"}])
    assert response.console[0].returncode is None
    assert response.console[0].status is None
