from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.models.event import ToolEvent, ToolStatus
from app.domain.models.tool_result import ToolResult
from app.domain.services import agent_task_runner
from app.domain.services.agent_task_runner import AgentTaskRunner


def event(name="geoscience_trend", *, success=True):
    return ToolEvent(
        tool_call_id="call-1",
        tool_name="plugin",
        function_name=name,
        function_args={},
        status=ToolStatus.CALLED,
        function_result=ToolResult(success=success),
    )


@pytest.mark.asyncio
async def test_successful_scientific_tool_reports_sso_linked_dataset_once(monkeypatch):
    runner = object.__new__(AgentTaskRunner)
    runner._active_datasets = [SimpleNamespace(
        dataset_id="tds-1",
        name="气象数据集",
        metadata={"sso_uid": "user-1"},
    )]
    runner._reported_analysis_tool_usage = set()
    report = AsyncMock()
    monkeypatch.setattr(agent_task_runner, "record_analysis_tool_usage", report)

    await runner._report_analysis_tool_usage(event())
    await runner._report_analysis_tool_usage(event())

    report.assert_awaited_once_with(
        uid="user-1",
        title="气象数据集",
        tool_id="geoscience_trend",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_event", [
    event("shell_run"),
    event(success=False),
])
async def test_unrelated_or_failed_tool_is_not_reported(monkeypatch, tool_event):
    runner = object.__new__(AgentTaskRunner)
    runner._active_datasets = [SimpleNamespace(
        dataset_id="tds-1",
        name="气象数据集",
        metadata={"sso_uid": "user-1"},
    )]
    runner._reported_analysis_tool_usage = set()
    report = AsyncMock()
    monkeypatch.setattr(agent_task_runner, "record_analysis_tool_usage", report)

    await runner._report_analysis_tool_usage(tool_event)

    report.assert_not_awaited()
