"""Clarification admission uses durable events, never model-authored lineage.

All fixtures are in memory. No model, source data, storage object or sandbox is
accessed while verifying the waiting and reply lifecycle.
"""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.application.services.dataset_request_resolver import (
    ExecutionDecision, FrontControllerResolution, RequestDecision,
)
from app.domain.models.event import DoneEvent, ErrorEvent, MessageEvent, WaitEvent
from app.domain.models.file import FileInfo
from app.domain.models.analysis_input import assign_upload_namespace
from app.domain.models.input_admission import input_key
from app.domain.models.request_intent import ClarificationQuestion, ClarificationRequest
from app.domain.models.safety import SafetyReview
from app.domain.models.session import SessionStatus
from app.domain.services.agent_domain_service import AgentDomainService
from app.domain.services.analysis_checkpoint import configuration_digest
from app.domain.services.analysis_clarification import (
    build_clarification_reply, clarified_request, create_clarification_view,
    get_clarification_context, pending_clarification,
)
from app.domain.services.input_delivery import InputDeliveryService
from app.domain.services.lightweight_task_runner import LightweightTaskRunner
from app.interfaces.schemas.event import EventMapper, MessageSSEEvent
from app.interfaces.schemas.session import ChatRequest
from test_input_delivery import FakeTask, MemoryInputs


def question_request(text="先按国家汇总，还是先检查缺失值？"):
    return ClarificationRequest(questions=[ClarificationQuestion(
        id="scope", question=text, reason="需要先明确本轮范围。",
        options=["按国家汇总", "检查缺失值"],
    )])


def question_history(*, ready=True, stage="request", execution_context=None):
    source = MessageEvent(id="original", seq=1, role="user",
                          message="先分析国家差异，以后再做预测。金额按万元，不要训练模型。")
    view = create_clarification_view(question_request(), source.seq, stage, source.id)
    metadata = {"clarification": view.model_dump(mode="json")}
    if execution_context is not None:
        metadata["clarification_execution"] = execution_context
    question = MessageEvent(id="question", seq=2, role="assistant", message="请确认范围", metadata=metadata)
    return [source, question, *([WaitEvent(seq=3)] if ready else [])], view


def clarification_resolution(*, response=None, context=None):
    return FrontControllerResolution(
        decision=RequestDecision(safety=SafetyReview(decision="allow"),
            execution=ExecutionDecision(mode="clarify", required_evidence="conversation"),
            clarification=question_request(), clarification_response=response),
        answer="", controller_metadata={}, clarification_context=context,
    )


def domain_service(repository):
    service = AgentDomainService(agent_repository=object(), session_repository=repository,
        sandbox_cls=object, task_cls=SimpleNamespace(get=lambda _: None), file_storage=object(),
        mcp_repository=object(), sandbox_runtime=object(), input_repository=repository)
    service._get_task = AsyncMock(return_value=None)
    service._prepare_input_selection = AsyncMock(return_value=([], []))
    service._dispatch_claimed_input = AsyncMock(return_value=None)
    return service


async def submit(service, repository, *, message="按国家汇总", client_message_id="reply-1", clarification_id=None,
                 input_file_ids=None):
    return await service._bootstrap_chat_task(
        session=repository.session, user_id="user", message=message, timestamp=None,
        attachments=None, skills=None, mcp_servers=None, dataset_ids=None, mcp_access_all=False,
        client_message_id=client_message_id, clarification_id=clarification_id, input_file_ids=input_file_ids,
    )


def test_question_identity_is_stable_and_source_bound():
    request = question_request()
    view = create_clarification_view(request, 3, identity="input-1")
    assert view == create_clarification_view(request, 3, identity="input-1")
    assert view.id != create_clarification_view(request, 4, identity="input-1").id
    assert view.id != create_clarification_view(request, 3, identity="input-2").id
    assert view.id != create_clarification_view(question_request("请选择统计单位"), 3, identity="input-1").id


def test_question_cannot_be_answered_before_wait_and_must_match_current_source():
    history, view = question_history(ready=False)
    assert pending_clarification(history)[3] is False
    with pytest.raises(ValueError, match="尚未发布完成"):
        build_clarification_reply(history, "按国家汇总", view.id)
    history[1].metadata["clarification"]["source_event_seq"] = 999
    assert pending_clarification(history + [WaitEvent(seq=3)]) is None


@pytest.mark.parametrize("terminal", [DoneEvent(seq=4), ErrorEvent(seq=4, error="execution failed"),
                                     MessageEvent(seq=4, role="user", message="新的请求")])
