import importlib.util
import json
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "tools" / "pxp"
SPEC = importlib.util.spec_from_file_location("pxp_operations", PLUGIN / "operations.py")
OPS = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(OPS)


class FakeWaveRecord:
    def __init__(self):
        self.wave = {
            "version": 5,
            "wave": {
                "wave_header": {
                    "bname": b"spectrum\0",
                    "sfA": np.array([0.5, 0, 0, 0]),
                    "sfB": np.array([10.0, 0, 0, 0]),
                    "dataUnits": np.frombuffer(b"a.u.\0", dtype=np.uint8),
                    "dimUnits": np.array([np.frombuffer(b"nm\0", dtype=np.uint8)] + [np.zeros(3, dtype=np.uint8)] * 3),
                },
                "wData": np.array([1.0, 2.0, 5.0, 2.0, 1.0]),
                "note": np.frombuffer(b"sample=demo\0", dtype=np.uint8),
            },
        }


def test_pxp_manifest_registers_complete_tool_set():
    manifest = json.loads((PLUGIN / "manifest.json").read_text(encoding="utf-8"))
    names = {tool["name"] for tool in manifest["tools"]}
    assert len(names) == 35
    assert {"pxp_inspect", "pxp_extract_wave", "pxp_peak_fit", "pxp_visualize", "pxp_export_report"} <= names
    assert {
        "pxp_extract_experiment_conditions",
        "pxp_parse_wave_notes",
        "pxp_associate_waves",
        "pxp_batch_process",
        "pxp_background_subtract",
        "pxp_internal_standard_normalize",
        "pxp_multipeak_deconvolution",
        "pxp_peak_drift",
        "pxp_peak_ratio",
        "pxp_replicate_statistics",
        "pxp_spectral_quality",
        "pxp_overlay_visualize",
        "pxp_waterfall_heatmap",
        "pxp_before_after_visualize",
        "pxp_reproducible_package",
    } <= names


def test_pxp_wave_metadata_axis_and_series_selection():
    record = FakeWaveRecord()
    filesystem = {"root": {"spectra": {"spectrum": record}}}
    waves = OPS.all_waves(filesystem)
    assert waves[0][0] == "root:spectra:spectrum"
    metadata = OPS.describe_wave(*waves[0])
    assert metadata["point_count"] == 5
    assert metadata["axis_start"] == 10.0
    assert metadata["axis_end"] == 12.0
    path, _, x, y = OPS.series(filesystem, {"wave_path": "root:spectra:spectrum"})
    assert path == "root:spectra:spectrum"
    assert x.tolist() == [10.0, 10.5, 11.0, 11.5, 12.0]
    assert y.tolist() == [1.0, 2.0, 5.0, 2.0, 1.0]


def test_pxp_peak_detection_uses_calibrated_axis():
    x = np.arange(0.0, 10.0, 0.1)
    y = np.exp(-0.5 * ((x - 4.0) / 0.3) ** 2)
    peaks, threshold = OPS.detect_peaks(x, y, {"prominence": 0.2, "min_distance": 3})
    assert threshold == 0.2
    assert len(peaks) == 1
    assert abs(peaks[0]["x"] - 4.0) < 0.11


def test_pxp_note_parser_and_quality_assessment():
    parsed = OPS.parse_note("sample=demo\rtemperature: 298 K;operator\tAlice")
    assert parsed == {"sample": "demo", "temperature": "298 K", "operator": "Alice"}

    x = np.arange(100.0, 200.0, 0.5)
    y = np.sin(x / 10.0) + np.linspace(0, 0.2, len(x))
    quality = OPS.spectral_quality(x, y)
    assert 0 <= quality["score"] <= 100
    assert quality["valid_fraction"] == 1.0
    assert quality["grade"] in {"good", "review", "poor"}


def test_pxp_alignment_and_processing_pipeline():
    meta_a = {"axis_step": 1.0}
    meta_b = {"axis_step": 0.5}
    items = [
        ("root:a", meta_a, np.arange(0.0, 6.0), np.arange(0.0, 6.0)),
        ("root:b", meta_b, np.arange(1.0, 5.5, 0.5), np.arange(1.0, 5.5, 0.5) * 2),
    ]
    grid, aligned = OPS.aligned_series(items)
    assert grid.tolist() == [1.0, 2.0, 3.0, 4.0, 5.0]
    assert len(aligned) == 2

    processed, stages = OPS.transformed_series(
        aligned[0][2],
        grid,
        {"smooth_window": 3, "polyorder": 1, "normalize": "max"},
    )
    assert len(stages) == 3
    assert np.isclose(np.max(np.abs(processed)), 1.0)


def test_pxp_peak_models_and_multi_plot(tmp_path):
    axis = np.linspace(-2.0, 2.0, 21)
    for name in ("gaussian", "lorentzian", "voigt"):
        values = OPS.peak_model(name)(axis, 1.0, 0.0, 0.5)
        assert np.all(np.isfinite(values))
        assert np.max(values) > 0

    target = tmp_path / "overlay.png"
    curves = [
        ("a", {}, np.sin(axis)),
        ("b", {}, np.cos(axis)),
    ]
    OPS.render_multi_plot(target, axis, curves, mode="overlay", title="comparison")
    assert target.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
