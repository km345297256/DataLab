import hashlib
import json
import re

import pytest

from app.domain.models.event import MessageEvent, PlanEvent, PlanStatus
from app.domain.models.plan import ExecutionStatus, Plan, Step
from app.domain.services.skills.method_contract import (
    COVERAGE_LOCATION_PATTERN, COVERAGE_ROOTS, CoverageError, MethodContract, MethodReview, RequirementCoverage, evidence_sources,
    validate_contract, validate_coverage, validate_review,
)
from app.domain.services.skills.program_method_evidence import method_program_references
from app.domain.services.skills.session_skill_creator import _PRIVATE, build_synthesis_evidence


def contract_for(statement="使用 HC3 协方差以及 Holm 校正，保留模型对比的方向。"):
    return MethodContract.model_validate({"requirements": [{"id": "R001", "kind": "method",
        "statement": statement, "source_ids": ["S001"]}]})


def test_coverage_schema_advertises_the_runtime_pointer_grammar_without_changing_error_type():
    from app.domain.services.skills.skill_creation.models import SkillRecipe
    schema = SkillRecipe.model_json_schema()["$defs"]["RequirementCoverage"]
    assert schema["properties"]["locations"]["items"]["pattern"] == COVERAGE_LOCATION_PATTERN
    assert re.fullmatch(COVERAGE_LOCATION_PATTERN, "/steps/0/action")
    assert re.fullmatch(COVERAGE_LOCATION_PATTERN, "/scripts/0/content")
    assert re.fullmatch(COVERAGE_LOCATION_PATTERN, "/outputs/0")
    assert not re.fullmatch(COVERAGE_LOCATION_PATTERN, "/applicability/2")
    assert not re.fullmatch(COVERAGE_LOCATION_PATTERN, "/goal")
    # JSON Schema is guidance; the host keeps its precise CoverageError path.
    item = RequirementCoverage(requirement_id="R001", locations=["/steps/0/action", "/applicability/2"])
    with pytest.raises(CoverageError, match="invalid_coverage_location"):
        validate_coverage(contract_for(), {"steps": [{"action": "Use HC3 and Holm."}], "coverage": [item.model_dump()]})


def test_coverage_diagnostics_collect_every_bad_slot_and_required_section_without_pointer_values():
    contract = MethodContract(requirements=[
        {"id": "R001", "kind": "method", "statement": "Use HC3 and Holm for the stated analysis.", "source_ids": ["S001"]},
        {"id": "R002", "kind": "output", "statement": "Export a complete summary table with diagnostics.", "source_ids": ["S001"]},
        {"id": "R003", "kind": "input", "statement": "Require the selected current input and validate its schema.", "source_ids": ["S001"]},
    ])
    document = {
        "steps": [{"action": "Use HC3 and Holm for the stated analysis."}],
        "parameters": [{"name": "input_path"}],
        "coverage": [
            {"requirement_id": "R001", "locations": ["/steps/0/action", "/applicability/2", "/steps/88/action"]},
            {"requirement_id": "R002", "locations": ["/steps/0/action"]},
            {"requirement_id": "R003", "locations": ["/parameters/0/private_account_detail"]},
        ],
    }
    with pytest.raises(CoverageError) as failure:
        validate_coverage(contract, document)
    assert failure.value.code == "invalid_coverage_location"
    assert failure.value.identifiers == ["R001", "R002", "R003"]
    issues = failure.value.details["coverage_issues"]
    assert {(tuple(issue["location"]), issue["code"]) for issue in issues} == {
        (("coverage", 0, "locations", 1), "invalid_coverage_location"),
        (("coverage", 0, "locations", 2), "unknown_coverage_location"),
        (("coverage", 1, "locations", 0), "requirement_wrong_section"),
        (("coverage", 2, "locations", 0), "unknown_coverage_location"),
    }
    assert issues[0]["allowed_sections"] == ["/steps", "/scripts"]
    assert issues[2]["allowed_sections"] == ["/outputs"]
    assert failure.value.details["allowed_roots"] == ["/" + root for root in COVERAGE_ROOTS]
    rendered = json.dumps(failure.value.details)
    assert "private_account_detail" not in rendered
    assert "/applicability/2" not in rendered and "/steps/88/action" not in rendered


