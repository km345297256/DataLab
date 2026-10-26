"""Large immutable sources have bounded, version-bound correction evidence."""
import hashlib

import pytest

from app.domain.services.program_source_context import (
    diagnostic_source_context, MAX_DIAGNOSTIC_CONTEXT_BYTES, MAX_DIAGNOSTIC_WINDOWS,
)
from test_program_diagnostic_planner import setup, record_failure, plan, PROGRAM
from app.domain.services.analysis_progress import program_identity
from app.domain.services.execution_identity import private_identity_hmac


def snapshot(source):
    digest = hashlib.sha256(source.encode()).hexdigest()
    return {"version": 1, "encoding": "utf-8", "size_bytes": len(source.encode()),
            "sha256": digest, "content": source}, digest


def context(source, line):
    value, digest = snapshot(source)
    return diagnostic_source_context(value, digest, {"line": line})


def test_large_source_selects_failure_and_related_expressions_without_full_read():
    lines = ["# preamble"] * 5000 + ["before = data.T", *(["# separation"] * 20),
             "broken = data.T * other", *(["# separation"] * 20), "after = data.T"]
    source = "\n".join(lines) + "\n"
    failed_line = lines.index("broken = data.T * other") + 1
    selected = context(source, failed_line)
    assert selected.source_digest == hashlib.sha256(source.encode()).hexdigest()
    assert selected.failed_line == failed_line - 1
    assert selected.related_attributes == ("T",)
    assert len(selected.windows) == 3
    assert selected.windows[0][0] <= failed_line - 1 < selected.windows[0][1]
    assert selected.selected_bytes <= MAX_DIAGNOSTIC_CONTEXT_BYTES
    assert all(end - start <= 7 for start, end in selected.windows)


def test_no_unbounded_neighbor_or_duplicate_overlapping_read():
    source = "#" + "X" * 50000 + "\nfirst = data.values\nsecond = data.values\n"
    selected = context(source, 2)
    assert selected.windows == ((1, 3),)
    assert selected.selected_bytes == len("first = data.values\nsecond = data.values\n".encode())


def test_related_locations_are_bounded_not_an_excuse_to_read_whole_source():
    source = "\n".join("value = data.T\n" + "# gap\n" * 10 for _ in range(100))
    selected = context(source, 1)
    assert len(selected.windows) <= MAX_DIAGNOSTIC_WINDOWS
    assert selected.selected_bytes <= MAX_DIAGNOSTIC_CONTEXT_BYTES


@pytest.mark.parametrize("line", [None, True, -1, 0, 3])
def test_no_guessed_failure_position(line):
    assert context("print(1)\n", line) is None


def test_forged_version_and_giant_failing_line_do_not_authorize_reads():
    value, digest = snapshot("print(missing)\n")
    value["content"] = "print(replacement)\n"
    assert diagnostic_source_context(value, digest, {"line": 1}) is None
    assert context("x = '" + "v" * MAX_DIAGNOSTIC_CONTEXT_BYTES + "'\n", 1) is None


def test_unicode_and_crlf_match_actual_text_mode_file_read():
    selected = context("# 汉字\r\nmissing_name\r\n", 2)
    assert selected.failed_line_content == "missing_name"
    assert selected.selected_bytes == len("# 汉字\nmissing_name\n".encode())


def test_large_source_diagnosis_requires_real_exact_failed_line_then_one_trial():
    source = "# padding\n" * 5000 + "print(missing_name)\n"
    fixture = setup(scope=set())
    fixture.call = record_failure(fixture.sandbox, fixture.runner, fixture.guard, fixture.ledger,
        source=source, exception="NameError", line=5001, index=4, error="f" * 64)
    planned = plan(fixture)
    assert planned and [call["args"]["file"] for call in planned.tool_calls] == [PROGRAM]
    assert planned.tool_calls[0]["args"]["start_line"] == 4997
    state = fixture.guard.programs[program_identity(PROGRAM)]
    assert state.diagnostic_required  # planning has no authority
    # A changed on-disk version must not unlock correction for the old launch.
    fixture.guard.record_program_diagnostic(path=PROGRAM, content_digest="untrusted-content",
        line_observation={"start_line": 5000, "line_digests": ["different-line"]})
    assert state.diagnostic_required
    line_hash = private_identity_hmac({"purpose": "program-diagnostic-line/v1",
                                      "content": "print(missing_name)"})
    fixture.guard.record_program_diagnostic(path=PROGRAM, content_digest="observed-failed-line",
        line_observation={"start_line": 5000, "line_digests": [line_hash]})
    assert not state.diagnostic_required
    assert state.unsuccessful_executions == 4
    assert not fixture.guard.evidence


def test_large_successful_source_is_available_to_private_skill_evidence():
    from app.domain.services.program_method_evidence import validated_method_evidence
    from app.domain.services.skills.program_method_evidence import method_program_references
    import re
    source = "# documentation\n" * 5000 + "def method(value):\n    return value * value\n"
    value, digest = snapshot(source)
    record = {"schema": "program-method-evidence/v1", "source_digest": digest,
              "source_snapshot": value, "operation_id": "native-op", "script_path": PROGRAM}
    assert validated_method_evidence(record)["source_snapshot"]["content"] == source
    refs = method_program_references([record], private_values=set(), private_pattern=re.compile("NEVER_MATCH"))
    assert "def method" in refs[0]["source"]
    assert "value * value" in refs[0]["source"]
