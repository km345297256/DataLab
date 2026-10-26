"""Bind a focused source audit without treating a test disagreement as a fix.

The host proves execution identity and quotation provenance, not mathematical
truth. Only a separate source-bound adverse assessment can authorize a repair;
the unchanged method/interface and frozen tests still have to approve it.
"""
from __future__ import annotations

import ast
from decimal import Decimal, DecimalException
import hashlib
import json
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from app.domain.services.skills.method_contract import MethodContract
from app.domain.services.skills.skill_creation_error import SkillPackageValidationError
from app.domain.services.skills.skill_public_output_contract import PublicOutputContract, validate_public_contract_receipt
from app.domain.services.skills.skill_validation import SyntheticValidationPlan


AUDIT_INSTRUCTION = """针对 review_scope 指定的原方法要求，独立检查 programs 的实现是否忠实于原来源。
你只收到原方法合同、原来源文字、候选源码和宿主指定的要求编号；没有合成数据、测试预期、实际结果、容差或参考实现。
不得推测任何测试答案，也不能因为被请求复审就假定程序有错。只按原来源定义判断。
每个 (script_path, requirement_id) 恰好返回一项：preserved 表示未发现来源违背；uncertain 表示无法确定；
只有能从原来源与实际代码直接说明的方法违背才能标 source_violation。
source_violation 必须给出该要求关联的 source_id、其原文字逐字 source_quote、程序中完整可执行语句或表达式的逐字 code_quote，
并解释二者的具体矛盾。可以引用整个函数；不能以注释、文档字符串或单独字符串常量代替实现定位。
不要发明额外要求、样例答案或数值容差，不以输出碰巧符合某个结果代替方法正确。
preserved/uncertain 的引文字段可以为空。explanation 如实说明判断依据；仅返回指定结构。"""

METHOD_REPAIR_INSTRUCTION = """依据已绑定原来源与候选源码的 source_violation 修正通用实现。
只使用原方法、原来源、原程序和这份方法负评；没有合成输入、测试预期、实际观测、容差或参考程序。
不得猜测测试答案、硬编码结果、删减方法、吞异常或靠特殊样例分支通过。
保持所有原输入/输出接口、参数、脚本身份和其他方法不变，只改真实的方法违背；返回完整脚本。
修订必须有实质可执行代码变化，不能只改注释或解释。之后仍须完整方法审查及同一冻结计划的真实执行。"""


class CandidateMethodRequirementAudit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    requirement_id: StrictStr
    status: Literal["preserved", "source_violation", "uncertain"]
    source_id: StrictStr
    source_quote: StrictStr
    script_path: StrictStr
    code_quote: StrictStr
    explanation: StrictStr = Field(min_length=1)


class CandidateMethodAudit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    requirements: list[CandidateMethodRequirementAudit] = Field(min_length=1)

    def verdict(self):
        if any(item.status == "source_violation" for item in self.requirements):
            return "rejected"
        return "uncertain" if any(item.status == "uncertain" for item in self.requirements) else "accepted"


class CandidateMethodEvidenceError(ValueError):
    def __init__(self):
        super().__init__("candidate_method_evidence_unconfirmed")


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _exact(first, second):
    return _json(first) == _json(second)


def _scalar_observation(value, expected, *, csv_value=False):
    """Reject missing/type/truncated observations, not merely unequal numbers."""
    if csv_value and type(value) is not str:
        return False
    if type(value) is str and len(value) >= 160 or type(expected) is str and len(expected) >= 160:
        return False  # The worker deliberately truncates long scalar strings.
    if expected is None:
        return value == "" if csv_value else value is None
    if type(expected) in {int, float}:
        if type(expected) is float and not math.isfinite(expected):
            return False
        if not csv_value and type(value) not in {int, float} or csv_value and not value.strip():
            return False
        try:
            return Decimal(str(value)).is_finite()
        except (ValueError, DecimalException):
            return False
    if type(expected) is bool:
        return value in {"True", "False"} if csv_value else type(value) is bool
    return type(expected) is str and type(value) is str


