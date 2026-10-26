"""Source provenance may justify an audit; a synthetic number never does."""
import copy
import hashlib
import json

import pytest
from pydantic import ValidationError

from app.domain.services.skills.method_contract import MethodContract
from app.domain.services.skills.skill_candidate_method_audit import (
    AUDIT_INSTRUCTION, METHOD_REPAIR_INSTRUCTION, CandidateMethodAudit,
    CandidateMethodEvidenceError, candidate_method_audit_identity,
    collect_candidate_method_scope, validate_candidate_method_audit,
)
from app.domain.services.skills.skill_creation_error import SkillPackageValidationError
from app.domain.services.skills.skill_public_output_contract import PublicOutputContract, PublicPrefixRule, public_contract_receipt
from app.domain.services.skills.skill_validation import SyntheticValidationPlan
from app.infrastructure.external.sandbox import skill_validation_worker as worker
from test_skill_validation_worker import rejection_plan


PROGRAMS = {"scripts/summary.py": '"""Program documentation, not a method proof."""\n'
            '# return 999  # a comment is not executed\n'
            'def aggregate(values):\n    """Return the requested statistic."""\n    return sum(values)\n'}
CONTRACT = MethodContract.model_validate({"requirements": [{"id": "R001", "kind": "method",
    "statement": "Compute the arithmetic mean of every accepted input record.", "source_ids": ["S001"]}]})
SOURCES = [{"id": "S001", "kind": "request", "text": CONTRACT.requirements[0].statement}]
SCOPE = [{"script_path": "scripts/summary.py", "requirement_ids": ["R001"]}]


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def evidence(*, json_output=False, multiple_rows=False):
    plan = rejection_plan()
    if json_output:
        for case in plan.scripts[0].cases[:2]:
            case.assertions = [__import__("app.domain.services.skills.skill_validation", fromlist=["JSONValueAssertion"]).JSONValueAssertion(
                type="json_value", path="summary.json", pointer="/value", expected={"value": 4})]
    cases = []
    for case in plan.scripts[0].cases:
        files = {"summary.csv": b"group,value\nA,2\nA,4\n" if multiple_rows and case.kind == "normal" else
                 b"group,value\nA,2\n" if case.kind == "normal" else b"group,value\nA,4\n",
                 "summary.json": b'{"value":2}' if case.kind == "normal" else b'{"value":4}'}
        checks = worker._assertions(case, files)
        passed = all(check["passed"] for check in checks)
        cases.append({"script_path": "scripts/summary.py", "id": case.id, "kind": case.kind,
            "status": "passed" if passed else "failed", "reason": "expectations_matched" if passed else "assertion_mismatch",
            "actual_exit_code": 0 if case.expected_exit_code == 0 else 2, "expected_exit_code": case.expected_exit_code,
            "input_unchanged": True, "checks": checks})
    execution = {"scope": "synthetic", "status": "failed", "cases": cases,
        "source_sha256": {path: hashlib.sha256(code.encode()).hexdigest() for path, code in PROGRAMS.items()},
        "plan_sha256": digest(plan.model_dump(mode="json"))}
    corrections = [{"script_path": "scripts/summary.py", "case_id": cases[0]["id"], "assertion_index": 0,
        "target_type": "json_value" if json_output else "csv_field", "field": "value", "before": 2, "after": 4}]
    return plan, execution, corrections


def audit(status="source_violation"):
    return CandidateMethodAudit(requirements=[{"requirement_id": "R001", "status": status,
        "source_id": "S001", "source_quote": SOURCES[0]["text"], "script_path": "scripts/summary.py",
        "code_quote": "return sum(values)", "explanation": "The source requires a mean; this function returns a sum without dividing by the valid count."}])


