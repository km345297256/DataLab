import base64
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
OPERATIONS = ROOT / "tools" / "molecular" / "operations.py"


def run_operation(name: str, arguments: dict):
    payload = base64.urlsafe_b64encode(json.dumps(arguments).encode()).decode()
    completed = subprocess.run(
        [sys.executable, str(OPERATIONS), name, payload],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_molecular_visualize_keeps_png_artifact(tmp_path):
    source = tmp_path / "water.xyz"
    source.write_text(
        "3\nwater\nO 0.000000 0.000000 0.000000\nH 0.758602 0.000000 0.504284\nH -0.758602 0.000000 0.504284\n",
        encoding="utf-8",
    )
    output = tmp_path / "water.png"

    result = run_operation(
        "molecular_visualize",
        {"input_path": str(source), "output_path": str(output)},
    )

    assert result["success"] is True
    assert result["atom_count_plotted"] == 3
    assert output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert output.stat().st_size > 1000