def _matches(value, expected, *, csv_value=False):
    target = expected.value
    if type(target) in {int, float}:
        try:
            return abs(Decimal(str(value)) - Decimal(str(target))) <= (
                Decimal(str(expected.atol)) + Decimal(str(expected.rtol)) * abs(Decimal(str(target))))
        except (ValueError, DecimalException):
            return False
    if target is None:
        return value == "" if csv_value else value is None
    if type(target) is bool:
        return value == str(target) if csv_value else value is target
    return value == target


def _complete_checks(case, item):
    checks = item.get("checks")
    if not isinstance(checks, list) or any(not isinstance(check, dict) for check in checks):
        return None
    identifiers = [check.get("id") for check in checks]
    if any(type(key) is not str for key in identifiers) or len(set(identifiers)) != len(identifiers):
        return None
    found, used = dict(zip(identifiers, checks)), set()

    def get(key, expected, compare, valid):
        check = found.get(key)
        if (not isinstance(check, dict) or set(check) != {"id", "passed", "expected", "actual"}
                or type(check["passed"]) is not bool or not _exact(check["expected"], expected)
                or not valid(check["actual"]) or check["passed"] is not bool(compare(check["actual"]))):
            raise ValueError
        used.add(key)
        return check["actual"]

    def scalar(key, expected, csv_value=False):
        return get(key, expected.value, lambda actual: _matches(actual, expected, csv_value=csv_value),
                   lambda actual: _scalar_observation(actual, expected.value, csv_value=csv_value))

    try:
        for index, assertion in enumerate(case.assertions, 1):
            prefix = f"assertion_{index}"
            if assertion.type == "csv_rows":
                count = get(prefix + ".count", assertion.count, lambda actual: actual == assertion.count,
                            lambda actual: type(actual) is int and actual >= 0)
                # Require exactly the observed rows, even when count differs.
                # Empty selection never supplies a CSV field observation.
                row_checks = len(found) - len(used)
                if assertion.fields and count * len(assertion.fields) > row_checks:
                    return None
                for row in range(1, count + 1) if assertion.fields else ():
                    for field, expected in assertion.fields.items():
                        scalar(f"{prefix}.row_{row}.{field}", expected, True)
            elif assertion.type == "json_value":
                scalar(prefix, assertion.expected)
            elif assertion.type == "json_array_length":
                get(prefix, assertion.length, lambda actual: actual == assertion.length,
                    lambda actual: type(actual) is int and actual >= 0)
            elif assertion.type in {"csv_header", "json_object_keys"}:
                expected = assertion.columns if assertion.type == "csv_header" else sorted(assertion.keys)
                get(prefix, expected, lambda actual: actual == expected,
                    lambda actual: type(actual) is list and actual == expected and all(type(value) is str for value in actual))
            elif assertion.type in {"output_empty", "stderr_contains"}:
                expected = True if assertion.type == "output_empty" else assertion.expected
                get(prefix, expected, lambda actual: actual is True, lambda actual: actual is True)
            else:
                return None
    except (ValueError, TypeError, KeyError, OverflowError):
        return None
    return found if used == set(found) else None


