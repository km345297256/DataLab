"""Scientific names can be snake_case components, never arbitrary substrings."""
import pytest

from app.domain.services.skills.method_contract import (
    CoverageError, MethodReview, validate_contract, validate_coverage, validate_review,
)
from test_skill_method_prohibitions import contract, document


@pytest.mark.parametrize("method,content", [
    ("Hedges", "hedges_correction = 1 - 3 / (4 * degrees_of_freedom - 1)"),
    ("Hedges", "hedges_g = correction * standardized_difference"),
    ("Hedges", "summary['group_hedges_g'] = correction * standardized_difference"),
    ("HC3", "HC3_cov = robust_covariance(design, residuals)"),
    ("HC3", "cov_hc3 = robust_covariance(design, residuals)"),
    ("HC3", "compute_hc3_covariance(design, residuals)"),
    ("Holm", "def compute_holm_adjusted(probabilities):\n    return adjust(probabilities)"),
    ("Holm", "holm_adjusted = adjust(probabilities)"),
    ("OLS", "ols_coefficients = fit(design, observations)"),
    ("OLS", "fit_ols(design, observations)"),
])
def test_scientific_method_names_are_recognized_inside_snake_case_components(method, content):
    statement = f"保留 {method} 方法与原统计方向并实施完整计算。"
    validate_coverage(contract(statement), document(content))
    # Source-to-contract traceability uses the same spelling rule, while the
    # full source remains available for independent scientific review.
    validate_contract(contract(f"计算 {content} 并保留原科学含义。"),
        [{"id": "S001", "kind": "request", "text": statement}])


@pytest.mark.parametrize("method,unrelated", [
    ("OLS", "tools"), ("OLS", "_tools_"), ("OLS", "ols2"),
    ("HC3", "HC30_cov"), ("HC3", "myhc3_cov"), ("HC3", "cov_hc3x"),
    ("Holm", "holmium"), ("Holm", "compute_scholm_adjusted"),
    ("Hedges", "hedges2_g"), ("Hedges", "thehedges_g"),
])
def test_letters_and_digits_cannot_turn_unrelated_identifiers_into_method_coverage(method, unrelated):
    with pytest.raises(CoverageError, match="method_identifiers_lost") as failure:
        validate_coverage(contract(f"必须保留 {method} 科学方法并实际执行计算。"),
            document(f"{unrelated} = 0"))
    assert failure.value.details["missing_anchors"][0]["anchors"] == [method.casefold()]


def test_full_compound_source_identifiers_must_still_be_preserved():
    value = contract("必须保留 HC3_COV 和 TODO_SCORE 两个已定义科学变量。")
    with pytest.raises(CoverageError, match="method_identifiers_lost"):
        validate_coverage(value, document("hc3 = 1\ntodo = 2"))
    validate_coverage(value, document("result_hc3_cov = covariance\ntodo_score = quality"))


def test_complex_effect_size_requirement_does_not_need_added_method_name_comments():
    statement = (
        "组间效应量公式：SE=sqrt(va/na+vb/nb)；Welch df=(va/na+vb/nb)^2/((va/na)^2/(na-1)+(vb/nb)^2/(nb-1))；"
        "Welch t=差值/SE，仅作描述统计，不得伪造 t 分布 p 值；"
        "pooled_sd=sqrt(((na-1)va+(nb-1)vb)/(na+nb-2))；d=差值/pooled_sd；"
        "近似 Hedges 修正 J=1-3/(4*(na+nb-2)-1)；g=J*d。分母为零时对应统计量为 null，不删除两组。"
    )
    script = """se = sqrt(va / na + vb / nb)
welch_df = (va / na + vb / nb) ** 2 / ((va / na) ** 2 / (na - 1) + (vb / nb) ** 2 / (nb - 1))
welch_t = difference / se if se else None
pooled_sd = sqrt(((na - 1) * va + (nb - 1) * vb) / (na + nb - 2))
cohen_d = difference / pooled_sd if pooled_sd else None
hedges_correction = 1 - 3 / (4 * (na + nb - 2) - 1)
hedges_g = hedges_correction * cohen_d if cohen_d is not None else None
"""
    value = contract(statement)
    validate_coverage(value, document(script))
    without_effect_size = "\n".join(line for line in script.splitlines() if "hedges" not in line)
    with pytest.raises(CoverageError, match="method_identifiers_lost") as failure:
        validate_coverage(value, document(without_effect_size))
    assert failure.value.details["missing_anchors"][0]["anchors"] == ["hedges"]


def test_matching_method_name_does_not_override_independent_scientific_rejection():
    value = contract("必须使用 Hedges 修正计算效应量并保留正确分母。")
    validate_coverage(value, document("hedges_g = 1"))
    review = MethodReview.model_validate({
        "sources": [{"source_id": "S001", "status": "preserved", "explanation": "方法需求已经完整保留在合同中。"}],
        "requirements": [{"requirement_id": "R001", "status": "changed", "explanation": "实现给出常数，没有实际计算效应量或修正系数。"}],
        "implementation_appropriate": False, "implementation_reason": "仅变量命名相同，实际计算不符合科学方法。"})
    with pytest.raises(CoverageError, match="method_review_rejected"):
        validate_review(review, value, [{"id": "S001"}])
