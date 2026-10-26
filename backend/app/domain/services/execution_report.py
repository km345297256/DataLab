"""Bounded failure history built exclusively at the first-party execution boundary."""
from typing import get_args

from app.domain.models.execution_record import ExecutionRecord


MAX_EXECUTION_RECORDS = 64
CATEGORY_LABELS = {
    "dependency": "运行依赖不可用", "code": "程序代码错误", "data": "数据或数据访问错误",
    "serialization": "结果序列化失败", "publication": "结果保存失败", "runtime": "程序运行失败，原因尚待诊断",
}


def record_program_execution(outcome: dict, feedback: dict) -> None:
    """Caller must supply adapter/ledger-verified feedback, never public tool text."""
    code = feedback.get("returncode")
    if type(code) is not int:
        return
    from app.domain.services.analysis_failure import classify_program_failure
    diagnostic = feedback.get("diagnostic")
    diagnostic = diagnostic if isinstance(diagnostic, dict) else {}
    exception = diagnostic.get("exception_type")
    allowed = get_args(get_args(ExecutionRecord.model_fields["exception_type"].annotation)[0])
    if exception is not None and exception not in allowed:
        exception = "OtherError"
    category = classify_program_failure(diagnostic,
        source_snapshot=feedback.get("source_snapshot"), source_digest=feedback.get("source_digest")) if code else None
    _append(outcome, state="failed" if code else "succeeded", returncode=code,
            category=category, exception_type=exception if code else None)


def record_program_preparation(outcome: dict, prerequisites: dict | None) -> None:
    if not prerequisites or prerequisites.get("ready") is not False:
        return
    dependencies = prerequisites.get("dependencies") or {}
    syntax = prerequisites.get("syntax") or {}
    category = ("dependency" if dependencies.get("state") in {"missing", "unavailable"}
                else "code" if syntax.get("state") == "invalid" else "runtime")
    exception = syntax.get("exception_type") if syntax.get("state") == "invalid" else None
    _append(outcome, state="not_started", category=category, exception_type=exception)


def _append(outcome: dict, **fields) -> None:
    records = outcome.setdefault("execution_records", [])
    count = records[-1]["sequence"] + 1 if records else 1
    outcome["execution_record_count"] = count
    records.append(ExecutionRecord(sequence=count, **fields).model_dump(exclude_none=True))
    del records[:-MAX_EXECUTION_RECORDS]


def checked_execution_records(execution: dict) -> list[ExecutionRecord]:
    raw = execution.get("execution_records")
    if not isinstance(raw, list):
        return []
    records = []
    for item in raw[-MAX_EXECUTION_RECORDS:]:
        try:
            record = ExecutionRecord.model_validate(item)
        except (ValueError, TypeError):
            continue
        if not records or record.sequence > records[-1].sequence:
            records.append(record)
    return records


def execution_history_message(records: list[ExecutionRecord]) -> str:
    failures = [item for item in records if item.state != "succeeded"]
    if not failures:
        return ""
    lines = ["实际程序记录："]
    if records[0].sequence > 1:
        lines.append("仅展示最近的运行记录，更早记录请查看运行详情。")
    for item in records:
        if item.state == "succeeded":
            detail = "程序退出成功；分析及文件交付状态以本阶段检查结果为准"
        else:
            detail = CATEGORY_LABELS.get(item.category, "程序运行失败")
            if item.exception_type:
                detail += f"（{item.exception_type}）"
            if item.state == "not_started":
                detail += "；预检未通过，程序未启动"
        lines.append(f"- 第 {item.sequence} 次程序请求：{detail}。")
    return "\n".join(lines)