def collect_candidate_method_scope(execution, plan, programs, corrections, *, public_output_contract=None):
    """Project real corrected-target disagreement to original requirement IDs.

    No observations are returned. Ordinary nonqualifying failures return [];
    wrong provenance or an ambiguous correction mapping fails closed explicitly.
    """
    if not corrections or not isinstance(execution, dict) or execution.get("status") != "failed":
        return []
    try:
        plan = SyntheticValidationPlan.model_validate(plan.model_dump(mode="json") if hasattr(plan, "model_dump") else plan)
        if (execution.get("scope") != "synthetic" or set(programs) != {script.script_path for script in plan.scripts}
                or execution.get("source_sha256") != {path: hashlib.sha256(code.encode()).hexdigest() for path, code in programs.items()}
                or execution.get("plan_sha256") != _digest(plan.model_dump(mode="json"))):
            raise ValueError
        items = execution.get("cases")
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ValueError
        keys = [(item.get("script_path"), item.get("id")) for item in items]
        expected = {(script.script_path, case.id) for script in plan.scripts for case in script.cases}
        expected.update((script.script_path, "not_run") for script in plan.scripts if script.support == "unsupported")
        if len(set(keys)) != len(keys) or set(keys) != expected:
            raise ValueError
        if public_output_contract is not None:
            public_output_contract = PublicOutputContract.model_validate(public_output_contract.model_dump()
                if hasattr(public_output_contract, "model_dump") else public_output_contract)
            if public_output_contract.rules or "public_output_verification" in execution:
                validate_public_contract_receipt(execution, public_output_contract, programs, plan, require_passed=False)
                if execution["public_output_verification"]["status"] not in {"passed", "unsupported", "not_applicable"}:
                    return []
        elif "public_output_verification" in execution:
            raise ValueError
        by_case = dict(zip(keys, items))
        checked, definitions = {}, {}
        for script in plan.scripts:
            if script.support == "unsupported":
                if by_case[(script.script_path, "not_run")].get("status") != "unsupported":
                    raise ValueError
                continue
            for case in script.cases:
                key = script.script_path, case.id
                item = by_case[key]
                actual = item.get("actual_exit_code")
                matches = (type(actual) is int and (0 < actual < 124 if case.expected_exit_code == "nonzero"
                            else actual == case.expected_exit_code))
                if (item.get("kind") != case.kind or item.get("input_unchanged") is not True or not matches
                        or not _exact(item.get("expected_exit_code"), case.expected_exit_code)
                        or item.get("status") not in {"passed", "failed"}
                        or item.get("reason") not in {"expectations_matched", "assertion_mismatch"}
                        or "runtime_build_evidence" in item or "runtime_error_type" in item):
                    return []
                checks = _complete_checks(case, item)
                if checks is None or (item["status"] == "passed") != all(check["passed"] for check in checks.values()):
                    return []
                if item["status"] == "failed" and (case.kind == "invalid" or case.expected_exit_code != 0):
                    return []
                checked[key], definitions[key] = checks, case
        scope, seen = {}, set()
        for correction in corrections:
            if not isinstance(correction, dict) or set(correction) != {
                    "script_path", "case_id", "assertion_index", "target_type", "field", "before", "after"}:
                raise ValueError
            key = correction["script_path"], correction["case_id"]
            index, kind, field = correction["assertion_index"], correction["target_type"], correction["field"]
            if (key not in definitions or type(index) is not int or not 0 <= index < len(definitions[key].assertions)
                    or (key, index, kind, field) in seen):
                raise ValueError
            seen.add((key, index, kind, field))
            assertion = definitions[key].assertions[index]
            prefix = f"assertion_{index + 1}"
            if kind == "csv_field" and assertion.type == "csv_rows" and field in assertion.fields:
                after = assertion.fields[field].value
                count = checked[key][prefix + ".count"]["actual"]
                targets = [checked[key][f"{prefix}.row_{row}.{field}"] for row in range(1, count + 1)]
            elif kind == "csv_row_count" and assertion.type == "csv_rows" and field == "count":
                after, targets = assertion.count, [checked[key][prefix + ".count"]]
            elif kind == "json_array_length" and assertion.type == kind and field == "length":
                after, targets = assertion.length, [checked[key][prefix]]
            elif kind == "json_value" and assertion.type == kind and field == "value":
                after, targets = assertion.expected.value, [checked[key][prefix]]
            else:
                raise ValueError
            if not _exact(correction["after"], after) or _exact(correction["before"], after):
                raise ValueError
            case = definitions[key]
            if targets and any(target["passed"] is False for target in targets):
                if case.kind == "invalid" or case.expected_exit_code != 0:
                    return []
                scope.setdefault(key[0], set()).update(case.requirement_ids)
        return [{"script_path": path, "requirement_ids": sorted(ids)} for path, ids in sorted(scope.items())]
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as error:
        raise CandidateMethodEvidenceError() from error


class _WithoutDocstrings(ast.NodeTransformer):
    def _body(self, node):
        self.generic_visit(node)
        if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str):
            node.body = node.body[1:]
        return node
    visit_Module = visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _body


