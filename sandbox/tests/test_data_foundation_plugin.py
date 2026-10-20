import importlib.util
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin
import xarray as xr


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "data_foundation_operations",
    ROOT / "tools" / "data_foundation" / "operations.py",
)
OPERATIONS = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(OPERATIONS)


def _raster(path, *, transform=None):
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=3,
        height=2,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=transform or from_origin(100, 20, 1, 1),
        nodata=-9999,
    ) as target:
        target.write(np.arange(6, dtype="float32").reshape(2, 3), 1)


def test_format_cf_and_grid_diagnostics(tmp_path):
    path = tmp_path / "climate.nc"
    xr.Dataset(
        {"temperature": (("time", "lat", "lon"), np.ones((1, 2, 3), dtype="float32"))},
        coords={
            "time": ("time", [0], {"standard_name": "time", "units": "days since 2000-01-01"}),
            "lat": ("lat", [10.0, 11.0], {"standard_name": "latitude", "units": "degrees_north"}),
            "lon": ("lon", [100.0, 101.0, 102.0], {"standard_name": "longitude", "units": "degrees_east"}),
        },
        attrs={"Conventions": "CF-1.8"},
    ).to_netcdf(path, engine="h5netcdf")

    inspected = OPERATIONS.format_inspect([str(path)])
    assert inspected["summary"]["items"][0]["format"] == "netcdf"
    validated = OPERATIONS.cf_validate(str(path))
    assert validated["summary"]["coordinate_roles"]["longitude"] == "lon"
    diagnosed = OPERATIONS.grid_diagnose(str(path))
    assert diagnosed["summary"]["grid_type"] == "rectilinear"
    assert diagnosed["answer_ready"] is True


def test_raster_compatibility_and_artifact_validation(tmp_path):
    first, second, shifted = tmp_path / "first.tif", tmp_path / "second.tif", tmp_path / "shifted.tif"
    _raster(first)
    _raster(second)
    _raster(shifted, transform=from_origin(101, 20, 1, 1))

    compatible = OPERATIONS.raster_compatibility([str(first), str(second)], "difference")
    assert compatible["summary"]["compatible"] is True
    incompatible = OPERATIONS.raster_compatibility([str(first), str(shifted)], "difference")
    assert incompatible["summary"]["compatible"] is False
    assert "transform" in incompatible["summary"]["differences"][0]["fields"]
    artifact = OPERATIONS.artifact_validate(str(first))
    assert artifact["summary"]["valid_sample_count"] == 6


def test_netcdf_time_units_slices_climatology_and_missing_values(tmp_path, monkeypatch):
    source, output_root = tmp_path / "source.nc", tmp_path / "output"
    output_root.mkdir()
    monkeypatch.setenv("AI_DATASEEK_OUTPUT_ROOT", str(output_root))
    values = np.array([[[273.15]], [[np.nan]], [[275.15]], [[276.15]]], dtype="float32")
    xr.Dataset(
        {"temperature": (("time", "level", "lat"), values, {"units": "K"})},
        coords={
            "time": ("time", np.array(["2000-01-01", "2000-02-01", "2000-04-01", "2000-05-01"], dtype="datetime64[ns]")),
            "level": ("level", [1000.0]),
            "lat": ("lat", [30.0]),
        },
    ).to_netcdf(source, engine="h5netcdf")

    timeline = OPERATIONS.time_axis_normalize(str(source), None)
    assert timeline["summary"]["count"] == 4
    assert timeline["summary"]["gap_count"] >= 1
    converted = OPERATIONS.unit_convert(str(source), "temperature", "degC", str(output_root / "celsius.nc"))
    assert converted["summary"]["target_unit"] == "degC"
    with xr.open_dataset(output_root / "celsius.nc") as ds:
        assert np.isclose(float(ds.temperature.isel(time=0, level=0, lat=0)), 0.0, atol=1e-5)
    sliced = OPERATIONS.vertical_slice(str(source), "temperature", "level", 0, None, str(output_root / "level.nc"))
    assert sliced["summary"]["shape"] == [4, 1]
    climate = OPERATIONS.climatology(str(source), "temperature", "month", str(output_root / "climate.nc"))
    assert climate["summary"]["groups"] == [1, 2, 4, 5]
    quality = OPERATIONS.missing_gap_detect(str(source), "temperature", None)
    assert quality["summary"]["missing_values"] == 1


def test_netcdf_concat_merge_dimension_and_encoding(tmp_path, monkeypatch):
    output = tmp_path / "output"; output.mkdir(); monkeypatch.setenv("AI_DATASEEK_OUTPUT_ROOT", str(output))
    first, second, humidity = tmp_path / "a.nc", tmp_path / "b.nc", tmp_path / "humidity.nc"
    coords = {"latitude": [30.0, 31.0], "longitude": [100.0, 101.0]}
    xr.Dataset({"temperature": (("time", "longitude", "latitude"), np.ones((1, 2, 2), dtype="float32"))}, coords={"time": [np.datetime64("2020-01-01")], **coords}).to_netcdf(first)
    xr.Dataset({"temperature": (("time", "longitude", "latitude"), np.ones((1, 2, 2), dtype="float32") * 2)}, coords={"time": [np.datetime64("2020-02-01")], **coords}).to_netcdf(second)
    xr.Dataset({"humidity": (("time", "latitude", "longitude"), np.ones((1, 2, 2), dtype="float32"))}, coords={"time": [np.datetime64("2020-01-01")], **coords}).to_netcdf(humidity)

    concatenated = OPERATIONS.multi_file_concat([str(first), str(second)], "time", "temperature", str(output / "concat.nc"))
    assert concatenated["summary"]["sizes"]["time"] == 2
    merged = OPERATIONS.multi_file_merge([str(first), str(humidity)], str(output / "merged.nc"))
    assert set(merged["summary"]["variables"]) == {"temperature", "humidity"}
    normalized = OPERATIONS.dimension_normalize(str(first), "temperature", str(output / "normalized.nc"))
    assert normalized["success"] is True
    with xr.open_dataset(output / "normalized.nc") as ds:
        assert ds.temperature.dims == ("time", "latitude", "longitude")
    optimized = OPERATIONS.encoding_optimize(str(first), "temperature", str(output / "optimized.nc"), 4)
    assert optimized["summary"]["compression_level"] == 4
