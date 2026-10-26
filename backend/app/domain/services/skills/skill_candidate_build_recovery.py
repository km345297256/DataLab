"""Identify one blind candidate build repair, never a numerical disagreement.

Only the fixed worker's uncaught-exception channel supplies build authority.
The returned projection contains no observed/expected values or stderr. This
does not approve the replacement: its method, interface and frozen numerical
plan must still pass their original validation boundaries.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.domain.services.skills.skill_public_output_contract import (
    PublicOutputContract, validate_public_contract_receipt,
)
from app.domain.services.skills.skill_validation import SyntheticValidationPlan


class CandidateBuildEvidenceError(ValueError):
    def __init__(self):
        super().__init__("candidate_build_evidence_unconfirmed")


class CandidateBuildFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    script_path: str
    case_id: str
    exception_type: Literal["TypeError", "NameError", "UnboundLocalError", "AttributeError", "KeyError", "IndexError"]


_BUILD_TYPES = frozenset({"TypeError", "NameError", "UnboundLocalError", "AttributeError", "KeyError", "IndexError"})


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _passed_checks(case, observed):
    """A missing check is not a successful neighbouring case."""
    checks = observed.get("checks")
    if not isinstance(checks, list) or any(not isinstance(item, dict) or item.get("passed") is not True for item in checks):
        return False
    identifiers = [item.get("id") for item in checks]
    if any(not isinstance(value, str) for value in identifiers) or len(set(identifiers)) != len(identifiers):
        return False
    seen = set(identifiers)
    needed_count = 0
    for index, assertion in enumerate(case.assertions):
        prefix = f"assertion_{index + 1}"
        if assertion.type != "csv_rows":
            needed_count += 1
            if prefix not in seen:
                return False
            continue
        needed_count += 1 + assertion.count * len(assertion.fields)
        if prefix + ".count" not in seen:
            return False
        matching = [name for name in seen if name.startswith(prefix + ".row_")]
        if len(matching) != assertion.count * len(assertion.fields):
            return False
        for name in matching:
            match = re.fullmatch(re.escape(prefix) + r"\.row_([1-9][0-9]*)\.(.+)", name)
            if match is None or not 1 <= int(match[1]) <= assertion.count or match[2] not in assertion.fields:
                return False
    return len(checks) == needed_count


def collect_candidate_build_failures(execution, plan, programs, *, public_output_contract=None) -> list[CandidateBuildFailure]:
    """Return only complete, pre-observation build failures of success cases.

    Environment/unavailable/ordinary scientific failures have no build-repair
    authority. A malformed or wrongly bound receipt raises a fixed safe error.
    Call only after independently confirming and freezing the given plan.
    """
    # Old/non-build receipts retain their existing failure path. Merely seeing
    # stderr or a runtime_error_type must never opt into this new authority.
    if (not isinstance(execution, dict) or not isinstance(execution.get("cases"), list)
            or not any(isinstance(item, dict) and "runtime_build_evidence" in item for item in execution["cases"])):
        return []
    try:
        plan = SyntheticValidationPlan.model_validate(plan.model_dump(mode="json") if hasattr(plan, "model_dump") else plan)
        if not isinstance(execution, dict):
            raise ValueError
        if execution.get("status") == "unavailable":
            return []
        if (execution.get("scope") != "synthetic" or execution.get("status") not in {"passed", "failed", "unsupported"}
                or set(programs) != {item.script_path for item in plan.scripts}
                or execution.get("source_sha256") != {path: hashlib.sha256(code.encode()).hexdigest() for path, code in programs.items()}
                or execution.get("plan_sha256") != _hash(plan.model_dump(mode="json"))):
            raise ValueError
        items = execution.get("cases")
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ValueError
        keys = [(item.get("script_path"), item.get("id")) for item in items]
        expected = {(script.script_path, case.id) for script in plan.scripts for case in script.cases}
        expected.update((script.script_path, "not_run") for script in plan.scripts if script.support == "unsupported")
        if len(set(keys)) != len(keys) or set(keys) != expected:
            raise ValueError
        observed = dict(zip(keys, items))
        for item in items:
            if "runtime_build_evidence" not in item:
                continue
            evidence = item["runtime_build_evidence"]
            kind = item.get("runtime_error_type")
            if (not isinstance(kind, str) or kind not in _BUILD_TYPES
                    or not isinstance(evidence, dict) or evidence != {
                        "version": 1, "origin": "host_uncaught_exception", "exception_type": kind}
                    or type(evidence.get("version")) is not int):
                raise ValueError
        if public_output_contract is not None:
            public_output_contract = PublicOutputContract.model_validate(public_output_contract.model_dump()
                if hasattr(public_output_contract, "model_dump") else public_output_contract)
            if public_output_contract.rules or "public_output_verification" in execution:
                validate_public_contract_receipt(execution, public_output_contract, programs, plan, require_passed=False)
        elif "public_output_verification" in execution:
            # The caller cannot omit a contract that contributed observations.
            raise ValueError
        public_cases = {(item["script_path"], item["id"]): item
                        for item in execution.get("public_output_verification", {}).get("cases", [])}
        if execution["status"] != "failed":
            return []
        failures = []
        for script in plan.scripts:
            if script.support == "unsupported":
                if observed[(script.script_path, "not_run")].get("status") != "unsupported":
                    raise ValueError
                continue
            for case in script.cases:
                item = observed[(script.script_path, case.id)]
                actual = item.get("actual_exit_code")
                if (item.get("kind") != case.kind or item.get("input_unchanged") is not True
                        or type(actual) is not int or type(item.get("expected_exit_code")) is not type(case.expected_exit_code)
                        or item.get("expected_exit_code") != case.expected_exit_code):
                    return []
                if item.get("status") == "passed":
                    matches = 0 < actual < 124 if case.expected_exit_code == "nonzero" else actual == case.expected_exit_code
                    if not matches or not _passed_checks(case, item) or item.get("runtime_build_evidence") is not None:
                        return []
                    public = public_cases.get((script.script_path, case.id))
                    if public is not None and (public.get("status") == "failed"
                            or any(check.get("status") == "failed" for check in public["checks"])):
                        return []
                    continue
                evidence = item.get("runtime_build_evidence")
                kind = item.get("runtime_error_type")
                if (item.get("status") != "failed" or case.kind == "invalid" or case.expected_exit_code != 0
                        or actual != 1 or item.get("reason") != "program_runtime_failure"
                        or kind not in _BUILD_TYPES or evidence != {"version": 1,
                            "origin": "host_uncaught_exception", "exception_type": kind}
                        or type(evidence.get("version")) is not int
                        or item.get("checks", []) != [] or item.get("output_sha256", {}) != {}):
                    return []
                if (public := public_cases.get((script.script_path, case.id))) is not None:
                    if any(check.get("status") != "failed" or check.get("observations") != 0
                           or check.get("reason") not in {"public_execution_failed", "public_execution_unconfirmed"}
                           for check in public["checks"]):
                        return []
                failures.append(CandidateBuildFailure(script_path=script.script_path, case_id=case.id, exception_type=kind))
        return failures
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as error:
        raise CandidateBuildEvidenceError() from error
