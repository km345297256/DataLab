"""Validate independent artificial evidence before real-model acceptance uses it."""
import copy
import math

import pytest

from skill_creator_scenarios import (
    SCENARIOS, PAIRED_MIXED_SIGN_INPUT, PAIRED_MIXED_SIGN_EXPECTED,
    aggregate_tables, paired_randomization, pivot_measurements,
)


@pytest.mark.parametrize("name", SCENARIOS)
def test_reference_method_reproduces_independently_authored_small_example(name):
    scenario = SCENARIOS[name]
    original = copy.deepcopy(scenario.normal)
    if name == "complex":
        from test_skill_creator_complex_scenario import assert_fixed_values
        assert_fixed_values(scenario.reference(scenario.normal), scenario.observed)
    else:
        assert scenario.reference(scenario.normal) == scenario.observed
    assert scenario.normal == original


def test_join_preserves_zero_all_missing_and_unmeasured_groups_with_custom_fields():
    result = aggregate_tables({"fields": {"sample": "id", "group": "cohort", "value": "reading"},
        "samples": [{"id": "s1", "cohort": "A"}, {"id": "s2", "cohort": "B"}, {"id": "s3", "cohort": "C"}],
        "measurements": [{"id": "s1", "reading": 0}, {"id": "s2", "reading": None}]})
    assert result["groups"] == [{"group": "A", "count": 1, "sum": 0, "mean": 0},
                                {"group": "B", "count": 0, "sum": 0, "mean": None},
                                {"group": "C", "count": 0, "sum": 0, "mean": None}]
    assert result["quality"] == {"rows": 2, "valid": 1, "missing": 1}
    assert aggregate_tables({"samples": [], "measurements": []}) == {
        "groups": [], "quality": {"rows": 0, "valid": 0, "missing": 0}}


@pytest.mark.parametrize("measurement", [True, "3.0", float("nan"), float("inf")])
def test_join_refuses_non_numeric_and_nonfinite_measurements(measurement):
    with pytest.raises(ValueError, match="invalid measurement"):
        aggregate_tables({"samples": [{"sample_id": "a", "group": "A"}],
                          "measurements": [{"sample_id": "a", "value": measurement}]})


def test_join_rejects_duplicate_and_unmatched_keys_instead_of_changing_denominator():
    with pytest.raises(ValueError, match="duplicate"):
        aggregate_tables({"samples": [{"sample_id": "a", "group": "A"}] * 2, "measurements": []})
    with pytest.raises(ValueError, match="unknown sample"):
        aggregate_tables({"samples": [], "measurements": [{"sample_id": "a", "value": 2}]})


def test_paired_direction_sample_denominator_and_exact_tail_are_preserved():
    result = paired_randomization({"fields": {"id": "subject", "before": "baseline", "after": "followup"},
        "pairs": [{"subject": "a", "baseline": 8, "followup": 7},
                  {"subject": "b", "baseline": 6, "followup": 3}]})
    assert result["mean_change"] == -2
    assert result["sample_sd"] == pytest.approx(math.sqrt(2))
    assert result["standard_error"] == pytest.approx(1)
    assert result["p_value"] == .5
    assert result["permutations"] == 4
    assert result["direction"] == "after-minus-before"


def test_paired_zero_difference_is_valid_and_missing_excludes_whole_pair():
    result = paired_randomization({"pairs": [{"pair_id": "a", "before": 0, "after": 0},
        {"pair_id": "b", "before": 3, "after": 3}, {"pair_id": "c", "before": 2, "after": None}]})
    assert result == {"n": 2, "excluded": 1, "mean_change": 0, "sample_sd": 0,
                      "standard_error": 0, "p_value": 1, "permutations": 4, "direction": "after-minus-before"}


def test_paired_mixed_sign_oracle_counts_all_six_extreme_permutations():
    result = paired_randomization(PAIRED_MIXED_SIGN_INPUT)
    assert result == PAIRED_MIXED_SIGN_EXPECTED
    assert result["p_value"] == 6 / 16
    assert result["mean_change"] == 5 / 4
    assert result["sample_sd"] == pytest.approx(math.sqrt(35 / 12))


def test_paired_fixture_states_excluded_count_type_without_changing_hand_oracle():
    scenario = SCENARIOS["paired"]
    assert "excluded为剔除配对数量的非负整数" in scenario.request
    assert "不是标识列表" in scenario.request
    assert type(scenario.observed["excluded"]) is int and scenario.observed["excluded"] == 1
    assert scenario.reference(scenario.normal)["excluded"] == scenario.observed["excluded"]


@pytest.mark.parametrize("count", [0, 1, 13])
def test_paired_requires_exact_test_supported_complete_pair_count(count):
    with pytest.raises(ValueError, match="2 through 12"):
        paired_randomization({"pairs": [{"pair_id": str(i), "before": 0, "after": i} for i in range(count)]})


def test_paired_rejects_duplicate_ids_and_invalid_values_even_in_incomplete_pairs():
    with pytest.raises(ValueError, match="duplicate"):
        paired_randomization({"pairs": [{"pair_id": "a", "before": 0, "after": 1}] * 2})
    with pytest.raises(ValueError, match="invalid paired"):
        paired_randomization({"pairs": [{"pair_id": "a", "before": None, "after": True}]})


def test_pivot_keeps_observed_missing_separate_from_absent_cells_and_sorts():
    result = pivot_measurements({"fields": {"row": "specimen", "column": "test", "value": "reading"},
        "rows": [{"specimen": "b", "test": "z", "reading": None},
                 {"specimen": "a", "test": "x", "reading": 0},
                 {"specimen": "b", "test": "x", "reading": 2.5}], "scale": 1000})
    assert result["columns"] == ["sample_id", "x", "z"]
    assert result["rows"] == [{"sample_id": "a", "x": 0, "z": None}, {"sample_id": "b", "x": 2500, "z": None}]
    assert result["quality"] == {"source_rows": 3, "output_rows": 2, "observed_missing": 1, "absent_cells": 1}
    assert pivot_measurements({"rows": []}) == {"columns": ["sample_id"], "rows": [],
        "quality": {"source_rows": 0, "output_rows": 0, "observed_missing": 0, "absent_cells": 0}}


@pytest.mark.parametrize("scale", [0, -1, True, "1000", float("nan"), float("inf")])
def test_pivot_rejects_invalid_scaling(scale):
    with pytest.raises(ValueError, match="scale"):
        pivot_measurements({"rows": [], "scale": scale})


def test_pivot_refuses_ambiguous_duplicates_and_reserved_output_identifier():
    with pytest.raises(ValueError, match="duplicate"):
        pivot_measurements({"rows": [{"sample_id": "a", "analyte": "x", "value": 0}] * 2})
    with pytest.raises(ValueError, match="invalid identifier"):
        pivot_measurements({"rows": [{"sample_id": "a", "analyte": "sample_id", "value": 2}]})
