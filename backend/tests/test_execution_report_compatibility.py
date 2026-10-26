"""Bidirectional evidence for optional, enum-only persisted execution records.

The new reader accepts old data. The frozen strict old reader rejects populated
new execution records: additive schema classification is not rollback approval.
"""
import json
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter, ValidationError

from app.domain.models.event import AgentEvent
from app.domain.models.plan import Plan, Step
from app.domain.services.execution_history import ExecutionHistory
from app.domain.utils.persistence_contracts import compare_catalogs


ROOT = Path(__file__).resolve().parents[2]
DIRECTORY = ROOT / "docs/persistence"
OLD = json.loads((DIRECTORY / "2026-10-21-datalab-analysis.json").read_text())
NEW = json.loads((DIRECTORY / "2026-10-22-analysis-recovery.json").read_text())
FROZEN = json.loads((Path(__file__).parent / "fixtures/persistence/datalab-pre-v2-records.json").read_text())
ADAPTERS = {"AgentEvent": TypeAdapter(AgentEvent), "ExecutionHistory": TypeAdapter(ExecutionHistory),
            "Plan": TypeAdapter(Plan), "Step": TypeAdapter(Step)}
RECORDS = [
    {"sequence": 1, "state": "not_started", "category": "dependency"},
    {"sequence": 2, "state": "failed", "returncode": 1, "category": "dependency", "exception_type": "ModuleNotFoundError"},
    {"sequence": 3, "state": "failed", "returncode": 1, "category": "serialization", "exception_type": "TypeError"},
    {"sequence": 4, "state": "failed", "returncode": 1, "category": "code", "exception_type": "ValueError"},
    {"sequence": 5, "state": "failed", "returncode": 1, "category": "data", "exception_type": "FileNotFoundError"},
    {"sequence": 6, "state": "failed", "returncode": 1, "category": "publication", "exception_type": "OSError"},
    {"sequence": 7, "state": "failed", "returncode": 1, "category": "runtime", "exception_type": "RuntimeError"},
    {"sequence": 8, "state": "succeeded", "returncode": 0},
]


def legacy_record(name):
    if name == "ExecutionHistory":
        # This is a v3 cache envelope with a frozen old event, not a current
        # model dump which could accidentally introduce the new default field.
        return {"version": 3, "seq": 3, "event_count": 1,
                "latest_plan": deepcopy(FROZEN["records"]["AgentEvent"])}
    return deepcopy(FROZEN["records"][name])


def outcome(record, name):
    if name == "ExecutionHistory":
        return record["latest_plan"]["plan"]["steps"][0]["outcome"]
    if name == "AgentEvent":
        return record["plan"]["steps"][0]["outcome"]
    if name == "Plan":
        return record["steps"][0]["outcome"]
    return record["outcome"]


def step_of(value, name):
    if name == "ExecutionHistory":
        return value.latest_plan.plan.steps[0]
    if name == "AgentEvent":
        return value.plan.steps[0]
    if name == "Plan":
        return value.steps[0]
    return value


def nested_errors(errors):
    for error in errors:
        yield error
        yield from nested_errors(error.context)


@pytest.mark.parametrize("name", ADAPTERS)
def test_new_reader_keeps_missing_legacy_execution_history_missing_on_write(name):
    raw = legacy_record(name)
    old_reader = Draft202012Validator(OLD["roots"][name]["read_schema"])
    old_reader.validate(raw)
    assert "execution_records" not in outcome(raw, name)
    adapter = ADAPTERS[name]
    restored = adapter.validate_python(raw)
    step = step_of(restored, name)
    assert step.outcome.execution_records == []
    assert step.outcome.status == "partial" and step.outcome.scope == "step"
    assert step.outcome.can_resume is False and step.outcome.resume_from is None
    assert step.outcome.reason_code == outcome(raw, name)["reason_code"]
    assert step.success is False
    encoded = adapter.dump_python(restored, mode="json")
    assert "execution_records" not in outcome(encoded, name)  # exclude_if, not exclude_unset
    assert outcome(encoded, name) == outcome(raw, name)
    old_reader.validate(encoded)
    assert adapter.dump_python(adapter.validate_json(adapter.dump_json(restored)), mode="json") == encoded
    if name == "ExecutionHistory":
        assert not restored.reviewed_steps_by_plan and not restored.contextual_steps_by_plan
        assert restored.version == 3 and restored.seq == 3 and restored.event_count == 1


