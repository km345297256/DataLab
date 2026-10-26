"""Cross-task offline authoring contracts, independent of any model response.

These examples characterize static policy only. Passing never substitutes for
isolated execution, numerical validation, or fidelity to the user's method.
All syntactically valid source layouts are admitted; only actual isolated
execution can establish input immutability or output isolation.
"""
from textwrap import dedent, indent
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.services.skills.skill_creation.script_policy import (
    SkillScriptValidationError, validate_script,
)
from app.domain.services.skills.skill_script_verification import verify_skill_script_cases


def script(body, *, imports="", helpers="", parser_helper=False):
    parser = dedent("""
        parser = argparse.ArgumentParser()
        parser.add_argument('--input', required=True)
        parser.add_argument('--output', required=True)
    """).strip()
    if parser_helper:
        helpers = dedent(helpers) + "\ndef read_arguments():\n" + indent(parser, "    ") + "\n    return parser.parse_args()\n"
        parsing = "args = read_arguments()"
    else:
        parsing = parser + "\nargs = parser.parse_args()"
    main = parsing + dedent("""

        input_path = Path(args.input).resolve()
        output_path = Path(args.output).resolve()
        if input_path == output_path:
            raise ValueError('input and output must differ')
    """) + dedent(body)
    source = "import argparse\nfrom pathlib import Path\n" + dedent(imports) + "\n" + dedent(helpers)
    source += "\ndef main():\n" + indent(main, "    ") + "\n\nif __name__ == '__main__':\n    main()\n"
    return SimpleNamespace(path="scripts/task.py", invocation="python scripts/task.py --input INPUT --output OUTPUT",
                           content=source)


SUPPORTED = [
    pytest.param("", "", """
        text = input_path.read_text(encoding='utf-8')
        output_path.write_text(text.replace('old', 'new'), encoding='utf-8')
    """, id="text-replacement-pathlib"),
    pytest.param("import csv\nimport json", "", """
        with input_path.open('r', encoding='utf-8', newline='') as handle:
            rows = list(csv.DictReader(handle))
        with output_path.open('w', encoding='utf-8') as handle:
            json.dump(rows, handle, ensure_ascii=False, allow_nan=False)
    """, id="csv-to-json-reused-handle"),
    pytest.param("import pandas as pd", "", """
        frame = pd.read_csv(input_path)
        cleaned = frame.dropna(subset=['id']).drop_duplicates(subset=['id'])
        cleaned.to_csv(output_path, index=False)
    """, id="pandas-table-cleaning"),
    pytest.param("import json\nimport pandas as pd", "", """
        document = json.loads(input_path.read_text(encoding='utf-8'))
        left = pd.DataFrame(document['left'])
        right = pd.DataFrame(document['right'])
        merged = left.merge(right, on='id', validate='one_to_one')
        merged.to_csv(path_or_buf=output_path, index=False)
    """, id="key-based-table-merge"),
    pytest.param("import pandas as pd\nimport matplotlib.pyplot as plt", "", """
        frame = pd.read_csv(input_path)
        figure, axes = plt.subplots()
        axes.plot(frame['time'], frame['value'])
        axes.set_xlabel('Time')
        axes.set_ylabel('Value')
        figure.savefig(fname=output_path)
        plt.close(figure)
    """, id="matplotlib-visualization"),
    pytest.param("import json\nimport statistics", "", """
        values = json.loads(input_path.read_text(encoding='utf-8'))
        result = {'count': len(values), 'mean': statistics.mean(values)}
        output_path.write_text(json.dumps(result, allow_nan=False), encoding='utf-8')
    """, id="basic-statistics-json"),
    pytest.param("import json", """
        def read_values(path):
            return json.loads(Path(path).read_text(encoding='utf-8'))

        def summarize(values):
            return {'count': len(values), 'sum': sum(values)}
    """, """
        values = read_values(input_path)
        result = summarize(values)
        output_path.write_text(json.dumps(result, allow_nan=False), encoding='utf-8')
    """, id="readonly-helper-and-pure-computation"),
    pytest.param("import csv\nimport json", "", """
        rows = json.loads(input_path.read_text(encoding='utf-8'))
        with output_path.open('w', encoding='utf-8', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=['id', 'value'])
            writer.writeheader()
            writer.writerows(rows)
    """, id="json-to-csv-explicit-writable-handle"),
    pytest.param("from PIL import Image", "", """
        with Image.open(input_path) as image:
            converted = image.convert('RGB')
            converted.save(fp=output_path, format='PNG')
    """, id="pillow-conversion-with-keyword-destination"),
]


@pytest.mark.parametrize("imports,helpers,body", SUPPORTED)
@pytest.mark.parametrize("parser_helper", [False, True], ids=["entry-parser", "one-parser-helper"])
def test_common_tasks_use_supported_readonly_input_and_explicit_output(imports, helpers, body, parser_helper):
    validate_script(script(body, imports=imports, helpers=helpers, parser_helper=parser_helper))


