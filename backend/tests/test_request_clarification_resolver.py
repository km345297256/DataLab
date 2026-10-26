"""Clarification must stop routing before catalog work or sandbox admission."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from app.application.services import dataset_request_resolver as module
from app.application.services.dataset_request_resolver import DatasetRequestResolver
from app.domain.models.event import MessageEvent
from app.domain.models.request_intent import ClarificationQuestion, ClarificationRequest


QUESTIONS = {"questions": [{
    "id": "sample_unit", "question": "独立样本是每个培养瓶，还是每条测量记录？",
    "reason": "样本单位会影响样本量与统计检验。", "options": ["培养瓶", "测量记录"],
}]}


def payload(mode="clarify", **updates):
    value = {
        "safety": {"decision": "allow", "risk_level": "low"},
        "execution": {"mode": mode, "required_evidence": "conversation"},
        "clarification": QUESTIONS if mode == "clarify" else None,
        "intent": {"summary": "按确认的独立样本单位分析所选实验数据", "current_actions": ["分析实验"],
                   "constraints": ["不能猜测独立样本单位"], "skill_creation": "none"},
        "answer": "说明" if mode == "direct" else "",
    }
    return {**value, **updates}


async def resolve(monkeypatch, value, *, question="分析这个实验", context=None):
    calls = []
    responses = list(value) if isinstance(value, list) else [value, value]

    class Model:
        def bind(self, **kwargs):
            return self

        async def ainvoke(self, messages):
            calls.append(messages)
            return SimpleNamespace(content=json.dumps(responses.pop(0), ensure_ascii=False))

    class Policies:
        async def list_enabled(self):
            return []

    monkeypatch.setattr(module, "create_chat_model", lambda *args, **kwargs: Model())
    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace())
    resolver = DatasetRequestResolver()
    resolver._policy_store = Policies()
    resolver._catalog.execute = Mock(side_effect=AssertionError("clarification must not read catalog"))
    # A prior analysis normally upgrades conversational direct answers. It must
    # not upgrade a question to executable analysis.
    history = [MessageEvent(role="assistant", message="此前分析", metadata={
        "analysis_outcome": {"version": 1, "status": "failed", "reason_code": "execution_failed"},
    })]
    result = await resolver.resolve(question=question, datasets=[], events=history,
                                    clarification_context=context)
    return result, calls


@pytest.mark.asyncio
async def test_clarification_is_a_normal_wait_without_catalog_work(monkeypatch):
    result, _ = await resolve(monkeypatch, payload())
    assert result.mode == "clarify"
    assert result.decision.safety.allowed
    assert result.answer == ""
    assert result.artifacts == []
    assert result.decision.clarification.questions[0].options == ["培养瓶", "测量记录"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["direct", "catalog", "sandbox"])
async def test_questions_override_inconsistent_computation_fields(monkeypatch, mode):
    value = payload(mode, clarification=QUESTIONS)
    value["execution"].update(required_capabilities=["python"], requires_artifacts=True,
                              deliverables=[{"kind": "report", "min_count": 1}],
                              completion_mode="background_acquisition")
    result, _ = await resolve(monkeypatch, value)
    assert result.mode == "clarify"
    assert result.decision.execution.completion_mode == "inline"
    assert result.decision.execution.required_capabilities == []
    assert result.decision.execution.deliverables == []
    assert not result.decision.execution.requires_artifacts


@pytest.mark.asyncio
async def test_ambiguous_skill_command_waits_instead_of_overriding_question(monkeypatch):
    result, _ = await resolve(monkeypatch, payload(), question="/skill-creator")
    assert result.mode == "clarify"


@pytest.mark.asyncio
async def test_missing_reply_protocol_field_is_not_a_question_for_the_user(monkeypatch):
    result, _ = await resolve(monkeypatch, payload("sandbox"), question="是的",
                              context={"original_request": "分析实验", "questions": QUESTIONS["questions"]})
    assert result.mode == "reject"
    assert result.controller_metadata["failure_code"] == "invalid_routing"
    assert result.decision.clarification is None


@pytest.mark.asyncio
async def test_missing_reply_marker_is_repaired_internally_before_execution(monkeypatch):
    result, calls = await resolve(monkeypatch, [payload("sandbox"),
        payload("sandbox", clarification_response="continue")], question="培养瓶",
        context={"original_request": "分析实验", "questions": QUESTIONS["questions"]})
    assert len(calls) == 2
    assert result.mode == "sandbox"
    assert result.decision.clarification is None


@pytest.mark.asyncio
async def test_continue_can_ask_remaining_question_without_default_consent(monkeypatch):
    result, _ = await resolve(monkeypatch, payload(clarification_response="continue"),
                              question="按第一个吧，单位我不知道", context={"original_request": "比较两组"})
    assert result.mode == "clarify"
    assert result.decision.clarification_response == "continue"


@pytest.mark.asyncio
async def test_full_original_request_is_preserved_in_reply_context(monkeypatch):
    original = "任务约定。" * 700 + "严禁填补缺失端点；培养瓶是独立样本。"
    context = {"original_request": original, "questions": QUESTIONS["questions"], "answer": "培养瓶"}
    result, calls = await resolve(monkeypatch, payload("sandbox", clarification_response="continue"),
                                  question="培养瓶", context=context)
    assert result.mode == "sandbox"
    sent = json.loads(calls[0][1].content)
    assert sent["clarification_context"]["original_request"] == original
    assert result.clarification_context == context


@pytest.mark.asyncio
async def test_cancel_cannot_execute_surplus_analysis_or_artifact_requirements(monkeypatch):
    value = payload("sandbox", clarification_response="cancel", answer="已取消。")
    value["execution"].update(requires_artifacts=True, required_capabilities=["python"])
    result, _ = await resolve(monkeypatch, value, question="取消吧", context={"original_request": "分析实验"})
    assert result.mode == "direct"
    assert result.answer == "已取消。"
    assert not result.decision.execution.requires_artifacts
    assert result.decision.execution.required_capabilities == []


@pytest.mark.asyncio
async def test_replacement_does_not_force_original_analysis_route(monkeypatch):
    value = payload("direct", clarification_response="replace")
    value["execution"]["required_evidence"] = "user_message"
    result, _ = await resolve(monkeypatch, value,
                              question="换个问题，解释方差的含义", context={"original_request": "分析实验"})
    assert result.mode == "direct"
    assert result.decision.clarification_response == "replace"


@pytest.mark.asyncio
async def test_router_does_not_receive_private_execution_checkpoint(monkeypatch):
    context = {"original_request": "分析实验", "turns": [],
               "execution_context": {"plan": {"private_path": "/private/host/input"}}}
    _, calls = await resolve(monkeypatch, payload(clarification_response="continue"), context=context)
    sent = json.loads(calls[0][1].content)["clarification_context"]
    assert "execution_context" not in sent
    assert "/private/host/input" not in json.dumps(sent)


@pytest.mark.asyncio
async def test_reply_marker_without_pending_question_has_no_authority(monkeypatch):
    result, _ = await resolve(monkeypatch, payload("sandbox", clarification_response="cancel"))
    assert result.mode == "sandbox"
    assert result.decision.clarification_response is None


@pytest.mark.asyncio
async def test_factual_inspection_is_allowed_without_unnecessary_confirmation(monkeypatch):
    result, _ = await resolve(monkeypatch, payload("sandbox"), question="按所选数据字典检查字段和单位")
    assert result.mode == "sandbox"
    assert result.decision.clarification is None


@pytest.mark.asyncio
async def test_future_skill_wish_remains_deferred_in_analysis_decision(monkeypatch):
    value = payload("sandbox")
    value["intent"].update(skill_creation="deferred", deferred_actions=["以后保存分析方法为技能"])
    result, _ = await resolve(monkeypatch, value, question="先分析实验，以后希望保存为技能")
    assert result.mode == "sandbox"
    assert result.decision.intent.skill_creation == "deferred"


@pytest.mark.asyncio
async def test_safety_rejection_remains_authoritative_over_clarification(monkeypatch):
    value = payload()
    value["safety"] = {"decision": "reject", "risk_level": "high"}
    result, _ = await resolve(monkeypatch, value)
    assert result.mode == "reject"
    assert result.decision.clarification is None


@pytest.mark.parametrize("questions", [[], [{"id": "q", "question": "   "}],
    [{"id": "q", "question": "选择哪一个？", "options": ["A", "A"]}],
    [{"id": "q", "question": "问题"}] * 2])
def test_clarification_rejects_empty_or_ambiguous_question_shapes(questions):
    with pytest.raises(ValidationError):
        ClarificationRequest(questions=questions)


def test_questions_have_no_implicit_default_answer():
    question = ClarificationQuestion(id="q", question="选择哪个样本单位？", options=["培养瓶", "记录"])
    assert set(question.model_dump()) == {"id", "question", "reason", "options"}
