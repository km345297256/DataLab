"""The script policy is independently usable without creator/model services."""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.domain.services.skills.skill_creation.script_policy import (
    SkillScriptValidationError,
    validate_script,
)


def script(body: str):
    return SimpleNamespace(
        path="scripts/summarize.py",
        invocation="python scripts/summarize.py --input INPUT --output OUTPUT",
        content="""import argparse
import json
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument('--input', required=True)
parser.add_argument('--output', required=True)
args = parser.parse_args()
if Path(args.input).resolve() == Path(args.output).resolve():
    raise ValueError('input and output must differ')
""" + body,
    )


def test_static_policy_accepts_structural_source_without_recipe_or_model():
    validate_script(script("""with Path(args.input).open('r') as handle:
    values = json.load(handle)
result = {'count': len(values), 'sum': sum(values)}
with Path(args.output).open('w') as handle:
    json.dump(result, handle)
"""))


@pytest.mark.parametrize("operation", [
    "Path(args.input).write_text('changed')",
    "Path(args.input).open('w')",
    "destination = args.input\nPath(destination).write_text('changed')",
    "with open(args.input, 'w') as handle:\n    json.dump({'sum': 0}, handle)",
])
def test_static_admission_defers_actual_input_mutation_checks_to_isolated_execution(operation):
    # Compilation does not certify this behavior. The read-only worker must reject it.
    validate_script(script(operation))


@pytest.mark.parametrize("operation", [
    "eval('1 + 1')",
    "getattr(args, 'output')",
    "Path(args.output).unlink()",
    "value.__class__",
    "import re\npattern = re.compile('pattern')",
    "from re import compile as regex\npattern = regex('pattern')",
    "import pandas as pd\nframe = pd.DataFrame({'old': [1]})\nframe.rename(columns={'old': 'new'})",
    "values = [1, 2]\nvalues.remove(1)",
    "import numpy as np\nversion = np.__version__",
    "module = __import__('arbitrary_scientific_package')",
    "remove = False",
])
def test_api_names_attributes_and_dynamic_calls_are_admitted_without_execution(operation):
    validate_script(script(operation))


def test_policy_never_executes_generated_source(tmp_path):
    sentinel = tmp_path / "must-not-be-created"
    # Source validation must not run even an expression that fails immediately.
    value = script(f"raise RuntimeError({str(sentinel)!r})\nPath(args.output).write_text('result')")
    validate_script(value)
    assert not sentinel.exists()


def test_policy_dependency_boundary_excludes_orchestration_and_io():
    from app.domain.services.skills.skill_creation import script_policy
    tree = ast.parse(Path(script_policy.__file__).read_text())
    imports = {
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert imports <= {"__future__", "ast", "typing", "script_dependencies", "shlex"}


@pytest.mark.parametrize("content", ["return 1", "await task()", "if True print(1)",
    "value = 1\nfrom __future__ import annotations", "def main(: pass"])
def test_static_policy_rejects_parse_or_compile_errors(content):
    value = script("")
    value.content = content
    with pytest.raises(SkillScriptValidationError) as caught:
        validate_script(value)
    assert caught.value.code == "script_syntax_invalid"


@pytest.mark.parametrize("invocation", [
    "python scripts/summarize.py.extra --input INPUT --output OUTPUT",
    "python prefix-scripts/summarize.py --input INPUT --output OUTPUT",
    "python scripts/summarize.py --input-files INPUT --output OUTPUT",
    "python scripts/summarize.py --input INPUT --output-directory OUTPUT",
    "python scripts/summarize.py -- 'description --input --output'",
    "python scripts/summarize.py '--input INPUT --output OUTPUT'",
    "python scripts/summarize.py --input= --output=OUTPUT",
    "python scripts/summarize.py --input --output",
    "python scripts/summarize.py --input INPUT --output",
    "python scripts/summarize.py --input '' --output OUTPUT",
    "python scripts/summarize.py --input INPUT --output ''",
    "python scripts/summarize.py --input INPUT --output 'unterminated",
    "python --input INPUT --output OUTPUT scripts/summarize.py",
])
def test_invocation_requires_exact_path_and_option_tokens(invocation):
    value = script("")
    value.invocation = invocation
    with pytest.raises(SkillScriptValidationError) as caught:
        validate_script(value)
    assert caught.value.code == "script_invocation_invalid"


@pytest.mark.parametrize("invocation", [
    "python scripts/summarize.py --input INPUT --output OUTPUT",
    "python 'scripts/summarize.py' --input='input file.json' --output='output file.json'",
])
def test_invocation_supports_separate_or_equals_options_without_parser_assumptions(invocation):
    value = script("")
    value.content = "import sys\nvalues = sys.argv[1:]\n"
    value.invocation = invocation
    validate_script(value)


def test_historical_hints_describe_behavior_without_prescribing_source_shape():
    from app.domain.services.skills.skill_creation.script_policy import script_authoring_capabilities
    capabilities = script_authoring_capabilities("complete_script")
    assert set(capabilities["static_rules"]) == {"script_syntax_invalid", "script_invocation_invalid"}
    hints = " ".join(SkillScriptValidationError.HINTS.values())
    assert not any(old in hints for old in ("argparse.add_argument", "Keep helper functions pure", "simple output-path alias"))
    assert capabilities["runtime_contract"] and capabilities["execution_validation_required"] is True