@pytest.mark.parametrize("pointer", ["/Users/private-user/source.csv", "/steps/0/private_account_detail"])
def test_coverage_issue_positions_never_echo_private_or_unknown_model_pointers(pointer):
    document = {"steps": [{"action": "Use HC3 and Holm for the complete analysis."}],
                "coverage": [{"requirement_id": "R001", "locations": ["/steps/0/action", pointer]}]}
    with pytest.raises(CoverageError) as failure:
        validate_coverage(contract_for(), document)
    issue = failure.value.details["coverage_issues"][0]
    assert issue["location"] == ["coverage", 0, "locations", 1]
    assert issue["requirement_id"] == "R001"
    assert pointer not in json.dumps(failure.value.details)
    assert "private_account_detail" not in json.dumps(failure.value.details)


def test_valid_mixed_references_and_declared_pending_script_rules_are_preserved():
    document = {"steps": [{"action": "Use HC3 covariance for the requested analysis."}],
                "limitations": ["The implementation must preserve its stated applicability constraints."],
                "scripts": [],
                "coverage": [{"requirement_id": "R001", "locations": ["/steps/0/action", "/limitations/0", "/scripts/0/content"]}]}
    validate_coverage(contract_for(), document, pending_scripts=[pending_script()])
    with pytest.raises(CoverageError, match="undeclared_script_requirement"):
        validate_coverage(contract_for(), document, pending_scripts=[pending_script(requirement_ids=["R002"])])
    with pytest.raises(CoverageError, match="unknown_coverage_location"):
        validate_coverage(contract_for(), document)


def test_full_request_complete_steps_and_delivery_are_not_character_or_count_truncated():
    request = "请完整执行这些研究要求。" * 1000 + "最后必须使用 HC3 和 Holm。"
    result = "详细解释实际完成的方法。" * 1000 + "保留最后的 Welch 诊断说明。"
    steps = [Step(id=str(i), description=f"第{i}项独立计算与验收", status=ExecutionStatus.COMPLETED,
                  success=True, result=result) for i in range(35)]
    evidence = build_synthesis_evidence([MessageEvent(role="user", message=request),
        PlanEvent(status=PlanStatus.COMPLETED, plan=Plan(steps=steps)), MessageEvent(message=result)])
    assert evidence["research_request"] == request
    assert len(evidence["verified_steps"]) == 35
    assert evidence["verified_steps"][-1]["evidence_summary"] == result
    assert evidence["delivery_summary"] == result
    sources = evidence_sources(evidence)
    assert sources[0]["text"].endswith("HC3 和 Holm。")
    assert sources[-1]["text"].endswith("Welch 诊断说明。")


def test_method_tables_and_fenced_formulas_are_not_discarded_as_raw_rows():
    specification = "比较方向必须遵循下表：\n| 方法 | 校正 |\n| 对比 | Holm |\n\n```text\nHC3 = diag-adjusted covariance\n```"
    evidence = build_synthesis_evidence([MessageEvent(role="user", message=specification)])
    assert evidence["research_request"] == specification


@pytest.mark.parametrize("method", ["HC3", "Holm", "Welch", "Tukey", "Benjamini-Hochberg", "RANSAC"])
def test_general_method_identifiers_cannot_disappear_during_requirement_extraction(method):
    sources = [{"id": "S001", "kind": "request", "text": f"请执行 {method} 方法并报告其诊断。"}]
    with pytest.raises(CoverageError, match="method_identifiers_lost"):
        validate_contract(contract_for("完成总体分析并给出研究结论。"), sources)
    validate_contract(contract_for(f"请执行 {method} 方法并报告其诊断。"), sources)


def test_every_source_needs_an_explicit_traceable_disposition():
    sources = [{"id": "S001", "kind": "request", "text": "必须检查比较方向与误差估计。"},
               {"id": "S002", "kind": "request", "text": "必须输出诊断与所有验证用例。"}]
    with pytest.raises(CoverageError, match="missing_source_coverage"):
        validate_contract(contract_for("必须检查比较方向与误差估计。"), sources)


@pytest.mark.parametrize("problem", ["missing", "wrong_pointer", "wrong_section", "keyword_only"])
def test_coverage_must_point_to_real_method_content(problem):
    document = {"steps": [{"action": "实际计算 HC3 协方差并应用 Holm 校正。"}],
                "limitations": ["本流程包含 HC3 和 Holm。"],
                "coverage": [{"requirement_id": "R001", "locations": ["/steps/0/action"]}]}
    if problem == "missing":
        document["coverage"] = []
    elif problem == "wrong_pointer":
        document["coverage"][0]["locations"] = ["/steps/99/action"]
    elif problem == "wrong_section":
        document["coverage"][0]["locations"] = ["/limitations/0"]
    else:
        document["steps"][0]["action"] = "完成总体模型拟合并报告。"
    with pytest.raises(CoverageError):
        validate_coverage(contract_for(), document)


