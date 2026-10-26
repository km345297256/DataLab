"""Actual output checks; input success flags and PNG existence are insufficient."""
import csv
import json
from pathlib import Path

import pytest

from scripts import datalab_results as module
from scripts.datalab_results import ResultBundle, ResultConsistencyError


def fixture():
    return {"test": {"method": "nested_ols_f_test", "statistic": 7.5,
                     "df_num": 4, "df_den": 107, "pvalue": 0.00002},
            "rows": [{"x": 1, "estimate": 3.5, "lower": 2.5, "upper": 4.5, "group": "a"},
                     {"x": 2, "estimate": 6.0, "lower": 5.0, "upper": 7.0, "group": "a"},
                     {"x": 1, "estimate": 2.0, "lower": 1.0, "upper": 3.0, "group": "b"}]}


def test_mutating_original_or_returned_copy_cannot_change_frozen_results(tmp_path):
    source = fixture()
    bundle = ResultBundle(source)
    original_version = bundle.version
    source["test"]["df_den"] = 4
    returned = bundle.snapshot()
    returned["rows"][0]["estimate"] = 100
    bundle.write_json(tmp_path / "result.json")
    data = json.loads((tmp_path / "result.json").read_text())
    assert data["test"]["df_den"] == 107
    assert data["rows"][0]["estimate"] == 3.5
    assert bundle.version == original_version
    assert bundle.verify()["result_version"] == original_version


def test_json_object_insertion_order_has_one_result_version():
    assert ResultBundle({"x": 1, "y": 2}).version == ResultBundle({"y": 2, "x": 1}).version
    assert ResultBundle({"x": 1}).version != ResultBundle({"x": "1"}).version


@pytest.mark.parametrize("data", [
    {}, [], {1: "ambiguous-key"}, {"value": float("nan")},
    {"value": float("inf")}, {"nested": {"value": object()}}, {"value": (1, 2)},
])
def test_ambiguous_or_nonfinite_result_values_are_rejected(data):
    with pytest.raises(ResultConsistencyError):
        ResultBundle(data)


def test_csv_preserves_actual_columns_rows_precision_and_quoted_cells(tmp_path):
    rows = [{"row_id": "001", "value": 0.12345678901234566, "label": 'a,"quoted"\nline'},
            {"row_id": "003", "value": None, "label": "NA"}]
    bundle = ResultBundle({"rows": rows})
    path = tmp_path / "table.csv"
    bundle.write_csv(path, table="rows", columns=["row_id", "value", "label"])
    with path.open(newline="") as stream:
        actual = list(csv.reader(stream))
    assert actual == [["row_id", "value", "label"],
                      ["001", "0.12345678901234566", 'a,"quoted"\nline'],
                      ["003", "null", "NA"]]
    assert bundle.verify()["checks"][0]["format"] == "csv"


@pytest.mark.parametrize("columns", [["x"], ["x", "estimate", "hc3"],
                                      ["x", "x", "lower", "upper", "group"]])
def test_missing_extra_or_duplicate_required_csv_columns_are_rejected(tmp_path, columns):
    with pytest.raises(ResultConsistencyError, match="every named table column"):
        ResultBundle(fixture()).write_csv(tmp_path / "table.csv", table="rows", columns=columns)
    assert not (tmp_path / "table.csv").exists()


@pytest.mark.parametrize("rows", [[], [{"a": 1}, {"b": 2}], [{"a": [1]}]])
def test_empty_or_inconsistent_tables_do_not_silently_drop_values(tmp_path, rows):
    with pytest.raises(ResultConsistencyError):
        ResultBundle({"rows": rows}).write_csv(tmp_path / "table.csv", table="rows")


def test_write_csv_reads_back_actual_export_and_detects_dropped_row(tmp_path, monkeypatch):
    original = module._atomic_write
    def incomplete(path, content):
        original(path, b"x,y\n1,2\n")
    monkeypatch.setattr(module, "_atomic_write", incomplete)
    bundle = ResultBundle({"rows": [{"x": 1, "y": 2}, {"x": 3, "y": 4}]})
    with pytest.raises(ResultConsistencyError, match="CSV columns, row order or cell values"):
        bundle.write_csv(tmp_path / "table.csv", table="rows")