@pytest.mark.parametrize("json_output,multiple_rows", [(False, False), (False, True), (True, False)])
def test_only_real_disagreement_projects_original_requirements_without_answers(json_output, multiple_rows):
    plan, receipt, corrections = evidence(json_output=json_output, multiple_rows=multiple_rows)
    before = copy.deepcopy((plan.model_dump(mode="json"), receipt, corrections, PROGRAMS))
    receipt["diagnostic"] = "PRIVATE_TEST_ANSWER_908771"
    assert collect_candidate_method_scope(receipt, plan, PROGRAMS, corrections) == SCOPE
    assert "908771" not in json.dumps(SCOPE)
    receipt.pop("diagnostic")
    assert (plan.model_dump(mode="json"), receipt, corrections, PROGRAMS) == before


@pytest.mark.parametrize("damage", ["source_hash", "plan_hash", "missing_case", "duplicate_case", "unknown_case",
    "wrong_correction_case", "wrong_index", "bool_index", "wrong_after", "wrong_field", "wrong_type", "duplicate_correction",
    "missing_after", "same_before", "public_contract_omitted"])
def test_ambiguous_provenance_cannot_authorize_a_focused_audit(damage):
    plan, receipt, corrections = evidence()
    if damage == "source_hash": receipt["source_sha256"] = {}
    elif damage == "plan_hash": receipt["plan_sha256"] = "0" * 64
    elif damage == "missing_case": receipt["cases"].pop()
    elif damage == "duplicate_case": receipt["cases"].append(copy.deepcopy(receipt["cases"][0]))
    elif damage == "unknown_case": receipt["cases"][0]["id"] = "unknown"
    elif damage == "wrong_correction_case": corrections[0]["case_id"] = "unknown"
    elif damage == "wrong_index": corrections[0]["assertion_index"] = 99
    elif damage == "bool_index": corrections[0]["assertion_index"] = False
    elif damage == "wrong_after": corrections[0]["after"] = 5
    elif damage == "wrong_field": corrections[0]["field"] = "unrelated"
    elif damage == "wrong_type": corrections[0]["target_type"] = "csv_header"
    elif damage == "duplicate_correction": corrections.append(copy.deepcopy(corrections[0]))
    elif damage == "missing_after": corrections[0].pop("after")
    elif damage == "same_before": corrections[0]["before"] = 4
    elif damage == "public_contract_omitted": receipt["public_output_verification"] = {}
    with pytest.raises(CandidateMethodEvidenceError, match="^candidate_method_evidence_unconfirmed$"):
        collect_candidate_method_scope(receipt, plan, PROGRAMS, corrections)


@pytest.mark.parametrize("damage", ["missing_output", "missing_actual", "missing_check", "duplicate_check", "false_passed",
    "wrong_expected", "wrong_expected_type", "wrong_kind", "wrong_exit_type", "runtime", "timeout", "unsafe_input",
    "invalid_failure", "wrong_reason", "structure_failure", "nonfinite_csv", "empty_numeric_csv", "truncated_csv",
    "json_null", "json_bool", "json_container", "neighbour_check_missing", "status_inconsistent"])
def test_incomplete_structural_environment_or_rejection_failures_do_not_trigger_audit(damage):
    plan, receipt, corrections = evidence(json_output=damage.startswith("json_"))
    case = receipt["cases"][0]
    check = case["checks"][-1]
    if damage == "missing_output": check.clear(); check.update(id="assertion_1", passed=False, reason="missing_or_invalid_output")
    elif damage == "missing_actual": check.pop("actual")
    elif damage == "missing_check": case["checks"].pop()
    elif damage == "duplicate_check": case["checks"].append(copy.deepcopy(check))
    elif damage == "false_passed": check["passed"] = True
    elif damage == "wrong_expected": check["expected"] = 99
    elif damage == "wrong_expected_type": check["expected"] = 4.0
    elif damage == "wrong_kind": case["kind"] = "invalid"
    elif damage == "wrong_exit_type": case["actual_exit_code"] = False
    elif damage == "runtime": case.update(actual_exit_code=1, reason="program_runtime_failure", runtime_error_type="KeyError")
    elif damage == "timeout": case.update(actual_exit_code=124, reason="program_timeout")
    elif damage == "unsafe_input": case["input_unchanged"] = False
    elif damage == "invalid_failure": receipt["cases"][-1].update(status="failed", reason="exit_code_mismatch", actual_exit_code=0)
    elif damage == "wrong_reason": case["reason"] = "public_output_contract_mismatch"
    elif damage == "structure_failure": check["reason"] = "missing_or_duplicate_column"
    elif damage == "nonfinite_csv": check["actual"] = "NaN"
    elif damage == "empty_numeric_csv": check["actual"] = ""
    elif damage == "truncated_csv": check["actual"] = "1" * 160
    elif damage == "json_null": check["actual"] = None
    elif damage == "json_bool": check["actual"] = False
    elif damage == "json_container": check["actual"] = {"value": 2}
    elif damage == "neighbour_check_missing": receipt["cases"][1]["checks"].pop()
    elif damage == "status_inconsistent": case["status"] = "passed"
    assert collect_candidate_method_scope(receipt, plan, PROGRAMS, corrections) == []