def test_terminal_or_later_input_invalidates_old_question(terminal):
    history, view = question_history()
    assert pending_clarification(history)[3] is True
    with pytest.raises(ValueError, match="失效|已经回答"):
        build_clarification_reply([*history, terminal], "按国家汇总", view.id)


@pytest.mark.parametrize("explicit", [True, False])
def test_explicit_and_free_text_answer_bind_literal_original_request(explicit):
    history, view = question_history()
    reply = build_clarification_reply(history, "按国家汇总", view.id if explicit else None)
    assert reply["original_request"] == history[0].message
    assert reply["original_event_seq"] == 1
    assert reply["source_event_seq"] == 1
    assert reply["clarification_id"] == view.id
    assert reply["turns"][0]["answer"] == "按国家汇总"
    assert reply["turns"][0]["questions"] == view.model_dump()["questions"]
    with pytest.raises(ValueError, match="失效"):
        build_clarification_reply(history, "按国家汇总", "old-question")


def test_repeated_clarification_keeps_all_literal_answers_and_scope_constraints():
    history, view = question_history()
    first = build_clarification_reply(history, "按国家汇总", view.id)
    answer_event = MessageEvent(id="reply-1", seq=4, role="user", message="按国家汇总",
                               metadata={"clarification_reply": first})
    second_view = create_clarification_view(question_request("缺失国家如何处理？"), 4, identity="reply-1")
    second_question = MessageEvent(seq=5, role="assistant", message="请确认缺失值处理",
        metadata={"clarification": second_view.model_dump(mode="json")})
    second = build_clarification_reply([*history, answer_event, second_question, WaitEvent(seq=6)],
                                      "缺失值单独成组", second_view.id)
    assert second["original_event_seq"] == 1
    assert second["source_event_seq"] == 4
    assert [turn["answer"] for turn in second["turns"]] == ["按国家汇总", "缺失值单独成组"]
    effective = clarified_request(second, "continue", "缺失值单独成组")
    assert effective.startswith(history[0].message)
    assert all(value in effective for value in ["不要训练模型", "以后再做预测", "万元", "按国家汇总", "缺失值单独成组"])
    for response in ["replace", "cancel", None]:
        assert clarified_request(second, response, "新的请求") == "新的请求"
    copied = get_clarification_context({"clarification_reply": second})
    copied["turns"][0]["answer"] = "mutated"
    assert second["turns"][0]["answer"] == "按国家汇总"


def test_execution_lineage_survives_followup_but_replacement_resets_it():
    execution = {"schema": "analysis_clarification_execution/v1", "plan": {"steps": []},
                 "step_id": "step-1", "original_request": "分析原始任务，不要预测"}
    history, view = question_history(stage="execution", execution_context=execution)
    first = build_clarification_reply(history, "先做描述统计", view.id)
    assert first["stage"] == "execution" and first["execution_context"] == execution
    assert first["original_request"] == execution["original_request"]
    answer_event = MessageEvent(seq=4, role="user", message="改为画箱线图",
                               metadata={"clarification_reply": first})
    next_view = create_clarification_view(question_request("箱线图使用什么单位？"), 4)
    next_question = MessageEvent(seq=5, role="assistant", message="请确认单位",
                                metadata={"clarification": next_view.model_dump(mode="json")})
    events = [*history, answer_event, next_question, WaitEvent(seq=6)]
    continuing = build_clarification_reply(events, "万元")
    assert continuing["stage"] == "execution" and continuing["execution_context"] == execution
    next_question.metadata["clarification_reset"] = True
    replacing = build_clarification_reply(events, "万元")
    assert replacing["stage"] == "request"
    assert replacing["original_event_seq"] == 4
    assert replacing["original_request"] == "改为画箱线图"
    assert "execution_context" not in replacing
    assert len(replacing["turns"]) == 1


@pytest.mark.parametrize("payload", [
    {"clarification_id": "question"},
    {"clarification_id": "question", "message": "   "},
    {"clarification_id": "invalid/path", "message": "answer"},
    {"clarification_id": "question", "message": "answer", "observe_client_message_id": "input"},
    {"clarification_id": "question", "resume_from": "a" * 32, "client_message_id": "reply"},
])
def test_reply_schema_requires_answer_and_rejects_observation_or_checkpoint_mix(payload):
    with pytest.raises(ValidationError):
        ChatRequest.model_validate(payload)


