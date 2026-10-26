"""Actual per-launch facts must survive an inaccurate model failure narrative."""
import hashlib
import json

import pytest

from app.domain.models.analysis_outcome import AnalysisOutcome
from app.domain.models.event import StepEvent, StepStatus
from app.domain.models.message import Message
from app.domain.models.plan import Step
from app.domain.services.execution_report import (
    checked_execution_records, execution_history_message, record_program_execution, record_program_preparation,
)
from test_analysis_delivery_flow import runner_for


def failure(kind, source, line, operation):
    digest = hashlib.sha256(source.encode()).hexdigest()
    return {"returncode": 1, "operation_id": operation, "source_digest": digest,
            "diagnostic": {"exception_type": kind, "line": line},
            "source_snapshot": {"version": 1, "encoding": "utf-8", "size_bytes": len(source.encode()),
                                "sha256": digest, "content": source}}


def history():
    outcome = {}
    record_program_execution(outcome, failure("ModuleNotFoundError", "import sklearn\n", 1, "a"))
    record_program_execution(outcome, failure("TypeError", "import json\njson.dumps({(1, 2): 3})\n", 2, "b"))
    record_program_execution(outcome, failure("ValueError", "a, b, c = (1, 2, 3, 4)\n", 1, "c"))
    return outcome


def test_three_distinct_errors_stay_distinct_without_private_source_or_exception_text():
    execution = history()
    records = checked_execution_records(execution)
    assert [item.exception_type for item in records] == ["ModuleNotFoundError", "TypeError", "ValueError"]
    assert records[0].category == "dependency" and records[1].category == "serialization"
    assert records[2].category != "serialization"
    outcome = AnalysisOutcome(status="failed", reason_code="analysis_no_progress_loop", execution_records=records)
    encoded = outcome.model_dump_json()
    for private in ("source_digest", "operation_id", "source_snapshot", "json.dumps", "script_path"):
        assert private not in encoded
    assert "ModuleNotFoundError" in execution_history_message(records)


@pytest.mark.asyncio
async def test_failed_publication_replaces_inaccurate_model_error_summary_with_observed_history():
    execution = {**history(), "code": "analysis_no_progress_loop"}
    step = Step(success=False, result="三次都在 JSON 写出失败，算法已完成。", outputs={"execution_outcome": execution})
    runner = runner_for(step, [])
    await runner._finalize_analysis_step(StepEvent(status=StepStatus.FAILED, step=step), Message(message="分析"), [])
    assert not step.success
    assert "三次都" not in step.result and "算法已完成" not in step.result
    assert all(name in step.result for name in ("ModuleNotFoundError", "TypeError", "ValueError"))
    assert len(step.outcome.execution_records) == 3


def test_preflight_is_distinct_from_program_failure_and_success_does_not_erase_prior_errors():
    execution = {}
    record_program_preparation(execution, {"ready": False, "dependencies": {"state": "missing"}})
    record_program_execution(execution, {"returncode": 0})
    records = checked_execution_records(execution)
    assert records[0].state == "not_started" and records[0].returncode is None
    assert records[1].state == "succeeded"
    text = execution_history_message(records)
    assert "程序未启动" in text and "程序退出成功" in text


def test_malformed_untrusted_records_are_not_projected_and_history_is_bounded():
    execution = history()
    execution["execution_records"].extend([
        {"sequence": 4, "state": "failed", "returncode": 1, "exception_type": "/private/credential"},
        {"sequence": 5, "state": "succeeded", "returncode": 1},
    ])
    assert len(checked_execution_records(execution)) == 3
    execution = {}
    for _ in range(70):
        record_program_execution(execution, {"returncode": 1, "diagnostic": {"exception_type": "SecretCustomError"}})
    records = checked_execution_records(execution)
    assert len(records) == 64 and records[0].sequence == 7
    assert all(item.exception_type == "OtherError" for item in records)
    assert "更早记录" in execution_history_message(records)
    assert "SecretCustomError" not in json.dumps(execution)


def test_bypassed_model_copy_does_not_leak_extra_fields_to_public_outcome():
    outcome = AnalysisOutcome(status="failed", reason_code="execution_failed").model_copy(update={
        "execution_records": [{"sequence": 1, "state": "failed", "returncode": 1,
                               "source_snapshot": "PRIVATE"}]})
    assert "PRIVATE" not in outcome.model_dump_json()


def test_late_execution_confirmation_is_recorded_once_from_its_original_call():
    from app.domain.services.agents.base import BaseAgent
    from test_program_attempt_presentation import bound
    tool, call, ledger = bound()
    agent = object.__new__(BaseAgent)
    agent._tool_execution_ledger = ledger
    agent._program_report_calls = {call["id"]: (tool, call)}
    agent.last_execution_outcome = {}
    agent._refresh_execution_evidence()
    agent._refresh_execution_evidence()
    records = checked_execution_records(agent.last_execution_outcome)
    assert len(records) == 1 and records[0].state == "failed"
    assert "operation_id" not in json.dumps(agent.last_execution_outcome["execution_records"])
