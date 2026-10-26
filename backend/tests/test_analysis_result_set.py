import json
from unittest.mock import AsyncMock
import pytest
from app.domain.services.analysis_result_set import build_result_set
from app.domain.models.event import StepEvent, StepStatus
from app.domain.models.message import Message
from app.domain.models.plan import Step
from test_analysis_delivery_flow import artifact, runner_for


def bundle(pair, **overrides):
    args = dict(owner_id='owner', session_id='session', step_id='step',
                input_identity={'path': '/private/source', 'version': 'v1'},
                files=[pair[1]], records=[pair[0]], executions=[])
    args.update(overrides)
    return build_result_set(**args)


def test_version_set_changes_with_inputs_objects_and_owner_and_hides_paths():
    pair = artifact('plot.png', 'image')
    result = bundle(pair)
    assert result == bundle(pair)
    assert result['files'][0]['state'] == 'current'
    assert result['execution_linkage'] == 'unknown'
    assert '/private/' not in json.dumps(result) and '/home/' not in json.dumps(result)
    assert bundle(pair, owner_id='another')['id'] != result['id']
    assert bundle(pair, input_identity={'version': 'v2'})['id'] != result['id']
    assert bundle(artifact('plot.png', 'image', digest='b'*64))['id'] != result['id']


def test_retained_old_object_is_never_presented_as_current():
    pair = artifact('plot.png', 'image')
    pair[0]['sha256'] = 'b'*64
    assert bundle(pair)['files'][0]['state'] == 'retained_prior_version'
    assert bundle(pair, records=[])['files'][0]['state'] == 'unconfirmed'


def test_execution_versions_hide_private_proofs_and_are_scoped():
    pair = artifact('plot.png', 'image')
    proof = {'operation_id': 'c' * 32, 'source_digest': 'd' * 64}
    result = bundle(pair, executions=[proof, proof])
    public = json.dumps(result)
    assert proof['operation_id'] not in public and proof['source_digest'] not in public
    assert 'operation_id' not in public and 'source_digest' not in public
    assert result['execution_linkage'] == 'observed_step_only'
    assert len(result['executions']) == 1
    assert set(result['executions'][0]) == {'execution_version'}
    assert result == bundle(pair, executions=[proof])
    for scope in ({'owner_id': 'another'}, {'session_id': 'another'}, {'step_id': 'another'}):
        assert bundle(pair, executions=[proof], **scope)['executions'] != result['executions']
    assert bundle(pair, executions=[{**proof, 'source_digest': 'e' * 64}])['executions'] != result['executions']


@pytest.mark.asyncio
async def test_runner_replaces_forged_manifest_and_uses_final_uploaded_objects():
    step = Step(success=True, outputs={'result_set': {'scientific_review': 'passed'}})
    pair = artifact('plot.png', 'image')
    runner = runner_for(step, [pair])
    await runner._finalize_analysis_step(StepEvent(status=StepStatus.COMPLETED, step=step),
                                         Message(message='plot'), [pair[1]])
    result = step.outputs['result_set']
    assert result['scientific_review'] == 'not_performed'
    assert result['files'][0]['file_id'] == pair[1].file_id
    assert result['files'][0]['sha256'] == pair[0]['sha256']
    assert runner._sandbox.validate_artifacts.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('scope', ['missing_owner', 'blank_owner', 'missing_session'])
async def test_legacy_runner_without_trusted_scope_delivers_without_result_set(scope):
    step = Step(success=True, outputs={'result_set': {'scientific_review': 'passed'}})
    pair = artifact('plot.png', 'image')
    runner = runner_for(step, [pair])
    if scope == 'missing_owner':
        del runner._user_id
    elif scope == 'blank_owner':
        runner._user_id = ' '
    else:
        del runner._session_id
    await runner._finalize_analysis_step(StepEvent(status=StepStatus.COMPLETED, step=step),
                                         Message(message='plot'), [pair[1]])
    assert step.success and step.attachments == [pair[1].file_path]
    assert 'result_set' not in step.outputs