def _scope_pairs(scope, programs, requirements):
    if not isinstance(scope, list) or not scope:
        raise ValueError
    pairs, paths = set(), set()
    for item in scope:
        if not isinstance(item, dict) or set(item) != {"script_path", "requirement_ids"}:
            raise ValueError
        path, ids = item["script_path"], item["requirement_ids"]
        if (path not in programs or path in paths or not isinstance(ids, list) or not ids
                or any(type(identifier) is not str or identifier not in requirements for identifier in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError
        paths.add(path)
        pairs.update((path, identifier) for identifier in ids)
    return pairs


def _executable_quotes(code):
    tree = _WithoutDocstrings().visit(ast.parse(code))
    return {segment for node in ast.walk(tree)
            if isinstance(node, (ast.stmt, ast.expr)) and not isinstance(node, (ast.Constant, ast.Name, ast.Pass))
            and not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
            if (segment := ast.get_source_segment(code, node)) is not None}


def validate_candidate_method_audit(value, contract, sources, programs, scope):
    """Check exact request coverage and literal provenance, never decide math."""
    try:
        value = CandidateMethodAudit.model_validate(value.model_dump() if hasattr(value, "model_dump") else value)
        contract = MethodContract.model_validate(contract.model_dump() if hasattr(contract, "model_dump") else contract)
        requirements = {item.id: item for item in contract.requirements}
        if len(requirements) != len(contract.requirements):
            raise ValueError
        source_map = {item["id"]: item for item in sources}
        if len(source_map) != len(sources):
            raise ValueError
        expected = _scope_pairs(scope, programs, requirements)
        actual = [(item.script_path, item.requirement_id) for item in value.requirements]
        if len(set(actual)) != len(actual) or set(actual) != expected:
            raise ValueError
        quotes = {}
        for item in value.requirements:
            if not item.explanation.strip():
                raise ValueError
            if item.source_id or item.source_quote:
                if (item.source_id not in requirements[item.requirement_id].source_ids or item.source_id not in source_map
                        or not item.source_quote.strip() or item.source_quote not in source_map[item.source_id]["text"]):
                    raise ValueError
            if item.code_quote:
                if item.script_path not in quotes:
                    quotes[item.script_path] = _executable_quotes(programs[item.script_path])
                if item.code_quote not in quotes[item.script_path]:
                    raise ValueError
            if item.status == "source_violation" and not (item.source_id and item.source_quote.strip() and item.code_quote.strip()):
                raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError, SyntaxError) as error:
        raise SkillPackageValidationError("candidate_method_audit_invalid") from error


def candidate_method_audit_identity(payload):
    """No test content or cosmetic code changes can create a fresh audit vote."""
    try:
        if set(payload) != {"method_contract", "sources", "programs", "review_scope"}:
            raise ValueError
        contract = MethodContract.model_validate(payload["method_contract"])
        programs, sources = payload["programs"], payload["sources"]
        requirements = {item.id: item for item in contract.requirements}
        if len(requirements) != len(contract.requirements):
            raise ValueError
        pairs = _scope_pairs(payload["review_scope"], programs, requirements)
        if len({item["id"] for item in sources}) != len(sources):
            raise ValueError
        document = contract.model_dump(mode="json")
        document["requirements"] = sorted(document["requirements"], key=lambda item: item["id"])
        for item in document["requirements"]:
            item["source_ids"] = sorted(item["source_ids"])
        document["excluded_sources"] = sorted(document["excluded_sources"], key=lambda item: item["source_id"])
        return {"method_contract": document, "sources": sorted(sources, key=lambda item: item["id"]),
                "programs": {path: ast.dump(_WithoutDocstrings().visit(ast.parse(code)), include_attributes=False)
                             for path, code in sorted(programs.items())},
                "review_scope": [{"script_path": path, "requirement_ids": sorted(identifier for p, identifier in pairs if p == path)}
                                 for path in sorted({path for path, _ in pairs})]}
    except (ValueError, TypeError, KeyError, AttributeError, SyntaxError) as error:
        raise SkillPackageValidationError("candidate_method_audit_invalid") from error
