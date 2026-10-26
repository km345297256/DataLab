"""Artificial interfaces declared independently of generated test source."""


def group_summary_interface():
    return {
        "version": 1,
        "cli_options": [{"flags": [flag], "required": True, "value_type": "str",
                         "meaning": "Bind selected read-only input or isolated writable output."}
                        for flag in ("--input", "--output")],
        "inputs": [{"argument": "--input", "format": "csv", "columns": ["group", "value"],
                    "structure": "Rows bind a group label and one numeric observation.",
                    "missing_semantics": ["An empty value cell is missing; a zero value remains a valid observation."]}],
        "output_mode": "directory",
        "outputs": [{"path": "@output/summary.csv", "format": "csv",
                     "columns": ["group", "mean", "valid_count", "missing_count", "missing_rate"],
                     "structure": "One summary row per group in the input.",
                     "missing_semantics": ["All-missing groups retain empty mean, with zero valid_count."]},
                    {"path": "@output/status.json", "format": "json",
                     "shape": {"type": "object", "properties": {"status": {"type": "string"}}},
                     "structure": "A status object records the completion state.",
                     "missing_semantics": ["The status property is required and must never be null."]}],
    }
