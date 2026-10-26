"""Deterministic oracle/metric tests; these never call a model or Docker."""
from copy import deepcopy
import importlib.util
import json
import math
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).parent/"acceptance"
sys.path.insert(0, str(ROOT))
from analysis_code_cases import CASES, case_identity, compare_reports, evaluate, request_for
from analysis_code_real_model import program_metrics


def numeric_result():
    # Exact fixture uses orthogonal x,z,e with e'e=8 and z'z=8.
    return {"F": 45., "p": 1-2*math.atan(3)/math.pi-.64/math.pi,
            "df_num": 1, "df_den": 5, "n": 8}


def f_files(value=None):
    value = value or numeric_result()
    return {"result.json": json.dumps(value).encode(),
            "test.csv": (",".join(value)+"\n"+",".join(map(str, value.values()))+"\n").encode(),
            "report.md": ("```json\n"+json.dumps({"values": {"test": value}})+"\n```\n").encode()}


def test_independent_closed_form_f_oracle_accepts_complete_facts():
    outcome = evaluate("nested_f", f_files())
    assert outcome["passed"]
    assert outcome["unchecked"] == ["free_text_scientific_interpretation"]


@pytest.mark.parametrize("field,value", [("df_den", 1), ("df_num", 5), ("F", 4.5), ("p", .5), ("n", 7)])
def test_self_consistent_wrong_statistics_cannot_pass(field, value):
    wrong = {**numeric_result(), field: value}
    assert not evaluate("nested_f", f_files(wrong))["passed"]


def test_csv_and_report_are_checked_independently_of_json():
    files = f_files()
    files["test.csv"] = b"F,p,df_num,df_den,n\n45,.5,1,1,8\n"
    assert not evaluate("nested_f", files)["checks"]["csv_same_values"]
    files = f_files()
    files["report.md"] = b"The test is correct."
    assert not evaluate("nested_f", files)["checks"]["report_contains_result"]


def test_missing_middle_row_must_keep_original_identity_and_group_mapping():
    summary = {"original_n": 6, "retained_n": 5, "excluded_n": 1,
               "group_counts": {"A": 2, "B": 3}, "group_mean_residual": {"A": 1.5, "B": 2/3}}
    files = {"result.json": json.dumps(summary).encode(), "retained.csv":
        b"row_id,group,x,y,residual\n10,A,0,2,1\n11,B,1,2,-1\n13,B,3,9,2\n14,A,4,11,2\n15,B,5,12,1\n"}
    assert evaluate("alignment", files)["passed"]
    files["retained.csv"] = files["retained.csv"].replace(b"13,B,3,9,2", b"12,B,3,9,2")
    assert not evaluate("alignment", files)["passed"]


def test_first_request_includes_preparation_failure_and_repairs_require_source_change():
    records = [{"state": "not_started"}, {"state": "failed"}, {"state": "succeeded"}]
    feedback = [{"script_path": "x.py", "source_digest": "a", "returncode": 1},
                {"script_path": "probe.py", "source_digest": "p", "returncode": 0},
                {"script_path": "x.py", "source_digest": "b", "returncode": 0}]
    actual = program_metrics(records, feedback)
    assert actual["first_program_success"] is False
    assert actual["program_failures"] == 2 and actual["repair_runs"] == 1
    feedback[-1]["source_digest"] = "a"
    assert program_metrics(records, feedback)["repair_runs"] == 0


def report():
    return {"kind": "real_model_analysis_acceptance/v1", "completed": True,
        "source_sha256": "treatment-a", "comparison_identity": {"task": "fixed", "model": "fixed", "runtime": "fixed",
            "cases": [("alignment", "fixed")], "repeat": 1},
        "cases": [{"case": "alignment", "repeat": 1, "model_calls": 2, "model_seconds": 1.5,
                   "elapsed_seconds": 2, "program_failures": 0, "repair_runs": 0,
                   "first_program_success": True, "oracle": {"passed": True},
                   "execution_records_complete": True, "input_unchanged": True}]}


def test_truncated_record_window_cannot_invent_first_success():
    result = program_metrics([{"sequence": 66, "state": "succeeded"}], [], total_count=66)
    assert result["first_program_success"] is None and not result["execution_records_complete"]


def test_comparison_allows_treatment_source_change_but_rejects_confounding():
    baseline = report()
    candidate = deepcopy(baseline)
    candidate["source_sha256"] = "treatment-b"
    assert compare_reports(baseline, candidate)["delta_candidate_minus_baseline"]["model_calls"] == 0
    candidate["comparison_identity"]["model"] = "different"
    with pytest.raises(ValueError, match="identity differs"):
        compare_reports(baseline, candidate)


@pytest.mark.parametrize("mutation", ["incomplete", "harness_error", "zero_calls", "duplicate"])
def test_invalid_real_model_runs_are_not_performance_evidence(mutation):
    baseline, candidate = report(), report()
    if mutation == "incomplete": candidate["completed"] = False
    if mutation == "harness_error": candidate["cases"][0]["harness_error"] = "TimeoutError"
    if mutation == "zero_calls": candidate["cases"][0]["model_calls"] = 0
    if mutation == "duplicate": candidate["cases"].append(deepcopy(candidate["cases"][0]))
    with pytest.raises(ValueError): compare_reports(baseline, candidate)


def test_fixed_requests_do_not_embed_the_nested_f_answer_key():
    assert "45" not in request_for("nested_f")
    assert "vertical error bars" in request_for("delivery")
    assert case_identity("alignment") == case_identity("alignment")
    assert len({case_identity(case) for case in CASES}) == 3
