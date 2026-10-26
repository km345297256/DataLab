"""Host-owned checks for declared JSON column prefixes, not scientific answers.

These checks never enter the model's validation plan. They inspect actual JSON
from the same isolated execution, preserving every scientific expectation and
the original plan digest. A JSON prefix does not prove CSV header provenance.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.services.skills.skill_validation import ScriptValidationInterface, SyntheticValidationPlan, _relative_path

PUBLIC_FAILURE_REASONS = frozenset({"public_prefix_not_array", "public_prefix_mismatch", "public_property_missing",
    "public_parent_not_array", "public_json_invalid", "public_execution_unconfirmed", "public_execution_failed",
    "public_output_binding_unknown", "public_output_unavailable"})


class PublicOutputContractError(ValueError):
    pass


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PublicLocationStep(ContractModel):
    kind: Literal["property", "items"]
    name: str | None = None

    @model_validator(mode="after")
    def valid_name(self):
        if (self.kind == "property") != (self.name is not None):
            raise ValueError("Only property steps have an exact name")
        return self


class PublicPrefixRule(ContractModel):
    id: str = Field(pattern=r"^C[0-9]{3,}$")
    script_path: str = Field(pattern=r"^scripts/[a-z][a-z0-9_-]{0,47}\.py$")
    output_path: str
    filename_argument: str | None = None
    steps: list[PublicLocationStep]
    prefix: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def safe_binding(self):
        if self.output_path != "@output":
            if not self.output_path.startswith("@output/"):
                raise ValueError("A public output must bind to the output argument")
            _relative_path(self.output_path[len("@output/"):])
        if self.filename_argument is not None and not self.filename_argument.startswith("--"):
            raise ValueError("A filename override must name a declared CLI argument")
        if any(not item or "\x00" in item for item in self.prefix):
            raise ValueError("Public column names must be nonempty")
        return self


class PublicOutputContract(ContractModel):
    version: Literal[1] = 1
    scope: Literal["declared_json_column_prefix"] = "declared_json_column_prefix"
    rules: list[PublicPrefixRule]

    @model_validator(mode="after")
    def unique_ids(self):
        if len({rule.id for rule in self.rules}) != len(self.rules):
            raise ValueError("Public contract IDs must be unique")
        return self


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def compile_public_output_contract(interfaces) -> PublicOutputContract:
    """Project proven public prefixes; arbitrary array values stay private."""
    rules = []
    def walk(shape, steps):
        if not isinstance(shape, dict):
            raise PublicOutputContractError("invalid_public_shape")
        if "column_name_prefix" in shape:
            if shape.get("type") != "array":
                raise PublicOutputContractError("invalid_public_prefix_shape")
            yield steps, shape["column_name_prefix"]
        if shape.get("type") == "object":
            for key, child in shape.get("properties", {}).items():
                yield from walk(child, [*steps, {"kind": "property", "name": key}])
        elif shape.get("type") == "array":
            yield from walk(shape.get("items", {"type": "unknown"}), [*steps, {"kind": "items"}])
    try:
        for interface in interfaces:
            interface = (interface if isinstance(interface, ScriptValidationInterface)
                         else ScriptValidationInterface.model_validate(interface))
            for output in interface.json_outputs:
                for steps, prefix in walk(output.get("shape", {"type": "unknown"}), []):
                    argument = output.get("filename_argument")
                    if argument is not None and argument not in interface.arguments:
                        raise PublicOutputContractError("unknown_public_filename_argument")
                    rules.append({"script_path": interface.script_path, "output_path": output.get("path"),
                        "filename_argument": argument, "steps": steps, "prefix": prefix})
        canonical = sorted({json.dumps(rule, ensure_ascii=False, sort_keys=True) for rule in rules})
        return PublicOutputContract(rules=[PublicPrefixRule(id=f"C{index + 1:03d}", **json.loads(rule))
                                           for index, rule in enumerate(canonical)])
    except (ValueError, TypeError, KeyError) as error:
        raise PublicOutputContractError("invalid_public_output_contract") from error


def public_output_path(rule, case) -> str:
    """Resolve only validated argument bindings, never arbitrary host paths."""
    try:
        if case.argv.count("--output") != 1:
            raise ValueError("Ambiguous output argument")
        base = case.argv[case.argv.index("--output") + 1]
        if base != "{output}" and not base.startswith("{output}/"):
            raise ValueError("Output must be isolated writable space")
        declared = rule.output_path
        if rule.filename_argument is not None and rule.filename_argument in case.argv:
            if case.argv.count(rule.filename_argument) != 1:
                raise ValueError("Ambiguous filename override")
            name = case.argv[case.argv.index(rule.filename_argument) + 1]
            if "/" in name or "\\" in name:
                raise ValueError("Filename override must be a filename")
            _relative_path(name)
            if "/" not in declared:
                raise ValueError("Filename override needs a directory binding")
            declared = declared.rsplit("/", 1)[0] + "/" + name
        path = base + declared[len("@output"):]
        if not path.startswith("{output}/"):
            raise ValueError("A JSON file cannot be the output directory")
        return _relative_path(path[len("{output}/"):])
    except (ValueError, IndexError) as error:
        raise PublicOutputContractError("public_output_binding_unknown") from error


def public_rules_for_case(contract, script_path, case):
    return [rule for rule in contract.rules if rule.script_path == script_path] if case.expected_exit_code == 0 else []


def _json_document(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    def invalid(_):
        raise ValueError("Nonfinite JSON number")
    def finite(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("Nonfinite JSON number")
        return number
    return json.loads(data.decode("utf-8-sig"), object_pairs_hook=pairs, parse_constant=invalid, parse_float=finite)


def check_public_output(rule, data) -> dict:
    """Return only public rule identity and status, never observed data values."""
    observations = 0
    def visit(value, steps):
        nonlocal observations
        if not steps:
            if not isinstance(value, list):
                raise PublicOutputContractError("public_prefix_not_array")
            if len(value) < len(rule.prefix) or value[:len(rule.prefix)] != rule.prefix:
                raise PublicOutputContractError("public_prefix_mismatch")
            observations += 1
            return
        step, remaining = steps[0], steps[1:]
        if step.kind == "property":
            if not isinstance(value, dict) or step.name not in value:
                raise PublicOutputContractError("public_property_missing")
            visit(value[step.name], remaining)
        else:
            if not isinstance(value, list):
                raise PublicOutputContractError("public_parent_not_array")
            for item in value:
                visit(item, remaining)
    try:
        visit(_json_document(data), rule.steps)
    except PublicOutputContractError as error:
        return {"rule_id": rule.id, "status": "failed", "observations": observations, "reason": str(error)}
    except (ValueError, UnicodeError, TypeError, RecursionError):
        return {"rule_id": rule.id, "status": "failed", "observations": 0, "reason": "public_json_invalid"}
    return {"rule_id": rule.id, "status": "passed" if observations else "not_observed", "observations": observations}


def failed_public_case(rules, *, reason):
    return [{"rule_id": rule.id, "status": "failed", "observations": 0, "reason": reason} for rule in rules]


def public_case_status(checks):
    return "failed" if any(item["status"] == "failed" for item in checks) else (
        "passed" if any(item["status"] == "passed" for item in checks) else "not_observed")


def public_contract_receipt(contract, plan, cases):
    by_script = {script.script_path: script for script in plan.scripts}
    observed = {check["rule_id"] for case in cases for check in case["checks"]
                if check["status"] == "passed" and check["observations"] > 0}
    required = {rule.id for rule in contract.rules if by_script[rule.script_path].support == "supported"}
    unsupported = any(by_script[rule.script_path].support == "unsupported" for rule in contract.rules)
    status = ("not_applicable" if not contract.rules else "failed"
              if any(case["status"] == "failed" for case in cases) or not required <= observed
              else "unsupported" if unsupported else "passed")
    return {"scope": contract.scope, "contract_sha256": _digest(contract.model_dump(mode="json")),
            "status": status, "cases": cases}


def validate_public_contract_receipt(execution, contract, programs, plan, *, require_passed=True):
    """Bind complete host evidence to the frozen code, plan and public rules."""
    contract = PublicOutputContract.model_validate(contract.model_dump() if hasattr(contract, "model_dump") else contract)
    # Match the worker's existing typed normalization (e.g. default numeric
    # tolerances); do not modify the caller's frozen plan or its assertions.
    plan = SyntheticValidationPlan.model_validate(plan.model_dump() if hasattr(plan, "model_dump") else plan)
    try:
        if (execution.get("scope") != "synthetic" or execution.get("status") not in {"passed", "failed", "unsupported"}
                or execution.get("source_sha256") != {path: hashlib.sha256(code.encode()).hexdigest() for path, code in programs.items()}
                or execution.get("plan_sha256") != _digest(plan.model_dump())):
            raise ValueError("Wrong execution identity")
        receipt = execution["public_output_verification"]
        if (set(receipt) != {"scope", "contract_sha256", "status", "cases"}
                or receipt["scope"] != contract.scope
                or receipt["contract_sha256"] != _digest(contract.model_dump(mode="json"))):
            raise ValueError("Wrong public contract")
        cases = receipt["cases"]
        expected = {(script.script_path, case.id): public_rules_for_case(contract, script.script_path, case)
                    for script in plan.scripts if script.support == "supported" for case in script.cases
                    if public_rules_for_case(contract, script.script_path, case)}
        keys = [(case["script_path"], case["id"]) for case in cases]
        if len(keys) != len(set(keys)) or set(keys) != set(expected):
            raise ValueError("Incomplete public cases")
        actual_cases = execution["cases"]
        actual_keys = [(case["script_path"], case["id"]) for case in actual_cases]
        if len(actual_keys) != len(set(actual_keys)) or not set(expected) <= set(actual_keys):
            raise ValueError("Public checks lack corresponding executions")
        actual_cases = dict(zip(actual_keys, actual_cases))
        for case, key in zip(cases, keys):
            if set(case) != {"script_path", "id", "status", "checks"}:
                raise ValueError("Unexpected public case fields")
            checks = case["checks"]
            ids = [check["rule_id"] for check in checks]
            if len(ids) != len(set(ids)) or set(ids) != {rule.id for rule in expected[key]}:
                raise ValueError("Incomplete public checks")
            for check in checks:
                status = check["status"]
                if (set(check) != ({"rule_id", "status", "observations", "reason"} if status == "failed"
                                   else {"rule_id", "status", "observations"})
                        or status not in {"passed", "not_observed", "failed"}
                        or type(check["observations"]) is not int
                        or status == "passed" and check["observations"] <= 0
                        or status == "not_observed" and check["observations"] != 0
                        or status == "failed" and check["reason"] not in PUBLIC_FAILURE_REASONS
                        or check["observations"] < 0):
                    raise ValueError("Failed or invalid public observation")
            if case["status"] != public_case_status(checks):
                raise ValueError("Wrong public case status")
            if case["status"] != "failed" and (actual_cases[key].get("actual_exit_code") != 0
                    or actual_cases[key].get("input_unchanged") is not True):
                raise ValueError("Public outputs were not observed in a successful safe execution")
        calculated = public_contract_receipt(contract, plan, cases)
        if receipt != calculated or require_passed and receipt["status"] not in {"passed", "unsupported", "not_applicable"}:
            raise ValueError("Unconfirmed public observations")
        if receipt["status"] == "failed" and execution["status"] != "failed":
            raise ValueError("Public contract failure was concealed")
    except (KeyError, ValueError, TypeError) as error:
        raise PublicOutputContractError("public_output_contract_unconfirmed") from error
    return receipt
