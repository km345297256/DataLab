"""Negative constraints survive extraction without demanding forbidden code."""
import pytest

from app.domain.services.skills.method_contract import (
    CoverageError, MethodContract, MethodReview, implementation_anchors, method_anchors,
    validate_contract, validate_coverage, validate_review,
)


def contract(statement):
    return MethodContract.model_validate({"requirements": [{"id": "R001", "kind": "method",
        "statement": statement, "source_ids": ["S001"]}]})


def document(content="def compute(rows):\n    return len(rows)"):
    return {"scripts": [{"content": content}], "coverage": [{"requirement_id": "R001", "locations": ["/scripts/0/content"]}]}


@pytest.mark.parametrize("statement", [
    "不得使用 TODO 占位符替代完整计算。", "禁止用 TODO 代替计算，必须完整实现。",
    "禁止 TODO 占位符以及虚构结果。", "严禁采用 TODO 占位实现原方法。",
    "不要使用 `TODO` 作为完成标记。", "不能添加 TODO 作为实现替代。",
    "不允许使用任何 TODO 占位符。", "Do not use TODO placeholders instead of computation.",
    "Don't use TODO placeholders instead of computation.", "Don’t use TODO placeholders instead of computation.",
    "must not include TODO placeholders in the implementation.", "Never use TODO placeholders instead of computation.",
])
def test_annotation_constraints_preserve_text_without_requiring_literal_markers(statement):
    assert "todo" not in method_anchors(statement)
    assert "todo" not in implementation_anchors(statement)
    value = contract(statement)
    source = [{"id": "S001", "kind": "request", "text": statement}]
    validate_contract(value, source)
    validate_coverage(value, document())
    assert value.requirements[0].statement == statement
    # A faithful paraphrase does not need to copy an editorial marker. The
    # full source and requirement remain inputs to independent method review.
    validate_contract(contract("完整实现原方法并保留全部验证，不得用占位符。"), source)


@pytest.mark.parametrize("statement", [
    "不得使用 ALPHA_STUB 或 BETA_STUB 代替实际计算。",
    "禁止使用 ALPHA_STUB、BETA_STUB 来伪造分析结果。",
    "Do not use ALPHA_STUB or BETA_STUB instead of computation.",
    "Don't use ALPHA_STUB and BETA_STUB instead of computation.",
    "Never use ALPHA_STUB/BETA_STUB instead of computation.",
])
def test_direct_prohibited_lists_are_general_syntax_not_a_todo_exception(statement):
    assert method_anchors(statement) >= {"alpha_stub", "beta_stub"}
    assert not implementation_anchors(statement)
    validate_coverage(contract(statement), document())


@pytest.mark.parametrize("statement", [
    "不要遗漏 HC3 协方差与 Holm 校正。",
    "不能不使用 HC3 协方差和 Holm 校正。",
    "不得不使用 HC3 协方差以及 Holm 校正。",
    "禁止不使用 HC3 协方差和 Holm 校正。",
    "并非禁止使用 HC3 协方差与 Holm 校正。",
    "不能禁止使用 HC3 协方差与 Holm 校正。",
    "不要只使用 HC3，还需要 Holm 校正。",
    "Don't omit HC3 covariance or Holm correction.",
    "do not fail to use HC3 covariance and Holm correction.",
    "do not only use HC3; also apply Holm correction.",
    "not only HC3 covariance but also Holm correction must be retained.",
    "不得使用 HC3 以外的协方差；必须保留 Holm 校正。",
])
def test_double_negatives_and_no_omission_instructions_keep_positive_method_anchors(statement):
    assert implementation_anchors(statement) >= {"hc3", "holm"}
    with pytest.raises(CoverageError, match="method_identifiers_lost") as failure:
        validate_coverage(contract(statement), document())
    assert {"hc3", "holm"} <= set(failure.value.details["missing_anchors"][0]["anchors"])
    validate_coverage(contract(statement), document("hc3 = compute_covariance(rows)\nholm = adjust_comparisons(hc3)"))