@pytest.mark.asyncio
async def test_public_projection_preserves_question_and_reply_id_but_strips_private_lineage():
    history, view = question_history(stage="execution", execution_context={
        "plan": {"source": "/private/host/path", "credential": "private-token"},
    })
    question = history[1]
    question.metadata["clarification_reset"] = True
    public_question = (await MessageSSEEvent.from_event_async(question)).model_dump(mode="json")
    assert public_question["data"]["metadata"] == {"clarification": view.model_dump(mode="json")}
    reply = MessageEvent(role="user", message="answer", metadata={
        "clarification_id": view.id,
        "clarification_reply": build_clarification_reply(history, "answer"),
        "requested_clarification_id": None,
        "analysis_input_files": [{"file_id": "private-file"}],
    })
    public_reply = (await MessageSSEEvent.from_event_async(reply)).model_dump(mode="json")
    assert public_reply["data"]["metadata"] == {"clarification_id": view.id}
    assert "private-token" not in str(public_question) + str(public_reply)
    assert "private/host" not in str(public_question) + str(public_reply)
    assert "clarification_execution" in question.metadata  # Stored event is unchanged.


@pytest.mark.asyncio
async def test_invalid_public_question_does_not_bypass_typed_projection():
    history, _ = question_history()
    question = history[1]
    question.metadata["clarification"]["execution_context"] = {"path": "/private/host/path"}
    projected = await MessageSSEEvent.from_event_async(question)
    assert "clarification" not in projected.data.metadata


@pytest.mark.asyncio
async def test_clarification_text_is_sanitized_in_live_sse_and_history_without_changing_other_messages():
    from app.interfaces.api import session_routes
    request = ClarificationRequest(questions=[ClarificationQuestion(
        id="private", question="请分析 /private/host/mount/data.csv",
        reason="token=secret-credential", options=["/Users/private/a.csv", "/Users/private/b.csv"],
    )])
    view = create_clarification_view(request, 1)
    question = MessageEvent(seq=2, role="assistant", message="请确认 /private/host/mount/data.csv token=secret-credential",
                            metadata={"clarification": view.model_dump(mode="json")})
    before = question.model_dump(mode="json")
    live = await EventMapper.event_to_sse_event(question)
    repository = MemoryInputs()
    service = SimpleNamespace(get_session=AsyncMock(return_value=repository.session),
        get_session_history=AsyncMock(return_value=SimpleNamespace(events=[question], has_more=False, next_before_seq=None)))
    historical = await session_routes.get_session_history("session", turns=5, before_seq=None,
        current_user=SimpleNamespace(id="user"), agent_service=service)
    for payload in (live.model_dump_json(), historical.model_dump_json()):
        assert "/private/host" not in payload
        assert "/Users/private" not in payload
        assert "secret-credential" not in payload
        assert "redacted" in payload
    assert live.data.metadata["clarification"]["questions"][0]["options"] == []
    assert question.model_dump(mode="json") == before
    ordinary = MessageEvent(role="assistant", message="原文 /Users/private/example.csv token=example")
    assert (await EventMapper.event_to_sse_event(ordinary)).data.content == ordinary.message


