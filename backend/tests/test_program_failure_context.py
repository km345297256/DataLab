"""First-failure context is bounded, launch-bound and never retry authority."""
import hashlib
import json
from types import SimpleNamespace

import pytest
from langchain.messages import AIMessage, ToolMessage

from app.domain.models.event import ToolEvent, ToolStatus
from app.domain.services.analysis_program_diagnostics import (
    MAX_COMPARISON_SOURCE_BYTES, MAX_FAILURE_CONTEXT_BYTES, ProgramDiagnosticPlanner,
)
from app.domain.services.analysis_progress import AnalysisProgressGuard
from app.domain.services.analysis_recovery import analysis_recovery_scope
from app.domain.services.execution_evidence import ToolExecutionLedger
from app.domain.services.tools.shell import ShellToolkit
from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox
from test_program_diagnostic_planner import PROGRAM, record_failure


SOURCE = 'values = [1, 2]\nresult = values[9]\nprint(result)\n'


def fixture(source=SOURCE, *, line=2):
    sandbox = object.__new__(DockerSandbox)
    sandbox._container_name = "first-failure-context"
    tool = ShellToolkit(sandbox).get_tool("program_run")
    ledger, progress = ToolExecutionLedger(), AnalysisProgressGuard()
    call = record_failure(sandbox, tool, progress, ledger, source=source, line=line,
                          exception="IndexError")
    return SimpleNamespace(sandbox=sandbox, tool=tool, ledger=ledger, progress=progress, call=call)


def feedback(value, planner=None):
    return (planner or ProgramDiagnosticPlanner()).failure_feedback(
        program_tool=value.tool, program_call=value.call, ledger=value.ledger)


def packet(notice):
    return json.loads(notice.split("\n", 1)[1])


def test_first_failure_has_exact_context_without_marking_inspection_or_new_evidence():
    value = fixture()
    planner = ProgramDiagnosticPlanner()
    before = repr(value.progress.__dict__)
    notice = feedback(value, planner)
    result = packet(notice)
    assert result["execution"] == {"state": "exited", "returncode": 1,
                                   "source_digest": hashlib.sha256(SOURCE.encode()).hexdigest()}
    assert result["source_context"]["lines"] == [
        {"line": index + 1, "text": line} for index, line in enumerate(SOURCE.splitlines())]
    assert result["source_changes"]["state"] == "unknown"
    assert result["outputs"]["state"] == "not_checked"
    assert "not new input evidence" in notice
    assert repr(value.progress.__dict__) == before
    assert feedback(value, planner) is None  # Reconciliation cannot repeat the packet.


def test_changes_compare_real_launch_snapshots_and_do_not_echo_previous_code():
    original = 'private_old_literal = "OLD_MARKER"\nresult = values[9]\nprint(result)\n'
    value = fixture(original)
    value.call = record_failure(value.sandbox, value.tool, value.progress, value.ledger,
        source=SOURCE, index=1, line=2, exception="IndexError")
    notice = feedback(value)
    change = packet(notice)["source_changes"]
    assert change["state"] == "changed"
    assert change["basis"] == "previous_confirmed_execution"
    assert change["previous_line_range"] == change["executed_line_range"] == [0, 1]
    assert change["previous_source_digest"] == hashlib.sha256(original.encode()).hexdigest()
    assert "OLD_MARKER" not in notice
    assert "last_edit" not in notice