@pytest.mark.parametrize("statement", [
    "不得使用 HC3 处理前置汇总；最终估计必须使用 HC3，并保留 Holm 校正。",
    "Do not use HC3 for preliminary fits, but use HC3 and Holm for final comparisons.",
])
def test_one_prohibited_occurrence_does_not_erase_positive_occurrences_of_same_name(statement):
    expected = "hc3"
    assert expected in implementation_anchors(statement)
    with pytest.raises(CoverageError, match="method_identifiers_lost") as failure:
        validate_coverage(contract(statement), document())
    assert expected in failure.value.details["missing_anchors"][0]["anchors"]


def test_prohibited_anchor_does_not_swallow_neighboring_positive_clause_or_feedback():
    value = contract("不得使用 TODO，占位符必须移除；使用 HC3 协方差并执行 Holm 校正。")
    with pytest.raises(CoverageError) as failure:
        validate_coverage(value, document())
    assert failure.value.details == {"missing_anchors": [{"identifier": "R001", "anchors": ["hc3", "holm"], "locations": ["/scripts/0/content"]}]}
    validate_coverage(value, document("hc3 = covariance(rows)\nholm = correct(hc3)"))


@pytest.mark.parametrize("content", [
    "hc3 = covariance(rows)\nholm = correction(hc3)",
    "# Current computation\nhc3 = covariance(rows)\n\nholm = correction(hc3)\n",
    "# Current computation\r\nhc3 = covariance(rows)\r\nholm = correction(hc3)",
    "hc3 = covariance(rows)\nlabel = '研究方法'\nholm = correction(hc3)",
])
def test_multiline_script_method_names_at_column_zero_are_not_lost_to_json_escaping(content):
    validate_coverage(contract("使用 HC3 协方差和 Holm 校正，保留计算结果。"), document(content))


def test_nested_pointer_values_use_real_string_leaves_without_requiring_serialized_keys():
    value = {"steps": [{"action": {"computations": ["hc3 = covariance(rows)", {"code": "\nholm = correction(hc3)"}]}}],
             "coverage": [{"requirement_id": "R001", "locations": ["/steps/0/action"]}]}
    validate_coverage(contract("使用 HC3 协方差与 Holm 校正，完整保留方法。"), value)


def test_structural_dictionary_keys_and_cross_leaf_fragments_are_not_method_implementation():
    value = {"steps": [{"action": {"Holm": ["Hol", "m"], "HC3": "generic model fit"}}],
             "coverage": [{"requirement_id": "R001", "locations": ["/steps/0/action"]}]}
    with pytest.raises(CoverageError, match="method_identifiers_lost") as failure:
        validate_coverage(contract("使用 HC3 协方差与 Holm 校正，完整保留方法。"), value)
    assert failure.value.details["missing_anchors"][0]["anchors"] == ["hc3", "holm"]


def test_negative_constraint_still_needs_coverage_and_independent_semantic_acceptance():
    value = contract("不得使用 TODO 占位符代替真实计算。")
    with pytest.raises(CoverageError, match="incomplete_requirement_coverage"):
        validate_coverage(value, {"scripts": [{"content": "return 1"}], "coverage": []})
    review = MethodReview.model_validate({
        "sources": [{"source_id": "S001", "status": "preserved", "explanation": "原始约束文字仍在合同中完整保留。"}],
        "requirements": [{"requirement_id": "R001", "status": "changed", "explanation": "实际实现使用了被禁止的占位计算，未满足该约束。"}],
        "implementation_appropriate": False, "implementation_reason": "实现没有遵守必须执行真实计算的约束。"})
    with pytest.raises(CoverageError, match="method_review_rejected"):
        validate_review(review, value, [{"id": "S001"}])


COMPLEX_SCRIPT_CONSTRAINT = (
    "交付物必须是单个可独立运行的完整 Python 脚本，仅使用标准库，不得拆分为多个脚本汇总，也不得删减方法；"
    "脚本须保留六个可独立理解和测试的计算函数（质量画像、配对、组间比较、相关与回归、多重检验、长宽转换）"
    "以及统一 CLI，不得用代码注释、无用分支、TODO、占位或文字描述代替实际实现。"
)


