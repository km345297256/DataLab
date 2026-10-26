"""Additive progress preserves old event bytes and never creates execution evidence."""
import json
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter

from app.domain.models.event import AgentEvent
from app.domain.services.execution_history import ExecutionHistory
from app.domain.utils.persistence_contracts import compare_catalogs


ROOT = Path(__file__).resolve().parents[2]
OLD = json.loads((ROOT / "docs/persistence/2026-10-23-native-model-output.json").read_text())
NEW = json.loads((ROOT / "docs/persistence/2026-10-25-skill-creation-progress.json").read_text())
ADAPTERS = {"AgentEvent": TypeAdapter(AgentEvent), "ExecutionHistory": TypeAdapter(ExecutionHistory)}


def record(name):
    replay = Path(__file__).parent / "fixtures/replays/versioned_session.jsonl"
    tool = json.loads(replay.read_text().splitlines()[2])["event"]
    return tool if name == "AgentEvent" else {
        "version": 3, "seq": 4, "event_count": 1, "archive_events": [tool],
    }


def event(record, name):
    return record if name == "AgentEvent" else record["archive_events"][0]


@pytest.mark.parametrize("name", ADAPTERS)
def test_old_records_do_not_acquire_progress_or_change_their_tool_result(name):
    raw = record(name)
    old_reader = Draft202012Validator(OLD["roots"][name]["read_schema"])
    old_reader.validate(raw)
    restored = ADAPTERS[name].validate_python(raw)
    encoded = ADAPTERS[name].dump_python(restored, mode="json")
    assert "skill_creation_progress" not in event(encoded, name)
    assert event(encoded, name)["function_result"] == event(raw, name)["function_result"]
    old_reader.validate(encoded)
    assert ADAPTERS[name].dump_python(ADAPTERS[name].validate_python(encoded), mode="json") == encoded


@pytest.mark.parametrize("name", ADAPTERS)
def test_current_reader_roundtrips_progress_without_execution_or_scientific_authority(name):
    raw = record(name)
    original = deepcopy(event(raw, name))
    progress = {"stage": "workflow", "status": "failed", "attempt": 2, "code": "invalid_schema"}
    event(raw, name)["skill_creation_progress"] = progress
    encoded = ADAPTERS[name].dump_python(ADAPTERS[name].validate_python(raw), mode="json")
    assert event(encoded, name)["skill_creation_progress"] == progress
    assert event(encoded, name)["function_result"] == original["function_result"]
    assert event(encoded, name)["status"] == original["status"]
    assert event(encoded, name).get("program_attempt") is None
    assert ADAPTERS[name].dump_python(ADAPTERS[name].validate_python(encoded), mode="json") == encoded
    # The older schema permits unknown ToolEvent properties, but does not
    # promise typed retention. Schema acceptance alone is not lossless rollback.
    assert "skill_creation_progress" not in OLD["roots"][name]["read_schema"]["$defs"]["ToolEvent"]["properties"]
    Draft202012Validator(OLD["roots"][name]["read_schema"]).validate(encoded)


def test_progress_catalog_is_additive_for_only_its_two_owners():
    changes = compare_catalogs(OLD, NEW)
    assert {change["root"] for change in changes} == set(ADAPTERS)
    for change in changes:
        assert change["classification"] == "compatible"
        assert change["before_version"] == change["after_version"]
        assert all("SkillCreationProgressView" in path or path.endswith("/ToolEvent/properties/skill_creation_progress")
                   for path in change["paths"])
