"""Preserve the actual pre-migration DataLab contracts beside v2 additions."""
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import TypeAdapter

from app.domain.models.event import AgentEvent, PlanEvent, RunEvent
from app.domain.models.plan import Plan, Step
from app.domain.services.execution_history import ExecutionHistory


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = json.loads((Path(__file__).parent / "fixtures/persistence/datalab-pre-v2-records.json").read_text())
ADAPTERS = {"AgentEvent": TypeAdapter(AgentEvent), "Plan": TypeAdapter(Plan), "Step": TypeAdapter(Step)}


@pytest.mark.parametrize("name", ADAPTERS)
def test_pre_migration_datalab_records_keep_stage_and_data_contract(name):
    adapter = ADAPTERS[name]
    value = adapter.validate_python(FIXTURE["records"][name])
    dumped = adapter.dump_python(value, mode="json")
    # The pre-migration DataLab reader still accepts the resulting record.
    Draft202012Validator(FIXTURE["read_schemas"][name]).validate(dumped)
    restored = adapter.validate_python(dumped)
    step = restored.plan.steps[0] if name == "AgentEvent" else restored.steps[0] if name == "Plan" else restored
    assert step.outcome.scope == "step"
    assert step.outcome.status == "partial"
    assert step.deliverables[0].kind == step.outcome.missing[0].kind == "data"
    assert step.outcome.missing[0].formats == ["fastq"]
    assert step.outcome.can_resume is False
    assert "result_assessment" not in step.outputs


def test_run_identity_survives_readback_and_cache_does_not_invent_result_evidence():
    run = TypeAdapter(AgentEvent).validate_python(FIXTURE["records"]["RunEvent"])
    plan = TypeAdapter(AgentEvent).validate_python(FIXTURE["records"]["AgentEvent"])
    assert isinstance(run, RunEvent) and isinstance(plan, PlanEvent)
    assert run.run_id == plan.run_id == "a" * 32
    Draft202012Validator(FIXTURE["read_schemas"]["AgentEvent"]).validate(run.model_dump(mode="json"))
    cache = ExecutionHistory()
    cache.fold(run)
    cache.fold(plan)
    restored = ExecutionHistory.model_validate_json(cache.model_dump_json())
    assert restored.latest_plan.run_id == run.run_id
    assert restored.latest_plan.plan.steps[0].outcome.scope == "step"
    assert not restored.contextual_steps_by_plan
    assert not restored.reviewed_steps_by_plan


def test_upstream_v2_reader_contract_is_not_claimed_as_a_datalab_downgrade():
    source = json.loads((ROOT / "docs/persistence/2026-09-30-program-preparation.json").read_text())
    # DataLab already had these extensions before this migration. A DataSeek
    # v2 reader is not a valid rollback target for its event log or data kinds.
    schema = source["roots"]["AgentEvent"]["read_schema"]
    assert not Draft202012Validator(schema).is_valid(FIXTURE["records"]["RunEvent"])
    assert not Draft202012Validator(schema).is_valid(FIXTURE["records"]["AgentEvent"])


def test_pre_migration_reader_refuses_new_nonempty_private_delivery_contract():
    step = Step.model_validate(FIXTURE["records"]["Step"])
    step.deliverables[0].output_paths = ["/home/ubuntu/output/result.fastq"]
    step.deliverables[0].objective = "Preserve all selected records"
    encoded = step.model_dump(mode="json")
    assert Step.model_validate(encoded).deliverables[0].output_paths == ["/home/ubuntu/output/result.fastq"]
    assert not Draft202012Validator(FIXTURE["read_schemas"]["Step"]).is_valid(encoded)
