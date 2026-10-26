"""Creation failure is a recoverable authoring result, not a new analysis verdict."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.domain.models.event import DoneEvent, ErrorEvent, MessageEvent, ToolEvent, ToolStatus
from app.domain.services.flows.plan_act import PlanActFlow
from app.domain.services.skills.registry import SkillRegistry
from app.domain.services.skills.skill_creation_error import SkillCreationError
from app.domain.services.tools.skill import SkillToolkit
from app.interfaces.schemas.event import EventMapper


def creation_flow(tmp_path):
    registry = SkillRegistry(str(tmp_path / "skills"), user_id="owner")
    flow = PlanActFlow.__new__(PlanActFlow)
    flow._session_id = "private-session"
    flow.skill_toolkit = SkillToolkit(registry, session_id=flow._session_id,
                                    user_id="owner", session_repository=SimpleNamespace())
    return flow


@pytest.mark.asyncio
@pytest.mark.parametrize("stage,code", [
    ("source_analysis", "analysis_not_ready"),
    ("source_analysis", "source_evidence_invalid"),
    ("method_requirements", "source_exclusion_overlap"),
    ("implementation", "script_syntax_invalid"),
    ("synthetic_execution", "synthetic_execution_failed"),
    ("package_storage", "storage_unavailable"),
    ("package_storage", "package_too_large"),
])
async def test_expected_creation_failure_closes_command_without_changing_analysis(
    tmp_path, monkeypatch, stage, code,
):
    error = SkillCreationError(stage, code)
    creator = AsyncMock(side_effect=error)
    monkeypatch.setattr("app.domain.services.tools.skill.create_skill_from_session", creator)
    flow = creation_flow(tmp_path)
    source = MessageEvent(message="原分析已交付。", metadata={"analysis_outcome": {"status": "succeeded"}})
    source_before = source.model_dump()

    events = [event async for event in flow._run_skill_create_command()]

    creator.assert_awaited_once()  # no replay or automatic creation loop
    assert sum(isinstance(event, DoneEvent) for event in events) == 1
    assert not any(isinstance(event, ErrorEvent) for event in events)
    tool = next(event for event in events if isinstance(event, ToolEvent) and event.status == ToolStatus.CALLED)
    assert tool.function_result.success is False
    assert tool.function_result.data["skill_creation_failure"]["code"] == code
    message = next(event for event in events if isinstance(event, MessageEvent))
    assert error.public_message in message.message
    assert "已有分析结果保持不变" in message.message
    assert "analysis_outcome" not in message.metadata
    assert message.metadata["skill_creation_failure"] == {
        "stage": stage, "code": code, "retryable": error.retryable,
    }
    assert ("重试创建技能" in message.message) == error.retryable
    assert "Task error" not in message.message
    assert "private-session" not in message.message
    assert source.model_dump() == source_before
    assert flow.skill_toolkit.registry.list_skills() == []
    # Public live and restored messages both retain the same recovery metadata.
    mapped = await EventMapper.event_to_sse_event(message)
    restored = await EventMapper.event_to_sse_event(MessageEvent.model_validate_json(message.model_dump_json()))
    assert mapped.data.metadata == restored.data.metadata == message.metadata


@pytest.mark.asyncio
async def test_each_explicit_retry_has_a_distinct_tool_identity(tmp_path, monkeypatch):
    monkeypatch.setattr("app.domain.services.tools.skill.create_skill_from_session",
                        AsyncMock(side_effect=SkillCreationError("implementation", "script_syntax_invalid")))
    flow = creation_flow(tmp_path)
    first = [event async for event in flow._run_skill_create_command()]
    second = [event async for event in flow._run_skill_create_command()]
    first_ids = {event.tool_call_id for event in first if isinstance(event, ToolEvent)}
    second_ids = {event.tool_call_id for event in second if isinstance(event, ToolEvent)}
    assert len(first_ids) == len(second_ids) == 1
    assert first_ids.isdisjoint(second_ids)


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError("unknown failure"), ValueError("unexpected invariant")])
async def test_unknown_creator_failures_are_not_reported_as_success(tmp_path, monkeypatch, error):
    monkeypatch.setattr("app.domain.services.tools.skill.create_skill_from_session", AsyncMock(side_effect=error))
    events = []
    with pytest.raises(type(error), match=str(error)):
        async for event in creation_flow(tmp_path)._run_skill_create_command():
            events.append(event)
    assert not any(isinstance(event, (DoneEvent, MessageEvent)) for event in events)


@pytest.mark.asyncio
async def test_rest_creation_uses_fixed_public_failure_contract(monkeypatch):
    from app.interfaces.api import skill_routes
    from app.interfaces.schemas.skill import CreateSkillRequest

    error = SkillCreationError("method_requirements", "source_exclusion_overlap")
    monkeypatch.setattr(skill_routes, "create_session_skill", AsyncMock(side_effect=error))
    monkeypatch.setattr(skill_routes, "MongoSessionRepository", lambda: SimpleNamespace())
    monkeypatch.setattr(skill_routes, "_registry", lambda user_id: SimpleNamespace())
    with pytest.raises(HTTPException) as caught:
        await skill_routes.create_skill_from_session(
            CreateSkillRequest(session_id="private-session"), SimpleNamespace(id="owner"),
            SimpleNamespace(default_workspace_id=AsyncMock(return_value="workspace")),
            SimpleNamespace(record=AsyncMock()),
        )
    assert caught.value.status_code == 400
    assert caught.value.detail == error.public_message


@pytest.mark.asyncio
@pytest.mark.parametrize("stage,code", [
    ("method_requirements", "source_exclusion_overlap"),
    ("source_analysis", "source_evidence_invalid"),
])
async def test_runner_does_not_reclassify_creation_rejection_as_failed_analysis(tmp_path, monkeypatch, stage, code):
    from app.application.services.dataset_request_resolver import ExecutionDecision, FrontControllerResolution, RequestDecision
    from app.domain.models.message import Message
    from app.domain.models.safety import SafetyReview
    from app.domain.services.agent_task_runner import AgentTaskRunner
    from app.domain.services.flows.plan_act import AgentStatus

    monkeypatch.setattr("app.domain.services.tools.skill.create_skill_from_session",
                        AsyncMock(side_effect=SkillCreationError(stage, code)))
    command = creation_flow(tmp_path)

    class Flow:
        status = AgentStatus.IDLE
        plan = None

        async def run(self, message):
            async for event in command._run_skill_create_command():
                yield event

    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._agent_id, runner._session_id = "agent", "private-session"
    runner._flow = Flow()
    runner._front_controller_resolution = FrontControllerResolution(
        decision=RequestDecision(safety=SafetyReview(decision="allow", risk_level="low"),
                                 execution=ExecutionDecision(mode="sandbox", required_evidence="file_content")),
        answer="", controller_metadata={},
    )
    runner._session_repository = SimpleNamespace(find_by_id=AsyncMock(return_value=None))
    runner._generated_files = []
    runner._record_safety_audit = AsyncMock()
    runner._initialize_mcp_tool = AsyncMock()
    runner._handle_tool_event = AsyncMock()
    runner._sync_discovered_artifacts_to_storage = AsyncMock(return_value=[])

    events = [event async for event in runner._run_flow(Message(message="/skill-creator"))]

    assert isinstance(events[-1], DoneEvent)
    assert not any(isinstance(event, ErrorEvent) for event in events)
    messages = [event for event in events if isinstance(event, MessageEvent)]
    assert len(messages) == 1
    assert messages[0].metadata["skill_creation_failure"]["code"] == code
    assert "analysis_outcome" not in messages[0].metadata
    runner._sync_discovered_artifacts_to_storage.assert_not_awaited()
