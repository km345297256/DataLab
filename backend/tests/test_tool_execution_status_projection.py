import pytest

from app.domain.models.event import ToolEvent, ToolStatus
from app.domain.models.tool_result import ToolResult
from app.interfaces.schemas.event import ToolSSEEvent


@pytest.mark.asyncio
@pytest.mark.parametrize("result,expected", [
    (ToolResult(success=True), "succeeded"),
    (ToolResult(success=False, message="private failure"), "failed"),
    ({"success": False, "data": {"secret": "hidden"}}, "failed"),
    ({"data": {"success": True}}, None),
    ({"success": "true"}, None),
    (None, None),
])
async def test_public_tool_status_is_exact_result_not_terminal_event(result, expected):
    event = ToolEvent(tool_call_id="revision", tool_name="file", function_name="file_write",
                      function_args={"file": "/home/ubuntu/output/test.py"},
                      status=ToolStatus.CALLED, function_result=result)
    public = await ToolSSEEvent.from_event_async(event)
    assert public.data.execution_status == expected
    assert "private failure" not in public.model_dump_json()
    assert "hidden" not in public.model_dump_json()


@pytest.mark.asyncio
async def test_calling_event_does_not_publish_terminal_success():
    event = ToolEvent(tool_call_id="revision", tool_name="file", function_name="file_write",
                      function_args={}, status=ToolStatus.CALLING,
                      function_result=ToolResult(success=True))
    assert (await ToolSSEEvent.from_event_async(event)).data.execution_status is None


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped", [False, True])
async def test_host_confirmed_successful_noop_projects_as_reuse_not_completed_write(wrapped):
    data = {"changed": False, "write_action": "reuse", "side_effect_state": "not_started"}
    result = ToolResult(success=True, data=data) if wrapped else {"success": True, **data}
    event = ToolEvent(tool_call_id="noop", tool_name="file", function_name="file_write",
        function_args={"file": "/home/ubuntu/report.md"}, status=ToolStatus.CALLED,
        function_result=result, dispatch_status="not_started")
    public = (await ToolSSEEvent.from_event_async(event)).data
    assert public.execution_status == "not_started" and public.write_action == "reuse"
    event.dispatch_status = None
    public = (await ToolSSEEvent.from_event_async(event)).data
    assert public.execution_status == "succeeded" and public.write_action is None