@pytest.mark.asyncio
async def test_chat_api_and_application_service_forward_bound_question_identity(monkeypatch):
    from starlette.requests import Request
    from app.application.services.agent_service import AgentService
    from app.interfaces.api import session_routes
    captured = {}
    async def domain_chat(**kwargs):
        captured.update(kwargs)
        if False:
            yield None
    service = AgentService.__new__(AgentService)
    service._agent_domain_service = SimpleNamespace(chat=domain_chat)
    user = SimpleNamespace(id="user", role="user", auto_enabled_skills=[])
    monkeypatch.setattr(session_routes, "_agent_profile_overrides", AsyncMock(return_value=None))
    monkeypatch.setattr(session_routes, "_installed_skill_names", AsyncMock(return_value=[]))
    monkeypatch.setattr(session_routes, "_installed_mcp_names", AsyncMock(return_value=[]))
    response = await session_routes.chat("session", ChatRequest(message="按国家汇总", client_message_id="reply",
        clarification_id="question-id"), Request({"type": "http", "headers": []}), current_user=user,
        agent_service=service, profile_service=object(),
        user_repository=SimpleNamespace(get_user_by_id=AsyncMock(return_value=user)))
    assert [event async for event in response.body_iterator] == []
    assert captured["clarification_id"] == "question-id"
    assert captured["client_message_id"] == "reply"
    assert captured["message"] == "按国家汇总"


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [None, "replace"])
async def test_lightweight_question_is_durable_wait_without_artifact_or_advice(response):
    repository = MemoryInputs()
    repository.increment_unread_message_count = AsyncMock()
    delivery = InputDeliveryService(repository, repository)
    event = MessageEvent(id="new-input", role="user", message="只分析国家差异，不要训练模型")
    record = await delivery.claim(await repository.accept("session", "user", event))
    task = FakeTask("clarification-task")
    task.pop_input_or_close = AsyncMock(return_value=("input-stream-id", record.event.model_dump_json()))
    task.output_stream = SimpleNamespace(put=AsyncMock(side_effect=["stream-question", "stream-wait"]))
    await delivery.bind(record, task)
    runner = LightweightTaskRunner.__new__(LightweightTaskRunner)
    runner._session_id, runner._user_id = "session", "user"
    runner._session_repository, runner._input_delivery = repository, delivery
    runner._accepted_input_key, runner._accepted_input_finished = None, False
    runner._resolution = clarification_resolution(response=response)
    runner._record_execution_snapshot = AsyncMock()
    runner._record_safety_audit = AsyncMock()
    runner._upload_catalog_artifacts = AsyncMock(side_effect=AssertionError("No artifacts while waiting"))
    runner._completion_advice = SimpleNamespace(analyze=AsyncMock(side_effect=AssertionError("No completion while waiting")))
    await runner._run_with_model_budget(task)
    assert [item.type for item in repository.events] == ["message", "message", "wait"]
    question = repository.events[1]
    assert question.attachments is None
    assert question.metadata["clarification"]["source_event_seq"] == event.seq
    assert question.metadata["clarification"]["stage"] == "request"
    assert question.metadata.get("clarification_reset", False) is (response == "replace")
    assert "analysis_outcome" not in question.metadata
    assert repository.session.status == SessionStatus.WAITING
    assert (await repository.get("session", record.key)).admission.state == "completed"
    assert await repository.terminal_kind(record) == "wait"
    assert runner._accepted_input_finished
    runner._upload_catalog_artifacts.assert_not_called()
    runner._completion_advice.analyze.assert_not_called()
    assert pending_clarification(repository.events)[3] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [True, False])
async def test_durable_reply_is_server_bound_and_same_client_retry_does_not_consume_twice(explicit):
    repository = MemoryInputs()
    repository.events, view = question_history()
    service = domain_service(repository)
    question_id = view.id if explicit else None
    await submit(service, repository, clarification_id=question_id)
    record = next(iter(repository.records.values()))
    assert record.event.message == "按国家汇总"
    assert record.event.metadata["clarification_reply"]["original_request"] == repository.events[0].message
    assert record.event.metadata["clarification_id"] == view.id
    assert record.event.metadata["requested_clarification_id"] == question_id
    assert record.event.metadata["analysis_input_files"] == []
    assert await submit(service, repository, clarification_id=question_id) is None
    assert len(repository.records) == 1
    service._dispatch_claimed_input.assert_awaited_once()
    with pytest.raises(ValueError, match="失效|已经回答"):
        await submit(service, repository, client_message_id="different-client", clarification_id=question_id)
    with pytest.raises(ValueError, match="different input"):
        await submit(service, repository, message="改成预测", clarification_id=question_id)
    assert len(repository.records) == 1


@pytest.mark.asyncio
async def test_reply_before_wait_does_not_accept_input_or_touch_selection():
    repository = MemoryInputs()
    repository.events, view = question_history(ready=False)
    service = domain_service(repository)
    with pytest.raises(ValueError, match="尚未发布完成"):
        await submit(service, repository, clarification_id=view.id)
    assert repository.records == {}
    service._prepare_input_selection.assert_not_called()
    service._dispatch_claimed_input.assert_not_called()


@pytest.mark.asyncio
async def test_reply_waits_for_previous_runner_cleanup_before_reading_wait():
    repository = MemoryInputs()
    repository.events, view = question_history(ready=False)
    service = domain_service(repository)
    previous = FakeTask("question-task")
    async def close():
        repository.events.append(WaitEvent(seq=3))
        previous.done = True
    previous.wait_closed = AsyncMock(side_effect=close)
    service._get_task = AsyncMock(return_value=previous)
    await submit(service, repository, clarification_id=view.id)
    previous.wait_closed.assert_awaited_once()
    assert len(repository.records) == 1
    assert next(iter(repository.records.values())).event.metadata["clarification_id"] == view.id


