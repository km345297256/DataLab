"""Select complete source units; the host, never the model, writes requirements.

Segmentation is conservative syntax handling, not semantic completeness proof.
The complete sources remain necessary for the existing independent method review.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from app.domain.services.skills.method_contract import MethodContract, SourceExclusion


_KINDS = ("input", "method", "output", "validation", "constraint")
# Keep dependent sentences together instead of isolating an unqualified rule.
# Unknown natural-language dependencies still belong to full-source review.
_DEPENDENT = re.compile(
    r"\b(?:if|unless|when|whenever|otherwise|provided|assuming|whereas|except|then|else)\b"
    r"|(?:如果|除非|否则|仅当|只要|若|当.{0,40}时|前提|条件下)"
    r"|(?:^|[。.!?！？\n])\s*(?:但|而|则|其|该|同时|其中|并且|因此|However\b|This\b|It\b|These\b)",
    re.IGNORECASE,
)
_ABBREVIATION = re.compile(
    r"(?:\b(?:e\.g|i\.e|et\s+al|vs|etc|fig|eq|dr|mr|mrs|prof|approx|ref|no)|\b[A-Za-z])\.$",
    re.IGNORECASE,
)
_SOURCE_ID = re.compile(r"[A-Z][0-9]{3,}")
REQUIREMENT_FORM_INSTRUCTION = """只从宿主 source_units 选择完整原文单位并分类、分组，不改写科学要求。
requirements 每项仅为 {kind, unit_ids}。kind 取已有五类；unit_ids 只能引用给定 U 编号。不能生成 R 编号、statement 或 source_ids，也不能添加解释、缩写、同义替换、额外检查或默认行为。
同一句可支持多个要求；同一要求内部不能重复单位。宿主会按原来源顺序拼接原文，不添加语义连接词，并分配 R 编号。
保留完整方法、输入输出契约、否定、范围、条件和默认行为；不得加严、放松或增加来源未定义的义务。将私有输入绑定与可迁移参数放到后续 workflow 设计，不能在此改写原义务。
source_units 是候选池，不要求全部选入。混合方法和一次性结果的来源，选择完整方法单位而不把本次观测结果变成每次必须复现的值；不得丢掉原方法规定的阈值、默认值或比较方向。不能安全分离时保留原文语境，不能发明替代文字。
每个完整 source 仍须被至少一条要求引用或放入 excluded_sources，二者不能重叠。excluded_sources 沿用原 source_id/reason/explanation 格式，只可排除实际结果、一次性操作或重复内容。完整来源仍会独立审查，引用一个单位并不证明该来源所有方法已覆盖。"""


class RequirementFormError(ValueError):
    def __init__(self, fields):
        self.reason = "invalid_requirement_evidence"
        self.fields = list(fields)
        super().__init__(self.reason)


@dataclass(frozen=True)
class SourceUnit:
    id: str
    source_id: str
    text: str


def _source_units(text):
    """Whole sentences/lines, retaining ambiguous context as one intact unit.

