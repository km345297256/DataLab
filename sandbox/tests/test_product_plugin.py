import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("product_operations", ROOT / "tools" / "product" / "operations.py")
OPS = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(OPS)


def test_metadata_checksum_and_package(tmp_path, monkeypatch):
    output = tmp_path / "output"; output.mkdir(); monkeypatch.setenv("AI_DATASEEK_OUTPUT_ROOT", str(output))
    source = tmp_path / "result.txt"; source.write_text("result", encoding="utf-8")
    metadata = OPS.metadata_generate({"input_paths": [str(source)], "product_name": "Test", "output_path": str(output / "metadata.json")})
    assert metadata["summary"]["asset_count"] == 1
    checksum = OPS.checksum_manifest({"input_paths": [str(source)], "output_path": str(output / "checksums.json")})
    assert checksum["summary"]["file_count"] == 1
    packaged = OPS.product_package({"input_paths": [str(source), str(output / "metadata.json")], "product_name": "Test", "output_path": str(output / "product.zip")})
    assert packaged["artifacts"][0]["size_bytes"] > 0
