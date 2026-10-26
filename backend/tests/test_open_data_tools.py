from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.models.event import ToolEvent, ToolStatus
from app.domain.services.tools.data_discovery import DataDiscoveryToolkit
from app.domain.services.tools.tool_contract import audited_public_data_read
from app.interfaces.schemas.event import ToolSSEEvent


def toolkit(service=None):
    return DataDiscoveryToolkit(user_id='owner', session_id='session', service=SimpleNamespace(), open_data_service=service)


@pytest.mark.asyncio
async def test_search_binds_owner_and_session_preserves_need_and_projects_durable_card():
    saved = SimpleNamespace(id='search_123', query='中国教育与就业数据', model_dump=lambda **_: {'id': 'search_123', 'query': '中国教育与就业数据', 'status': 'queued'})
    service = SimpleNamespace(create_search=AsyncMock(return_value=saved))
    tool = toolkit(service).get_tool('data_search')
    result = await tool._arun(query=saved.query)
    owner, session, request = service.create_search.call_args.args
    assert (owner, session, request.query) == ('owner', 'session', saved.query)
    assert result.data['search_id'] == saved.id and result.data['status'] == 'queued'
    event = ToolEvent(tool_call_id='call', tool_name='data_discovery', function_name='data_search', function_args={}, status=ToolStatus.CALLED, function_result=result)
    assert (await ToolSSEEvent.from_event_async(event)).data.open_data_search_id == saved.id


@pytest.mark.asyncio
@pytest.mark.parametrize('result', [
    {'success': False, 'data': {'search_id': 'abc'}},
    {'success': True, 'data': {'search_id': '/private/data'}},
    {'success': True, 'data': {'search_id': 'https://example.com'}},
    {'success': True, 'data': []},
])
async def test_failed_or_malformed_result_cannot_project_a_search_reference(result):
    event = ToolEvent(tool_call_id='call', tool_name='data_discovery', function_name='data_search', function_args={}, status=ToolStatus.CALLED, function_result=result)
    assert (await ToolSSEEvent.from_event_async(event)).data.open_data_search_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize('name,args', [
    ('ncbi_search_genomes', {'taxon': 'human'}),
    ('ncbi_describe_genome', {'accession': 'GCF_000009045.1'}),
    ('ncbi_plan_acquisition', {'candidate_id': 'a' * 64}),
    ('data_search', {'query': 'education', 'source_ids': ['ncbi-geo']}),
])
async def test_all_entry_points_respect_explicit_source_selection(name, args):
    tools = toolkit()
    tools.selected_connectors = ['worldbank']
    result = await tools.get_tool(name)._arun(**args)
    assert not result.success and result.data['reason'] == 'connector_not_selected'


def test_metadata_reads_are_audited_but_acquisition_is_a_write():
    tools = toolkit()
    for name in ('data_search', 'data_search_status', 'data_search_continue', 'data_search_resolve'):
        assert audited_public_data_read(tools.get_tool(name))
    assert not audited_public_data_read(tools.get_tool('data_acquire'))
    assert 'external_side_effect' in tools.get_tool('data_acquire').execution_contract['effects']


@pytest.mark.asyncio
async def test_conversational_download_refreshes_existing_card_without_a_duplicate():
    event = ToolEvent(tool_call_id='call', tool_name='data_discovery', function_name='data_acquire', function_args={'search_id': 'saved_search'}, status=ToolStatus.CALLED, function_result={'success': True, 'data': {'status': 'queued'}})
    result = (await ToolSSEEvent.from_event_async(event)).data
    assert result.open_data_search_id is None
    assert result.open_data_update_id == 'saved_search'


@pytest.mark.asyncio
@pytest.mark.parametrize('name,args', [
    ('data_search_continue', {'search_id': 'search'}),
    ('data_search_resolve', {'search_id': 'search', 'candidate_id': 'candidate'}),
    ('data_acquire', {'search_id': 'search', 'candidate_id': 'candidate', 'file_ids': ['file']}),
])
async def test_saved_candidates_cannot_bypass_new_explicit_source_selection(name, args):
    service = SimpleNamespace(get_search=AsyncMock(return_value=SimpleNamespace(
        sources=[SimpleNamespace(id='zenodo')], results=[SimpleNamespace(id='candidate', source_id='zenodo')])))
    tools = toolkit(service)
    tools.selected_connectors = ['worldbank']
    result = await tools.get_tool(name)._arun(**args)
    assert not result.success and result.data['reason'] == 'connector_not_selected'
    assert service.get_search.call_args.args == ('owner', 'session', 'search')


@pytest.mark.asyncio
async def test_hidden_historical_candidate_cannot_bypass_explicit_source_selection():
    service = SimpleNamespace(get_search=AsyncMock(return_value=SimpleNamespace(
        sources=[SimpleNamespace(id='zenodo')], results=[])), create_acquisition=AsyncMock())
    tools = toolkit(service)
    tools.selected_connectors = ['worldbank']
    result = await tools.get_tool('data_acquire')._arun(search_id='search', candidate_id='hidden', file_ids=['file'])
    assert not result.success
    service.create_acquisition.assert_not_awaited()
