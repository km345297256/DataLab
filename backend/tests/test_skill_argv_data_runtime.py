"""Artificial CLI transport checks using the real isolated validation worker."""
import copy
import hashlib
import json
import os
from types import SimpleNamespace

import pytest

from app.domain.services.skills.skill_validation import SyntheticValidationPlan
from app.domain.services.skills.skill_validation_review import oracle_decision_identity
from app.infrastructure.external.sandbox import skill_validation_worker as worker


SCRIPT = "scripts/configuration.py"
SOURCE = '''import json
import sys
from pathlib import Path

arguments = sys.argv[1:]
input_path = Path(arguments[arguments.index('--input') + 1])
output_path = Path(arguments[arguments.index('--output') + 1])
configuration = arguments[arguments.index('--config') + 1]
raw_values = arguments[arguments.index('--raw') + 1:]
if input_path.resolve() == output_path.resolve():
    print('invalid_input', file=sys.stderr)
    raise SystemExit(2)
input_path.read_bytes()
try:
    parsed = json.loads(configuration)
except json.JSONDecodeError:
    print('invalid_json', file=sys.stderr)
    raise SystemExit(2)
result = {'argv': raw_values, 'configuration_original': configuration,
          'value': sum(parsed['values']), 'pattern': parsed['pattern'],
          'unit': parsed['unit'], 'nested': parsed['nested']}
output_path.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
'''

RAW_VALUES = [
    "", "\n", "\r\n", "  keep spaces  ", "中文\u2028line\u2029end", "[A-Z]+", r"\s+", "{1,3}",
    "mg/L", "prefix{input:table.csv}", "prefix{output}/result", '{"nested":"{input:table.csv}"}',
    ' {"unfinished": ', "a; echo data", "$(echo data)", "relative/name",
]


def plan():
    cases = []
    for kind, configuration, expected in (
        ("normal", ' { "values": [2, 3, 6], "pattern": "\\\\s+", "unit": "mg/L", "nested": "{input:table.csv}" } ', 11),
        ("boundary", '{"values":[],"pattern":"[A-Z]+","unit":"","nested":"{output}/result"}', 0),
        ("invalid", '{"values":', None),
    ):
        assertions = [{"type": "output_empty"}, {"type": "stderr_contains", "expected": "invalid_json"}]
        if expected is not None:
            parsed = json.loads(configuration)
            observed = [("/configuration_original", configuration), ("/value", expected),
                        *[("/" + key, parsed[key]) for key in ("pattern", "unit", "nested")],
                        *[("/argv/" + str(index), value) for index, value in enumerate(RAW_VALUES)]]
            assertions = [{"type": "json_value", "path": "result.json", "pointer": pointer,
                           "expected": {"value": value}} for pointer, value in observed]
        cases.append({
            "id": kind + "_configuration", "kind": kind,
            "description": "Artificial configuration values retain their original CLI bytes",
            "requirement_ids": ["R001"], "source_ids": ["S001"],
            "oracle_explanation": "CLI text is unchanged; two plus three plus six is eleven, empty sum is zero, malformed JSON is rejected.",
            "argv": ["--input", "{input:table.csv}", "--output", "{output}/result.json",
                     "--config", configuration, "--raw", *RAW_VALUES],
            "input_files": [{"path": "table.csv", "format": "csv", "columns": ["value"], "rows": [{"value": 1}]}],
            "expected_exit_code": "nonzero" if kind == "invalid" else 0,
            "assertions": assertions,
        })
    return SyntheticValidationPlan.model_validate({"scripts": [{
        "script_path": SCRIPT, "support": "supported",
        "reason": "Real CLI parses configuration and reports exact transport values", "cases": cases,
    }]})


def test_argv_plan_identity_and_worker_materialization_preserve_all_data():
    value = plan()
    document = value.model_dump()
    frozen = copy.deepcopy(document)
    reparsed = SyntheticValidationPlan.model_validate(document)
    assert reparsed.model_dump() == frozen
    identity = oracle_decision_identity(value)
    for script in identity["scripts"]:
        originals = {case["id"]: case for case in frozen["scripts"][0]["cases"]}
        for case in script["cases"]:
            assert case["argv"] == originals[case["id"]]["argv"]
    for case in value.scripts[0].cases:
        rendered = worker._argv(case, "/validation/case")
        assert rendered[1] == "/validation/case/table.csv"
        assert rendered[3] == worker.OUTPUT_ROOT + "/result.json"
        assert rendered[4:] == case.argv[4:]
        assert [item.encode("utf-8") for item in rendered[4:]] == [item.encode("utf-8") for item in case.argv[4:]]
    changed = copy.deepcopy(frozen)
    changed["scripts"][0]["cases"][0]["argv"][-1] += " "
    assert oracle_decision_identity(SyntheticValidationPlan.model_validate(changed)) != identity


@pytest.mark.skipif(os.environ.get("DATALAB_RUN_SYNTHETIC_DOCKER_TEST") != "1",
                    reason="Opt-in artificial CLI data transport through the real isolated worker")
@pytest.mark.asyncio
@pytest.mark.parametrize("strip_values", [False, True])
async def test_real_worker_transports_complex_data_and_cli_rejects_malformed_json(monkeypatch, strip_values):
    monkeypatch.setattr(worker, "helper_security_options", lambda: ["no-new-privileges:true"])
    value = plan()
    original_plan = value.model_dump()
    source = SOURCE.replace("'argv': raw_values", "'argv': [item.strip() for item in raw_values]") if strip_values else SOURCE
    receipt = await worker.validate_skill_scripts({SCRIPT: source}, value,
        SimpleNamespace(sandbox_image=os.environ["DATALAB_SYNTHETIC_TEST_IMAGE"]))
    assert value.model_dump() == original_plan
    assert receipt["plan_sha256"] == hashlib.sha256(worker._json(original_plan)).hexdigest()
    assert receipt["source_sha256"] == {SCRIPT: hashlib.sha256(source.encode()).hexdigest()}
    assert all(case["input_unchanged"] for case in receipt["cases"]), receipt
    assert [case["actual_exit_code"] for case in receipt["cases"]] == [0, 0, 2]
    rejected = receipt["cases"][2]
    assert rejected["status"] == "passed" and all(item["passed"] for item in rejected["checks"])
    assert receipt["status"] == ("failed" if strip_values else "passed"), receipt
    for case in receipt["cases"][:2]:
        if strip_values:
            assert case["reason"] == "assertion_mismatch"
            assert any(not item["passed"] for item in case["checks"])
        else:
            assert case["status"] == "passed" and all(item["passed"] for item in case["checks"])