@pytest.mark.parametrize("name", ADAPTERS)
def test_explicit_empty_execution_records_are_also_omitted_for_old_readers(name):
    raw = legacy_record(name)
    outcome(raw, name)["execution_records"] = []
    adapter = ADAPTERS[name]
    encoded = adapter.dump_python(adapter.validate_python(raw), mode="json")
    assert "execution_records" not in outcome(encoded, name)
    Draft202012Validator(OLD["roots"][name]["read_schema"]).validate(encoded)


@pytest.mark.parametrize("name", ADAPTERS)
def test_new_records_roundtrip_without_reclassifying_stage_or_dropping_program_facts(name):
    raw = legacy_record(name)
    outcome(raw, name)["execution_records"] = deepcopy(RECORDS)
    adapter = ADAPTERS[name]
    restored = adapter.validate_python(raw)
    encoded = adapter.dump_python(restored, mode="json")
    assert outcome(encoded, name)["execution_records"] == RECORDS
    Draft202012Validator(NEW["roots"][name]["read_schema"]).validate(encoded)
    Draft202012Validator(NEW["roots"][name]["write_schema"]).validate(encoded)
    reloaded = adapter.validate_json(adapter.dump_json(restored))
    assert adapter.dump_python(reloaded, mode="json") == encoded
    step = step_of(reloaded, name)
    assert step.outcome.status == "partial" and step.outcome.can_resume is False
    assert step.success is False  # a later exit(0) does not certify the stage
    assert [item.category for item in step.outcome.execution_records] == [item.get("category") for item in RECORDS]
    if name == "ExecutionHistory":
        assert not reloaded.reviewed_steps_by_plan and not reloaded.contextual_steps_by_plan


@pytest.mark.parametrize("name", ADAPTERS)
def test_old_strict_reader_explicitly_rejects_nonempty_records_not_a_supported_rollback(name):
    raw = legacy_record(name)
    outcome(raw, name)["execution_records"] = deepcopy(RECORDS)
    adapter = ADAPTERS[name]
    encoded = adapter.dump_python(adapter.validate_python(raw), mode="json")
    errors = list(Draft202012Validator(OLD["roots"][name]["read_schema"]).iter_errors(encoded))
    assert errors, "A pre-upgrade reader must not silently discard new execution history"
    assert any(error.validator == "additionalProperties" and "execution_records" in error.message
               for error in nested_errors(errors))


@pytest.mark.parametrize("field,value", [("state", "secret-state"), ("category", "secret-category"),
                                        ("exception_type", "PrivateExceptionMessage"), ("sequence", True)])
def test_new_reader_rejects_non_enum_or_coerced_execution_facts(field, value):
    raw = legacy_record("Step")
    record = dict(RECORDS[1])
    record[field] = value
    outcome(raw, "Step")["execution_records"] = [record]
    with pytest.raises(ValidationError):
        ADAPTERS["Step"].validate_python(raw)


@pytest.mark.parametrize("version", [1, 2])
def test_old_projection_versions_still_require_rebuild_instead_of_relabeling(version):
    raw = legacy_record("ExecutionHistory")
    raw["version"] = version
    with pytest.raises(ValidationError):
        ExecutionHistory.model_validate(raw)


def test_snapshot_registers_only_four_additive_roots_with_unchanged_writer_versions():
    changes = compare_catalogs(OLD, NEW)
    assert {change["root"] for change in changes} == set(ADAPTERS)
    for change in changes:
        assert change["classification"] == "compatible"
        assert change["before_version"] == change["after_version"]
        assert all(path.endswith("/AnalysisOutcome/properties/execution_records")
                   or path.endswith("/$defs/ExecutionRecord") for path in change["paths"])