@pytest.mark.asyncio
async def test_two_clients_cannot_answer_the_same_question_under_bootstrap_lock():
    repository = MemoryInputs()
    repository.events, view = question_history()
    service = domain_service(repository)
    results = await asyncio.gather(
        submit(service, repository, client_message_id="tab-one", clarification_id=view.id),
        submit(service, repository, client_message_id="tab-two", clarification_id=view.id),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ValueError) for result in results) == 1
    assert len(repository.records) == 1
    service._dispatch_claimed_input.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("selected_ids, expected", [(None, ["source"]), (["a"], ["a"])])
async def test_quoted_question_filenames_do_not_promote_unselected_outputs(selected_ids, expected):
    from test_derived_analysis_inputs import admission, artifact, publication
    repository = MemoryInputs()
    source = FileInfo(file_id="source", filename="original.csv", size=20, user_id="user")
    a, b = artifact("a", "a.csv", owner="user"), artifact("b", "b.csv", owner="user")
    original = admission(assign_upload_namespace([source]), message="继续分析上次那个文件")
    original.seq = 2
    published = publication(a, b)
    published.seq = 1
    view = create_clarification_view(question_request("使用 a.csv 还是 b.csv？"), 2)
    repository.events = [published, original,
        MessageEvent(seq=3, role="assistant", message="使用 a.csv 还是 b.csv？",
            metadata={"clarification": view.model_dump(mode="json")}), WaitEvent(seq=4)]
    service = domain_service(repository)
    service._prepare_input_selection = AgentDomainService._prepare_input_selection.__get__(service)
    stored = {item.file_id: item for item in [source, a, b]}
    service._file_storage = SimpleNamespace(get_file_info=AsyncMock(side_effect=lambda fid, user: stored.get(fid)),
        download_file=AsyncMock(side_effect=AssertionError("must not copy inputs")),
        upload_file=AsyncMock(side_effect=AssertionError("must not republish inputs")))
    await submit(service, repository, clarification_id=view.id, input_file_ids=selected_ids,
                 message="问题：使用 a.csv 还是 b.csv？\n答案：a.csv")
    accepted = next(iter(repository.records.values())).event
    assert accepted.metadata["analysis_input_file_ids"] == expected
    assert "b" not in {call.args[0] for call in service._file_storage.get_file_info.await_args_list}
    service._file_storage.download_file.assert_not_called()
    service._file_storage.upload_file.assert_not_called()


@pytest.mark.asyncio
async def test_recovered_reply_routes_from_frozen_context_and_wait_does_not_allocate_sandbox():
    repository = MemoryInputs()
    history, view = question_history()
    context = build_clarification_reply(history, "按国家汇总", view.id)
    repository.events = history
    event = MessageEvent(id="accepted-reply", role="user", message="按国家汇总", metadata={
        "clarification_reply": context, "analysis_input_file_ids": [], "analysis_input_files": [],
    })
    service = domain_service(repository)
    service._dispatch_claimed_input = AgentDomainService._dispatch_claimed_input.__get__(service)
    service._reauthorize_input_files = AsyncMock()
    resolution = clarification_resolution()
    resolver = AsyncMock(return_value=resolution)
    service._dataset_request_resolver = SimpleNamespace(resolve=resolver)
    task = FakeTask("lightweight")
    task.enqueue_input, task.run = AsyncMock(), AsyncMock()
    service._create_lightweight_task = AsyncMock(return_value=task)
    service._create_task = AsyncMock(side_effect=AssertionError("Waiting cannot allocate a sandbox"))
    record = await service._input_delivery.claim(await repository.accept("session", "user", event))
    frozen = deepcopy(record.event.metadata)
    # A later message must not reinterpret the context of an accepted input.
    repository.events.append(MessageEvent(seq=10, role="assistant", message="unrelated later message"))
    assert await service._dispatch_claimed_input(record) is task
    assert resolver.await_args.kwargs["clarification_context"] == context
    assert resolver.await_args.kwargs["question"] == "按国家汇总"
    assert all(item.seq < record.event.seq for item in resolver.await_args.kwargs["events"])
    assert resolution.clarification_context == context
    assert (await repository.get("session", input_key(event))).event.metadata == frozen
    assert MessageEvent.model_validate_json(task.enqueue_input.await_args.args[0]).metadata == frozen
    service._create_lightweight_task.assert_awaited_once()
    service._create_task.assert_not_called()
    service._reauthorize_input_files.assert_awaited_once_with("session", "user", [])


