"""Fixed artificial analysis tasks and independent, deliberately bounded oracles.

No fixture is copied from user data. Expected values are authored independently
of generated programs and of the optional DataLab calculation helpers. The PNG
check establishes a real image file, not pixel-level scientific correctness.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import struct


VERSION = "analysis-code-cases/v1"
INPUT_ROOT = "/home/ubuntu/inputs/regression"
OUTPUT_ROOT = "/home/ubuntu/output"

CASES = {
    "alignment": {
        "csv": "row_id,group,x,y\n10,A,0,2\n11,B,1,2\n12,A,2,\n13,B,3,9\n14,A,4,11\n15,B,5,12\n",
        "request": """Read the selected observations.csv. Preserve original row_id values. Exclude only rows with missing y. For every retained row compute residual=y-(2*x+1), then summarize residuals by group. Save retained.csv with columns row_id,group,x,y,residual in original row order and result.json with original_n,retained_n,excluded_n,group_counts,group_mean_residual. Do not reset and then align a new index to the filtered rows. Validate row conservation and alignment. Do not create a chart or fit a model for this task.""",
        "outputs": ("result.json", "retained.csv"),
    },
    "nested_f": {
        "csv": "row_id,x,z,y\n10,-3,-1,-3\n11,-1,-1,-1\n13,1,-1,3\n14,3,-1,9\n20,-3,1,3\n21,-1,1,5\n23,1,1,9\n24,3,1,15\n",
        "request": """Read observations.csv and fit ordinary least squares full model y~1+x+z and restricted model y~1+x on the same observations. Use the classical nested-model F test (homoscedastic OLS), without changing the requested method. Save result.json containing F,p,df_num,df_den,n; df_num means the difference in model ranks and df_den is the full model residual degrees of freedom. Save test.csv with exactly one row and the same named columns and values. Save report.md that states the actual F statistic, p value, both degrees of freedom and sample size from that result. Validate that the response and sample identities agree. Do not generate charts for this task.""",
        "outputs": ("result.json", "test.csv", "report.md"),
    },
    "delivery": {
        "csv": "row_id,x,y,lower,upper\n10,0,2,1.5,2.5\n20,1,4,3.5,4.5\n30,2,6,5.5,6.5\n",
        "request": """Read observations.csv. Save result.json containing rows (the original records) and stats={n:3,mean_y:the computed mean,bounds_kind:'supplied'}. Save plotted.csv with the original columns and values. Plot a line chart of y against x with vertical error bars at the supplied lower/upper bounds as chart.png. These are supplied bounds, not an estimated confidence interval or standard error. Save report.md giving n, mean_y and explaining that the line uses the supplied lower/upper bounds; do not call it a 95% confidence interval. Use the same computed records for JSON, CSV, plot and description and check the actual written artifacts.""",
        "outputs": ("result.json", "plotted.csv", "chart.png", "report.md"),
    },
}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()


def case_identity(name):
    return hashlib.sha256(canonical({"version": VERSION, "case": name, **CASES[name]})).hexdigest()


def request_for(name):
    return (CASES[name]["request"] + "\nThe only selected input is " + INPUT_ROOT
            + "/observations.csv (read-only). Save all requested outputs under " + OUTPUT_ROOT
            + ". Save any Python program in that writable directory and execute it using program_run. "
            "This is an artificial acceptance task; do not query databases, other sessions, or external datasets. "
            "Do not print whole datasets. Report the files actually produced.")


def _number(value, expected, *, tolerance=1e-9):
    if isinstance(value, bool):
        return False
    try:
        number = float(value)
        return math.isfinite(number) and math.isclose(number, expected, rel_tol=tolerance, abs_tol=tolerance)
    except (ValueError, TypeError, OverflowError):
        return False


def _records(blob):
    return list(csv.DictReader(io.StringIO(blob.decode("utf-8"))))


def evaluate(name, files):
    """Report independent check outcomes without returning generated content."""
    checks = {"requested_files_present": all(files.get(p) for p in CASES[name]["outputs"])}
    unchecked = []
    try:
        result = json.loads(files["result.json"])
        if name == "alignment":
            checks["row_conservation"] = all(_number(result.get(k), v) for k, v in {
                "original_n": 6, "retained_n": 5, "excluded_n": 1}.items())
            checks["group_counts"] = result.get("group_counts") == {"A": 2, "B": 3}
            means = result.get("group_mean_residual", {})
            checks["aligned_group_values"] = _number(means.get("A"), 1.5) and _number(means.get("B"), 2/3)
            rows = _records(files["retained.csv"])
            expected = [(10, "A", 0, 2, 1), (11, "B", 1, 2, -1), (13, "B", 3, 9, 2),
                        (14, "A", 4, 11, 2), (15, "B", 5, 12, 1)]
            checks["retained_csv_values"] = len(rows) == len(expected) and all(
                set(row) == {"row_id", "group", "x", "y", "residual"}
                and row["group"] == wanted[1]
                and all(_number(row[k], wanted[i]) for k, i in (("row_id", 0), ("x", 2), ("y", 3), ("residual", 4)))
                for row, wanted in zip(rows, expected))
        elif name == "nested_f":
            # Orthogonal columns give SSE_full=8, SSE_restricted=80,
            # ranks 3 and 2. Two-tailed t_5 at sqrt(45) has this closed form.
            expected = {"F": 45., "p": 1-2*math.atan(3)/math.pi-.64/math.pi,
                        "df_num": 1., "df_den": 5., "n": 8.}
            checks["independent_F_and_degrees"] = all(_number(result.get(k), v) for k, v in expected.items())
            rows = _records(files["test.csv"])
            checks["csv_same_values"] = len(rows) == 1 and set(rows[0]) == set(expected) and all(
                _number(rows[0][k], v) for k, v in expected.items())
            prose = files["report.md"].decode("utf-8")
            # Limited factual check: the structured statistical record, rather
            # than a second prose inference, must appear as a JSON code block.
            checks["report_contains_result"] = _contains_json_record(prose, expected)
            unchecked = ["free_text_scientific_interpretation"]
        else:
            expected = _records(CASES[name]["csv"].encode())
            rows, actual = result.get("rows"), _records(files["plotted.csv"])
            def matches(items):
                return isinstance(items, list) and len(items) == len(expected) and all(
                    set(a) == set(b) and all(_number(a[k], float(b[k])) for k in b)
                    for a, b in zip(items, expected))
            checks["json_and_csv_values"] = matches(rows) and matches(actual)
            stats = result.get("stats", {})
            checks["summary_values"] = _number(stats.get("n"), 3) and _number(stats.get("mean_y"), 4)
            image = files["chart.png"]
            checks["actual_png"] = (len(image) > 100 and image[:8] == b"\x89PNG\r\n\x1a\n"
                and image[12:16] == b"IHDR" and min(struct.unpack(">II", image[16:24])) >= 100)
            prose = files["report.md"].decode("utf-8")
            checks["report_contains_result"] = _contains_json_record(prose, {"n": 3., "mean_y": 4.})
            checks["structured_bounds_kind"] = stats.get("bounds_kind") == "supplied"
            unchecked = ["chart_pixel_semantics", "free_text_scientific_interpretation"]
    except (KeyError, ValueError, TypeError, AttributeError, UnicodeError, struct.error):
        checks["parseable_outputs"] = False
    return {"scope": "authored_numeric_and_artifact_oracles/v1", "checks": checks,
            "passed": bool(checks) and all(checks.values()), "unchecked": unchecked}


def _contains_json_record(prose, expected):
    """Find factual numbers in an explicit structured block, not fuzzy prose."""
    import re
    for match in re.finditer(r"```json\s*(.*?)\s*```", prose, flags=re.S):
        try:
            value = json.loads(match[1])
        except ValueError:
            continue
        def dictionaries(item, depth=0):
            if depth > 8:
                return
            if isinstance(item, dict):
                yield item
                for child in item.values():
                    yield from dictionaries(child, depth+1)
            elif isinstance(item, list):
                for child in item[:100]:
                    yield from dictionaries(child, depth+1)
        candidates = dictionaries(value)
        if any(isinstance(item, dict) and all(_number(item.get(k), v) for k, v in expected.items()) for item in candidates):
            return True
    return False


# The report contract is explicit to both variants and contains no answer key.
for _case in ("nested_f", "delivery"):
    CASES[_case]["request"] += " Include the numeric result object (or stats object) in a fenced JSON block inside the report, so its factual values can be checked independently."


def summarize(cases):
    count = len(cases)
    return {"case_count": count,
            "first_program_success_rate": sum(item.get("first_program_success") is True for item in cases)/count if count else None,
            "oracle_pass_rate": sum(item.get("oracle", {}).get("passed") is True for item in cases)/count if count else None,
            "program_failures": sum(item.get("program_failures", 0) for item in cases),
            "repair_runs": sum(item.get("repair_runs", 0) for item in cases),
            "model_calls": sum(item.get("model_calls", 0) for item in cases),
            "model_seconds": sum(item.get("model_seconds", 0) for item in cases),
            "elapsed_seconds": sum(item.get("elapsed_seconds", 0) for item in cases)}


def compare_reports(baseline, candidate):
    """Only the explicitly varied application/helper source may differ."""
    for report in (baseline, candidate):
        if report.get("kind") != "real_model_analysis_acceptance/v1" or not report.get("completed"):
            raise ValueError("Comparison requires complete real-model acceptance reports")
        cases = report.get("cases", [])
        if not cases or len({(item["case"], item["repeat"]) for item in cases}) != len(cases):
            raise ValueError("Comparison requires unique case/repetition observations")
        if any(item.get("model_calls", 0) < 1 or item.get("harness_error")
               or item.get("execution_records_complete") is not True
               or item.get("input_unchanged") is not True for item in cases):
            raise ValueError("Comparison cannot use missing model calls or harness failures")
        binding = report.get("comparison_identity", {})
        expected = {(name, repeat) for name, _digest in binding.get("cases", [])
                    for repeat in range(1, binding.get("repeat", 0)+1)}
        if not expected or {(item["case"], item["repeat"]) for item in cases} != expected:
            raise ValueError("Observed cases do not match the complete fixed task matrix")
    if baseline.get("comparison_identity") != candidate.get("comparison_identity"):
        raise ValueError("Task, fixture, harness, model or scientific runtime identity differs")
    b, c = summarize(baseline["cases"]), summarize(candidate["cases"])
    return {"kind": "analysis_acceptance_comparison/v1", "baseline": b, "candidate": c,
            "delta_candidate_minus_baseline": {key: c[key]-b[key] for key in b},
            "limits": ["small artificial task sample, not a population error-rate estimate",
                       "reported oracle scope is not general scientific or chart-pixel verification"]}
