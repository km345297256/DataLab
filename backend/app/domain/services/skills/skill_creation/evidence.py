"""Select a completed owned analysis and prepare non-executable method evidence."""
from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Any
from app.domain.models.event import DoneEvent, ErrorEvent, MessageEvent, PlanEvent, StepEvent, ToolEvent, ToolStatus
from app.domain.models.plan import ExecutionStatus
from app.domain.services.skills.skill_creation_error import SkillCreationError
from app.domain.services.skills.method_contract import evidence_sources
from .intent import is_explicit_skill_create_command
from .privacy import PrivacyContext, ensure_context

logger = logging.getLogger(__name__)

def select_analysis_turn(events: list[Any]) -> list[Any]:
    """Skip creation-only turns, but never silently fall back past a failed analysis."""
    starts = [i for i, event in enumerate(events) if isinstance(event, MessageEvent) and event.role == "user"]
    for position in range(len(starts) - 1, -1, -1):
        start = starts[position]
        end = starts[position + 1] if position + 1 < len(starts) else len(events)
        turn = list(events[start:end])
        # A future wish in an analysis request must not hide that analysis (or
        # let an earlier successful turn replace its failure). Only an explicit
        # command or an actual creator invocation identifies creation-only work.
        if any(isinstance(event, (PlanEvent, StepEvent)) for event in turn):
            return turn
        if is_explicit_skill_create_command(events[start].message) or any(
            isinstance(event, ToolEvent) and event.function_name == "skill_create_from_session"
            for event in turn
        ):
            continue
        return turn
    raise ValueError("没有找到可提炼的分析回合，请先完成一次分析，再使用 /skill-creator。")

def _successful_step(step: Any) -> bool:
    return (step.outcome.status == "succeeded" if step.outcome is not None
            else step.status == ExecutionStatus.COMPLETED and step.success is True)

def _tool_succeeded(event: ToolEvent) -> bool:
    if event.status != ToolStatus.CALLED or event.dispatch_status == "not_started":
        return False
    result = event.function_result
    if hasattr(result, "model_dump"):
        result = result.model_dump()
    return isinstance(result, dict) and result.get("success") is True

def _verified_turn(events: list[Any]) -> None:
    latest_steps: dict[str, Any] = {}
    latest_plan = None
    for event in events:
        if isinstance(event, PlanEvent):
            latest_plan = event.plan
            latest_steps = {step.id: step for step in event.plan.steps}
        elif isinstance(event, StepEvent):
            latest_steps[event.step.id] = event.step
    completed = any(isinstance(event, DoneEvent) for event in events) or (
        latest_plan is not None and latest_plan.status == ExecutionStatus.COMPLETED)
    if (not completed or any(isinstance(event, ErrorEvent) for event in events)
            or latest_plan is not None and latest_plan.status == ExecutionStatus.FAILED
            or any(not _successful_step(step) for step in latest_steps.values())):
        raise ValueError("最近一次分析尚未完整完成，不能固化未验证的流程；请先完成或修复该分析。")
    final = next((event for event in reversed(events) if isinstance(event, MessageEvent) and event.role == "assistant" and event.message.strip()), None)
    metadata = (final.metadata or {}) if final else {}
    outcome = metadata.get("analysis_outcome")
    if metadata.get("partial") or isinstance(outcome, dict) and outcome.get("status") in {"failed", "partial"}:
        raise ValueError("最近一次分析的最终交付尚未完整完成，请先解决未交付结果。")
    if final is None:
        raise ValueError("最近一次分析缺少已交付结果，暂时无法生成可复用技能。")
    if not any(_successful_step(step) for step in latest_steps.values()) and not any(
        isinstance(event, ToolEvent) and _tool_succeeded(event) for event in events
    ):
        raise ValueError("最近一次分析缺少成功执行的证据，暂时无法生成可复用技能。")

def _safe_text(text, private_values):
    return ensure_context(private_values).redact_text(str(text or ""))

def build_synthesis_evidence(events: list[Any], *, private_values: PrivacyContext | set[str] | None = None) -> dict[str, Any]:
    """Complete method evidence with no executable trace or raw tool output."""
    private_values = ensure_context(private_values)
    safe = lambda value: _safe_text(value, private_values)
    plans = [event.plan for event in events if isinstance(event, PlanEvent)]
    steps: dict[str, Any] = {}
    for event in events:
        if isinstance(event, PlanEvent):
            steps.update({step.id: step for step in event.plan.steps})
        elif isinstance(event, StepEvent):
            steps[event.step.id] = event.step
    messages = [event for event in events if isinstance(event, MessageEvent)]
    final = next((event for event in reversed(messages) if event.role == "assistant"), None)
    return {
        "research_request": safe(next((event.message for event in messages if event.role == "user"), "")),
        "goal": safe(plans[-1].goal if plans else ""),
        "verified_steps": [{"method": safe(step.description), "evidence_summary": safe(step.result or "")}
                           for step in steps.values() if _successful_step(step)],
        "successful_tool_capabilities": sorted({f"{event.tool_name}.{event.function_name}" for event in events
                                                if isinstance(event, ToolEvent) and _tool_succeeded(event)}),
        "delivery_summary": safe(final.message if final else ""),
        "artifact_formats": sorted({Path(attachment.filename or "").suffix.lower() for event in messages
                                    for attachment in event.attachments or [] if Path(attachment.filename or "").suffix}),
    }

@dataclass(frozen=True)
class AnalysisContext:
    """One source turn; raw events never reach generation or delivery."""
    privacy: PrivacyContext
    evidence: dict
    sources: list[dict]
    programs: list[dict]


async def prepare_analysis_context(session_id, user_id, repository, registry):
    if registry.user_id != user_id:
        raise SkillCreationError("source_analysis", "identity_mismatch")
    if not await repository.find_by_id_and_user_id(session_id, user_id):
        raise SkillCreationError("source_analysis", "session_unavailable")
    try:
        events = select_analysis_turn(await repository.get_events(session_id))
        _verified_turn(events)
    except ValueError as exc:
        raise SkillCreationError("source_analysis", "analysis_not_ready") from exc
    privacy = PrivacyContext.collect(events, session_id=session_id, user_id=user_id)
    evidence = build_synthesis_evidence(events, private_values=privacy)
    sequences = [event.seq for event in events if isinstance(event, ToolEvent)
                 and event.function_name == "program_run" and _tool_succeeded(event)
                 and type(event.seq) is int]
    read_sources = getattr(repository, "get_successful_program_sources", None)
    records = await read_sources(session_id, user_id, sequences) if callable(read_sources) and sequences else []
    from app.domain.services.skills.program_method_evidence import ProgramMethodEvidenceError, method_program_references
    try:
        programs = method_program_references(records, privacy_context=privacy)
    except ProgramMethodEvidenceError as exc:
        # Failure to extract a private method reference is an authoring failure,
        # not evidence that the original completed analysis failed. Never drop
        # the source silently or send its exception/source text to the model.
        logger.warning("Skill source evidence extraction rejected reason=%s", exc.reason)
        raise SkillCreationError("source_analysis", "source_evidence_invalid") from exc
    return AnalysisContext(privacy, evidence, evidence_sources(evidence), programs)