@pytest.mark.parametrize("kind", ["line", "scatter"])
def test_actual_grouped_plot_uses_all_rows_and_supplied_bounds(tmp_path, kind):
    pytest.importorskip("matplotlib")
    import matplotlib.image
    bundle = ResultBundle(fixture())
    path = tmp_path / "plot.png"
    receipt = bundle.plot(path, table="rows", x="x", y="estimate", kind=kind,
                          group="group", lower="lower", upper="upper")
    pixels = matplotlib.image.imread(path)
    assert pixels.ndim == 3 and pixels.shape[0] > 100 and pixels.shape[1] > 100
    assert receipt["plot"]["series"] == 2
    assert receipt["plot"]["points"] == 3
    assert receipt["plot"]["uncertainty"] == "supplied_bounds_not_statistically_verified"
    assert bundle.verify()["checks"][0]["scope"] == "rendered_data_and_unchanged_bytes"


def test_plot_without_bounds_does_not_invent_uncertainty(tmp_path):
    pytest.importorskip("matplotlib")
    receipt = ResultBundle(fixture()).plot(tmp_path / "plot.png", table="rows", x="x", y="estimate")
    assert receipt["plot"]["uncertainty"] == "none"


@pytest.mark.parametrize("value", [2**53 + 1, -(2**53 + 1), 10**400])
def test_large_json_integers_cannot_silently_collapse_or_overflow_in_plots(tmp_path, value):
    pytest.importorskip("matplotlib")
    bundle = ResultBundle({"rows": [{"x": 2**53, "y": 1}, {"x": value, "y": 2}]})
    with pytest.raises(ResultConsistencyError, match="exactly representable"):
        bundle.plot(tmp_path / "plot.png", table="rows", x="x", y="y")
    assert not (tmp_path / "plot.png").exists()


def test_single_observation_line_group_has_an_actual_visible_marker(tmp_path, monkeypatch):
    pytest.importorskip("matplotlib")
    from matplotlib.axes import Axes
    original = Axes.plot
    markers = []
    def observe(self, xs, ys, *args, **kwargs):
        artists = original(self, xs, ys, *args, **kwargs)
        if len(xs) == 1:
            markers.append((artists[0].get_marker(), artists[0].get_markersize(), artists[0].get_visible()))
        return artists
    monkeypatch.setattr(Axes, "plot", observe)
    bundle = ResultBundle(fixture())
    bundle.plot(tmp_path / "plot.png", table="rows", x="x", y="estimate", group="group")
    assert len(markers) == 1 and markers[0][0] == "o" and markers[0][1] > 0 and markers[0][2]
    bundle.verify()


def test_single_observation_line_without_marker_cannot_pass_geometry_check(tmp_path, monkeypatch):
    pytest.importorskip("matplotlib")
    from matplotlib.axes import Axes
    original = Axes.plot
    def invisible(self, xs, ys, *args, **kwargs):
        artists = original(self, xs, ys, *args, **kwargs)
        if len(xs) == 1:
            artists[0].set_marker("None")
        return artists
    monkeypatch.setattr(Axes, "plot", invisible)
    with pytest.raises(ResultConsistencyError, match="no visible geometry"):
        ResultBundle(fixture()).plot(tmp_path / "plot.png", table="rows", x="x", y="estimate", group="group")


def test_rendered_artist_coordinate_mismatch_is_detected_before_export(tmp_path, monkeypatch):
    pytest.importorskip("matplotlib")
    from matplotlib.axes import Axes
    original = Axes.plot
    def wrong(self, xs, ys, *args, **kwargs):
        return original(self, xs, [0] * len(ys), *args, **kwargs)
    monkeypatch.setattr(Axes, "plot", wrong)
    with pytest.raises(ResultConsistencyError, match="Rendered line coordinates"):
        ResultBundle(fixture()).plot(tmp_path / "plot.png", table="rows", x="x", y="estimate")
    assert not (tmp_path / "plot.png").exists()


def test_rendered_errorbar_geometry_mismatch_is_detected(tmp_path, monkeypatch):
    pytest.importorskip("matplotlib")
    from matplotlib.axes import Axes
    original = Axes.errorbar
    def wrong(self, xs, ys, *args, **kwargs):
        errors = original(self, xs, ys, *args, **kwargs)
        errors.lines[2][0].set_segments([[(0, 0), (0, 1)]] * len(xs))
        return errors
    monkeypatch.setattr(Axes, "errorbar", wrong)
    with pytest.raises(ResultConsistencyError, match="Rendered error bars"):
        ResultBundle(fixture()).plot(tmp_path / "plot.png", table="rows", x="x", y="estimate",
                                     lower="lower", upper="upper")


def test_rendered_group_labels_are_checked(tmp_path, monkeypatch):
    pytest.importorskip("matplotlib")
    from matplotlib.axes import Axes
    original = Axes.legend
    def wrong(self, *args, **kwargs):
        legend = original(self, *args, **kwargs)
        legend.get_texts()[0].set_text("incorrect group")
        return legend
    monkeypatch.setattr(Axes, "legend", wrong)
    with pytest.raises(ResultConsistencyError, match="Rendered groups"):
        ResultBundle(fixture()).plot(tmp_path / "plot.png", table="rows", x="x", y="estimate", group="group")