def test_passing_corrected_target_does_not_authorize_audit_of_unrelated_failure():
    plan, receipt, corrections = evidence()
    corrections[0]["case_id"] = plan.scripts[0].cases[1].id
    assert collect_candidate_method_scope(receipt, plan, PROGRAMS, corrections) == []
    assert collect_candidate_method_scope({"status": "unavailable"}, plan, PROGRAMS, corrections) == []
    assert collect_candidate_method_scope(receipt, plan, PROGRAMS, []) == []


def test_row_count_disagreement_has_complete_observations_without_inventing_missing_rows():
    plan, receipt, corrections = evidence(multiple_rows=True)
    corrections[0].update(target_type="csv_row_count", field="count", before=2, after=1)
    assert collect_candidate_method_scope(receipt, plan, PROGRAMS, corrections) == SCOPE
    receipt["cases"][0]["checks"].pop()  # Claimed second row is now incomplete.
    assert collect_candidate_method_scope(receipt, plan, PROGRAMS, corrections) == []


@pytest.mark.parametrize("failed", [False, True])
def test_public_structure_must_be_complete_and_pass_even_for_neighbour_case(failed):
    plan, receipt, corrections = evidence()
    public = PublicOutputContract(rules=[PublicPrefixRule(id="C001", script_path="scripts/summary.py",
        output_path="@output/schema.json", steps=[], prefix=["id"])])
    cases = []
    for index, case in enumerate(plan.scripts[0].cases[:2]):
        check = {"rule_id": "C001", "status": "failed" if index == 1 and failed else "passed", "observations": 1}
        if check["status"] == "failed": check["reason"] = "public_prefix_mismatch"
        cases.append({"script_path": "scripts/summary.py", "id": case.id, "status": check["status"], "checks": [check]})
    receipt["public_output_verification"] = public_contract_receipt(public, plan, cases)
    assert collect_candidate_method_scope(receipt, plan, PROGRAMS, corrections, public_output_contract=public) == ([] if failed else SCOPE)


@pytest.mark.parametrize("quote", ["return sum(values)", "sum(values)", 'def aggregate(values):\n    """Return the requested statistic."""\n    return sum(values)'])
def test_source_violation_binds_verbatim_original_source_and_executable_ast(quote):
    value = audit().model_copy(deep=True)
    value.requirements[0] = value.requirements[0].model_copy(update={"code_quote": quote})
    validate_candidate_method_audit(value, CONTRACT, SOURCES, PROGRAMS, SCOPE)
    assert value.verdict() == "rejected"


@pytest.mark.parametrize("status,verdict", [("preserved", "accepted"), ("uncertain", "uncertain")])
def test_no_confirmed_source_violation_never_claims_repair_authority(status, verdict):
    value = audit(status)
    value.requirements[0] = value.requirements[0].model_copy(update={"source_id": "", "source_quote": "", "code_quote": ""})
    validate_candidate_method_audit(value, CONTRACT, SOURCES, PROGRAMS, SCOPE)
    assert value.verdict() == verdict


