"""Verify host-observed import failures without granting algorithm repair.

An import declaration is allowed independently of whether the runtime can load
it. Only the worker's separate interpreter exception channel identifies this
environment failure. No module name, exception text or fixture value is exposed.
"""
from __future__ import annotations

import hashlib
import json

from app.domain.services.skills.skill_candidate_build_recovery import _passed_checks
from app.domain.services.skills.skill_public_output_contract import (
    PublicOutputContract, validate_public_contract_receipt,
)
from app.domain.services.skills.skill_validation import SyntheticValidationPlan


class DependencyEvidenceError(ValueError):
    def __init__(self):
        super().__init__("dependency_evidence_unconfirmed")


def has_dependency_unavailable(execution, plan, programs, *, public_output_contract=None) -> bool:
    """Recognize a complete frozen run failing only at real import operations.

    Legacy receipts and stderr alone do not acquire this classification. Any
    competing scientific, interface or ordinary program failure keeps its own
    failure route; this diagnosis cannot conceal it or authorize code repair.
    """
    if (not isinstance(execution, dict) or not isinstance(execution.get("cases"), list)
            or not any(isinstance(item, dict) and "runtime_dependency_evidence" in item
                       for item in execution["cases"])):
        return False
    try:
        plan = SyntheticValidationPlan.model_validate(plan.model_dump(mode="json")
            if hasattr(plan, "model_dump") else plan)
        if (execution.get("scope") != "synthetic" or execution.get("status") != "failed"
                or set(programs) != {script.script_path for script in plan.scripts}
                or execution.get("source_sha256") != {
                    path: hashlib.sha256(content.encode()).hexdigest() for path, content in programs.items()}
                or execution.get("plan_sha256") != hashlib.sha256(json.dumps(
                    plan.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()):
            raise ValueError
        items = execution["cases"]
        if any(not isinstance(item, dict) for item in items):
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
        elif "public_output_verification" in execution:
            raise ValueError
        public_cases = {(item["script_path"], item["id"]): item
                        for item in execution.get("public_output_verification", {}).get("cases", [])}
        observed = dict(zip(keys, items))
        matched = False
        other_failure = False
        for script in plan.scripts:
            if script.support == "unsupported":
                item = observed[(script.script_path, "not_run")]
                if item.get("status") != "unsupported" or "runtime_dependency_evidence" in item:
                    raise ValueError
                continue
            for case in script.cases:
                item = observed[(script.script_path, case.id)]
                actual = item.get("actual_exit_code")
                proof = item.get("runtime_dependency_evidence")
                if "runtime_dependency_evidence" in item:
                    if (not isinstance(proof, dict) or set(proof) != {"version", "origin", "exception_type"}
                            or type(proof["version"]) is not int or proof["version"] != 1
                            or proof["origin"] != "host_import_exception"
                            or proof["exception_type"] not in {"ModuleNotFoundError", "ImportError"}
                            or item.get("status") != "failed" or item.get("reason") != "dependency_unavailable"
                            or type(actual) is not int or actual != 1 or item.get("input_unchanged") is not True
                            or item.get("kind") != case.kind
                            or type(item.get("expected_exit_code")) is not type(case.expected_exit_code)
                            or item.get("expected_exit_code") != case.expected_exit_code
                            or item.get("checks", []) != [] or item.get("output_sha256", {}) != {}
                            or "runtime_build_evidence" in item or "runtime_error_type" in item or "diagnostic" in item):
                        raise ValueError
                    public = public_cases.get((script.script_path, case.id))
                    if public is not None and any(check.get("status") != "failed" or check.get("observations") != 0
                            or check.get("reason") not in {"public_execution_failed", "public_execution_unconfirmed"}
                            for check in public["checks"]):
                        raise ValueError
                    matched = True
                    continue
                if item.get("status") != "passed":
                    other_failure = True
                    continue
                matches = 0 < actual < 124 if type(actual) is int and case.expected_exit_code == "nonzero" else (
                    type(actual) is int and actual == case.expected_exit_code)
                if (not matches or item.get("input_unchanged") is not True or item.get("kind") != case.kind
                        or type(item.get("expected_exit_code")) is not type(case.expected_exit_code)
                        or item.get("expected_exit_code") != case.expected_exit_code
                        or not _passed_checks(case, item) or "runtime_build_evidence" in item):
                    other_failure = True
                public = public_cases.get((script.script_path, case.id))
                if public is not None and (public.get("status") == "failed"
                        or any(check.get("status") == "failed" for check in public["checks"])):
                    other_failure = True
        return matched and not other_failure
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as error:
        raise DependencyEvidenceError() from error