def execution_resume_fixture():
    repository = MemoryInputs()
    saved = {"schema": "analysis_clarification_execution/v1", "plan": {"steps": []},
             "step_id": "pending", "original_request": "只做分组统计，不训练模型", "input_file_ids": [],
             "dataset_ids": [], "skills": ["summary"], "mcp_servers": ["statistics"],
             "data_connectors": ["catalog"], "mcp_access_all": False,
             "configuration_digest": configuration_digest(repository.session),
             "controller_target_files": ["/home/ubuntu/inputs/source/selected.csv"]}
    history, view = question_history(stage="execution", execution_context=saved)
    repository.events = history
    metadata = {"clarification_reply": build_clarification_reply(history, "只做分组统计", view.id),
        "analysis_input_file_ids": [], "analysis_input_files": [],
        "skills": ["summary"], "mcp_servers": ["statistics"], "data_connectors": ["catalog"],
        "mcp_access_all": False}
    service = domain_service(repository)
    service._dispatch_claimed_input = AgentDomainService._dispatch_claimed_input.__get__(service)
    service._reauthorize_input_files = AsyncMock()
    task = FakeTask("sandbox-resume")
    task.enqueue_input, task.run = AsyncMock(), AsyncMock()
    service._create_task = AsyncMock(return_value=task)
    service._create_lightweight_task = AsyncMock(return_value=FakeTask("unexpected-lightweight"))
    resolution = FrontControllerResolution(decision=RequestDecision(safety=SafetyReview(decision="allow"),
        execution=ExecutionDecision(mode="direct", required_evidence="conversation"),
        clarification_response="continue", answer="好的"), answer="好的",
        controller_metadata={"execution_mode": "direct"})
    service._dataset_request_resolver = SimpleNamespace(resolve=AsyncMock(return_value=resolution))
    return repository, service, metadata, resolution, task


@pytest.mark.asyncio
async def test_execution_answer_cannot_be_reduced_to_direct_reply():
    repository, service, metadata, resolution, task = execution_resume_fixture()
    event = MessageEvent(id="reply", role="user", message="只做分组统计", metadata=metadata)
    record = await service._input_delivery.claim(await repository.accept("session", "user", event))
    assert await service._dispatch_claimed_input(record) is task
    assert resolution.mode == "sandbox"
    assert resolution.controller_metadata["execution_mode"] == "sandbox"
    expected = metadata["clarification_reply"]["execution_context"]["controller_target_files"]
    assert resolution.target_files == resolution.decision.execution.target_files == expected
    service._create_task.assert_awaited_once()
    service._create_lightweight_task.assert_not_called()
    assert service._create_task.await_args.kwargs["input_files"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["input_file_ids", "dataset_ids", "skills", "mcp_servers",
                                    "data_connectors", "mcp_access_all", "configuration_digest"])
async def test_changed_execution_scope_is_rejected_before_sandbox_restore(changed):
    repository, service, metadata, _, _ = execution_resume_fixture()
    saved = metadata["clarification_reply"]["execution_context"]
    saved[changed] = True if changed == "mcp_access_all" else "old-config" if changed == "configuration_digest" else ["old-selection"]
    event = MessageEvent(id="reply", role="user", message="只做分组统计", metadata=metadata)
    record = await service._input_delivery.claim(await repository.accept("session", "user", event))
    assert await service._dispatch_claimed_input(record) is None
    service._create_task.assert_not_called()
    service._create_lightweight_task.assert_not_called()
    terminal = repository.events[-1]
    assert isinstance(terminal, ErrorEvent)
    assert "原步骤没有继续执行" in terminal.error
    assert "重新提出分析请求" in terminal.error or "重新发起分析请求" in terminal.error
    assert (await repository.get("session", record.key)).admission.state == "completed"
    assert repository.session.status == SessionStatus.COMPLETED


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ["replace", "cancel"])
async def test_replacement_or_cancel_does_not_force_execution_resume(response):
    repository, service, metadata, resolution, _ = execution_resume_fixture()
    resolution.decision.clarification_response = response
    metadata["skills"] = ["changed-selection"]
    task = FakeTask("new-lightweight")
    task.enqueue_input, task.run = AsyncMock(), AsyncMock()
    service._create_lightweight_task = AsyncMock(return_value=task)
    event = MessageEvent(id="reply", role="user", message="取消" if response == "cancel" else "换个问题：解释均值",
                         metadata=metadata)
    record = await service._input_delivery.claim(await repository.accept("session", "user", event))
    assert await service._dispatch_claimed_input(record) is task
    service._create_task.assert_not_called()
    service._create_lightweight_task.assert_awaited_once()