@pytest.mark.parametrize("change", [
    {"source_id": "S999"}, {"source_quote": "Calculate a sum instead."}, {"source_quote": ""},
    {"code_quote": "return 999"}, {"code_quote": '# return 999  # a comment is not executed'},
    {"code_quote": '"""Return the requested statistic."""'}, {"code_quote": '"""Program documentation, not a method proof."""'},
    {"code_quote": "values"}, {"code_quote": ""}, {"requirement_id": "R999"},
    {"script_path": "scripts/other.py"}, {"explanation": "   "},
])
def test_foreign_fake_or_nonexecutable_quotations_fail_closed(change):
    value = audit()
    value.requirements[0] = value.requirements[0].model_copy(update=change)
    with pytest.raises(SkillPackageValidationError) as failure:
        validate_candidate_method_audit(value, CONTRACT, SOURCES, PROGRAMS, SCOPE)
    assert failure.value.code == "candidate_method_audit_invalid"


def test_exact_scope_duplicate_and_cross_requirement_sources_are_rejected():
    value = audit()
    value.requirements.append(value.requirements[0])
    with pytest.raises(SkillPackageValidationError): validate_candidate_method_audit(value, CONTRACT, SOURCES, PROGRAMS, SCOPE)
    for scope in ([], [*SCOPE, *SCOPE], [{"script_path": "scripts/summary.py", "requirement_ids": ["R001", "R001"]}]):
        with pytest.raises(SkillPackageValidationError): validate_candidate_method_audit(audit(), CONTRACT, SOURCES, PROGRAMS, scope)
    foreign = [*SOURCES, {"id": "S002", "kind": "method", "text": "This unrelated source says compute a sum."}]
    value = audit()
    value.requirements[0] = value.requirements[0].model_copy(update={"source_id": "S002", "source_quote": foreign[-1]["text"]})
    with pytest.raises(SkillPackageValidationError): validate_candidate_method_audit(value, CONTRACT, foreign, PROGRAMS, SCOPE)


def payload():
    return {"method_contract": CONTRACT.model_dump(), "sources": copy.deepcopy(SOURCES),
            "programs": dict(PROGRAMS), "review_scope": copy.deepcopy(SCOPE)}


def test_semantic_identity_ignores_comments_docstrings_and_scope_order_but_binds_executable_method():
    original = payload()
    baseline = candidate_method_audit_identity(original)
    changed = copy.deepcopy(original)
    changed["programs"]["scripts/summary.py"] = '\n# new comment\ndef aggregate(values):\n    """Other docs."""\n    return sum(values)\n'
    assert candidate_method_audit_identity(changed) == baseline
    changed["programs"]["scripts/summary.py"] = 'def aggregate(values):\n    return sum(values) / len(values)\n'
    assert candidate_method_audit_identity(changed) != baseline
    changed = copy.deepcopy(original)
    changed["sources"][0]["text"] += " The definition changed."
    assert candidate_method_audit_identity(changed) != baseline
    changed = copy.deepcopy(original)
    changed["method_contract"]["requirements"][0]["statement"] += " New obligation."
    assert candidate_method_audit_identity(changed) != baseline
    assert original == payload()


def test_unknown_test_context_cannot_be_silently_ignored_by_audit_identity():
    for key in ("validation_plan", "observed_validation", "corrections", "reference_scripts", "fixed_answer"):
        document = payload()
        document[key] = "PRIVATE_TEST_SENTINEL"
        with pytest.raises(SkillPackageValidationError): candidate_method_audit_identity(document)


def test_prompts_are_source_based_without_fixture_answer_interpolation():
    for instruction in (AUDIT_INSTRUCTION, METHOD_REPAIR_INSTRUCTION):
        assert "原方法" in instruction and "源码" in instruction or "原程序" in instruction
        assert "没有合成" in instruction
    with pytest.raises(ValidationError):
        CandidateMethodAudit.model_validate({**audit().model_dump(), "test_answer": 123})
