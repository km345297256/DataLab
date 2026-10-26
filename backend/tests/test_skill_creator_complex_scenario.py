"""Deterministic scientific checks for the artificial long-script evidence."""
import copy
import math

import pytest

from skill_creator_complex_scenario import comprehensive_study, COMPLEX_INPUT, COMPLEX_EXPECTED


def assert_fixed_values(actual, expected):
    if isinstance(expected, dict):
        assert actual.keys() == expected.keys()
        for key in expected:
            assert_fixed_values(actual[key], expected[key])
    elif isinstance(expected, list):
        assert len(actual) == len(expected)
        for a, b in zip(actual, expected):
            assert_fixed_values(a, b)
    elif isinstance(expected, float):
        assert actual == pytest.approx(expected, rel=1e-12, abs=1e-12)
    else:
        assert actual == expected


def test_complete_report_matches_hand_derived_values_and_preserves_input():
    data = copy.deepcopy(COMPLEX_INPUT)
    assert_fixed_values(comprehensive_study(data), COMPLEX_EXPECTED)
    assert data == COMPLEX_INPUT


def test_every_field_role_can_change_without_changing_the_method():
    data = copy.deepcopy(COMPLEX_INPUT)
    roles = ("id", "group", "feature", "value", "unit", "before", "after", "x", "y", "family", "p")
    data["fields"] = {role: "mapped_" + role for role in roles}
    for table in ("observations", "pairs", "associations", "hypotheses", "long_rows"):
        data[table] = [{data["fields"][key]: value for key, value in row.items()} for row in data[table]]
    assert_fixed_values(comprehensive_study(data), COMPLEX_EXPECTED)


def test_unit_conversion_missing_zero_and_outliers_do_not_change_denominators():
    profile = comprehensive_study(COMPLEX_INPUT)["profiles"][0]
    assert (profile["rows"], profile["n"], profile["missing"], profile["zeros"]) == (6, 5, 1, 1)
    assert (profile["sum"], profile["mad_outliers"], profile["iqr_outliers"]) == (106, 1, 1)
    assert profile["sample_sd"] == pytest.approx(math.sqrt(1941.7))


def test_all_missing_and_zero_mad_profile_have_explicit_defined_behavior():
    data = copy.deepcopy(COMPLEX_INPUT)
    for row in data["observations"]:
        row["value"] = None
    empty = comprehensive_study(data)["profiles"][0]
    assert empty["n"] == 0 and empty["sum"] == 0 and empty["mean"] is None and empty["mad"] is None
    assert empty["mad_outliers"] == empty["iqr_outliers"] == 0
    for row in data["observations"]:
        row.update(value=0, unit="mg")
    zero = comprehensive_study(data)["profiles"][0]
    assert zero["mad"] == 0 and zero["mad_outliers"] == 0 and zero["sample_sd"] == 0


@pytest.mark.parametrize("table", ["observations", "pairs", "associations", "hypotheses", "long_rows"])
def test_duplicate_keys_are_rejected_in_each_independent_table(table):
    data = copy.deepcopy(COMPLEX_INPUT)
    data[table].append(copy.deepcopy(data[table][0]))
    with pytest.raises(ValueError, match="duplicate"):
        comprehensive_study(data)


@pytest.mark.parametrize("value", [True, "1", math.nan, math.inf])
def test_invalid_nonnull_values_are_not_hidden_by_missing_partner(value):
    data = copy.deepcopy(COMPLEX_INPUT)
    data["pairs"][-1].update(before=None, after=value)
    with pytest.raises(ValueError, match="finite non-boolean"):
        comprehensive_study(data)


def test_exact_mixed_sign_and_group_reversal_preserve_probability():
    data = copy.deepcopy(COMPLEX_INPUT)
    data["pairs"] = [{"id": str(i), "group": "A", "before": 0, "after": value}
                     for i, value in enumerate([2, 3, -1, 1])] + data["pairs"][2:4]
    result = comprehensive_study(data)
    assert result["paired"][0]["p_value"] == .375
    original = comprehensive_study(COMPLEX_INPUT)["comparison"]
    reversed_data = copy.deepcopy(COMPLEX_INPUT)
    reversed_data.update(group_a="B", group_b="A")
    reversed_result = comprehensive_study(reversed_data)["comparison"]
    assert reversed_result["difference_b_minus_a"] == -original["difference_b_minus_a"]
    assert reversed_result["hedges_g"] == -original["hedges_g"]
    assert reversed_result["p_value"] == original["p_value"] == 2 / 3


def test_association_uses_midranks_and_null_regression_edge_cases():
    result = comprehensive_study(COMPLEX_INPUT)["association"]
    assert result["spearman"] == pytest.approx(math.sqrt(.4))
    assert result["sse"] == pytest.approx(46 / 19)
    data = copy.deepcopy(COMPLEX_INPUT)
    data["associations"] = [{"id": "a", "x": 1, "y": 1}, {"id": "b", "x": 1, "y": 2}]
    constant = comprehensive_study(data)["association"]
    assert all(constant[key] is None for key in ("pearson", "spearman", "slope", "intercept", "sse", "r_squared", "slope_se"))
    data["associations"][1]["x"] = 2
    two = comprehensive_study(data)["association"]
    assert two["slope"] == 1 and two["residual_df"] == 0 and two["slope_se"] is None


def test_adjustments_keep_null_family_and_tie_conventions():
    data = copy.deepcopy(COMPLEX_INPUT)
    data["hypotheses"] = [{"id": "a", "family": "empty", "p": None},
                          {"id": "b", "family": "ties", "p": .05},
                          {"id": "c", "family": "ties", "p": .05}]
    result = comprehensive_study(data)["adjustments"]
    assert result[0]["family_size"] == 0 and result[0]["bh"] is None and result[0]["reject_bh"] is None
    assert all(row["bh"] == .05 and row["holm"] == .1 and row["reject_bh"] for row in result[1:])


def test_empty_long_table_keeps_header_and_zero_quality_counts():
    data = copy.deepcopy(COMPLEX_INPUT)
    data["long_rows"] = []
    result = comprehensive_study(data)["pivot"]
    assert result == {"columns": ["id"], "rows": [],
                      "quality": {"source_rows": 0, "output_rows": 0, "observed_missing": 0, "absent_cells": 0}}
