"""A clarification resumes the admitted task, never replans a short answer."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.models.analysis_outcome import DeliverableRequirement
from app.domain.models.event import MessageEvent, PlanEvent, PlanStatus, ToolEvent, ToolStatus, WaitEvent
from app.domain.models.message import Message
from app.domain.models.plan import ExecutionStatus, Plan, Step
from app.domain.models.request_intent import RequestIntent
from app.domain.models.session import SessionStatus
from app.domain.models.tool_result import ToolResult
from app.domain.services.agent_task_runner import AgentTaskRunner
from app.domain.services.agents.execution import ExecutionAgent
from app.domain.services.flows.plan_act import AgentStatus, PlanActFlow
from app.domain.services.tools.message import MessageToolkit


def saved_reply():
    plan = Plan(steps=[
        Step(id="inspect", status=ExecutionStatus.COMPLETED, success=True),
        Step(id="compare", status=ExecutionStatus.RUNNING, description="Compare the confirmed groups"),
    ])
    return {
        "version": 1, "clarification_id": "question-id", "stage": "execution",
        "source_event_seq": 10, "original_event_seq": 10,
        "original_request": "Analyze the algae experiment and compare the treatments",
        "turns": [{"questions": [{"question": "Which experimental unit?"}], "answer": "Use one tank as one unit"}],
        "execution_context": {
            "schema": "analysis_clarification_execution/v1", "plan": plan.model_dump(mode="json"),
            "step_id": "compare", "input_file_ids": ["a" * 24], "dataset_ids": [],
            "deliverables": [{"kind": "image", "min_count": 1}],
            "controller_requires_artifacts": True, "controller_target_files": ["experiment.csv"],
            "completion_mode": "inline",
            "request_intent": {"summary": "Compare treatments", "skill_creation": "deferred"},
        },
    }


def bind_reply(context=None, response="continue", **message_updates):
    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._front_controller_resolution = SimpleNamespace(
        decision=SimpleNamespace(clarification_response=response),
        clarification_context=context if context is not None else saved_reply(),
    )
    message = Message(message="Use one tank as one unit", attachment_file_ids=["a" * 24], **message_updates)
    runner._bind_clarification_message(message, {})
    return message


def test_execution_answer_restores_original_task_scope_contract_and_pending_step():
    message = bind_reply()
    assert message.message.startswith("Analyze the algae experiment")
    assert "Use one tank as one unit" in message.message
    assert message.controller_target_files == ["experiment.csv"]
    assert message.controller_requires_artifacts is True
    assert message.deliverables == [DeliverableRequirement(kind="image", min_count=1)]
    assert message.request_intent.skill_creation == "deferred"
    assert message._clarification_resume["step_id"] == "compare"
    assert message._clarification_resume["answer"] == "Use one tank as one unit"
    assert message._clarification_resume["plan"]["steps"][0]["status"] == "completed"
    assert not any(key.startswith("_clarification") for key in message.model_dump())


@pytest.mark.parametrize("response", ["replace", "cancel", None])
def test_only_explicit_continue_can_resume_the_saved_task(response):
    message = bind_reply(response=response)
    assert message.message == "Use one tank as one unit"
    assert message._clarification_resume is None
    assert message.deliverables == []
    assert message._clarification_response == response


@pytest.mark.parametrize("mutation", ["files", "datasets", "step", "empty_answer", "schema", "no_context"])
def test_changed_or_incomplete_saved_execution_fails_closed(mutation):
    context = saved_reply()
    saved = context["execution_context"]
    if mutation == "files":
        saved["input_file_ids"] = ["b" * 24]
    elif mutation == "datasets":
        saved["dataset_ids"] = ["different-dataset"]
    elif mutation == "step":
        saved["step_id"] = "inspect"
    elif mutation == "empty_answer":
        context["turns"][0]["answer"] = " "
    elif mutation == "schema":
        saved["schema"] = "untrusted"
    else:
        context.pop("execution_context")
    with pytest.raises(ValueError):
        bind_reply(context)


def test_request_stage_answer_has_original_request_but_no_execution_resume():
    context = saved_reply()
    context["stage"] = "request"
    context.pop("execution_context")
    message = bind_reply(context)
    assert message.message.startswith(context["original_request"])
    assert message._clarification_resume is None


@pytest.mark.parametrize("field, value", [("skills", ["changed"]), ("mcp_servers", ["changed"]),
    ("data_connectors", ["changed"]), ("mcp_access_all", True)])
def test_execution_answer_cannot_reuse_plan_under_changed_tool_scope(field, value):
    context = saved_reply()
    context["execution_context"][field] = value
    with pytest.raises(ValueError, match="工具或技能配置已经变化"):
        bind_reply(context)


@pytest.mark.asyncio
async def test_execution_question_persists_exact_pending_plan_and_public_choices():
    agent = object.__new__(ExecutionAgent)
    agent.reset_context = AsyncMock()
    args = {"text": "Which experimental unit?", "reason": "Repeated measurements are correlated",
            "options": ["Each tank", "Each measurement"]}

    async def execute(_message):
        for status in (ToolStatus.CALLING, ToolStatus.CALLED):
            yield ToolEvent(status=status, tool_call_id="stable-call", tool_name="message",
                            function_name="message_ask_user", function_args=args,
                            function_result=ToolResult(success=True) if status == ToolStatus.CALLED else None)

    agent.execute = execute
    plan = Plan(steps=[Step(id="inspect", success=True, status=ExecutionStatus.COMPLETED), Step(id="compare")])
    message = Message(message="Compare algae treatments", attachment_file_ids=["a" * 24],
                      controller_target_files=["experiment.csv"],
                      deliverables=[DeliverableRequirement(kind="image")])
    message._accepted_event_seq = 10
    events = [event async for event in agent.execute_step(plan, plan.steps[1], message)]
    assert [event.type for event in events[-2:]] == ["message", "wait"]
    metadata = events[-2].metadata
    public = metadata["clarification"]
    assert public["stage"] == "execution" and public["source_event_seq"] == 10
    assert public["questions"][0]["options"] == args["options"]
    assert public["questions"][0]["reason"] == args["reason"]
    saved = metadata["clarification_execution"]
    assert saved["step_id"] == "compare" and saved["input_file_ids"] == ["a" * 24]
    assert saved["plan"]["steps"][0]["status"] == "completed"
    assert saved["plan"]["steps"][1]["status"] == "running"
    repeated = [event async for event in agent.execute_step(plan, plan.steps[1], message)]
    assert repeated[-2].metadata["clarification"]["id"] == public["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [["A", "A"], [""], ["A", "B", "C", "D", "E"]])
async def test_invalid_choices_do_not_create_a_wait_receipt(options):
    tool = MessageToolkit().get_tool("message_ask_user")
    result = await tool.ainvoke({"id": "question", "args": {"text": "Which unit?", "options": options}})
    assert result.artifact.success is False
    assert result.artifact.data["error_code"] == "invalid_user_question"


def configured_flow(*, rollback=True, status=SessionStatus.RUNNING):
    flow = PlanActFlow.__new__(PlanActFlow)
    flow.enabled_subagents = {"execution": SimpleNamespace(enabled=True, handler_type="execution")}
    stale_plan = Plan(steps=[Step(id="unrelated-stale-step")])
    flow._session_id, flow._agent_id = "session", "agent"
    flow._session_repository = SimpleNamespace(
        find_by_id=AsyncMock(return_value=SimpleNamespace(status=status)),
        update_status=AsyncMock(), get_events=AsyncMock(return_value=[PlanEvent(status=PlanStatus.CREATED, plan=stale_plan)]))
    flow.status, flow.plan = AgentStatus.IDLE, None
    flow.session_context = flow.dataset_context = flow.active_skill_context = ""
    flow._activate_skills = lambda _: []
    flow.planner = SimpleNamespace(roll_back=AsyncMock(), reset_context=AsyncMock())
    flow.executor = SimpleNamespace(roll_back=AsyncMock(return_value=rollback), reset_context=AsyncMock(), compact_memory=AsyncMock())
    flow.vision, flow._sandbox = object(), object()
    flow._domain_agents = {}
    flow.executed = []

    async def execute_step(plan, step, message):
        flow.executed.append((deepcopy(plan), step.id, message.message))
        yield MessageEvent(message="resumed")

    flow.executor.execute_step = execute_step
    return flow


@pytest.mark.asyncio
async def test_host_bound_answer_resumes_exact_step_after_admission_already_sets_running():
    message = bind_reply()
    flow = configured_flow()
    stream = flow.run(message)
    async for event in stream:
        if isinstance(event, MessageEvent):
            break
    await stream.aclose()
    flow.planner.roll_back.assert_not_awaited()
    flow.executor.roll_back.assert_awaited_once()
    assert flow.executor.roll_back.call_args.args[0].message == "Use one tank as one unit"
    assert flow.executed[0][1] == "compare"
    assert flow.executed[0][0].steps[0].status == ExecutionStatus.COMPLETED
    assert flow.executed[0][2].startswith("Analyze the algae experiment")


@pytest.mark.asyncio
async def test_missing_waiting_memory_never_replans_or_reexecutes_completed_work():
    flow = configured_flow(rollback=False)
    with pytest.raises(ValueError, match="执行记忆不可用"):
        _ = [event async for event in flow.run(bind_reply())]
    assert flow.executed == []
    flow.planner.roll_back.assert_not_awaited()


@pytest.mark.asyncio
async def test_replace_discards_waiting_memory_instead_of_treating_new_task_as_answer():
    flow = configured_flow(status=SessionStatus.WAITING)
    message = bind_reply(response="replace")
    created = Plan(steps=[Step(id="new-step")])

    async def create_plan(_message):
        assert flow.plan is None
        yield PlanEvent(status=PlanStatus.CREATED, plan=created)

    flow.planner.create_plan = create_plan
    stream = flow.run(message)
    async for event in stream:
        if isinstance(event, PlanEvent):
            assert event.plan.steps[0].id == "new-step"
            break
    await stream.aclose()
    flow.executor.roll_back.assert_not_awaited()
    flow.planner.roll_back.assert_not_awaited()
    flow.executor.reset_context.assert_awaited_once()
    flow.planner.reset_context.assert_awaited_once()
    assert flow.executed == []


@pytest.mark.asyncio
async def test_new_request_after_lightweight_cancel_cannot_answer_old_tool_question():
    from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
    from app.domain.models.memory import Memory
    from app.domain.services.agents.base import BaseAgent

    flow = configured_flow(status=SessionStatus.RUNNING)
    executor = BaseAgent.__new__(BaseAgent)
    executor.memory = Memory(messages=[
        SystemMessage(content="analysis"),
        AIMessage(content="", tool_calls=[{"name": "message_ask_user", "id": "old-question",
            "args": {"text": "Which year?"}}]),
        ToolMessage(content="OK", name="message_ask_user", tool_call_id="old-question", status="success"),
    ])
    executor.system_prompt = "analysis"
    executor._ensure_memory = AsyncMock()
    executor._persist_memory = AsyncMock()
    executor._preserve_context_for_next_request = True
    executor.roll_back = AsyncMock(wraps=executor.roll_back)
    flow.executor = executor

    async def create_plan(_message):
        yield PlanEvent(status=PlanStatus.CREATED, plan=Plan(steps=[Step(id="new-task")]))

    flow.planner.create_plan = create_plan
    stream = flow.run(Message(message="A new unrelated analysis after cancellation"))
    async for event in stream:
        if isinstance(event, PlanEvent):
            assert event.plan.steps[0].id == "new-task"
            break
    await stream.aclose()
    executor.roll_back.assert_not_awaited()
    assert executor._preserve_context_for_next_request is False
    assert len(executor.memory.messages) == 1
    assert isinstance(executor.memory.messages[0], SystemMessage)
    executor._persist_memory.assert_awaited_once()


@pytest.mark.parametrize("skill_creation", ["none", "deferred", "after_analysis"])
def test_future_or_negated_skill_intent_cannot_preempt_current_analysis(skill_creation):
    message = Message(message="分析本轮藻类实验，我希望这次分析的方法以后能保存为可复用技能",
                      request_intent=RequestIntent(current_actions=["Analyze the experiment"],
                          deferred_actions=["Save the method in future"], skill_creation=skill_creation))
    assert PlanActFlow._requests_immediate_skill_creation(message) is False


def test_current_skill_intent_and_explicit_command_still_route_to_skill_creation():
    assert PlanActFlow._requests_immediate_skill_creation(Message(message="保存分析流程",
        request_intent=RequestIntent(skill_creation="now"))) is True
    assert PlanActFlow._requests_immediate_skill_creation(Message(message="/skill-creator")) is True
    assert PlanActFlow._requests_immediate_skill_creation(Message(message="不要执行 /skill-creator")) is False


@pytest.mark.asyncio
async def test_runner_preserves_question_metadata_and_injects_host_configuration_before_wait(monkeypatch):
    from app.application.services.dataset_request_resolver import ExecutionDecision, FrontControllerResolution, RequestDecision
    from app.domain.models.safety import SafetyReview
    from app.domain.services import analysis_checkpoint

    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._agent_id, runner._session_id, runner._user_id = "agent", "session", "owner"
    runner._artifact_baseline_paths = set()
    runner._generated_files = []
    session = SimpleNamespace()
    runner._session_repository = SimpleNamespace(find_by_id_and_user_id=AsyncMock(return_value=session))
    monkeypatch.setattr(analysis_checkpoint, "configuration_digest", lambda value: "host-config" if value is session else "wrong")
    metadata = {"clarification": {"id": "question", "stage": "execution", "source_event_seq": 10,
        "questions": [{"id": "unit", "question": "Which unit?", "options": ["Tank", "Measurement"]}]},
        "clarification_execution": saved_reply()["execution_context"]}

    class Flow:
        status = AgentStatus.EXECUTING
        plan = Plan(steps=[Step(id="pending", status=ExecutionStatus.RUNNING)])

        async def run(self, message):
            yield MessageEvent(message="Which unit?", metadata=metadata)
            yield WaitEvent()

    runner._flow = Flow()
    runner._front_controller_resolution = FrontControllerResolution(
        decision=RequestDecision(safety=SafetyReview(decision="allow", risk_level="low"),
            execution=ExecutionDecision(mode="sandbox", required_evidence="file_content")), answer="", controller_metadata={})
    runner._record_safety_audit = AsyncMock()
    runner._initialize_mcp_tool = AsyncMock()
    runner._sync_discovered_artifacts_to_storage = AsyncMock(return_value=[])
    runner._sync_message_attachments_to_storage = AsyncMock()
    events = [event async for event in runner._run_flow(Message(message="Compare tanks"))]
    assert [event.type for event in events] == ["message", "wait"]
    assert events[0].message == "Which unit?"
    assert events[0].metadata["clarification"] == metadata["clarification"]
    assert events[0].metadata["clarification_execution"]["configuration_digest"] == "host-config"
    runner._sync_message_attachments_to_storage.assert_not_awaited()
    runner._sync_discovered_artifacts_to_storage.assert_not_awaited()
