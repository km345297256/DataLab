"""Real execution, rather than AST coding style, establishes file boundaries.

Only artificial inputs are used. Opt in with DATALAB_RUN_SYNTHETIC_DOCKER_TEST
and an existing sandbox image; no packages, credentials or network are needed.
"""
import copy
import os
from types import SimpleNamespace

import pytest

from app.domain.services.skills.skill_creation.script_policy import validate_script
from app.domain.services.skills.skill_validation import SyntheticValidationPlan
from app.infrastructure.external.sandbox import skill_validation_worker as worker


SCRIPT_PATH = "scripts/aggregate.py"
INVOCATION = (
    "python scripts/aggregate.py --input mapping.json "
    "--output result.json --mode total"
)

PARSERS = {
    "argparse": '''
def options(argv):
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--mode', required=True)
    args = parser.parse_args(argv)
    return args.input, args.output, args.mode
''',
    "custom": '''
def options(argv):
    if len(argv) % 2:
        raise ValueError('invalid_input')
    values = {}
    for flag, value in zip(argv[::2], argv[1::2]):
        if flag not in ('--input', '--output', '--mode'):
            raise ValueError('invalid_input')
        values.setdefault(flag, []).append(value)
    if any(len(values.get(key, [])) != 1 for key in ('--input', '--output', '--mode')):
        raise ValueError('invalid_input')
    return values['--input'][0], values['--output'][0], values['--mode'][0]
''',
}

BODY = '''
import json
import sys
from pathlib import Path

def read_values(path):
    values = json.loads(path.read_text())
    if not isinstance(values, list) or any(type(value) not in (int, float) for value in values):
        raise ValueError('invalid_input')
    return values

def save(destination, result):
    mode = 'w'
    with destination.open(mode) as handle:
        json.dump({'value': result}, handle)

def execute(input_path, output, mode):
    mapping = json.loads(Path(input_path).read_text())
    if not isinstance(mapping, dict) or set(mapping) != {'left', 'right'} or mode != 'total':
        raise ValueError('invalid_input')
    destination = Path(output).resolve()
    paths = []
    for role, raw_path in {'manifest': input_path, **mapping}.items():
        source = Path(raw_path).resolve()
        if source == destination:
            raise ValueError('invalid_input')
        paths.append(source)
    # RUNTIME_PROBE
    result = sum(value for path in paths[1:] for value in read_values(path))
    save(destination, result)

if __name__ == '__main__':
    try:
        execute(*options(sys.argv[1:]))
    except ValueError:
        print('invalid_input', file=sys.stderr)
        raise SystemExit(2)
'''


def script(parser="custom", probe="pass"):
    source = PARSERS[parser] + BODY.replace("# RUNTIME_PROBE", probe)
    return SimpleNamespace(path=SCRIPT_PATH, content=source, invocation=INVOCATION)


def plan(*, same_path_cases=False):
    cases = []
    for kind, left, right, expected in (
        ("normal", [2, 5], [-3], 4),
        ("boundary", [], [], 0),
        ("invalid", ["not a number"], [1], None),
    ):
        cases.append({
            "id": kind + "_values", "kind": kind,
            "description": "Aggregate two independent artificial JSON arrays",
            "requirement_ids": ["R001"], "source_ids": ["S001"],
            "oracle_explanation": "Two plus five minus three equals four; empty sums equal zero; text is invalid.",
            "argv": ["--input", "{input:mapping.json}",
                     "--output", "{output}/result.json", "--mode", "total"],
            "input_files": [{"path": "mapping.json", "format": "json",
                             "data": {"left": "{input:left.json}", "right": "{input:right.json}"}},
                            *[{"path": name + ".json", "format": "json", "data": values}
                              for name, values in (("left", left), ("right", right))]],
            "expected_exit_code": "nonzero" if kind == "invalid" else 0,
            "input_unchanged": True,
            "assertions": ([{"type": "output_empty"},
                            {"type": "stderr_contains", "expected": "invalid_input"}]
                           if kind == "invalid" else
                           [{"type": "json_value", "path": "result.json", "pointer": "/value",
                             "expected": {"value": expected}}]),
        })
    if same_path_cases:
        item = copy.deepcopy(cases[0])
        item.update(id="output_aliases_manifest", kind="invalid", expected_exit_code="nonzero")
        item["argv"][3] = "{input:mapping.json}"
        item["assertions"] = [{"type": "output_empty"},
                              {"type": "stderr_contains", "expected": "invalid_input"}]
        cases.append(item)
    return SyntheticValidationPlan.model_validate({"version": 1, "status": "not_run", "scripts": [{
        "script_path": SCRIPT_PATH, "support": "supported",
        "reason": "Deterministic synthetic multi-input sum and rejection behavior", "cases": cases,
    }]})


@pytest.mark.parametrize("parser", PARSERS)
def test_static_portability_accepts_helpers_loops_and_different_cli_parsers(parser):
    validate_script(script(parser))
    assert len(plan().scripts[0].cases) == 3


docker_only = pytest.mark.skipif(
    os.environ.get("DATALAB_RUN_SYNTHETIC_DOCKER_TEST") != "1",
    reason="Opt-in disposable sandbox execution contract",
)


async def run(source, monkeypatch, *, same_path_cases=False):
    monkeypatch.setattr(worker, "helper_security_options", lambda: ["no-new-privileges:true"])
    return await worker.validate_skill_scripts(
        {SCRIPT_PATH: source.content}, plan(same_path_cases=same_path_cases),
        SimpleNamespace(sandbox_image=os.environ["DATALAB_SYNTHETIC_TEST_IMAGE"]),
    )


@docker_only
@pytest.mark.asyncio
@pytest.mark.parametrize("parser", PARSERS)
async def test_different_code_styles_compute_values_and_reject_bad_input(parser, monkeypatch):
    source = script(parser)
    validate_script(source)
    receipt = await run(source, monkeypatch, same_path_cases=True)
    assert receipt["status"] == "passed", receipt
    assert len(receipt["cases"]) == 4
    for item in receipt["cases"]:
        assert item["status"] == "passed" and item["input_unchanged"]
        assert item["checks"] and all(check["passed"] for check in item["checks"])
    assert [item["actual_exit_code"] for item in receipt["cases"]] == [0, 0, 2, 2]


@docker_only
@pytest.mark.asyncio
@pytest.mark.parametrize("probe", [
    "paths[0].write_text('changed input')",
    "Path(__file__).write_text('changed script')",
    "(destination.parent.parent / 'outside-output.json').write_text('outside output')",
])
async def test_runtime_blocks_writes_without_static_api_or_coding_style_lists(probe, monkeypatch):
    source = script(probe=probe)
    # Compilation is deliberately not a claim that this program is safe to run.
    validate_script(source)
    receipt = await run(source, monkeypatch)
    assert receipt["status"] == "failed", receipt
    assert len(receipt["cases"]) == 3
    for item in receipt["cases"]:
        assert item["status"] == "failed", item
        assert item["actual_exit_code"] != 0 and item["input_unchanged"], item
        if item["kind"] == "invalid":
            # An unhandled permission traceback is not the required ordinary
            # bad-input rejection, even though both have a nonzero exit code.
            assert item["reason"] == "program_runtime_failure", item
        else:
            assert item["reason"] == "output_missing:result.json", item
            assert "OSError: [Errno 30] Read-only file system" in item["diagnostic"], item
        assert not item.get("output_sha256"), item
