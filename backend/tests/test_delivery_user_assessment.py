"""Readable stable uploads are deliverable without content or renderer approval."""
from types import SimpleNamespace
import hashlib
import io
from unittest.mock import AsyncMock
import pytest

from app.domain.models.analysis_outcome import DeliverableRequirement
from app.domain.models.event import StepEvent, StepStatus
from app.domain.models.message import Message
from app.domain.models.plan import Step
from app.domain.services.analysis_completion import assess_delivery, verified_deliveries, verified_delivery_counts
from app.domain.services.agent_task_runner import AgentTaskRunner
from test_analysis_delivery_flow import artifact, runner_for
from test_analysis_repair_flow import collect, output, scenario


def mechanical(pair, *, size=None):
    record, info = pair
    record.update(valid=True, reason='delivery_ready', metadata={
        'validation_scope': 'file_integrity', 'content_validation': 'not_performed'})
    if size is not None:
        record['size'] = info.size = size
    return pair


@pytest.mark.parametrize('name,kind,size', [
    ('diagram.svg', 'image', 41), ('undecodable.png', 'image', 7),
    ('uneven.csv', 'table', 14), ('unfinished.json', 'report', 3),
    ('empty.txt', 'report', 0), ('custom.xxcc1', 'data', 0),
    ('notes.md', 'table', 5),
])
@pytest.mark.asyncio
async def test_saved_file_is_delivered_once_without_content_review(name, kind, size):
    pair = mechanical(output(name, kind), size=size)
    runner, flow, step, message, state = scenario([[pair]], [{'kind': kind}])
    events = await collect(runner, message)
    assert len(state['prompts']) == state['drained'] == 1 and step.success
    assert not flow._artifact_repair_requests
    assert all(call.kwargs == {'purpose': 'delivery'} for call in runner._sandbox.validate_artifacts.await_args_list)
    delivered = [item for event in events for item in (getattr(event, 'attachments', None) or [])]
    assert any(item.file_id == pair[1].file_id for item in delivered)
    for item in delivered:
        assert item.metadata['artifact_validation_scope'] == 'file_integrity'
        assert item.metadata['content_assessment'] == 'user_evaluated'
    assert step.outputs['result_assessment']['scientific_review'] == 'not_performed'


def test_empty_markdown_table_slot_means_file_delivery_not_table_content():
    record, info = mechanical(artifact('table.md', 'report'), size=0)
    requirements = [DeliverableRequirement(kind='table', formats=['md'])]
    selected = verified_deliveries([record], [info])
    outcome = assess_delivery(requirements, [record], selected, execution_success=True)
    assert outcome.status == 'succeeded' and not outcome.missing
    assert verified_delivery_counts(selected) == {'report': 1}
    assert 'table_validation' not in record['metadata']


@pytest.mark.asyncio
async def test_discovered_working_directory_is_not_a_failed_file_candidate():
    saved = mechanical(artifact('result.svg', 'image'))
    directory = artifact('work', 'data', valid=False, uploaded=False)
    directory[0].update(reason='not_regular_file', sha256=None, size=None)
    step = Step(success=True, attachments=[saved[0]['path'], directory[0]['path']],
                deliverables=[DeliverableRequirement(kind='image')])
    runner = runner_for(step, [saved, directory])
    await runner._finalize_analysis_step(StepEvent(status=StepStatus.COMPLETED, step=step),
                                         Message(message='生成文件'), [saved[1]])
    assert step.success and step.outcome.issues == []
    assert step.attachments == [saved[0]['path']]


@pytest.mark.asyncio
async def test_explicitly_promised_file_at_directory_path_is_an_engineering_failure():
    directory = artifact('required.csv', 'table', valid=False, uploaded=False)
    directory[0].update(reason='not_regular_file', sha256=None, size=None)
    step = Step(success=True, deliverables=[DeliverableRequirement(kind='table', output_paths=[directory[0]['path']])])
    runner = runner_for(step, [directory])
    await runner._finalize_analysis_step(StepEvent(status=StepStatus.COMPLETED, step=step),
                                         Message(message='保存 required.csv'), [])
    assert not step.success and step.outcome.issues[0].reason_code == 'not_regular_file'


@pytest.mark.asyncio
async def test_mounted_source_and_private_work_tree_never_become_generated_delivery():
    for roots in ['_protected_dataset_roots', '_private_artifact_roots']:
        pair = mechanical(artifact('input-tree/source.csv', 'table'))
        step = Step(success=True, attachments=[pair[0]['path']], deliverables=[])
        runner = runner_for(step, [pair])
        setattr(runner, roots, {'/home/ubuntu/output/input-tree'})
        runner._sandbox.download_file = AsyncMock(side_effect=AssertionError('Source must not be copied'))
        assert await runner._sync_file_to_storage(pair[0]['path']) is None
        await runner._finalize_analysis_step(StepEvent(status=StepStatus.COMPLETED, step=step),
                                             Message(message='说明输入'), [pair[1]])
        assert step.attachments == [] and 'result_set' not in step.outputs
        runner._sandbox.validate_artifacts.assert_not_awaited()
        runner._sandbox.download_file.assert_not_awaited()


