"""Durable acquisition submission is distinct from validated analysis delivery."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.application.services import open_data_service
from app.application.services.dataset_request_resolver import (
    DatasetRequestResolver, ExecutionDecision, FrontControllerResolution, RequestDecision,
)
from app.domain.models.analysis_outcome import DeliverableRequirement
from app.domain.models.event import DoneEvent, MessageEvent, StepEvent, StepStatus, ToolEvent, ToolStatus
from app.domain.models.file import FileInfo
from app.domain.models.message import Message
from app.domain.models.plan import Plan, Step
from app.domain.models.safety import SafetyReview
from app.domain.services.flows.plan_act import AgentStatus, PlanActFlow
from app.domain.services.open_data_completion import read_acquisition_evidence
from test_analysis_delivery_flow import artifact, runner_for


def acquired_event(**changes):
    values = dict(tool_call_id="call", tool_name="data_discovery", function_name="data_acquire", status=ToolStatus.CALLED,
        function_args={"search_id": "search", "candidate_id": "candidate", "file_ids": ["option"]},
        function_result={"success": True, "data": {"id": "job", "status": "ready"}})
    values.update(changes)
    return ToolEvent(**values)


def job(**changes):
    payload = dict(id="job", session_id="session", search_id="search", candidate_id="candidate",
                   status="queued", items=[{"id": "option"}], files=[])
    payload.update(changes)
    return SimpleNamespace(model_dump=lambda **_: payload)


def service(monkeypatch, result=None, error=None):
    fake = SimpleNamespace(get_acquisition=AsyncMock(return_value=result or job(), side_effect=error))
    monkeypatch.setattr(open_data_service, "get_open_data_service", lambda: fake)
    return fake


def background_message():
    message = Message(message="获取这份教育数据")
    message._completion_mode = "background_acquisition"
    return message


def resolution(mode="background_acquisition"):
    return FrontControllerResolution(decision=RequestDecision(
        safety=SafetyReview(decision="allow", risk_level="low"),
        execution=ExecutionDecision(mode="sandbox", required_evidence="user_message", completion_mode=mode)),
        answer="", controller_metadata={})


def test_background_contract_is_explicit_and_cannot_waive_artifacts():
    assert Message(_completion_mode="background_acquisition")._completion_mode == "inline"
    assert ExecutionDecision(mode="sandbox", required_evidence="file_content").completion_mode == "inline"
    pure = resolution().decision
    assert DatasetRequestResolver._normalize_decision(pure, has_datasets=False) is None
    for changes in ({"requires_artifacts": True}, {"deliverables": [DeliverableRequirement(kind="image")]}, {"mode": "direct"}):
        decision = resolution().decision
        for key, value in changes.items():
            setattr(decision.execution, key, value)
        assert DatasetRequestResolver._normalize_decision(decision, has_datasets=False)
    flow = PlanActFlow.__new__(PlanActFlow)
    message = background_message()
    # Even with a mounted dataset, pure acquisition must not enter the analysis
    # fast path whose "download" heuristic requires a sandbox artifact.
    message.datasets = [SimpleNamespace()]
    assert flow._should_use_dataset_fast_path(message) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"session_id": "other"}, {"search_id": "other"}, {"candidate_id": "other"},
    {"items": [{"id": "other"}]}, {"status": "failed"}, {"status": "cancelled"},
])
async def test_tool_claims_need_exact_owned_persistent_job(monkeypatch, changes):
    fake = service(monkeypatch, job(**changes))
    evidence = await read_acquisition_evidence([acquired_event()], owner="owner", session="session", message=background_message())
    assert evidence.unconfirmed and not evidence.jobs
    fake.get_acquisition.assert_awaited_once_with("owner", "session", "job")


@pytest.mark.asyncio
async def test_storage_access_denial_and_spoofed_tool_cannot_confirm_submission(monkeypatch):
    fake = service(monkeypatch, error=PermissionError())
    evidence = await read_acquisition_evidence([acquired_event()], owner="other-owner", session="session", message=background_message())
    assert evidence.unconfirmed and not evidence.jobs
    fake.get_acquisition.assert_awaited_once_with("other-owner", "session", "job")
    fake.get_acquisition.reset_mock()
    evidence = await read_acquisition_evidence([acquired_event(tool_name="untrusted-plugin")], owner="owner", session="session", message=background_message())
    assert not evidence.jobs
    fake.get_acquisition.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("result", ["damaged result", [], 123, None, {"success": True, "data": []}])
async def test_malformed_tool_receipts_fail_closed_without_crashing_finalization(monkeypatch, result):
    fake = service(monkeypatch)
    evidence = await read_acquisition_evidence([acquired_event(function_result=result)],
        owner="owner", session="session", message=background_message())
    assert evidence.unconfirmed and not evidence.jobs
    fake.get_acquisition.assert_not_awaited()


@pytest.mark.asyncio
async def test_partial_submission_preserves_success_and_reports_failed_other_selection(monkeypatch):
    fake = service(monkeypatch)
    failed = acquired_event(function_args={"search_id": "search", "candidate_id": "other", "file_ids": ["other"]},
                            function_result={"success": False, "data": {"reason": "invalid_open_data_request"}})
    step = Step(success=True)
    runner = runner_for(step, [])
    runner._open_data_acquisition_calls = [acquired_event(), failed]
    await runner._finalize_analysis_step(StepEvent(status=StepStatus.COMPLETED, step=step), background_message(), [])
    assert not step.success and step.outcome.status == "partial"
    assert step.outputs["open_data_acquisition"]["jobs"] == [{"id": "job", "status": "queued"}]
    assert "部分" in step.result and "另有获取请求" in step.result
    fake.get_acquisition.assert_awaited_once()


@pytest.mark.asyncio
async def test_successful_explicit_retry_of_exact_selection_replaces_failed_attempt(monkeypatch):
    fake = service(monkeypatch)
    events = [acquired_event(function_result={"success": False}), acquired_event()]
    evidence = await read_acquisition_evidence(events, owner="owner", session="session", message=background_message())
    assert not evidence.unconfirmed and evidence.jobs == [{"id": "job", "status": "queued"}]
    fake.get_acquisition.assert_awaited_once()


@pytest.mark.asyncio
async def test_pure_acquisition_confirms_queue_without_fake_files_or_artifact_repair(monkeypatch):
    service(monkeypatch)
    step = Step(success=True, result="已经全部下载完成")
    runner = runner_for(step, [])
    runner._open_data_acquisition_calls = [acquired_event()]
    runner._review_artifact_repair = AsyncMock(side_effect=AssertionError("must not fake an artifact"))
    event = StepEvent(status=StepStatus.COMPLETED, step=step)
    result = await runner._finalize_analysis_step(event, background_message(), [])
    assert result == [] and step.success and step.outcome.reason_code == "acquisition_submitted"
    assert "后台" in step.result and "尚未完成" in step.result
    assert "已经全部下载完成" not in step.result
    runner._list_sandbox_artifacts.assert_not_awaited()
    runner._review_artifact_repair.assert_not_awaited()


@pytest.mark.asyncio
async def test_pure_acquisition_without_verified_job_fails_instead_of_claiming_success(monkeypatch):
    fake = service(monkeypatch)
    step = Step(success=True, result="已获取完成")
    runner = runner_for(step, [])
    event = StepEvent(status=StepStatus.COMPLETED, step=step)
    await runner._finalize_analysis_step(event, background_message(), [])
    assert not step.success and step.outcome.reason_code == "acquisition_not_confirmed"
    assert "尚未确认" in step.result
    fake.get_acquisition.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("has_valid_artifact", [False, True])
async def test_mixed_request_keeps_strict_validation_and_waits_for_real_input_handoff(monkeypatch, has_valid_artifact):
    service(monkeypatch)
    step = Step(success=True, result="已完成新数据趋势分析", deliverables=[DeliverableRequirement(kind="image")])
    pairs = [artifact("old_plot.png", "image")] if has_valid_artifact else []
    runner = runner_for(step, pairs)
    runner._open_data_acquisition_calls = [acquired_event()]
    runner._review_artifact_repair = AsyncMock(side_effect=AssertionError("never redownload to repair acquisition"))
    event = StepEvent(status=StepStatus.COMPLETED, step=step)
    kept = await runner._finalize_analysis_step(event, Message(message="下载后分析并绘图"), [pair[1] for pair in pairs])
    assert not step.success and step.outcome.reason_code == "data_acquisition_pending"
    assert step.outcome.status == ("partial" if has_valid_artifact else "failed")
    assert len(kept) == int(has_valid_artifact) and "加入分析输入" in step.result
    assert "已完成新数据趋势分析" not in step.result and not step.outcome.can_resume
    runner._review_artifact_repair.assert_not_awaited()


@pytest.mark.asyncio
async def test_ready_job_still_requires_explicit_materialized_input_and_never_waives_plot(monkeypatch):
    service(monkeypatch, job(status="ready", files=[{"file_id": "input"}]))
    event = acquired_event()
    message = Message(message="下载后分析并绘图")
    evidence = await read_acquisition_evidence([event], owner="owner", session="session", message=message)
    assert evidence.needs_handoff
    message.attachment_file_ids = ["input"]
    message.attachments = ["/home/ubuntu/upload/data.csv"]
    message.attachment_file_infos = [FileInfo(file_id="input", filename="data.csv", file_path=message.attachments[0])]
    evidence = await read_acquisition_evidence([event], owner="owner", session="session", message=message)
    assert not evidence.needs_handoff
    step = Step(success=True, deliverables=[DeliverableRequirement(kind="image")])
    runner = runner_for(step, [])
    runner._open_data_acquisition_calls = [event]
    await runner._finalize_analysis_step(StepEvent(status=StepStatus.COMPLETED, step=step), message, [])
    assert not step.success and step.outcome.reason_code == "artifacts_missing"
    assert step.outcome.missing[0].kind == "image"


@pytest.mark.asyncio
async def test_background_mode_never_waives_planner_declared_analysis_artifacts(monkeypatch):
    service(monkeypatch)
    step = Step(success=True, deliverables=[DeliverableRequirement(kind="image")])
    runner = runner_for(step, [])
    runner._open_data_acquisition_calls = [acquired_event()]
    await runner._finalize_analysis_step(StepEvent(status=StepStatus.COMPLETED, step=step), background_message(), [])
    assert not step.success and step.outcome.missing[0].kind == "image"


@pytest.mark.asyncio
async def test_real_runner_event_boundary_cannot_upgrade_queued_job_in_summary(monkeypatch):
    service(monkeypatch)
    step = Step(success=True, result="已完成获取及分析")
    runner = runner_for(step, [])

    class Flow:
        status = AgentStatus.EXECUTING
        plan = Plan(steps=[step])

        async def run(self, _message):
            yield acquired_event()
            yield StepEvent(status=StepStatus.COMPLETED, step=step)
            yield MessageEvent(message=step.result)
            self.status = AgentStatus.SUMMARIZING
            yield MessageEvent(message="所有数据已经下载完毕，分析已经完成。")
            yield DoneEvent()

    runner._flow = Flow()
    runner._front_controller_resolution = resolution()
    runner._record_safety_audit = AsyncMock()
    runner._initialize_mcp_tool = AsyncMock()
    runner._sync_step_attachments_to_storage = AsyncMock(return_value=[])
    runner._sync_discovered_artifacts_to_storage = AsyncMock(return_value=[])
    runner._sync_message_attachments_to_storage = AsyncMock()
    events = [event async for event in runner._run_flow(background_message())]
    answers = [event for event in events if isinstance(event, MessageEvent)]
    assert len(answers) == 1 and "后台" in answers[0].message and "尚未完成" in answers[0].message
    assert "分析已经完成" not in answers[0].message and not answers[0].attachments
    assert step.outcome.reason_code == "acquisition_submitted"
    runner._sync_discovered_artifacts_to_storage.assert_not_awaited()
