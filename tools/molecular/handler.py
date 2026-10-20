from __future__ import annotations
import sys
from pathlib import Path

def build_command(tool_name: str, arguments: dict) -> list[str]:
    import json
    import base64
    payload = base64.urlsafe_b64encode(json.dumps(arguments, ensure_ascii=False).encode()).decode()
    return [sys.executable, str(Path(__file__).with_name('operations.py')), tool_name, payload]