def test_same_source_and_large_source_comparisons_remain_honest_and_bounded():
    value = fixture()
    value.call = record_failure(value.sandbox, value.tool, value.progress, value.ledger,
                               source=SOURCE, index=1, line=2)
    assert packet(feedback(value))["source_changes"]["state"] == "unchanged"
    large = SOURCE + "# trailing context\n" * (MAX_COMPARISON_SOURCE_BYTES // 10)
    value.call = record_failure(value.sandbox, value.tool, value.progress, value.ledger,
                               source=large, index=2, line=2)
    result = packet(feedback(value))
    assert result["source_changes"]["state"] == "changed"
    assert result["source_changes"]["changed_region"] == "not_projected_size_limit"


def test_different_invocation_is_not_reported_as_a_source_repair():
    value = fixture()
    value.call = record_failure(value.sandbox, value.tool, value.progress, value.ledger,
        source=SOURCE.replace("values[9]", "values[7]"), index=1, line=2, argv=["other-input"])
    assert packet(feedback(value))["source_changes"]["state"] == "unknown"


@pytest.mark.parametrize("fault", ["pending", "wrong_call", "impostor", "success", "returncode_mismatch"])
def test_unconfirmed_mismatched_or_nonfailed_executions_never_supply_context(fault):
    value = fixture()
    attempt = next(reversed(value.ledger._attempts.values()))
    if fault == "pending":
        value.ledger.record_unknown("unknown-program")
    elif fault == "wrong_call":
        value.call["args"]["argv"] = ["different-input"]
    elif fault == "impostor":
        value.tool = SimpleNamespace(name="program_run", toolkit=value.tool.toolkit)
    elif fault == "success":
        attempt.program_execution["returncode"] = 0
        attempt.receipt = attempt.receipt.model_copy(update={"returncode": 0})
    else:
        attempt.program_execution["returncode"] = 2
    assert feedback(value) is None


@pytest.mark.parametrize("fault", ["missing", "tampered", "invalid_line", "oversized_line"])
def test_unavailable_source_never_becomes_an_invented_excerpt(fault):
    value = fixture('value = "' + "超长" * 5000 + '"\n', line=1) if fault == "oversized_line" else fixture()
    current = next(reversed(value.ledger._attempts.values())).program_execution
    if fault == "missing":
        current.pop("source_snapshot")
    elif fault == "tampered":
        current["source_snapshot"]["content"] = "UNBOUND_SOURCE_MUST_NOT_APPEAR"
    elif fault == "invalid_line":
        current["diagnostic"]["line"] = 900
    notice = feedback(value)
    assert packet(notice)["source_context"]["lines"] == []
    assert "UNBOUND_SOURCE_MUST_NOT_APPEAR" not in notice
    assert len(notice.encode()) <= MAX_FAILURE_CONTEXT_BYTES


def test_json_escaping_and_long_adjacent_lines_cannot_overflow_projection():
    source = '# ' + "\t" * 20_000 + '\nvalues = [1]\nresult = values[9]\n' + "# tail\n" * 50 + '# DISTANT_PRIVATE_MARKER\n'
    value = fixture(source, line=3)
    notice = feedback(value)
    context = packet(notice)["source_context"]
    assert context["state"] == "available" and context["neighbour_lines_omitted"]
    assert {"line": 3, "text": "result = values[9]"} in context["lines"]
    assert "DISTANT_PRIVATE_MARKER" not in notice
    assert len(notice.encode()) <= MAX_FAILURE_CONTEXT_BYTES


@pytest.mark.asyncio
async def test_first_failure_reaches_next_model_turn_without_reads_or_public_source(monkeypatch):
    from test_program_diagnostic_agent_flow import PROGRAM as FLOW_PROGRAM, native, run, runtime
    from test_tool_response_protocol_recovery import make_agent

    source = '# SNAPSHOT_CONTEXT_ONLY\nitems = [1]\nvalue = items[9]\nprint(value)\n'
    correction = 'value = items[0]'
    agent, _, requests, _ = make_agent(monkeypatch, [
        run("first-run"),
        native("file_str_replace", "correction", file=FLOW_PROGRAM,
               old_str='value = items[9]', new_str=correction),
        run("corrected-run"), AIMessage(content="The corrected program completed."),
    ])
    context = runtime(agent, source=source, failure_exception="IndexError", failure_line=3,
                      success_marker=correction)
    with analysis_recovery_scope(context.recovery):
        events = [event async for event in agent.execute("Process the selected input")]
    failed_response = next(item for item in requests[1]
                          if isinstance(item, ToolMessage) and item.tool_call_id == "first-run")
    assert "HOST PROGRAM FAILURE CONTEXT" in failed_response.content
    assert "SNAPSHOT_CONTEXT_ONLY" in failed_response.content
    assert len(requests) == 4 and len(context.launches) == 2
    assert context.reads == []
    assert not context.recovery.progress.evidence
    first = next(event for event in events if isinstance(event, ToolEvent)
                 and event.tool_call_id == "first-run" and event.status == ToolStatus.CALLED)
    assert "SNAPSHOT_CONTEXT_ONLY" not in first.model_dump_json()
    assert "program-failure-context" not in first.model_dump_json()
