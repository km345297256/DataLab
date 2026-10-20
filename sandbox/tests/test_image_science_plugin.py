import json
from pathlib import Path

import geopandas as gpd
from PIL import Image
from shapely.geometry import box

from scripts.tool_plugin_runner import load_handler, load_registry

ROOT = Path(__file__).resolve().parents[2]


def run(tool, arguments, monkeypatch, tmp_path):
    monkeypatch.setenv("AI_DATASEEK_OUTPUT_ROOT", str(tmp_path))
    registry = load_registry(ROOT / "tools")
    spec, handler = registry[tool]
    command = load_handler(handler)(tool, arguments)
    import subprocess
    result = subprocess.run(command, check=True, capture_output=True, text=True, env={**__import__("os").environ, "AI_DATASEEK_OUTPUT_ROOT": str(tmp_path)})
    return json.loads(result.stdout)


def test_image_collection_duplicates_quality_contact_and_derivative(tmp_path, monkeypatch):
    first = tmp_path / "first.jpg"
    duplicate = tmp_path / "duplicate.jpg"
    other = tmp_path / "other.png"
    Image.new("RGB", (64, 48), (20, 80, 160)).save(first)
    duplicate.write_bytes(first.read_bytes())
    Image.new("RGB", (64, 48), (240, 240, 240)).save(other)

    inspected = run("image_collection_inspect", {"input_dir": str(tmp_path)}, monkeypatch, tmp_path)
    assert inspected["file_count"] == 3
    duplicates = run("image_duplicate_detect", {"input_paths": [str(first), str(duplicate), str(other)]}, monkeypatch, tmp_path)
    assert len(duplicates["exact_groups"]) == 1
    quality = run("image_quality_assess", {"input_paths": [str(first), str(other)]}, monkeypatch, tmp_path)
    assert len(quality["records"]) == 2
    sheet = run("image_contact_sheet", {"input_paths": [str(first), str(other)], "output_dir": str(tmp_path / "sheets")}, monkeypatch, tmp_path)
    assert Path(sheet["artifacts"][0]["path"]).is_file()
    derivative = run("image_safe_derivative", {"input_paths": [str(first)], "output_dir": str(tmp_path / "derived")}, monkeypatch, tmp_path)
    assert Path(derivative["artifacts"][0]["path"]).is_file()


def test_image_integrity_metadata_and_spatial_group_without_gps(tmp_path, monkeypatch):
    image = tmp_path / "photo.jpg"
    Image.new("RGB", (32, 32), "red").save(image)
    assert run("image_integrity_check", {"input_paths": [str(image)]}, monkeypatch, tmp_path)["invalid_count"] == 0
    metadata = run("image_metadata_extract", {"input_paths": [str(image)]}, monkeypatch, tmp_path)
    assert metadata["records"][0]["gps_coordinates"] is None
    zones = tmp_path / "zones.geojson"
    gpd.GeoDataFrame({"zone_id": ["A"]}, geometry=[box(100, 20, 110, 30)], crs="EPSG:4326").to_file(zones, driver="GeoJSON")
    registry = load_registry(ROOT / "tools")
    assert "image_spatial_group" in registry
