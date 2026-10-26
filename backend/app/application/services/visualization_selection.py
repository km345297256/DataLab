"""Revalidate a bounded view before attaching its coordinates to an analysis.

The browser contributes selectors, never source data, storage locations or an
authorization grant. Existing readers validate selectors and read through their
normal bounded object-store boundary. Only a small description reaches the agent.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.application.services.file_preview import PreviewVersionChanged
from app.application.services.scientific_visualization import ScientificPreviewRejected
from app.application.services.unified_visualization import VisualizationRequest, VisualizationResult, unified_visualization


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class ViewRequest(_Strict):
    operation: Literal["preview"]
    params: dict[str, Any] = Field(max_length=8)


class ViewSelection(_Strict):
    kind: Literal["range"]
    coordinateSystem: Literal["axis-values", "source-index"]
    bounds: Literal["inclusive"]
    x: list[float] = Field(min_length=2, max_length=2)
    y: list[float] | None = Field(default=None, min_length=2, max_length=2)

    @model_validator(mode="after")
    def ordered(self):
        if self.x[0] > self.x[1] or self.y and self.y[0] > self.y[1]:
            raise ValueError("Selection bounds must be ordered")
        return self


class VisualizationViewContext(_Strict):
    schemaVersion: Literal[1]
    fileId: str = Field(min_length=1, max_length=135, pattern=r"^(?:(?:minio|gridfs):)?[A-Za-z0-9_-]{1,128}$")
    version: str = Field(pattern=r"^[0-9a-f]{64}$")
    pluginId: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    pluginVersion: str = Field(min_length=1, max_length=64, pattern=r"^[0-9A-Za-z][0-9A-Za-z.+_-]*$")
    adapter: str = Field(min_length=1, max_length=64)
    family: Literal["scientific", "plotly", "array-window", "h5web"]
    request: ViewRequest
    view: dict[str, Any] = Field(max_length=16)
    selection: ViewSelection | None = None

    @model_validator(mode="after")
    def bounded(self):
        _check_small_data(self.model_dump(exclude_none=True))
        allowed = {"kind", "title", "xLabel", "yLabel", "units", "valueSemantics", "sampled",
                   "colorRange", "xColumn", "yColumns", "mode", "xRange", "yRange", "sourceShape", "strides"}
        if set(self.view) - allowed:
            raise ValueError("Unexpected view metadata")
        return self


def _check_small_data(value):
    if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > 16 * 1024:
        raise ValueError("View context exceeds budget")
    count = 0

    def visit(item, depth=0):
        nonlocal count
        count += 1
        if depth > 8 or count > 512:
            raise ValueError("View context exceeds budget")
        if isinstance(item, str):
            if len(item) > 256 or re.search(r"[\x00-\x1f\x7f]|(?:/Users/|/home/|/tmp/|/private/|/var/|https?://|file:|[A-Za-z]:\\)", item, re.I):
                raise ValueError("Invalid view label")
        elif isinstance(item, dict):
            for key, child in item.items():
                visit(key, depth + 1)
                visit(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
        elif item is not None and type(item) not in {bool, int, float}:
            raise ValueError("Invalid view value")
        elif type(item) in {int, float} and (not math.isfinite(item) or type(item) is int and abs(item) > 2**53 - 1):
            raise ValueError("Invalid numeric view value")
    visit(value)


def _extent(values, *, pixels=False):
    valid = [float(v) for v in values if type(v) in {int, float} and math.isfinite(v)]
    if not valid:
        return None
    lower, upper = min(valid), max(valid)
    padding = abs(valid[1] - valid[0]) / 2 if pixels and len(valid) > 1 else .5 if pixels else 0
    return [lower - padding, upper + padding]


def _numeric(value):
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, str):
        if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value.strip()):
            return None
        value = float(value)
        if value.is_integer() and abs(value) > 2**53 - 1:
            return None
    return value if type(value) in {float, int} and math.isfinite(value) else None


def _axes(context, result):
    payload, metadata = result.payload, result.metadata
    if context.family == "scientific":
        return payload.get("x", []), payload.get("y", []), payload.get("view_kind") in {"image", "map"}
    if context.family == "array-window":
        axes = payload.get("axes", [])
        return (axes[-1].get("indices", []) if axes else []), (axes[0].get("indices", []) if len(axes) == 2 else []), len(axes) == 2
    if result.kind == "table":
        table = payload.get("table", {})
        rows, columns = table.get("rows", []), table.get("columns", [])
        x = context.view.get("xColumn", -1)
        ys = context.view.get("yColumns", [])
        if type(x) is not int or not -1 <= x < len(columns) or not isinstance(ys, list) or not 1 <= len(ys) <= 8 or any(type(y) is not int or not 0 <= y < len(columns) for y in ys):
            raise ScientificPreviewRejected("选区包含无效的表格列，请重新绘制。")
        return ([_numeric(row[x]) for row in rows] if x >= 0 else list(range(table.get("row_offset", 0) + 1, table.get("row_offset", 0) + 1 + len(rows)))), [], False
    array = payload.get("array", {})
    shape = array.get("shape", [])
    strides = metadata.get("strides", [1] * len(shape))
    if not shape or len(strides) != len(shape):
        raise ScientificPreviewRejected("数组缺少原始索引信息。")
    return [i * strides[-1] for i in range(shape[-1])], ([i * strides[0] for i in range(shape[0])] if len(shape) == 2 else []), len(shape) == 2


async def validate_visualization_contexts(file_service, catalog, image, user_id, contexts,
                                          selected_file_ids, *, reader=unified_visualization):
    """Return deterministic, server-verified descriptions for one submitted turn."""
    descriptions = []
    selected_file_ids = {item.removeprefix("gridfs:") for item in selected_file_ids}
    for context in contexts:
        if context.fileId.removeprefix("gridfs:") not in selected_file_ids:
            raise ScientificPreviewRejected("选区对应的文件未加入本轮分析。")
        plugin = await catalog.require_enabled(user_id, context.pluginId)
        families = {"scientific": {"scientific-series", "scientific-map", "scientific-image", "scientific-quality"},
                    "plotly": {"plotly"}, "array-window": {"array-window"}, "h5web": {"h5web"}}
        if plugin.adapter not in families[context.family] or plugin.adapter != context.adapter or plugin.version != context.pluginVersion:
            raise PreviewVersionChanged("可视化插件已变化，请重新选择范围。")
        params = dict(context.request.params)
        allowed = {"scientific": {"variable", "x_dimension", "indices", "hdu"},
                   "plotly": {"kind", "variable", "indices", "row_offset", "column_offset"},
                   "array-window": {"kind", "variable", "decode", "selection"},
                   "h5web": {"kind", "path", "indices"}}[context.family]
        if set(params) - allowed:
            raise ScientificPreviewRejected("选区参数不受支持。")
        kind = params.pop("kind", None)
        result = await reader(file_service, catalog, image, context.fileId, user_id,
                              VisualizationRequest(plugin_id=context.pluginId, version=context.version,
                                                   operation="preview", kind=kind, options=params))
        if not isinstance(result, VisualizationResult) or result.version != context.version or result.plugin_id != context.pluginId:
            raise PreviewVersionChanged("文件版本已变化，请重新选择范围。")
        xs, ys, pixels = _axes(context, result)
        if context.selection and result.kind == "table" and context.view.get("mode") == "histogram":
            raise ScientificPreviewRejected("直方图桶不能直接转换为原始数据行范围，请分析当前窗口。")
        if context.selection:
            for chosen, axis in ((context.selection.x, xs), (context.selection.y, ys)):
                if chosen is None:
                    continue
                extent = _extent(axis, pixels=pixels)
                if extent is None or chosen[0] < extent[0] or chosen[1] > extent[1]:
                    raise ScientificPreviewRejected("选区超出已读取窗口，请重新框选。")
            expected = "axis-values" if context.family == "scientific" and result.payload.get("view_kind") != "image" or result.kind == "table" and context.view.get("xColumn", -1) >= 0 else "source-index"
            if context.selection.coordinateSystem != expected:
                raise ScientificPreviewRejected("选区坐标约定与此视图不一致。")
        variable = None
        if context.family == "array-window":
            variable = next((item for item in result.payload.get("choices", {}).get("variables", [])
                             if item.get("id") == params.get("variable") and item.get("selectable")), None)
            if variable is None:
                raise ScientificPreviewRejected("无法定位所选数组变量，请重新打开预览。")
        descriptions.append({
            "file_id": context.fileId, "file_version": context.version,
            "plugin_id": context.pluginId, "plugin_version": context.pluginVersion,
            "request": context.request.model_dump(), "selection": context.selection.model_dump(exclude_none=True) if context.selection else None,
            "sampled": result.sampled, "value_semantics": result.metadata.get("value_semantics", "unknown"),
            "source_metadata": {k: result.metadata[k] for k in ("source_shape", "strides", "units", "calendar", "coordinate_semantics", "missing_value_policy", "attributes") if k in result.metadata},
            **({"selected_variable": {k: variable[k] for k in ("id", "label", "shape", "dtype")},
                "variable_id_rule": "v- + SHA256(HDF5 internal absolute node path encoded as UTF-8 with surrogatepass).hexdigest()[:32]; enumerate supported hard links only; never follow external/soft links or virtual storage; require one matching dataset and verify shape/dtype before reading"} if variable else {}),
            "columns": {k: context.view[k] for k in ("xColumn", "yColumns", "mode") if k in context.view},
        })
    return descriptions


def append_visualization_context(message, descriptions):
    if not descriptions:
        return message
    return ((message or "") + "\n\n可视化范围（提交时已校验文件版本和读取参数）：\n"
            + json.dumps(descriptions, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n请通过本轮已选文件的只读挂载读取原始数据，按上述变量、窗口和选区分析。"
            "范围端点包含；source-index表示原始坐标值区间，不能直接当Python切片（负边界应裁到0）。数组selection.stop不包含。表格行号从1开始且不含列名，列索引相对当前列窗口从0开始。"
            "预览的抽样值不是完整数据，不可据此声称全量统计；请报告实际分析范围、单位和缺失值处理。")