def test_actual_complex_constraint_keeps_python_cli_without_demanding_todo():
    assert method_anchors(COMPLEX_SCRIPT_CONSTRAINT) == {"python", "cli"}
    assert implementation_anchors(COMPLEX_SCRIPT_CONSTRAINT) == {"python", "cli"}
    value = contract(COMPLEX_SCRIPT_CONSTRAINT)
    value.requirements[0].kind = "constraint"
    source = [{"id": "S001", "kind": "request", "text": COMPLEX_SCRIPT_CONSTRAINT}]
    validate_contract(value, source)
    complete = {"steps": [{"action": "Use a single Python CLI with all six complete computations and no placeholder substitutes."}],
                "coverage": [{"requirement_id": "R001", "locations": ["/steps/0/action"]}]}
    validate_coverage(value, complete)
    assert value.requirements[0].statement == COMPLEX_SCRIPT_CONSTRAINT
    for missing in ("Python", "CLI"):
        incomplete = {**complete, "steps": [{"action": complete["steps"][0]["action"].replace(missing, "interface")}]}
        with pytest.raises(CoverageError, match="method_identifiers_lost") as failure:
            validate_coverage(value, incomplete)
        assert failure.value.details["missing_anchors"][0]["anchors"] == [missing.casefold()]


@pytest.mark.parametrize("marker", ["TODO", "FIXME", "Todo", "Fixme"])
@pytest.mark.parametrize("wording", [
    "不得用代码注释、无用分支、{marker}、占位或文字描述代替实际实现。保留 HC3、Holm、Python 和 CLI。",
    "Complete the HC3 and Holm methods in Python with a CLI; never substitute comments, empty branches or {marker} placeholders.",
    "检查注释、占位字段以及 {marker} 并报告；不得遗漏 HC3、Holm、Python、CLI。",
])
def test_annotation_marker_classification_does_not_depend_on_negation_grammar(marker, wording):
    statement = wording.format(marker=marker)
    assert method_anchors(statement) >= {"hc3", "holm", "python", "cli"}
    assert implementation_anchors(statement) >= {"hc3", "holm", "python", "cli"}
    assert marker.casefold() not in method_anchors(statement)
    assert marker.casefold() not in implementation_anchors(statement)
    with pytest.raises(CoverageError, match="method_identifiers_lost") as failure:
        validate_coverage(contract(statement), document())
    assert {"hc3", "holm", "python", "cli"} <= set(failure.value.details["missing_anchors"][0]["anchors"])
    assert marker.casefold() not in failure.value.details["missing_anchors"][0]["anchors"]


def test_only_whole_annotation_markers_are_excluded_not_compound_identifiers():
    statement = "保留 TODO_SCORE、FIXME_RATE、TODO2 与 HC3 的科学变量和方法。"
    expected = {"todo_score", "fixme_rate", "todo2", "hc3"}
    assert method_anchors(statement) == expected
    assert implementation_anchors(statement) == expected
    with pytest.raises(CoverageError, match="method_identifiers_lost") as failure:
        validate_coverage(contract(statement), document())
    assert set(failure.value.details["missing_anchors"][0]["anchors"]) == expected


def test_prohibited_scientific_name_remains_mandatory_in_source_contract():
    source = [{"id": "S001", "kind": "request", "text": "不得使用 HC3 协方差，保留 Holm 校正。"}]
    # Scientific names still need preservation even when explicitly prohibited;
    # their prohibition can change the method and may not disappear silently.
    with pytest.raises(CoverageError, match="method_identifiers_lost") as failure:
        validate_contract(contract("保留 Holm 校正并使用正确协方差。"), source)
    assert failure.value.details["missing_anchors"][0]["anchors"] == ["hc3"]
    assert implementation_anchors(source[0]["text"]) == {"holm"}
