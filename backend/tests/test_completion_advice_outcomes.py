import json
from types import SimpleNamespace

import pytest

from app.domain.models.analysis_outcome import AnalysisOutcome
from app.domain.models.event import ErrorEvent, MessageEvent, PlanEvent, PlanStatus, StepEvent, StepStatus
from app.domain.models.file import FileInfo
from app.domain.models.plan import ExecutionStatus, Plan, Step
from app.domain.services import completion_advice_service as module


@pytest.fixture(autouse=True)
def offline_settings(monkeypatch):
    monkeypatch.setattr(module, "get_settings", lambda: object())


def analysis_events(status):
    return [
        MessageEvent(role="user", message="比较蛋白结构并生成图表"),
        StepEvent(status=StepStatus.COMPLETED if status == "succeeded" else StepStatus.FAILED,
                  step=Step(id="analysis", description="比较蛋白结构的空间差异", success=status == "succeeded", attachments=["structure.pdb"],
                            outcome=AnalysisOutcome(status=status, reason_code="analysis_completed" if status == "succeeded" else "answer_validation_rejected"))),
        MessageEvent(message="结构文件已保存，空间差异结论尚未通过验证。" if status != "succeeded" else "已验证两份蛋白结构的空间差异。",
                     attachments=[FileInfo(file_id="pdb", filename="structure.pdb", size=12)]),
    ]


def install_model(monkeypatch, observed):
    class Model:
        async def ainvoke(self, messages):
            observed.append(json.loads(messages[1].content))
            return SimpleNamespace(content=json.dumps({
                "recommendations": ["核对蛋白空间差异比较的证据缺口", "检查蛋白结构文件中的缺失原子", "为蛋白结构差异比较补充验证指标"],
                "is_skill_candidate": True, "skill_reason": "蛋白结构比较具备可复用输入和检查规则。"}, ensure_ascii=False))
    monkeypatch.setattr(module, "create_chat_model", lambda *_a, **_k: Model())


@pytest.mark.parametrize("status", ["failed", "partial"])
@pytest.mark.asyncio
async def test_incomplete_analysis_can_get_specific_recovery_but_not_skill(monkeypatch, status):
    observed = []
    install_model(monkeypatch, observed)
    advice = await module.CompletionAdviceService().analyze(analysis_events(status))
    assert observed[0]["completion_status"] == "incomplete"
    assert observed[0]["steps"][0]["reason_code"] == "answer_validation_rejected"
    assert not advice.is_skill_candidate and advice.skill_reason == ""
    assert advice.molecular_preview_available
    assert len(advice.recommendations) == 3
    assert all("蛋白" in item for item in advice.recommendations)


@pytest.mark.asyncio
async def test_previous_failed_turn_cannot_replace_current_successful_advice(monkeypatch):
    observed = []
    install_model(monkeypatch, observed)
    previous = [*analysis_events("failed"), ErrorEvent(error="old failure")]
    advice = await module.CompletionAdviceService().analyze([*previous, *analysis_events("succeeded")])
    assert observed[0]["completion_status"] == "complete"
    assert advice.is_skill_candidate


def test_latest_step_terminal_replaces_earlier_state():
    service = module.CompletionAdviceService()
    events = analysis_events("failed")
    events.append(analysis_events("succeeded")[1])
    assert not service._is_incomplete(events)
    events.append(analysis_events("partial")[1])
    assert service._is_incomplete(events)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [ErrorEvent(error="invalid_execution_result"),
    MessageEvent(message="已取消", metadata={"analysis_outcome": {"status": "failed", "reason_code": "request_cancelled"}}),
    MessageEvent(message="前置路由不可用", metadata={"analysis_started": False}),
])
async def test_technical_failure_or_cancel_does_not_generate_questions(monkeypatch, failure):
    monkeypatch.setattr(module, "create_chat_model", lambda *_a, **_k: pytest.fail("Technical failures have no recommendations"))
    advice = await module.CompletionAdviceService().analyze([*analysis_events("succeeded"), failure])
    assert advice.recommendations == [] and not advice.is_skill_candidate


def test_preview_requires_current_delivered_attachment():
    service = module.CompletionAdviceService()
    events = [*analysis_events("succeeded"), MessageEvent(role="user", message="另一个问题"), ErrorEvent(error="failed")]
    assert not service.analyze_fast(events).molecular_preview_available
    # A step attachment that failed final delivery is not a usable preview.
    assert not service.analyze_fast(analysis_events("succeeded")[:-1]).molecular_preview_available
    assert service.analyze_fast(analysis_events("succeeded")).molecular_preview_available


def test_final_plan_failure_and_delivery_partial_prevent_workflow_advice():
    service = module.CompletionAdviceService()
    events = analysis_events("succeeded")
    events.append(PlanEvent(status=PlanStatus.COMPLETED, plan=Plan(status=ExecutionStatus.FAILED, steps=[
        Step(id="analysis", status=ExecutionStatus.COMPLETED, success=True),
        Step(id="unexecuted", status=ExecutionStatus.FAILED, success=False),
    ])))
    assert service._is_incomplete(events)
    events.append(PlanEvent(status=PlanStatus.COMPLETED, plan=Plan(status=ExecutionStatus.COMPLETED, steps=[
        Step(id="analysis", status=ExecutionStatus.COMPLETED, success=True),
        Step(id="unexecuted", status=ExecutionStatus.COMPLETED, success=True),
    ])))
    assert not service._is_incomplete(events)
    events.append(MessageEvent(message="部分结果文件未通过最终检查", metadata={"partial": True}))
    assert service._is_incomplete(events)


def test_recovery_evidence_contains_missing_outputs_without_private_contracts():
    from app.domain.models.analysis_outcome import ArtifactIssue, DeliverableRequirement
    current = analysis_events("partial")
    current[1].step.outcome = AnalysisOutcome(
        status="partial", reason_code="missing_artifact",
        missing=[DeliverableRequirement(kind="image", formats=["png"],
            output_paths=["/home/ubuntu/output/private-design.png"], objective="蛋白空间差异图")],
        issues=[ArtifactIssue(artifact_name="structure.png", kind="image", reason_code="invalid_content", blocking=True)],
    )
    serialized = module.CompletionAdviceService()._serialize_events(current)
    outcome = json.loads(serialized)["steps"][0]["outcome"]
    assert outcome["missing"][0]["formats"] == ["png"]
    assert outcome["issues"][0]["artifact_name"] == "structure.png"
    assert "private-design" not in serialized
    assert "output_paths" not in serialized and "objective" not in serialized
