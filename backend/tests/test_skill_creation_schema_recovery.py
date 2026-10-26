"""A long independent plan repairs only invalid fields, retaining its oracle."""
import copy
import json
from unittest.mock import Mock

import pytest
from langchain.messages import AIMessage
from pydantic import ValidationError

from app.domain.services.skills.method_contract import CoverageError
from app.domain.services.skills.skill_creation.schema_repair import build_schema_repair_spec
from app.domain.services.skills.skill_creation_error import SkillCreationError
from app.domain.services.skills.skill_validation import SyntheticValidationPlan, validate_validation_plan
from app.domain.utils.model_response import ModelIncompleteResponseError
from test_skill_creation_gateway import make_gateway, requests
from test_skill_validation_plan import plan_payload, method_contract, sources, interfaces


def broken_plan():
    document = plan_payload()
    assertion = document['scripts'][0]['cases'][0]['assertions'][1]
    assertion['path'] = assertion.pop('pointer')
    return document


def corrections(document):
    with pytest.raises(ValidationError) as failure:
        SyntheticValidationPlan.model_validate(document)
    spec = build_schema_repair_spec(document, failure.value, SyntheticValidationPlan)
    assert spec is not None
    return {'repairs': [{'field_id': item['field_id'], 'action': 'set',
                         'value': 'status.json' if item['location'][-1] == 'path' else '/status'}
                        for item in spec.public_fields()]}


@pytest.mark.asyncio
async def test_independent_plan_field_repair_preserves_every_other_value_and_runs_full_checks():
    original = broken_plan()
    snapshot = copy.deepcopy(original)
    gateway, calls, _ = make_gateway([original, corrections(original)])
    check = Mock(side_effect=lambda value: validate_validation_plan(value, method_contract(), sources(), interfaces()))
    result = await gateway.generate(SyntheticValidationPlan, 'synthetic_validation_plan', 'Design independent tests.',
                                    {}, check, contract=method_contract())
    assert result.model_dump() == SyntheticValidationPlan.model_validate(plan_payload()).model_dump()
    assert original == snapshot
    check.assert_called_once_with(result)
    sent = requests(calls)
    assert len(sent) == 2
    assert sent[1]['schema']['title'] == 'SchemaFieldRepairs'
    assert sent[1]['previous_candidate'] == original
    assert [item['location'][-1] for item in sent[1]['repair_feedback']['field_repairs']] == ['path', 'pointer']
    assert sent[1]['repair_feedback']['document_schema']['title'] == 'SyntheticValidationPlan'


@pytest.mark.asyncio
@pytest.mark.parametrize('malformed', [
    AIMessage(content='not json'),
    AIMessage(content='', response_metadata={'finish_reason': 'length'}),
    ModelIncompleteResponseError('length'),
    {'repairs': [{'field_id': 'F999', 'action': 'set', 'value': 'forbidden'}]},
    {'repairs': [{'field_id': 'F001', 'action': 'set', 'value': {'expected': 0}}]},
])
async def test_bad_correction_does_not_discard_or_replace_original_plan(malformed):
    original = broken_plan()
    gateway, calls, _ = make_gateway([original, malformed, corrections(original)])
    result = await gateway.generate(SyntheticValidationPlan, 'synthetic_validation_plan', 'Design independent tests.',
                                    {}, contract=method_contract())
    assert result.model_dump() == SyntheticValidationPlan.model_validate(plan_payload()).model_dump()
    sent = requests(calls)
    assert len(sent) == 3
    assert sent[1]['previous_candidate'] == sent[2]['previous_candidate'] == original
    assert sent[1]['repair_feedback']['field_repairs'] == sent[2]['repair_feedback']['field_repairs']
    assert all(item['schema']['title'] == 'SchemaFieldRepairs' for item in sent[1:])


@pytest.mark.asyncio
async def test_corrected_schema_cannot_skip_adverse_domain_check():
    original = broken_plan()
    gateway, calls, _ = make_gateway([original, corrections(original)])
    def reject(value):
        raise CoverageError('method_review_rejected')
    with pytest.raises(SkillCreationError) as failure:
        await gateway.generate(SyntheticValidationPlan, 'synthetic_validation_plan', 'Design independent tests.',
                               {}, reject, contract=method_contract())
    assert failure.value.code == 'method_review_rejected'
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_real_governed_driver_audits_failed_original_and_successful_field_repair(monkeypatch):
    from app.domain.services import model_runtime
    from test_skill_model_completion_boundary import setup_boundary, TraceStore
    original = broken_plan()
    responses = [AIMessage(content=json.dumps(value), response_metadata={'finish_reason': 'stop'})
                 for value in (original, corrections(original))]
    _, client, gateway = setup_boundary(monkeypatch, responses, None)
    trace_store = TraceStore()
    with model_runtime.model_execution_scope(user_id='synthetic-owner', session_id='synthetic-session',
                                            task_id='synthetic-task', store=trace_store):
        result = await gateway.generate(SyntheticValidationPlan, 'synthetic_validation_plan',
            'Design independent tests.', {},
            lambda value: validate_validation_plan(value, method_contract(), sources(), interfaces()),
            contract=method_contract())
    assert result.model_dump() == SyntheticValidationPlan.model_validate(plan_payload()).model_dump()
    assert len(client._requests) == 2
    assert [item.status for item in trace_store.records.values()] == ['failed', 'succeeded']
    assert json.loads(client._requests[1]['messages'][1].content)['previous_candidate'] == original
