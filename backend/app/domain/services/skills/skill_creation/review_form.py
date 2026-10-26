"""Host-owned review targets, mechanically projected to the stored review type.

This is a wire protocol, not a review recovery or a new scientific judgment.
It never adds/removes a target from a response, infers a verdict from prose,
or upgrades an adverse legacy review. Canonical validation remains mandatory.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from app.domain.services.skills.method_contract import MethodReview


_REQUIREMENT_STATUSES = ("preserved", "missing", "changed", "unsupported")
_SOURCE_STATUSES = ("preserved", "valid_exclusion", "missing", "changed")
_EXCLUDED_SOURCE_CRITERION = (
    "This source is declared excluded in the frozen method contract. Review whether that exclusion is justified, "
    "not whether equivalent methods are implemented elsewhere. valid_exclusion means you independently agree "
    "that excluding this source is justified. preserved means you disagree with the exclusion because this "
    "source must be included; it is an adverse judgment against the frozen exclusion. Do not mark preserved "
    "merely because duplicated method text is already implemented. Explain the actual source evidence; "
    "the host's declared exclusion is not proof that the exclusion is reasonable. missing and changed remain "
    "available for genuine source obligations that are missing or changed."
)
_INCLUDED_SOURCE_CRITERION = (
    "This source is not declared excluded in the frozen method contract. Review whether its reusable obligations "
    "are faithfully preserved. preserved means they are included and preserved. valid_exclusion means you "
    "consider this source legitimately excludable instead; it disagrees with the frozen inclusion and is an "
    "adverse judgment. Use missing or changed for genuine omissions or changes, with evidence from the source. "
    "Do not accept the host's inclusion merely because it is declared."
)
_IMPLEMENTATION_CRITERION = (
    "Evaluate only whether the supplied implementation form is suitable for the frozen task, "
    "methods and declared input/output contract. This is a fixed independent review target. "
    "Do not reject it because of review response IDs, formatting errors or behavior the original "
    "sources do not require. A real implementation defect or inability to confirm suitability "
    "must be rejected and explained using the frozen task and supplied implementation."
)
REVIEW_FORM_INSTRUCTION = """本次使用宿主固定的审查表，不生成新的审查编号。
sources 与 requirements 是按宿主已有 ID 为键的对象：每个规定键必须恰好填写一次，只填写其 status 和 explanation；不得添加、删除、重命名 ID，也不返回旧列表结构。
每个 source 的 description 指明本项审查问题：已排除来源审查“排除是否合理”，valid_exclusion 表示认可排除，preserved 表示排除不合理、必须纳入；不能仅因重复方法已被实现就填写 preserved。未排除来源审查“义务是否保留”，preserved 表示保留，valid_exclusion 表示认为应排除、与当前合同不一致；必须按原来源独立判断，不强迫认可宿主的排除或纳入。
implementation 是固定、独立的“实现方式是否适用于当前冻结任务”审查项，status 只能为 accepted 或 rejected。它审查给定实现能否执行原方法并遵守公开输入输出契约；真实缺陷或无法确认时必须 rejected，并依据原义务和所给实现解释。
审查范围或响应格式错误由宿主处理，不是被审程序的缺陷；不得因自己生成未知编号而否决 implementation，也不得要求实现来源未规定的行为。不得为通过创建而改变真实负评。
只返回 schema 指定的完整对象，不返回 implementation_appropriate、implementation_reason 或任何额外字段。"""


class ReviewFormError(ValueError):
    """Fixed diagnostics, excluding response text and unknown model IDs."""
    def __init__(self, fields):
        self.reason = "invalid_review_scope"
        self.fields = list(fields)
        super().__init__(self.reason)


def _assessment_schema(statuses):
    return {"type": "object", "properties": {
        "status": {"type": "string", "enum": list(statuses)},
        "explanation": {"type": "string", "minLength": 8},
    }, "required": ["status", "explanation"], "additionalProperties": False}


@dataclass(frozen=True)
class ReviewFormAdapter:
    source_ids: tuple[str, ...]
    requirement_ids: tuple[str, ...]
    excluded_source_ids: frozenset[str] = frozenset()

    @property
    def instruction(self):
        return REVIEW_FORM_INSTRUCTION

    def request_schema(self):
        sources = {key: _assessment_schema(_SOURCE_STATUSES) for key in self.source_ids}
        for key, source in sources.items():
            source["description"] = (_EXCLUDED_SOURCE_CRITERION if key in self.excluded_source_ids
                                     else _INCLUDED_SOURCE_CRITERION)
        requirements = {key: _assessment_schema(_REQUIREMENT_STATUSES) for key in self.requirement_ids}
        implementation = _assessment_schema(("accepted", "rejected"))
        implementation["description"] = _IMPLEMENTATION_CRITERION
        return {"title": "HostBoundMethodReview", "type": "object", "properties": {
            "sources": {"type": "object", "properties": sources, "required": list(sources),
                        "additionalProperties": False},
            "requirements": {"type": "object", "properties": requirements, "required": list(requirements),
                             "additionalProperties": False},
            "implementation": implementation,
        }, "required": ["sources", "requirements", "implementation"], "additionalProperties": False}

    @staticmethod
    def _section(document, name, identifiers, statuses):
        section = document.get(name)
        if not isinstance(section, dict) or set(section) != set(identifiers):
            raise ReviewFormError([name])
        for value in section.values():
            if not isinstance(value, dict) or set(value) - {"status", "explanation"}:
                raise ReviewFormError([name])
            if not isinstance(value.get("status"), str) or value["status"] not in statuses:
                # Keep form protocol repairs in form shape. Only explanation
                # defects may enter canonical, field-local schema repair.
                raise ReviewFormError([name, "status"])
        return section

    def document(self, document):
        """Check complete target identity, then preserve every assessment value.

        The caller must first parse one JSON document with duplicate-key
        rejection. Missing/invalid explanatory fields remain for canonical
        Pydantic validation and narrow repairs; statuses keep the form protocol.
        """
        if not isinstance(document, dict) or set(document) != {"sources", "requirements", "implementation"}:
            raise ReviewFormError(["sources", "requirements", "implementation"])
        sources = self._section(document, "sources", self.source_ids, _SOURCE_STATUSES)
        requirements = self._section(document, "requirements", self.requirement_ids, _REQUIREMENT_STATUSES)
        implementation = document["implementation"]
        if (not isinstance(implementation, dict) or set(implementation) - {"status", "explanation"}
                or implementation.get("status") not in ("accepted", "rejected")):
            # Implementation status has no literal-enum equivalent in the old
            # boolean storage model. Never coerce an unknown spelling to True.
            raise ReviewFormError(["implementation", "status"])
        result = {
            "sources": [{"source_id": key, **sources[key]} for key in self.source_ids],
            "requirements": [{"requirement_id": key, **requirements[key]} for key in self.requirement_ids],
            "implementation_appropriate": implementation["status"] == "accepted",
        }
        if "explanation" in implementation:
            result["implementation_reason"] = implementation["explanation"]
        return result

    def observe(self, path, value):
        """Latch only an explicit adverse scalar on an actual request target.

        This observes incomplete/invalid responses for fail-closed handling,
        not acceptance. An illegal but explicit ``rejected``/``uncertain`` on
        a known target is also adverse; quoted prose and invented IDs are not.
        """
        path = tuple(path)
        if not isinstance(value, str):
            return False
        if path == ("implementation", "status"):
            return value in {"rejected", "uncertain", "inappropriate"}
        if len(path) != 3 or path[2] != "status":
            return False
        section, identifier, _ = path
        if section == "requirements" and identifier in self.requirement_ids:
            return value in {"missing", "changed", "unsupported", "rejected", "uncertain"}
        if section == "sources" and identifier in self.source_ids:
            if value in {"missing", "changed", "rejected", "uncertain", "unsupported"}:
                return True
            return (value == "preserved" if identifier in self.excluded_source_ids
                    else value == "valid_exclusion")
        return False


def _host_ids(values, key, pattern):
    if not isinstance(values, list):
        raise ValueError("Invalid host review scope")
    identifiers = []
    for value in values:
        identifier = value.get(key) if isinstance(value, dict) else None
        if not isinstance(identifier, str) or re.fullmatch(pattern, identifier) is None:
            raise ValueError("Invalid host review scope")
        identifiers.append(identifier)
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("Duplicate host review target")
    return tuple(identifiers)


def review_form_adapter(schema, stage, payload):
    if schema is not MethodReview or stage not in {"method_review", "synthetic_reference_review"}:
        return None
    if not isinstance(payload, dict) or "method_contract" not in payload or "sources" not in payload:
        return None
    contract = payload["method_contract"]
    if not isinstance(contract, dict) or "requirements" not in contract:
        raise ValueError("Invalid host review scope")
    requirements = _host_ids(contract["requirements"], "id", r"R[0-9]{3,}")
    if not requirements:
        raise ValueError("Empty host requirement scope")
    sources = _host_ids(payload["sources"], "id", r"[A-Z][0-9]{3,}")
    excluded = _host_ids(contract.get("excluded_sources", []), "source_id", r"[A-Z][0-9]{3,}")
    # Withheld exclusions have no source body in a blind reference request.
    # The form does not introduce an obligation to review that absent body.
    return ReviewFormAdapter(sources, requirements, frozenset(excluded) & set(sources))
