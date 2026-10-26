from copy import deepcopy
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.application.services.file_preview import PreviewVersionChanged
from app.application.services.scientific_visualization import ScientificPreviewRejected
from app.application.services.unified_visualization import VisualizationResult
from app.application.services.visualization_selection import (
    VisualizationViewContext, append_visualization_context, validate_visualization_contexts,
)
from app.interfaces.schemas.session import ChatRequest

VERSION = 'a' * 64


def context(**updates):
    value = dict(schemaVersion=1, fileId='minio:abc', version=VERSION, pluginId='fits-image',
        pluginVersion='2.1.0', adapter='scientific-image', family='scientific',
        request={'operation': 'preview', 'params': {'hdu': 0}},
        view={'kind': 'image', 'valueSemantics': 'physical', 'sampled': True},
        selection={'kind': 'range', 'coordinateSystem': 'source-index', 'bounds': 'inclusive', 'x': [8, 16], 'y': [0, 8]})
    value.update(updates)
    return VisualizationViewContext.model_validate(value)


def result(**updates):
    value = dict(plugin_id='fits-image', version=VERSION, revision='r1', kind='raster',
        payload={'view_kind': 'image', 'x': [0., 8., 16.], 'y': [0., 8.], 'width': 3, 'height': 2, 'values': [10.] * 6},
        metadata={'value_semantics': 'physical'}, sampled=True)
    value.update(updates)
    return VisualizationResult(**value)


class Catalog:
    async def require_enabled(self, user, plugin):
        assert user == 'owner'
        return SimpleNamespace(adapter='scientific-image', version='2.1.0')


async def validate(item, *, output=None, selected=None, catalog=None):
    calls = []
    async def reader(*args):
        calls.append(args)
        return output or result()
    checked = await validate_visualization_contexts('files', catalog or Catalog(), 'image', 'owner', [item],
        {'minio:abc'} if selected is None else selected, reader=reader)
    return checked, calls


@pytest.mark.asyncio
async def test_original_pixel_selection_rereads_same_file_and_version_without_exporting_source_values():
    checked, calls = await validate(context())
    assert calls[0][3:5] == ('minio:abc', 'owner')
    request = calls[0][5]
    assert request.operation == 'preview' and request.version == VERSION
    assert request.options == {'hdu': 0}
    assert checked[0]['selection']['x'] == [8., 16.]
    assert checked[0]['sampled'] is True
    prompt = append_visualization_context('Explain', checked)
    assert '只读挂载' in prompt and '原始数据' in prompt and '"values"' not in prompt
    assert append_visualization_context('Explain', checked) == prompt


@pytest.mark.asyncio
@pytest.mark.parametrize('selected', [set(), {'other'}])
async def test_view_does_not_grant_access_to_unselected_file(selected):
    with pytest.raises(ScientificPreviewRejected):
        await validate(context(), selected=selected)


@pytest.mark.asyncio
async def test_changed_content_or_plugin_is_rejected():
    with pytest.raises(PreviewVersionChanged):
        await validate(context(), output=result(version='b' * 64))
    with pytest.raises(PreviewVersionChanged):
        await validate(context(pluginVersion='obsolete'))


@pytest.mark.asyncio
async def test_range_must_use_source_pixels_and_fit_validated_axes():
    item = context()
    item.selection.x = [1000, 1016]
    with pytest.raises(ScientificPreviewRejected):
        await validate(item)
    item = context()
    item.selection.coordinateSystem = 'axis-values'
    with pytest.raises(ScientificPreviewRejected):
        await validate(item)
    # Image cell edges and a single selected pixel are both valid.
    item = context()
    item.selection.x = [-4, 20]
    await validate(item)
    item.selection.x = [8, 8]
    await validate(item)


@pytest.mark.parametrize('change', [
    {'source': '/private/source'}, {'xLabel': '/home/source/data'},
    {'units': 'https://secret.invalid'}, {'title': 'x' * 257},
    {'yColumns': list(range(600))}, {'colorRange': [0, float('inf')]},
])
def test_context_is_bounded_and_cannot_carry_storage_details(change):
    with pytest.raises((ValidationError, ValueError)):
        context(view={'kind': 'image', **change})