No punctuation normalization: decimal points, abbreviations, nested quotes and
parentheses are not boundaries. Fenced code, display math, dependent clauses
and explicit line continuations remain a single source block.
"""
    if (_DEPENDENT.search(text) or any(marker in text for marker in ("```", "~~~", "$$", "\\[", "\\]"))
            or re.search(r"[:：,，\\=+*/-]\s*\n", text)):
        return [text.strip()]
    units, start, stack = [], 0, []
    pairs = {"(": ")", "[": "]", "{": "}", "（": "）", "【": "】", "“": "”", "‘": "’"}
    for index, char in enumerate(text):
        if stack and char == stack[-1]:
            stack.pop()
            continue
        if stack and stack[-1] in {'"', "'", "`", "”", "’", "$"}:
            continue
        if char in pairs:
            stack.append(pairs[char])
        elif char in {'"', "'", "`", "$"}:
            # Apostrophes inside words are not a quotation delimiter.
            if char == "'" and index and index + 1 < len(text) and text[index - 1].isalnum() and text[index + 1].isalnum():
                continue
            stack.append(char)
        if stack:
            continue
        following = text[index + 1:index + 2]
        boundary = char in "。！？\n" or char in "!?" and (not following or following.isspace())
        if char == "." and (not following or following.isspace()):
            prefix = text[start:index + 1]
            boundary = not _ABBREVIATION.search(prefix)
        if boundary:
            unit = text[start:index + 1].strip()
            if unit:
                units.append(unit)
            start = index + 1
    # Unbalanced syntax is ambiguous, so discard tentative cuts, not text.
    if stack:
        return [text.strip()]
    tail = text[start:].strip()
    if tail:
        units.append(tail)
    return units


@dataclass(frozen=True)
class RequirementFormAdapter:
    units: tuple[SourceUnit, ...]
    source_ids: tuple[str, ...]

    @property
    def instruction(self):
        return REQUIREMENT_FORM_INSTRUCTION

    @property
    def source_units(self):
        return [{"id": unit.id, "source_id": unit.source_id, "text": unit.text} for unit in self.units]

    def request_schema(self):
        exclusion = SourceExclusion.model_json_schema()
        exclusion.pop("title", None)
        exclusion["properties"]["source_id"] = {"type": "string", "enum": list(self.source_ids)}
        return {"title": "SourceBoundMethodRequirements", "type": "object", "properties": {
            "requirements": {"type": "array", "minItems": 1, "items": {
                "type": "object", "properties": {
                    "kind": {"type": "string", "enum": list(_KINDS)},
                    "unit_ids": {"type": "array", "minItems": 1, "uniqueItems": True,
                                 "items": {"type": "string", "enum": [unit.id for unit in self.units]}},
                }, "required": ["kind", "unit_ids"], "additionalProperties": False}},
            "excluded_sources": {"type": "array", "items": exclusion},
        }, "required": ["requirements", "excluded_sources"], "additionalProperties": False}

    def document(self, document):
        if not isinstance(document, dict) or set(document) != {"requirements", "excluded_sources"}:
            raise RequirementFormError(["requirements", "excluded_sources"])
        excluded = document["excluded_sources"]
        if not isinstance(excluded, list):
            raise RequirementFormError(["excluded_sources"])
        for index, item in enumerate(excluded):
            if not isinstance(item, dict):
                raise RequirementFormError(["excluded_sources", index])
            try:
                SourceExclusion.model_validate(item, strict=True)
            except ValueError:
                raise RequirementFormError(["excluded_sources", index]) from None
            if item["source_id"] not in self.source_ids:
                raise RequirementFormError(["excluded_sources", index, "source_id"])
        rows = document["requirements"]
        if not isinstance(rows, list) or not rows:
            raise RequirementFormError(["requirements"])
        known = {unit.id for unit in self.units}
        requirements = []
        for index, row in enumerate(rows):
            fields = ["requirements", index]
            if not isinstance(row, dict) or set(row) != {"kind", "unit_ids"}:
                raise RequirementFormError(fields)
            if row["kind"] not in _KINDS:
                raise RequirementFormError([*fields, "kind"])
            ids = row["unit_ids"]
            if (not isinstance(ids, list) or not ids or any(not isinstance(item, str) for item in ids)
                    or len(set(ids)) != len(ids) or not set(ids) <= known):
                raise RequirementFormError([*fields, "unit_ids"])
            chosen = [unit for unit in self.units if unit.id in ids]
            requirements.append({"id": f"R{index + 1:03d}", "kind": row["kind"],
                "statement": "\n".join(unit.text for unit in chosen),
                "source_ids": list(dict.fromkeys(unit.source_id for unit in chosen))})
        # Existing canonical, source coverage and privacy validation remain
        # mandatory; no source exclusion is inferred or rewritten here.
        return {"requirements": requirements, "excluded_sources": excluded}


def requirement_form_adapter(schema, stage, payload):
    if schema is not MethodContract or stage != "method_requirements":
        return None
    if not isinstance(payload, dict) or "sources" not in payload:
        return None
    sources = payload["sources"]
    if not isinstance(sources, list) or not sources:
        raise ValueError("Invalid host requirement sources")
    units, source_ids = [], []
    for source in sources:
        if (not isinstance(source, dict) or not isinstance(source.get("id"), str)
                or not _SOURCE_ID.fullmatch(source["id"]) or source["id"] in source_ids
                or not isinstance(source.get("text"), str) or not source["text"].strip()):
            raise ValueError("Invalid host requirement source")
        source_ids.append(source["id"])
        for text in _source_units(source["text"]):
            units.append(SourceUnit(f"U{len(units) + 1:03d}", source["id"], text))
    return RequirementFormAdapter(tuple(units), tuple(source_ids))
