from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def build_command(tool_name: str, arguments: dict[str, Any]) -> list[str]:
    script = Path(__file__).with_name("operations.py")
    return [
        sys.executable,
        str(script),
        tool_name,
        "--arguments-json",
        json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
    ]
