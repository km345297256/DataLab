import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from scripts import datalab_json


def test_multiindex_dictionary_keys_preserve_structure_without_collisions():
    frame = pd.DataFrame([[1, 2]], columns=pd.MultiIndex.from_tuples([("x", "mean"), ("x", "count")]))
    result = datalab_json.normalize_json(frame.to_dict())
    assert result["$type"] == "mapping"
    assert [entry["key"] for entry in result["entries"]] == [["x", "mean"], ["x", "count"]]
    mixed = datalab_json.normalize_json({1: "numeric", "1": "text"})
    assert [entry["key"] for entry in mixed["entries"]] == [1, "1"]
    json.dumps(result, allow_nan=False)


def test_dataframe_and_series_retain_labels_and_numpy_missing_value_semantics():
    index = pd.MultiIndex.from_tuples([("A", 1), ("B", 2)], names=["group", "visit"])
    frame = pd.DataFrame({"value": [np.float32(1.25), np.nan]}, index=index)
    result = datalab_json.normalize_json(frame)
    assert result["index"] == [["A", 1], ["B", 2]]
    assert result["index_names"] == ["group", "visit"]
    assert result["columns"] == ["value"]
    assert result["data"][0] == [1.25]
    assert result["data"][1][0] == {"value": None, "status": "non_finite", "reason": "nan"}
    series = datalab_json.normalize_json(pd.Series([pd.NA, pd.NaT], name="missing"))
    assert series["name"] == "missing" and series["data"] == [None, None]
    array = datalab_json.normalize_json(np.array([np.inf, -np.inf]))
    assert [item["reason"] for item in array] == ["positive_infinity", "negative_infinity"]


def test_not_estimable_requires_reason_and_never_implies_zero():
    result = datalab_json.not_estimable("Constant predictors", method="discriminant analysis")
    assert result == {"value": None, "status": "not_estimable", "reason": "Constant predictors", "method": "discriminant analysis"}
    with pytest.raises(ValueError):
        datalab_json.not_estimable(" ")


@pytest.mark.parametrize("bad", [object(), {"unsupported": {1, 2}}, "\ud800"])
def test_normalization_or_encoding_failure_preserves_previous_file(tmp_path, bad):
    root = tmp_path.resolve()
    path = root / "report.json"
    path.write_text('{"previous": true}\n')
    with pytest.raises((TypeError, ValueError, UnicodeError)):
        datalab_json.write_json(path, bad, output_root=root)
    assert path.read_text() == '{"previous": true}\n'
    assert list(root.iterdir()) == [path]


def test_cycle_and_output_limit_are_rejected_before_target_created(tmp_path):
    root = tmp_path.resolve()
    value = []
    value.append(value)
    with pytest.raises(ValueError, match="circular"):
        datalab_json.write_json(root / "cycle.json", value, output_root=root)
    with pytest.raises(ValueError, match="byte limit"):
        datalab_json.write_json(root / "big.json", {"text": "large"}, output_root=root, max_bytes=1)
    assert list(root.iterdir()) == []


def test_atomic_replace_failure_cleans_temporary_and_keeps_previous(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    path = root / "result.json"
    path.write_text('{"previous": true}')
    def fail(*args, **kwargs):
        raise OSError("simulated filesystem failure")
    monkeypatch.setattr(datalab_json.os, "replace", fail)
    with pytest.raises(OSError):
        datalab_json.write_json(path, {"next": True}, output_root=root)
    assert path.read_text() == '{"previous": true}'
    assert list(root.iterdir()) == [path]


def test_repeated_directory_names_and_atomic_replacement(tmp_path):
    root = tmp_path.resolve() / "same"
    root.mkdir()
    path = root / "same" / "same" / "report.json"
    datalab_json.write_json(path, {"value": np.int64(42)}, output_root=root)
    assert json.loads(path.read_text()) == {"value": 42}
    datalab_json.write_json(path, {"value": np.int64(43)}, output_root=root)
    assert json.loads(path.read_text()) == {"value": 43}
    assert list(path.parent.iterdir()) == [path]


def test_writer_rejects_symlink_parent_and_destination(tmp_path):
    root = tmp_path.resolve() / "output"
    root.mkdir()
    outside = tmp_path.resolve() / "outside"
    outside.mkdir()
    (root / "alias").symlink_to(outside, target_is_directory=True)
    with pytest.raises((OSError, ValueError)):
        datalab_json.write_json(root / "alias" / "file.json", {"x": 1}, output_root=root)
    outside_file = outside / "original.json"
    outside_file.write_text("original")
    (root / "linked.json").symlink_to(outside_file)
    with pytest.raises(ValueError):
        datalab_json.write_json(root / "linked.json", {"x": 1}, output_root=root)
    assert outside_file.read_text() == "original"
