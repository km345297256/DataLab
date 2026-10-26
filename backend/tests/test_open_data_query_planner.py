import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.application.services import open_data_query_planner as planner


def mock_model(monkeypatch, content=None, error=None):
    runnable = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(content=content), side_effect=error))
    monkeypatch.setattr(planner, 'get_settings', lambda: SimpleNamespace())
    monkeypatch.setattr(planner, 'create_chat_model', lambda *a, **kw: SimpleNamespace(bind=lambda **_: runnable))
    return runnable


@pytest.mark.asyncio
async def test_chinese_need_translates_but_cannot_add_unselected_sources(monkeypatch):
    model = mock_model(monkeypatch, '{"search_query":"education employment", "source_ids":["worldbank","untrusted"], "notes":["地域和时间范围仍需核对"]}')
    result = await planner.plan_open_data_query('寻找2010年以来中国教育与就业数据', ['worldbank'])
    assert result['source_ids'] == ['worldbank']
    assert result['search_query'] == 'education employment'
    assert result['source_queries'] == {'worldbank': 'education employment'}
    assert '2010' in model.ainvoke.call_args.args[0][1].content


@pytest.mark.asyncio
@pytest.mark.parametrize('content', ['invalid', '[]', '{"search_query": 12}', '{"search_query": ""}'])
async def test_invalid_plan_falls_back_without_losing_original_constraints(monkeypatch, content):
    mock_model(monkeypatch, content)
    result = await planner.plan_open_data_query('中国近十年数据', ['zenodo', 'datacite'])
    assert result['search_query'] == '中国近十年数据'
    assert result['source_ids'] == ['zenodo', 'datacite']
    assert result['source_queries'] == {'zenodo': '中国近十年数据', 'datacite': '中国近十年数据'}


@pytest.mark.asyncio
async def test_model_unavailable_does_not_block_discovery(monkeypatch):
    mock_model(monkeypatch, error=TimeoutError())
    result = await planner.plan_open_data_query('rainfall', ['zenodo'])
    assert result['search_query'] == 'rainfall'


@pytest.mark.asyncio
async def test_quoted_dataset_title_is_not_translated_or_expanded(monkeypatch):
    model = mock_model(monkeypatch, error=AssertionError('literal lookup must not call model'))
    title = '"Human Iris Color Data"'
    result = await planner.plan_open_data_query(title, ['harvard-dataverse'])
    assert result['source_queries'] == {'harvard-dataverse': title}
    assert result['search_query'] == title
    model.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_planner_is_told_about_mandatory_term_semantics(monkeypatch):
    import json
    model = mock_model(monkeypatch, '{"search_query":"iris color", "source_ids":["harvard-dataverse"], "source_queries":{"harvard-dataverse":"iris color"}}')
    await planner.plan_open_data_query('寻找人类虹膜颜色数据', ['harvard-dataverse'])
    messages = model.ainvoke.call_args.args[0]
    assert json.loads(messages[1].content)['source_capabilities'][0]['query_semantics'] == 'all_terms'
    assert 'Never concatenate a translation' in messages[0].content


@pytest.mark.asyncio
async def test_planner_keeps_language_per_executable_source_and_preserves_manual_scope(monkeypatch):
    model = mock_model(monkeypatch, json.dumps({
        'search_query': '教育与就业', 'source_ids': ['worldbank'],
        'source_queries': {'zenodo': '中国 教育 就业', 'worldbank': 'education employment', 'untrusted': 'injected'},
    }))
    result = await planner.plan_open_data_query('寻找中国教育与就业数据', ['zenodo', 'worldbank'])
    assert result['source_ids'] == ['worldbank']
    assert result['source_queries'] == {'worldbank': 'education employment'}
    assert 'zenodo' in result['unavailable_sources']
    manual = await planner.plan_open_data_query('寻找中国教育与就业数据', ['zenodo', 'worldbank'], explicit_sources=True)
    assert manual['source_ids'] == ['zenodo', 'worldbank']
    assert manual['source_queries'] == {'zenodo': '中国 教育 就业', 'worldbank': 'education employment'}
    request = json.loads(model.ainvoke.call_args.args[0][1].content)
    assert request['allowed_sources'] == ['zenodo', 'worldbank']
    capabilities = {source['id']: source for source in request['source_capabilities']}
    assert set(capabilities) == {'zenodo', 'worldbank'}
    assert capabilities['worldbank']['discovery_mode'] == 'observations'
    assert capabilities['zenodo']['languages'] == ['en', 'zh']


@pytest.mark.asyncio
@pytest.mark.parametrize('source_queries', [None, [], {'zenodo': 4}, {'zenodo': ''}, {'zenodo': 'x' * 301}])
async def test_malformed_source_query_falls_back_to_usable_general_terms(monkeypatch, source_queries):
    mock_model(monkeypatch, json.dumps({'search_query': 'soil moisture', 'source_ids': ['zenodo'],
                                      'source_queries': source_queries}))
    result = await planner.plan_open_data_query('查找土壤水分数据', ['zenodo', 'not-registered', 'zenodo'])
    assert result['source_ids'] == ['zenodo']
    assert result['source_queries'] == {'zenodo': 'soil moisture'}


