from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from typing import Any


def build_command(tool_name: str, arguments: dict[str, Any]) -> list[str]:
    payload = base64.urlsafe_b64encode(
        json.dumps(arguments, ensure_ascii=False, separators=(",", ":")).encode()
    ).decode()
    return [sys.executable, str(Path(__file__).with_name("operations.py")), tool_name, payload]