def pending_script(*, requirement_ids=None):
    return {"path": "scripts/model.py", "purpose": "Implement the declared scientific calculations.",
            "invocation": "python scripts/model.py --input INPUT --output OUTPUT",
            "requirement_ids": requirement_ids or ["R001"], "implementation_source_ids": []}


@pytest.mark.parametrize("pointer,code", [
    ("/goal", "invalid_coverage_location"),
    ("/applicability/0", "invalid_coverage_location"),
    ("/steps/99/action", "unknown_coverage_location"),
    ("/scripts/1/content", "unknown_coverage_location"),
    ("/scripts/0/source", "unknown_coverage_location"),
    ("/scripts/0/requirement_ids", "unknown_coverage_location"),
    ("/scripts/-1/content", "invalid_coverage_location"),
    ("/implementation/scripts/0", "invalid_coverage_location"),
])
def test_workflow_rejects_invalid_locations_even_with_a_pending_script(pointer, code):
    document = {"steps": [{"action": "计算 HC3 协方差并执行 Holm 校正。"}], "scripts": [],
                "coverage": [{"requirement_id": "R001", "locations": ["/steps/0/action", pointer]}]}
    with pytest.raises(CoverageError) as error:
        validate_coverage(contract_for(), document, pending_scripts=[pending_script()])
    assert error.value.code == code


@pytest.mark.parametrize("pointer", ["/scripts/0", "/scripts/0/content"])
def test_only_declared_pending_content_can_defer_anchors_and_final_validation_still_requires_them(pointer):
    document = {"steps": [{"action": "计算 HC3 协方差。"}], "scripts": [],
                "coverage": [{"requirement_id": "R001", "locations": ["/steps/0/action", pointer]}]}
    declared = pending_script()
    validate_coverage(contract_for(), document, pending_scripts=[declared])
    with pytest.raises(CoverageError, match="unknown_coverage_location"):
        validate_coverage(contract_for(), document)
    document["scripts"] = [{"path": declared["path"], "purpose": declared["purpose"],
                            "invocation": declared["invocation"], "content": "Generic model fit without correction."}]
    # Neither a completed helper nor another pending declaration can suppress
    # the final check. HC3 may be in the step, but Holm must exist somewhere too.
    with pytest.raises(CoverageError, match="method_identifiers_lost"):
        validate_coverage(contract_for(), document)
    with pytest.raises(CoverageError, match="method_identifiers_lost"):
        validate_coverage(contract_for(), document, pending_scripts=[declared])
    document["scripts"][0]["content"] = "Apply Holm correction to the precomputed covariance contrasts."
    validate_coverage(contract_for(), document)


@pytest.mark.parametrize("problem,code", [
    ("unrelated", "undeclared_script_requirement"),
    ("known_metadata", "method_identifiers_lost"),
    ("existing_step", "method_identifiers_lost"),
    ("wrong_section", "requirement_wrong_section"),
    ("unknown_requirement", "incomplete_requirement_coverage"),
    ("duplicate_requirement", "duplicate_requirement_coverage"),
    ("invalid_script_path", "invalid_pending_script_declaration"),
])
def test_pending_declaration_does_not_bypass_requirements_sections_or_available_content(problem, code):
    declared = pending_script()
    document = {"steps": [{"action": "Fit the model and return a general summary."}], "scripts": [],
                "limitations": ["HC3 and Holm are not implemented here."],
                "coverage": [{"requirement_id": "R001", "locations": ["/scripts/0/content"]}]}
    if problem == "unrelated":
        declared["requirement_ids"] = ["R002"]
    elif problem == "known_metadata":
        document["coverage"][0]["locations"] = ["/scripts/0/purpose"]
    elif problem == "existing_step":
        document["coverage"][0]["locations"] = ["/steps/0/action"]
    elif problem == "wrong_section":
        document["coverage"][0]["locations"] = ["/limitations/0"]
    elif problem == "unknown_requirement":
        document["coverage"][0]["requirement_id"] = "R002"
    elif problem == "duplicate_requirement":
        document["coverage"] *= 2
    else:
        declared["path"] = "../private.py"
    with pytest.raises(CoverageError) as error:
        validate_coverage(contract_for(), document, pending_scripts=[declared])
    assert error.value.code == code


