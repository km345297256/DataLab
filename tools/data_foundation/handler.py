from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def build_command(tool_name: str, arguments: dict[str, Any]) -> list[str]:
    script = Path(__file__).with_name("operations.py")
    command = [sys.executable, str(script), tool_name]
    if "input_paths" in arguments:
        command += ["--input-paths-json", json.dumps(arguments["input_paths"], separators=(",", ":"))]
    if "input_path" in arguments:
        command += ["--input-path", str(arguments["input_path"])]
    if "operation" in arguments:
        command += ["--operation", str(arguments["operation"])]
    if "max_entries" in arguments:
        command += ["--max-entries", str(arguments["max_entries"])]
    if "dataset_path" in arguments:
        command += ["--dataset-path", str(arguments["dataset_path"])]
    if "selection" in arguments:
        command += ["--selection-json", json.dumps(arguments["selection"], separators=(",", ":"))]
    if "max_values" in arguments:
        command += ["--max-values", str(arguments["max_values"])]
    if "output_path" in arguments:
        command += ["--output-path", str(arguments["output_path"])]
    for key, flag in (("variable", "--variable"), ("target_unit", "--target-unit"), ("time_name", "--time-name"), ("dimension", "--dimension"), ("frequency", "--frequency"), ("compression_level", "--compression-level"), ("vector_path", "--vector-path")):
        if arguments.get(key) is not None:
            command += [flag, str(arguments[key])]
    if arguments.get("index") is not None:
        command += ["--index", str(arguments["index"])]
    if arguments.get("value") is not None:
        command += ["--value", str(arguments["value"])]
    return command