@pytest.mark.parametrize("operation", [
    "input_path.write_text('changed')",
    "destination = input_path\ndestination.write_bytes(b'changed')",
    "with input_path.open('w') as handle:\n    handle.write('changed')",
    "frame.to_csv(input_path, index=False)",
    "figure.savefig(fname=input_path)",
    "with input_path.open('a') as handle:\n    json.dump({}, handle)",
])
def test_input_write_shape_is_not_a_static_runtime_verdict(operation):
    value = script("frame = pd.DataFrame()\nfigure = plt.figure()\n" + operation,
        imports="import json\nimport pandas as pd\nimport matplotlib.pyplot as plt")
    validate_script(value)


@pytest.mark.parametrize("destination", ["Path('fixed-result.json')", "Path('/tmp/session-output.json')",
                                          "input_path.parent / 'result.json'"])
def test_destination_shape_requires_real_worker_evidence(destination):
    validate_script(script(f"({destination}).write_text('result')"))


def test_guard_shape_is_not_a_static_safety_proof():
    value = script("output_path.write_text('result')")
    value.content = value.content.replace("if input_path == output_path:", "if args.input == args.output:")
    validate_script(value)


@pytest.mark.parametrize("dependency", ["zipfile", "xml", "PIL", "openpyxl", "cairosvg", "os",
                                       "subprocess", "importlib", "arbitrary_scientific_package"])
def test_dependency_imports_are_allowed_independently_of_runtime_availability(dependency):
    # This static check does not import the library or assert availability.
    # Actual execution still observes read-only inputs and isolated outputs.
    validate_script(script("output_path.write_bytes(input_path.read_bytes())", imports=f"import {dependency}"))


@pytest.mark.parametrize("operation", ["os.system('echo unsafe')", "os.popen('echo unsafe')",
                                       "os.remove(input_path)", "eval('1 + 1')", "exec('pass')"])
def test_api_names_do_not_decide_static_admission(operation):
    validate_script(script(operation, imports="import os\nimport subprocess\nimport importlib"))


KNOWN_SAFE_IDIOMS = [
    pytest.param("import pandas as pd", "", """
        frame = pd.read_csv(input_path)
        renamed = frame.rename(columns={'old': 'new'})
        renamed.to_csv(output_path, index=False)
    """, id="dataframe-rename-is-not-filesystem-rename"),
    pytest.param("import json", "", """
        values = json.loads(input_path.read_text(encoding='utf-8'))
        values.remove('discard')
        output_path.write_text(json.dumps(values), encoding='utf-8')
    """, id="list-remove-is-not-filesystem-remove"),
    pytest.param("import pandas as pd", "", """
        frame = pd.read_csv(input_path)
        text = frame.to_csv(index=False)
        output_path.write_text(text, encoding='utf-8')
    """, id="dataframe-to-csv-without-destination-returns-text"),
    pytest.param("import json", """
        def write_result(destination, result):
            Path(destination).write_text(json.dumps(result), encoding='utf-8')
    """, """
        values = json.loads(input_path.read_text(encoding='utf-8'))
        write_result(output_path, {'count': len(values)})
    """, id="guarded-output-passed-to-write-helper"),
]


@pytest.mark.parametrize("imports,helpers,body", KNOWN_SAFE_IDIOMS[:3])
def test_standard_in_memory_operations_use_normal_library_apis(imports, helpers, body):
    validate_script(script(body, imports=imports, helpers=helpers))

@pytest.mark.parametrize("imports,helpers,body", KNOWN_SAFE_IDIOMS[3:])
def test_helper_io_layout_is_admitted_for_real_execution_validation(imports, helpers, body):
    validate_script(script(body, imports=imports, helpers=helpers))


@pytest.mark.asyncio
async def test_method_description_without_script_does_not_invent_io_or_execution():
    recipe = SimpleNamespace(scripts=[])
    generate, review = AsyncMock(), AsyncMock()
    def revalidate(_):
        pytest.fail("No source exists to revalidate")
    returned, plan, receipt = await verify_skill_script_cases(
        recipe, None, [], generate=generate, review=review, revalidate=revalidate)
    assert returned is recipe and plan is None
    assert receipt == {"status": "not_applicable", "scope": "synthetic", "cases": []}
    generate.assert_not_awaited()
    review.assert_not_awaited()


@pytest.mark.parametrize("parser_source", [
    "import click\n@click.command()\n@click.option('--input')\n@click.option('--output')\ndef main(input, output):\n    write_result(input, output)\nmain()",
    "import sys\nargs = dict(zip(sys.argv[1::2], sys.argv[2::2]))\nwrite_result(args['--input'], args['--output'])",
])
def test_parser_libraries_and_helper_io_do_not_limit_source_admission(parser_source):
    value = script("")
    value.content = "from pathlib import Path\ndef write_result(source, destination):\n    Path(destination).write_bytes(Path(source).read_bytes())\n" + parser_source
    validate_script(value)


def test_multi_input_loop_guard_and_dynamic_open_modes_are_admitted():
    value = script("")
    value.content = """from pathlib import Path
import argparse

def write_output(destination, value):
    mode = 'w'
    with Path(destination).open(mode) as handle:
        handle.write(value)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', nargs='+')
    parser.add_argument('--output')
    args = parser.parse_args()
    destination = Path(args.output).resolve()
    paths = {}
    for index, raw_path in enumerate(args.input):
        selected = Path(raw_path).resolve()
        if selected == destination:
            raise ValueError('input and output must differ')
        paths[index] = selected
    write_output(destination, ''.join(path.read_text() for path in paths.values()))
main()
"""
    validate_script(value)