@pytest.mark.asyncio
async def test_every_enabled_source_can_be_planned_without_a_separate_allowlist(monkeypatch):
    from app.domain.models.open_data_registry import SOURCES
    allowed = sorted(SOURCES)
    mock_model(monkeypatch, json.dumps({'search_query': 'carbon', 'source_ids': allowed}))
    result = await planner.plan_open_data_query('carbon', allowed)
    assert set(result['source_ids']) | set(result['unavailable_sources']) == SOURCES
    assert set(result['source_queries']) == set(result['source_ids'])
    assert {'usgs-earthquake', 'nasa-power', 'noaa-gml'} <= result['unavailable_sources'].keys()


@pytest.mark.asyncio
@pytest.mark.parametrize('proposed', [None, [], ['unknown'], 'zenodo'])
async def test_empty_automatic_route_never_expands_to_every_source(monkeypatch, proposed):
    from app.domain.models.open_data_registry import SOURCES
    allowed = sorted(SOURCES)
    mock_model(monkeypatch, json.dumps({'search_query': 'linguistic corpora', 'source_ids': proposed}))
    result = await planner.plan_open_data_query('研究语言语料', allowed)
    assert result['source_ids'] == ['zenodo', 'datacite', 'harvard-dataverse']
    assert result['source_queries'] == {source: '研究语言语料' for source in result['source_ids']}
    assert len(result['unavailable_sources']) == len(allowed) - 3


@pytest.mark.asyncio
async def test_declared_unavailable_sources_are_never_executed_even_if_model_lists_them(monkeypatch):
    mock_model(monkeypatch, json.dumps({'search_query': 'language corpora',
        'source_ids': ['zenodo', 'usgs-earthquake', 'nasa-power', 'noaa-gml'],
        'source_queries': {'zenodo': 'language corpora', 'usgs-earthquake': 'earthquake',
                           'nasa-power': 'T2M', 'noaa-gml': 'CO2'},
        'unavailable_sources': {'noaa-gml': '未要求气体观测'}}))
    result = await planner.plan_open_data_query('研究语言语料',
        ['zenodo', 'usgs-earthquake', 'nasa-power', 'noaa-gml'], explicit_sources=True)
    assert result['source_ids'] == ['zenodo'] and result['source_queries'] == {'zenodo': 'language corpora'}
    assert set(result['unavailable_sources']) == {'usgs-earthquake', 'nasa-power', 'noaa-gml'}


@pytest.mark.asyncio
async def test_observation_coordinates_and_dates_cannot_be_invented(monkeypatch):
    query = 'latitude=39.9 longitude=116.4 start=20240101 end=20240107 parameters=T2M'
    mock_model(monkeypatch, json.dumps({'search_query': query, 'source_ids': ['nasa-power']}))
    denied = await planner.plan_open_data_query('查北京天气', ['nasa-power'], explicit_sources=True)
    assert denied['source_ids'] == [] and 'nasa-power' in denied['unavailable_sources']
    accepted = await planner.plan_open_data_query('查纬度39.9经度116.4，2024年1月1日到2024年1月7日温度',
                                                 ['nasa-power'], explicit_sources=True)
    assert accepted['source_queries'] == {'nasa-power': query}


def test_fallback_preserves_manual_sources_without_filling_observation_placeholders():
    result = planner.fallback_plan('城市就业', ['worldbank', 'usgs-earthquake'], explicit_sources=True)
    assert result['source_ids'] == ['worldbank']
    assert result['source_queries'] == {'worldbank': '城市就业'}
    assert 'usgs-earthquake' in result['unavailable_sources']


@pytest.mark.asyncio
async def test_planner_passes_session_model_route_without_exposing_it(monkeypatch):
    model = mock_model(monkeypatch, '{"search_query":"climate", "source_ids":["zenodo"]}')
    routes = AsyncMock(return_value={'model_provider': 'openai', 'model_name': 'session-model', 'api_key': 'private'})
    monkeypatch.setattr(planner, 'discovery_model_overrides', routes)
    captured = []
    monkeypatch.setattr(planner, 'create_chat_model', lambda settings, overrides: captured.append(overrides) or SimpleNamespace(bind=lambda **_: model))
    monkeypatch.setattr(planner, 'record_discovery_usage', AsyncMock())
    result = await planner.plan_open_data_query('climate', ['zenodo'], user_id='alice', session_id='owned')
    routes.assert_awaited_once_with('alice', 'owned', max_tokens=1200)
    assert captured[0]['model_name'] == 'session-model' and 'private' not in json.dumps(result)