def test_invalid_ranges_and_observation_payloads_fail_before_execution():
    item = context().model_dump(exclude_none=True)
    bad = deepcopy(item)
    bad['selection']['x'] = [16, 8]
    with pytest.raises(ValidationError):
        VisualizationViewContext.model_validate(bad)
    for kwargs in ({'observe_client_message_id': 'old'}, {'resume_from': 'a' * 32, 'client_message_id': 'new'}, {}):
        with pytest.raises(ValidationError):
            ChatRequest(visualization_contexts=[item], **kwargs)
    assert ChatRequest(message='Explain', input_file_ids=['minio:abc'], visualization_contexts=[item]).visualization_contexts


@pytest.mark.asyncio
async def test_table_column_and_row_window_are_verified_from_reader_result():
    class TableCatalog:
        async def require_enabled(self, *_):
            return SimpleNamespace(adapter='plotly', version='2.1.0')
    item = context(pluginId='viz-plotly', adapter='plotly', family='plotly',
        request={'operation': 'preview', 'params': {'kind': 'table', 'row_offset': 200, 'column_offset': 0}},
        view={'kind': 'table', 'valueSemantics': 'raw', 'sampled': True, 'xColumn': -1, 'yColumns': [1]},
        selection={'kind': 'range', 'coordinateSystem': 'source-index', 'bounds': 'inclusive', 'x': [201, 202]})
    output = result(plugin_id='viz-plotly', kind='table', payload={'table': {'columns': ['id', 'value'], 'rows': [['9007199254740993', 5], ['9007199254740994', 8]], 'row_offset': 200}})
    await validate(item, output=output, catalog=TableCatalog())
    item.view['yColumns'] = [2]
    with pytest.raises(ScientificPreviewRejected):
        await validate(item, output=output, catalog=TableCatalog())
    item.view['yColumns'] = [1]
    item.view['xColumn'] = 0
    item.selection.coordinateSystem = 'axis-values'
    with pytest.raises(ScientificPreviewRejected):
        await validate(item, output=output, catalog=TableCatalog())


@pytest.mark.asyncio
async def test_array_window_handoff_can_resolve_original_node_without_storage_paths():
    class ArrayCatalog:
        async def require_enabled(self, *_):
            return SimpleNamespace(adapter='array-window', version='2.1.0')
    variable = 'v-' + 'd' * 32
    item = context(pluginId='viz-array-window', adapter='array-window', family='array-window',
        request={'operation': 'preview', 'params': {'kind': 'series', 'variable': variable, 'decode': 'physical', 'selection': [{'start': 0, 'stop': 3, 'step': 1}]}},
        view={'kind': 'series', 'valueSemantics': 'physical', 'sampled': False},
        selection={'kind': 'range', 'coordinateSystem': 'source-index', 'bounds': 'inclusive', 'x': [0, 2]})
    payload = {'axes': [{'dimension': 0, 'indices': [0, 1, 2]}], 'choices': {'variables': [
        {'id': variable, 'label': 'temperature', 'shape': [10], 'dtype': '<i2', 'selectable': True}]}}
    output = result(plugin_id='viz-array-window', kind='array', payload=payload, metadata={'value_semantics': 'physical', 'attributes': {'units': 'K', 'scale_factor': .1}})
    checked, _ = await validate(item, output=output, catalog=ArrayCatalog())
    assert checked[0]['selected_variable'] == {'id': variable, 'label': 'temperature', 'shape': [10], 'dtype': '<i2'}
    assert 'surrogatepass' in checked[0]['variable_id_rule']
    assert checked[0]['source_metadata']['attributes']['units'] == 'K'
    assert 'values' not in checked[0]
    payload['choices']['variables'][0]['id'] = 'v-' + 'e' * 32
    with pytest.raises(ScientificPreviewRejected):
        await validate(item, output=result(plugin_id='viz-array-window', kind='array', payload=payload), catalog=ArrayCatalog())


@pytest.mark.asyncio
async def test_legacy_gridfs_prefix_is_the_same_owned_input_reference():
    await validate(context(fileId='gridfs:abc'), selected={'abc'})
