"""Host-owned clarification binding reconstructed from immutable session events."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from typing import Literal

from app.domain.models.event import DoneEvent, ErrorEvent, MessageEvent, WaitEvent
from app.domain.models.request_intent import ClarificationRequest, ClarificationView


def create_clarification_view(request: ClarificationRequest, source_event_seq: int | None,
                              stage: Literal["request", "execution"] = "request",
                              identity: str | None = None) -> ClarificationView:
    payload = request.model_dump(mode="json")
    digest = hashlib.sha256(json.dumps({"source": source_event_seq, "stage": stage,
        "identity": identity, "request": payload}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:32]
    return ClarificationView(id=digest, stage=stage, source_event_seq=source_event_seq, **payload)


def get_clarification_context(metadata: dict | None) -> dict | None:
    value = (metadata or {}).get("clarification_reply")
    if not isinstance(value, dict):
        return None
    if (value.get("version") != 1 or not isinstance(value.get("original_request"), str)
            or not isinstance(value.get("turns"), list)
            or not isinstance(value.get("clarification_id"), str)):
        raise ValueError("确认记录不完整，请重新提出分析请求。")
    return deepcopy(value)


def pending_clarification(events) -> tuple[MessageEvent, MessageEvent, ClarificationView, bool] | None:
    """A question is answerable only after Wait, and only before the next input.

    Done/Error invalidate it. No plan, model prose or session status can create
    a pending question. The question must identify its actual source user event.
    """
    source = None
    pending = None
    for event in events:
        if isinstance(event, MessageEvent) and event.role == "user":
            source, pending = event, None
        elif isinstance(event, MessageEvent) and event.role == "assistant":
            raw = (event.metadata or {}).get("clarification")
            if raw is None:
                continue
            try:
                view = ClarificationView.model_validate(raw)
            except (ValueError, TypeError):
                continue
            if source is not None and type(source.seq) is int and view.source_event_seq == source.seq:
                pending = (source, event, view, False)
        elif isinstance(event, WaitEvent) and pending is not None:
            pending = (*pending[:3], True)
        elif isinstance(event, (DoneEvent, ErrorEvent)):
            pending = None
    return pending


def build_clarification_reply(events, answer: str, clarification_id: str | None = None) -> dict | None:
    pending = pending_clarification(events)
    if pending is None:
        if clarification_id is not None:
            raise ValueError("这条确认问题已失效或已经回答，请查看最新问题。")
        latest_user = next((event for event in reversed(events)
                            if isinstance(event, MessageEvent) and event.role == "user"), None)
        if (latest_user is not None and (latest_user.metadata or {}).get("clarification_reply")
                and latest_user.message == answer):
            raise ValueError("这条确认已经回答，请查看原请求的进展，或提出新的请求。")
        return None
    source, question, view, ready = pending
    if clarification_id is not None and view.id != clarification_id:
        raise ValueError("这条确认问题已失效，请回答当前最新问题。")
    if not ready:
        raise ValueError("确认问题尚未发布完成，请稍后重试。")
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("请填写对确认问题的回答。")
    # A replacement request may itself need clarification. Its answers must
    # not resurrect the superseded request or execution plan.
    prior = (None if (question.metadata or {}).get("clarification_reset") is True
             else get_clarification_context(source.metadata))
    context = {
        "version": 1, "clarification_id": view.id,
        "stage": prior.get("stage", view.stage) if prior else view.stage,
        "source_event_seq": source.seq,
        "original_event_seq": prior["original_event_seq"] if prior else source.seq,
        "original_request": prior["original_request"] if prior else source.message,
        "turns": [*(prior.get("turns", []) if prior else []), {
            "clarification_id": view.id,
            "questions": [item.model_dump(mode="json") for item in view.questions],
            "answer": answer,
        }],
    }
    execution_context = (question.metadata or {}).get("clarification_execution")
    if not isinstance(execution_context, dict) and prior is not None:
        execution_context = prior.get("execution_context")
    if isinstance(execution_context, dict):
        context["execution_context"] = deepcopy(execution_context)
        context["stage"] = "execution"
        original = execution_context.get("original_request")
        if isinstance(original, str) and original:
            context["original_request"] = original
    return context


def clarified_request(context: dict | None, response: str | None, current_message: str) -> str:
    """Preserve literal request/answers; a model summary cannot replace scope."""
    if context is None or response != "continue":
        return current_message
    original = context["original_request"]
    turns = [{"questions": turn["questions"], "answer": turn["answer"]} for turn in context["turns"]]
    return original + "\n\n用户针对本请求的确认记录（按时间顺序）：\n" + json.dumps(turns, ensure_ascii=False)


def validate_execution_clarification_scope(
    context: dict, *, input_file_ids: list[str], dataset_ids: list[str],
    metadata: dict, configuration: str,
) -> dict:
    """Validate the original step's scope before restoring any sandbox.

    An answer can refine semantics, but changing inputs or execution access
    requires a new request rather than silently reusing a partially run plan.
    """
    saved = context.get("execution_context")
    if (not isinstance(saved, dict)
            or saved.get("schema") != "analysis_clarification_execution/v1"):
        raise ValueError("待确认任务的执行记录不完整，请重新发起分析。")
    for key, current in (("input_file_ids", input_file_ids), ("dataset_ids", dataset_ids)):
        previous = saved.get(key)
        if (not isinstance(previous, list) or any(not isinstance(item, str) for item in previous)
                or sorted(previous) != sorted(current)):
            raise ValueError("待确认任务的数据范围已经变化，原步骤没有继续执行。请重新提出分析请求，并说明要使用的资料。")
    for key in ("skills", "mcp_servers", "data_connectors"):
        previous = saved.get(key)
        if (not isinstance(previous, list) or any(not isinstance(item, str) for item in previous)
                or sorted(previous) != sorted(metadata.get(key) or [])):
            raise ValueError("待确认任务的技能或工具选择已经变化，原步骤没有继续执行。请重新提出分析请求，并确认技能和工具选择。")
    if (type(saved.get("mcp_access_all")) is not bool
            or saved["mcp_access_all"] != bool(metadata.get("mcp_access_all", False))
            or saved.get("configuration_digest") != configuration):
        raise ValueError("待确认任务的执行配置已经变化，原步骤没有继续执行。请重新发起分析请求。")
    targets = saved.get("controller_target_files")
    if not isinstance(targets, list) or any(not isinstance(item, str) for item in targets):
        raise ValueError("待确认任务的数据范围记录不完整，请重新发起分析。")
    return saved


def clarification_message(view: ClarificationView) -> str:
    lines = ["开始前需要你确认：" if view.stage == "request" else "继续前需要你确认："]
    for number, item in enumerate(view.questions, 1):
        lines.append(f"{number}. {item.question}")
        if item.reason:
            lines.append(item.reason)
        if item.options:
            lines.append("可选：" + "；".join(item.options))
    return "\n\n".join(lines)