@pytest.mark.parametrize('declaration', ['attachment', 'requirement'])
@pytest.mark.asyncio
async def test_explicit_outputs_exclude_unrelated_work_copies_from_delivery(declaration):
    requested = mechanical(artifact('translated/result.svg', 'image'))
    working_copy = mechanical(artifact('working/source.svg', 'image'))
    step = Step(success=True, attachments=[requested[0]['path']] if declaration == 'attachment' else [],
                deliverables=[DeliverableRequirement(kind='image', min_count=2,
                    output_paths=[requested[0]['path']] if declaration == 'requirement' else [])])
    runner = runner_for(step, [requested, working_copy])
    files = await runner._finalize_analysis_step(StepEvent(status=StepStatus.COMPLETED, step=step),
                                                Message(message='生成两个文件'), [requested[1], working_copy[1]])
    assert [item.file_path for item in files] == [requested[0]['path']]
    assert step.outcome.status == 'partial' and step.outcome.missing[0].min_count == 1
    assert all(item['path'] != working_copy[0]['path']
               for call in runner._sandbox.validate_artifacts.await_args_list for item in call.args[0])


@pytest.mark.parametrize('declaration', ['attachment', 'requirement', 'all_outputs'])
@pytest.mark.asyncio
async def test_output_directory_expands_only_to_individually_checked_files(declaration):
    directory = '/home/ubuntu/output/published'
    first = mechanical(artifact('published/one.svg', 'image'))
    second = mechanical(artifact('published/nested/two.svg', 'image'))
    sibling = mechanical(artifact('published-other/source.svg', 'image'))
    folder = artifact('published', 'data', valid=False, uploaded=False)
    folder[0].update(reason='not_regular_file', sha256=None, size=None)
    scope = '/home/ubuntu/output' if declaration == 'all_outputs' else directory
    requirements = [DeliverableRequirement(kind='image', min_count=2,
                    output_paths=[scope] if declaration == 'requirement' else [])]
    step = Step(success=True, attachments=[scope] if declaration != 'requirement' else [],
                deliverables=requirements)
    runner = runner_for(step, [first, second, sibling, folder])
    files = await runner._finalize_analysis_step(StepEvent(status=StepStatus.COMPLETED, step=step),
        Message(message='交付指定目录'), [first[1], second[1], sibling[1]])
    expected = {first[0]['path'], second[0]['path']}
    if declaration == 'all_outputs':
        expected.add(sibling[0]['path'])
    assert step.success and {item.file_path for item in files} == expected
    assert directory not in step.attachments


@pytest.mark.asyncio
async def test_scoped_discovery_does_not_download_or_upload_other_work_files():
    requested = '/home/ubuntu/output/results/final.txt'
    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._active_artifact_delivery_scope = ('/home/ubuntu/output/results',)
    runner._list_sandbox_artifacts = AsyncMock(return_value=[requested, '/home/ubuntu/output/work/source.txt'])
    runner._sandbox = SimpleNamespace()
    runner._read_artifact_with_fingerprint = AsyncMock(return_value=(io.BytesIO(b'data'), (4, 'a' * 64)))
    runner._sync_file_to_storage = AsyncMock(return_value=artifact('results/final.txt', 'report')[1])
    runner._generated_files = []
    files = await runner._sync_discovered_artifacts_to_storage()
    assert [item.file_path for item in files] == [requested]
    runner._read_artifact_with_fingerprint.assert_awaited_once_with(requested)
    assert runner._sync_file_to_storage.await_count == 1


@pytest.mark.parametrize('reuse', [False, True])
@pytest.mark.asyncio
async def test_delivery_metadata_is_written_on_upload_and_reuse_does_not_duplicate(reuse):
    body = b'saved bytes'
    digest = hashlib.sha256(body).hexdigest()
    path = '/home/ubuntu/output/result.txt'
    _, info = artifact('result.txt', 'report', digest=digest)
    info.size = len(body)
    info.metadata['artifact_size'] = len(body)
    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._agent_id, runner._session_id, runner._user_id = 'agent', 'session', 'owner'
    runner._session_repository = SimpleNamespace(get_file_by_path=AsyncMock(return_value=info if reuse else None),
        add_file=AsyncMock(), remove_file=AsyncMock())
    runner._upload_file_to_storage = AsyncMock(return_value=info)
    result = await runner._sync_file_to_storage(path, file_data=io.BytesIO(body))
    assert result is info
    assert result.metadata['artifact_validation_scope'] == 'file_integrity'
    assert result.metadata['content_assessment'] == 'user_evaluated'
    if reuse:
        runner._upload_file_to_storage.assert_not_awaited()
        runner._session_repository.add_file.assert_not_awaited()
    else:
        metadata = runner._upload_file_to_storage.await_args.kwargs['metadata']
        assert metadata['artifact_validation_scope'] == 'file_integrity'
        assert metadata['content_assessment'] == 'user_evaluated'
        runner._session_repository.add_file.assert_awaited_once()
