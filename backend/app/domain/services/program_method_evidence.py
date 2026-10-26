"""Private immutable source evidence; ordinary event payloads cannot supply it."""
from copy import deepcopy
import re

from app.domain.services.program_execution import validated_source_snapshot


def validated_method_evidence(value) -> dict | None:
    if (type(value) is not dict or value.get("schema") != "program-method-evidence/v1"
            or set(value) != {"schema", "source_digest", "source_snapshot", "operation_id", "script_path"}
            or not isinstance(value.get("operation_id"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value["operation_id"])
            or not isinstance(value.get("script_path"), str)
            or not value["script_path"].startswith("/") or len(value["script_path"]) > 4096):
        return None
    snapshot = validated_source_snapshot(value.get("source_snapshot"), value.get("source_digest"))
    if snapshot is None:
        return None
    return deepcopy({**value, "source_snapshot": snapshot})


def private_method_evidence_for_event(event) -> dict | None:
    from app.domain.models.event import ToolEvent, ToolStatus
    if (not isinstance(event, ToolEvent) or event.status != ToolStatus.CALLED
            or event.dispatch_status == "not_started" or event.program_attempt is None
            or event.program_attempt.state != "succeeded" or event.program_attempt.returncode != 0):
        return None
    # No fallback to function_result, metadata, arguments, or model prose.
    return validated_method_evidence(event._program_method_evidence)