def test_independent_review_rejects_changed_meaning_even_when_method_names_survive():
    review = MethodReview.model_validate({
        "sources": [{"source_id": "S001", "status": "preserved", "explanation": "原始方法的所有文字均已保留。"}],
        "requirements": [{"requirement_id": "R001", "status": "changed", "explanation": "比较方向被反转，不能判定满足原方法要求。"}],
        "implementation_appropriate": True, "implementation_reason": "脚本实现存在，但比较含义被改变。"})
    with pytest.raises(CoverageError, match="method_review_rejected"):
        validate_review(review, contract_for(), [{"id": "S001"}])


def source_record(source):
    encoded = source.encode()
    digest = hashlib.sha256(encoded).hexdigest()
    return {"schema": "program-method-evidence/v1", "operation_id": "private-operation",
        "script_path": "/home/ubuntu/output/prior.py", "event_seq": 42, "source_digest": digest,
        "source_snapshot": {"version": 1, "encoding": "utf-8", "size_bytes": len(encoded),
                            "sha256": digest, "content": source}}


def test_successful_source_is_method_reference_without_paths_rows_or_replay_identity():
    record = source_record('''import numpy as np
source = "/home/ubuntu/inputs/private.csv"
rows = [["private-patient", 123.42], ["other-patient", 456.78]]
reported_mean = 987.654
def covariance(x, residuals):
    leverage = np.diag(x @ np.linalg.pinv(x.T @ x) @ x.T)
    return (residuals / (1 - leverage)) ** 2
''')
    result = method_program_references([record], private_values=set(), private_pattern=_PRIVATE)
    text = json.dumps(result)
    assert result[0]["reference_only"] is True
    assert "np.linalg.pinv" in result[0]["source"]
    assert "CURRENT_TASK" in text
    assert all(value not in text for value in ["private.csv", "private-patient", "123.42", "987.654",
                                               "private-operation", record["source_digest"], "/home/ubuntu"])


@pytest.mark.parametrize("problem", ["public_result", "changed_hash", "changed_source"])
def test_public_or_changed_source_cannot_claim_successful_implementation_provenance(problem):
    record = source_record("import numpy as np\nx = np.arange(10)\n")
    if problem == "public_result":
        record.pop("schema")
        record["success"] = True
    elif problem == "changed_hash":
        record["source_digest"] = "0" * 64
    else:
        record["source_snapshot"]["content"] += "print('changed')\n"
    with pytest.raises(ValueError):
        method_program_references([record], private_values=set(), private_pattern=_PRIVATE)


def test_missing_method_feedback_locates_all_requirements_without_echoing_source_prose():
    contract = MethodContract.model_validate({"requirements": [
        {"id": "R001", "kind": "method", "statement": "按 HC3 协方差进行参数化估计；原始研究说明不回传。", "source_ids": ["S001"]},
        {"id": "R002", "kind": "method", "statement": "针对同一个比较家族执行 Holm 校正。", "source_ids": ["S002"]}]})
    document = {"steps": [{"action": "按研究目标拟合回归模型。"}, {"action": "计算预先声明的对比结果。"}],
        "coverage": [{"requirement_id": "R001", "locations": ["/steps/0/action"]},
                     {"requirement_id": "R002", "locations": ["/steps/1/action"]}]}
    with pytest.raises(CoverageError) as failure:
        validate_coverage(contract, document)
    assert failure.value.identifiers == ["R001", "R002"]
    assert failure.value.details == {"missing_anchors": [
        {"identifier": "R001", "anchors": ["hc3"], "locations": ["/steps/0/action"]},
        {"identifier": "R002", "anchors": ["holm"], "locations": ["/steps/1/action"]}]}
    assert "原始研究说明" not in json.dumps(failure.value.details, ensure_ascii=False)
    document["steps"][0]["action"] += " 使用 hc3 协方差。"
    document["steps"][1]["action"] += " 应用 holm 校正。"
    validate_coverage(contract, document)


def test_missing_extraction_feedback_preserves_scientific_identifiers_without_source_text():
    sources = [{"id": "S001", "kind": "request", "text": "按 HC3 和 Holm 方法执行完整比较。"},
               {"id": "S002", "kind": "method", "text": "还需要 Welch 分析并交付诊断。"}]
    candidate = contract_for("只有一般计算方法，没有相应科学算法。")
    candidate.requirements[0].source_ids = ["S001", "S002"]
    with pytest.raises(CoverageError) as failure:
        validate_contract(candidate, sources)
    assert failure.value.details == {"missing_anchors": [
        {"identifier": "S001", "anchors": ["hc3", "holm"]},
        {"identifier": "S002", "anchors": ["welch"]}]}
