"""Authored, wholly artificial methods for real-model creator acceptance.

These programs are independent reference examples, not generated helpers and
not test oracles shown alongside generated code. They actually run before their
source can be offered as completed-method evidence. No database is accessed.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable


def aggregate_tables(data):
    import math

    fields = data.get("fields", {})
    identifier = fields.get("sample", "sample_id")
    grouping = fields.get("group", "group")
    measurement = fields.get("value", "value")
    samples, groups = {}, {}
    for row in data["samples"]:
        key, group = row[identifier], row[grouping]
        if key in samples or not isinstance(key, str) or not key:
            raise ValueError("duplicate or invalid sample identifier")
        if not isinstance(group, str) or not group:
            raise ValueError("invalid group")
        samples[key] = group
        groups.setdefault(group, {"count": 0, "sum": 0.0})
    valid, missing, total = 0, 0, 0.0
    for row in data["measurements"]:
        if row[identifier] not in samples:
            raise ValueError("unknown sample")
        value = row[measurement]
        if value is None:
            missing += 1
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("invalid measurement")
        entry = groups[samples[row[identifier]]]
        entry["count"] += 1
        entry["sum"] += value
        total += value
        valid += 1
    summaries = [{"group": key, **value, "mean": value["sum"] / value["count"] if value["count"] else None}
                 for key, value in sorted(groups.items())]
    assert valid + missing == len(data["measurements"])
    assert math.isclose(sum(item["sum"] for item in summaries), total, abs_tol=1e-9)
    return {"groups": summaries, "quality": {"rows": len(data["measurements"]), "valid": valid, "missing": missing}}


def paired_randomization(data):
    import itertools
    import math
    import statistics

    fields = data.get("fields", {})
    identifier = fields.get("id", "pair_id")
    before = fields.get("before", "before")
    after = fields.get("after", "after")
    identifiers, differences = set(), []
    excluded = 0
    for row in data["pairs"]:
        key = row[identifier]
        if not isinstance(key, str) or not key or key in identifiers:
            raise ValueError("duplicate or invalid pair identifier")
        identifiers.add(key)
        left, right = row[before], row[after]
        for value in (left, right):
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
                raise ValueError("invalid paired measurement")
        if left is None or right is None:
            excluded += 1
            continue
        differences.append(right - left)
    n = len(differences)
    if not 2 <= n <= 12:
        raise ValueError("exact paired test requires 2 through 12 complete pairs")
    mean = statistics.mean(differences)
    sample_sd = statistics.stdev(differences)
    permutations = 2 ** n
    extreme = sum(abs(sum(sign * value for sign, value in zip(signs, differences)) / n) >= abs(mean) - 1e-12
                  for signs in itertools.product((-1, 1), repeat=n))
    return {"n": n, "excluded": excluded, "mean_change": mean, "sample_sd": sample_sd,
            "standard_error": sample_sd / math.sqrt(n), "p_value": extreme / permutations,
            "permutations": permutations, "direction": "after-minus-before"}


def pivot_measurements(data):
    import math

    fields = data.get("fields", {})
    identifier = fields.get("row", "sample_id")
    variable = fields.get("column", "analyte")
    measurement = fields.get("value", "value")
    scale = data.get("scale", 1)
    if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not math.isfinite(scale) or scale <= 0:
        raise ValueError("scale must be positive finite number")
    records, columns, seen = {}, set(), set()
    missing = 0
    for row in data["rows"]:
        key, column, value = row[identifier], row[variable], row[measurement]
        if not isinstance(key, str) or not key or not isinstance(column, str) or not column or column == "sample_id":
            raise ValueError("invalid identifier or column")
        if (key, column) in seen:
            raise ValueError("duplicate measurement cell")
        seen.add((key, column))
        columns.add(column)
        records.setdefault(key, {})
        if value is None:
            missing += 1
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("invalid numeric measurement")
        records[key][column] = None if value is None else value * scale
    columns = sorted(columns)
    wide = [{"sample_id": key, **{column: values.get(column) for column in columns}}
            for key, values in sorted(records.items())]
    return {"columns": ["sample_id", *columns], "rows": wide,
            "quality": {"source_rows": len(data["rows"]), "output_rows": len(wide),
                        "observed_missing": missing, "absent_cells": len(wide) * len(columns) - len(seen)}}


@dataclass(frozen=True)
class Scenario:
    name: str
    title: str
    request: str
    reference: Callable
    normal: dict
    observed: dict


SCENARIOS = {
    "join": Scenario(
        "join", "人工多表关联与分组聚合",
        "对人工样本分组表和测量表做可复用分组聚合。JSON输入含samples和measurements两张表；fields可映射sample、group、value三个字段角色，默认sample_id、group、value。"
        "样本标识必须是非空字符串且唯一，组别是非空字符串；未知测量样本须拒绝。value为null计缺失，布尔、非数值、非有限值拒绝。"
        "关联后按组计算有效记录count、sum、mean；保留无测量或全缺失组，其count和sum为0、mean为null。按组名排序。"
        "输出标准JSON含groups和quality，quality含rows、valid、missing；检查有效加缺失等于总行数、分组总和守恒。空表输出空分组和零计数。"
        "标准库实现，输入只读；正常、边界、无效用例须覆盖字段映射、零值与缺失区别、未知样本、主键冲突。",
        aggregate_tables,
        {"samples": [{"sample_id": "a", "group": "left"}, {"sample_id": "b", "group": "right"}],
         "measurements": [{"sample_id": "a", "value": 2}, {"sample_id": "a", "value": 4}, {"sample_id": "b", "value": None}]},
        {"groups": [{"group": "left", "count": 2, "sum": 6.0, "mean": 3.0}, {"group": "right", "count": 0, "sum": 0.0, "mean": None}],
         "quality": {"rows": 3, "valid": 2, "missing": 1}}),
    "paired": Scenario(
        "paired", "人工配对变化与精确随机化检验",
        "对独立实验单位的前后配对观测总结after减before变化，并做双侧精确符号翻转随机化检验。JSON输入含pairs，fields可映射id、before、after，默认pair_id、before、after。"
        "pair_id须非空字符串且唯一；任何非null的观测须有限数值且不是布尔；任一时点为null则剔除整对并记录excluded，不允许单侧填补。"
        "完整配对数n须2至12；不足或超过上限须明确拒绝，不得改成独立样本检验或抽样近似。"
        "输出标准JSON含n、excluded、mean_change、sample_sd、standard_error、p_value、permutations、direction；excluded为剔除配对数量的非负整数，不是标识列表。"
        "mean_change为after-before的均值，sample_sd使用n-1分母，standard_error为sample_sd/sqrt(n)。"
        "枚举2^n个符号组合；双侧p_value为abs(符号翻转均值)>=abs(原均值)-1e-12的组合数除以2^n，不加1；direction固定after-minus-before。"
        "全部零差值仍有效，均值、标准差和标准误为0、p_value为1。须说明符号交换性假设、配对独立性及该检验不能自动解释为因果效应。"
        "仅标准库，输入只读，提供字段映射、缺失配对、最小样本、重复标识与方向/分母的验证，不复制本次具体结论。",
        paired_randomization,
        {"pairs": [{"pair_id": "a", "before": 0, "after": 1}, {"pair_id": "b", "before": 0, "after": 2},
                   {"pair_id": "c", "before": 0, "after": 3}, {"pair_id": "d", "before": None, "after": 4}]},
        {"n": 3, "excluded": 1, "mean_change": 2, "sample_sd": 1.0, "standard_error": 1 / math.sqrt(3),
         "p_value": .25, "permutations": 8, "direction": "after-minus-before"}),
    "conversion": Scenario(
        "conversion", "人工长表转宽表及单位转换",
        "将JSON中的rows数值长表转换为UTF-8 CSV宽表，同时交付质量JSON。fields可映射row、column、value角色，默认sample_id、analyte、value；scale默认1，必须是正有限数且不是布尔。"
        "行标识和列名必须是非空字符串，列名不能与输出sample_id主键冲突；重复行列组合必须拒绝，禁止任意保留第一项或聚合。"
        "数值乘以scale完成单位缩放，保留0；null写CSV空单元格，非数值、布尔、非有限值拒绝。"
        "按样本标识和测量列名各自升序排序，输出首列固定sample_id，缺少的行列组合为空。"
        "质量JSON含source_rows、output_rows、observed_missing、absent_cells；显式null与根本不存在的单元格分别计数。"
        "空输入CSV只有sample_id表头，质量计数全部0。输出文件名可参数化但输入只读、CSV和JSON写独立输出目录。"
        "使用标准库，正常边界无效验收须覆盖字段映射与缩放、零值和null区别、CSV引号转义、排序、重复单元格与非法scale。",
        pivot_measurements,
        {"rows": [{"sample_id": "b", "analyte": "z", "value": None}, {"sample_id": "a", "analyte": "x", "value": 0},
                  {"sample_id": "b", "analyte": "x", "value": 3}], "scale": 2},
        {"columns": ["sample_id", "x", "z"], "rows": [{"sample_id": "a", "x": 0, "z": None}, {"sample_id": "b", "x": 6, "z": None}],
         "quality": {"source_rows": 3, "output_rows": 2, "observed_missing": 1, "absent_cells": 1}}),
}

# Independently enumerated before the final real-model run: signed sums have
# frequencies {-7:1,-5:2,-3:2,-1:3,1:3,3:2,5:2,7:1}. Six of sixteen have
# absolute sum at least five, so the two-sided probability is 0.375, not 0.125.
PAIRED_MIXED_SIGN_INPUT = {"pairs": [
    {"pair_id": "a", "before": 0, "after": 2},
    {"pair_id": "b", "before": 0, "after": 3},
    {"pair_id": "c", "before": 0, "after": -1},
    {"pair_id": "d", "before": 0, "after": 1},
]}
PAIRED_MIXED_SIGN_EXPECTED = {
    "n": 4, "excluded": 0, "mean_change": 1.25,
    "sample_sd": math.sqrt(35 / 12), "standard_error": math.sqrt(35 / 12) / 2,
    "p_value": .375, "permutations": 16, "direction": "after-minus-before",
}

from skill_creator_complex_scenario import comprehensive_study, COMPLEX_INPUT, COMPLEX_EXPECTED, COMPLEX_REQUEST

SCENARIOS["complex"] = Scenario("complex", "人工纵向研究完整统计与质量流程", COMPLEX_REQUEST,
                                comprehensive_study, COMPLEX_INPUT, COMPLEX_EXPECTED)

from skill_creator_generic_scenarios import (
    normalize_document, inventory_documents, TEXT_REQUEST, TEXT_INPUT, TEXT_EXPECTED,
    XML_REQUEST, XML_INPUT, XML_EXPECTED,
)

SCENARIOS["text"] = Scenario("text", "人工文档文本规范化", TEXT_REQUEST,
                             normalize_document, TEXT_INPUT, TEXT_EXPECTED)
SCENARIOS["document"] = Scenario("document", "人工结构化文档盘点", XML_REQUEST,
                                 inventory_documents, XML_INPUT, XML_EXPECTED)
