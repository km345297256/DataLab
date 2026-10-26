"""Production progress wiring persists safe stages without inventing success."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.domain.models.event import ToolEvent, ToolStatus
from app.domain.models.skill_creation_progress import SkillCreationProgressView
from app.domain.services.skills.skill_creation.progress import SkillCreationProgressRecorder
from app.domain.services.skills.skill_creation_error import SkillCreationError
from app.domain.services.skills.registry import SkillRegistry
from app.domain.services.tools.skill import SkillToolkit
from app.interfaces.schemas.event import ToolSSEEvent


def progress(stage="workflow", status="running", attempt=1, reason=None):
    return SimpleNamespace(stage=stage, status=status, attempt=attempt, reason=reason,
                           candidate="PRIVATE CANDIDATE", private_path="/srv/private/source.csv")


@pytest.mark.asyncio
async def test_tool_wires_safe_progress_to_the_same_host_call_and_roundtrips_history(tmp_path, monkeypatch):
    sink = AsyncMock()
    toolkit = SkillToolkit(SkillRegistry(str(tmp_path / "skills"), user_id="owner"),
        session_id="private-session", user_id="owner", session_repository=SimpleNamespace())
    toolkit.creation_progress_sink = sink

    async def create(**kwargs):
        assert kwargs["user_id"] == "owner"
        await kwargs["on_progress"](progress())
        await kwargs["on_progress"](progress(status="repairing", reason="invalid_schema"))
        raise SkillCreationError("workflow", "invalid_schema")

    monkeypatch.setattr("app.domain.services.tools.skill.create_skill_from_session", create)
    result = await toolkit.get_tool("skill_create_from_session").ainvoke({"id": "current-host-call", "args": {}})
    assert result.artifact.success is False
    events = [call.args[0] for call in sink.await_args_list]
    assert [event.skill_creation_progress.status for event in events] == ["running", "repairing", "failed"]
    assert {event.tool_call_id for event in events} == {"current-host-call"}
    for event in events:
        assert event.status == ToolStatus.CALLING and event.function_result is None
        encoded = event.model_dump_json()
        assert "PRIVATE" not in encoded and "/srv" not in encoded and "private-session" not in encoded
        public = await ToolSSEEvent.from_event_async(ToolEvent.model_validate_json(encoded))
        assert public.data.skill_creation_progress == event.skill_creation_progress
        assert public.data.execution_status is None and public.data.skill_creation is None


@pytest.mark.asyncio
async def test_rest_wires_owner_scoped_audit_without_writing_source_session_events(monkeypatch):
    from app.interfaces.api import skill_routes
    from app.interfaces.schemas.skill import CreateSkillRequest

    repository = SimpleNamespace(add_event=AsyncMock())
    audit = SimpleNamespace(record=AsyncMock())
    monkeypatch.setattr(skill_routes, "MongoSessionRepository", lambda: repository)
    monkeypatch.setattr(skill_routes, "_registry", lambda _: SimpleNamespace())

    async def create(**kwargs):
        await kwargs["on_progress"](progress(status="repairing", reason="private_package_content"))
        raise SkillCreationError("workflow", "private_package_content")

    monkeypatch.setattr(skill_routes, "create_session_skill", create)
    with pytest.raises(HTTPException) as caught:
        await skill_routes.create_skill_from_session(CreateSkillRequest(session_id="owned-session"),
            SimpleNamespace(id="owner"), SimpleNamespace(default_workspace_id=AsyncMock(return_value="workspace")), audit)
    assert caught.value.status_code == 400
    records = [call.kwargs for call in audit.record.await_args_list]
    assert len(records) == 2 and len({record["resource_id"] for record in records}) == 1
    for record in records:
        assert record["actor_user_id"] == "owner" and record["session_id"] == "owned-session"
        assert record["action"] == "skill.creation_progress"
        assert set(record["metadata"]) == {"stage", "status", "attempt", "code"}
        assert "PRIVATE" not in str(record) and "/srv" not in str(record)
    assert records[-1]["metadata"]["status"] == "failed"
    repository.add_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_records_current_stage_and_propagates_without_retry():
    sink, started = AsyncMock(), asyncio.Event()
    recorder = SkillCreationProgressRecorder(sink)
    async def operation(callback):
        await callback(progress(stage="implementation", attempt=2))
        started.set()
        await asyncio.Event().wait()
    task = asyncio.create_task(recorder.run(operation))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    views = [call.args[0] for call in sink.await_args_list]
    assert [view.status for view in views] == ["running", "cancelled"]
    assert views[-1].stage == "implementation" and views[-1].attempt == 2


@pytest.mark.asyncio
async def test_progress_write_failure_stops_dependent_generation_and_keeps_original_error():
    sink = AsyncMock(side_effect=ConnectionError("private storage detail"))
    recorder = SkillCreationProgressRecorder(sink)
    continued = False
    async def operation(callback):
        nonlocal continued
        await callback(progress())
        continued = True
    with pytest.raises(ConnectionError):
        await recorder.run(operation)
    assert not continued


@pytest.mark.asyncio
async def test_success_terminal_is_recorded_only_after_draft_commit_and_preserves_result():
    sink = AsyncMock()
    recorder = SkillCreationProgressRecorder(sink)
    draft = object()
    async def operation(callback):
        await callback(progress(stage="method_requirements", status="passed", attempt=0))
        assert sink.await_args.args[0].stage == "method_requirements"
        return draft
    assert await recorder.run(operation) is draft
    assert sink.await_args.args[0].stage == "package_storage"
    assert sink.await_args.args[0].status == "passed"


@pytest.mark.parametrize("change", [
    {"stage": "PRIVATE SOURCE"}, {"code": "/srv/private"}, {"status": "raw model status"},
    {"attempt": True}, {"attempt": -1}, {"candidate": "private source"},
])
def test_progress_model_rejects_untrusted_strings_and_extra_values(change):
    with pytest.raises(ValidationError):
        SkillCreationProgressView.model_validate({"stage": "workflow", "status": "running", "attempt": 1, **change})


@pytest.mark.asyncio
async def test_plugin_result_cannot_forge_host_progress():
    view = SkillCreationProgressView(stage="workflow", status="passed", attempt=1)
    event = ToolEvent(tool_call_id="plugin", tool_name="plugin", function_name="skill_create_from_session",
        function_args={}, status=ToolStatus.CALLED, function_result={"success": True, "skill_creation_progress": view.model_dump()})
    assert (await ToolSSEEvent.from_event_async(event)).data.skill_creation_progress is None


@pytest.mark.asyncio
async def test_review_response_revision_and_acceptance_remain_distinct_in_durable_progress():
    sink = AsyncMock()
    recorder = SkillCreationProgressRecorder(sink)
    for status in ("response_received", "revision_required", "accepted"):
        await recorder.record(progress(stage="method_review", status=status))
    views = [call.args[0] for call in sink.await_args_list]
    assert [SkillCreationProgressView.model_validate_json(view.model_dump_json()).status for view in views] == [
        "response_received", "revision_required", "accepted"]
    assert all(view.stage == "method_review" for view in views)