@pytest.mark.parametrize("change", [{"lower": 99}, {"upper": -1}, {"estimate": None}, {"x": "1"}])
def test_bad_coordinates_or_bounds_fail_instead_of_disappearing_from_plot(tmp_path, change):
    pytest.importorskip("matplotlib")
    data = fixture()
    data["rows"][0].update(change)
    with pytest.raises(ResultConsistencyError):
        ResultBundle(data).plot(tmp_path / "plot.png", table="rows", x="x", y="estimate",
                                lower="lower", upper="upper")


def test_generated_report_facts_share_result_version_and_actual_export_columns(tmp_path):
    bundle = ResultBundle(fixture())
    bundle.write_csv(tmp_path / "table.csv", table="rows")
    report = tmp_path / "report.md"
    bundle.write_report(report, sections=["test"], narrative="This prose has not been numerically reviewed.")
    text = report.read_text()
    facts = json.loads(text.split("```json\n", 1)[1].split("\n```", 1)[0])
    assert facts["test"]["df_den"] == 107
    assert "Table table.csv: 3 rows; columns: estimate, group, lower, upper, x." in text
    assert "result_version" not in text and "specification" not in text and "check_scope" not in text
    assert "Interpretation (not independently checked)" in text
    assert bundle.verify()["free_text"] == "not_assessed"


def test_report_inventory_must_be_regenerated_after_new_plot_or_table(tmp_path):
    bundle = ResultBundle(fixture())
    report = tmp_path / "report.md"
    bundle.write_report(report, sections=["test"])
    bundle.write_csv(tmp_path / "table.csv", table="rows")
    with pytest.raises(ResultConsistencyError, match="inventory is stale"):
        bundle.verify()
    bundle.write_report(report, sections=["test"])
    assert len(bundle.verify()["checks"]) == 2


@pytest.mark.parametrize("kind", ["json", "csv", "report"])
def test_changed_deliverable_cannot_keep_old_verification(tmp_path, kind):
    bundle = ResultBundle(fixture())
    path = tmp_path / kind
    if kind == "json":
        bundle.write_json(path)
    elif kind == "csv":
        bundle.write_csv(path, table="rows")
    else:
        bundle.write_report(path, sections=["test"])
    path.write_text(path.read_text().replace("107", "4") + "\nchanged\n")
    with pytest.raises(ResultConsistencyError, match="bytes changed"):
        bundle.verify()


def test_caller_supplied_passed_flag_is_never_scientific_proof(tmp_path):
    bundle = ResultBundle({"test": {"passed": True, "checks": ["correct"], "df_den": 4}})
    bundle.write_json(tmp_path / "result.json")
    receipt = bundle.verify()
    assert receipt["scientific_validity"] == "not_assessed"
    assert "passed" not in receipt


def test_different_formats_cannot_overwrite_same_export(tmp_path):
    bundle = ResultBundle(fixture())
    bundle.write_csv(tmp_path / "result", table="rows")
    before = (tmp_path / "result").read_bytes()
    with pytest.raises(ResultConsistencyError, match="separate destinations"):
        bundle.write_json(tmp_path / "result")
    assert (tmp_path / "result").read_bytes() == before


def test_report_inventory_does_not_confuse_duplicate_filenames(tmp_path):
    bundle = ResultBundle(fixture())
    bundle.write_csv(tmp_path / "first" / "table.csv", table="rows")
    with pytest.raises(ResultConsistencyError, match="distinct filenames"):
        bundle.write_csv(tmp_path / "second" / "table.csv", table="rows")


def test_symlink_outputs_and_modified_symlink_parents_are_rejected(tmp_path):
    bundle = ResultBundle(fixture())
    target = tmp_path / "target.json"
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(ResultConsistencyError, match="symbolic links"):
        bundle.write_json(link)
    bundle.write_json(target)
    target.unlink()
    target.symlink_to(tmp_path / "elsewhere.json")
    with pytest.raises(ResultConsistencyError, match="missing or redirected"):
        bundle.verify()


def test_input_mount_paths_are_never_writable_export_targets():
    with pytest.raises(ResultConsistencyError, match="read-only"):
        ResultBundle({"x": 1}).write_json(Path("/home/ubuntu/inputs/source.json"))


def test_export_verification_requires_an_actual_export():
    with pytest.raises(ResultConsistencyError, match="No generated exports"):
        ResultBundle({"value": 1}).verify()
