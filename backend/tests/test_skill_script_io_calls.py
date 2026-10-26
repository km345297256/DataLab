"""Optional API classification is descriptive, never a static safety certificate."""
import ast
from textwrap import dedent

import pytest

from app.domain.services.skills.skill_creation.script_io_calls import ScriptIOCalls
from app.domain.services.skills.skill_creation.script_policy import validate_script
from test_skill_general_authoring_matrix import script


def inspect_open(source):
    tree = ast.parse(dedent(source))
    classifier = ScriptIOCalls(tree)
    calls = sorted((node for node in ast.walk(tree) if isinstance(node, ast.Call)),
                   key=lambda node: (node.lineno, node.col_offset))
    results = [(ast.unparse(call), classifier.classify_open(call)) for call in calls]
    return [(source, result) for source, result in results if result is not None]


@pytest.mark.parametrize("source,kind,mode,destination", [
    ("open(path)", "file", "r", "path"),
    ("open(file=path, mode='wb')", "file", "wb", "path"),
    ("import builtins as b; b.open(path, 'a')", "file", "a", "path"),
    ("from builtins import open as read; read(path)", "file", "r", "path"),
    ("import io; io.open(path, 'w')", "file", "w", "path"),
    ("import io as files; files.open(file=path, mode='r+')", "file", "r+", "path"),
    ("from io import open as write; write(path, 'x')", "file", "x", "path"),
    ("from io import open as read; alias = read; alias(path)", "file", "r", "path"),
    ("from pathlib import Path; Path(path).open('w')", "path", "w", "Path(path)"),
    ("import pathlib as p; p.Path(path).open(mode='rb')", "path", "rb", "p.Path(path)"),
    ("from pathlib import Path as P; output = P(path).resolve() / 'result'; output.open('w')",
     "path", "w", "output"),
    ("from pathlib import Path; p = Path(path); p = p.resolve(); p.open()", "path", "r", "p"),
    ("from pathlib import Path; p = Path(path).parent.joinpath('result').with_suffix('.txt'); p.open('w')",
     "path", "w", "p"),
    ("from PIL import Image; Image.open(path)", "image", "r", "path"),
    ("import PIL.Image as images; images.open(fp=path, mode='r')", "image", "r", "path"),
    ("import PIL.Image; PIL.Image.open(path, 'r')", "image", "r", "path"),
    ("from PIL.Image import open as load_image; load_image(fp=path)", "image", "r", "path"),
])
def test_known_apis_keep_their_distinct_argument_positions(source, kind, mode, destination):
    results = inspect_open(source)
    assert len(results) == 1
    call = results[0][1]
    assert call.kind == kind
    assert isinstance(call.mode, ast.Constant) and call.mode.value == mode
    assert ast.unparse(call.destination) == destination


@pytest.mark.parametrize("source", [
    "resource.open('w')",
    "from unknown_library import open; open(path)",
    "from unknown_library import open as read; read(path)",
    "open = loader; open(path)",
    "from pathlib import Path; p = Path(path); p = external; p.open('r')",
    "from pathlib import Path; p = Path(path); p.open = external; p.open('r')",
    "from pathlib import Path; Path.open = external; Path(path).open('r')",
    "import io; io.open = external; io.open(path)",
    "from PIL import Image; Image = external; Image.open(path)",
    "from PIL import Image; Image.open = external; Image.open(path)",
    "from PIL import Image; del Image; Image.open(path)",
    "from unknown_library import *; open(path)",
])
def test_unknown_or_mutated_api_does_not_gain_file_semantics_from_its_name(source):
    assert inspect_open(source) == []


@pytest.mark.parametrize("source", [
    "open(*arguments)",
    "open(path, **options)",
    "from pathlib import Path; Path(path).open(**options)",
    "from PIL import Image; Image.open(path, **options)",
    "open()",
    "from PIL import Image; Image.open()",
])
def test_known_file_api_with_unresolved_arguments_keeps_unknown_file_semantics(source):
    results = inspect_open(source)
    assert len(results) == 1 and results[0][1].kind == "unknown"



@pytest.mark.parametrize("source", [
    """
    from pathlib import Path
    def convert(Path):
        Path('file').open('r')
    """,
    """
    from PIL import Image
    def convert():
        Image.open('file')
        Image = external
    """,
    """
    from PIL import Image
    def convert():
        Image.open('file')
    Image = external
    convert()
    """,
    """
    import io
    def mutate():
        global io
        io = external
    io.open('file')
    """,
])
def test_runtime_function_scope_and_later_global_rebinding_are_not_proofs(source):
    results = inspect_open(source)
    assert results == []


def test_same_names_in_unrelated_functions_do_not_destroy_import_identity():
    results = inspect_open("""
        from PIL import Image
        def unrelated(Image):
            return Image
        def convert(path):
            Image.open(path)
    """)
    assert len(results) == 1 and results[0][1].kind == "image"


@pytest.mark.parametrize("source", [
    "from PIL import Image; image = Image.open(source); image.save(fp=target)",
    "import PIL.Image as Image; Image.open(source).convert('RGB').save(fp=target)",
    "from PIL import Image; image = Image.new('RGB', (4, 4)); image.save(target)",
    """
    from PIL import Image
    with Image.open(source) as image:
        converted = image.convert('RGB')
        converted.save(fp=target)
    """,
    """
    from PIL import Image
    with Image.open(source) as image, image.copy() as result:
        result.save(fp=target)
    """,
])
def test_pil_save_fp_is_recognized_only_from_proven_image_receiver(source):
    tree = ast.parse(dedent(source))
    classifier = ScriptIOCalls(tree)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute) and node.func.attr == "save"]
    assert len(calls) == 1
    assert ast.unparse(classifier.save_destination(calls[0])) == "target"


@pytest.mark.parametrize("source", [
    "object.save(fp=target)",
    "from PIL import Image; image = external; image.save(fp=target)",
    "from PIL import Image; image = Image.open(source); image = external; image.save(fp=target)",
    "from PIL import Image; image = Image.open(source); image.save = external; image.save(fp=target)",
])
def test_arbitrary_save_keyword_does_not_get_pil_semantics(source):
    tree = ast.parse(dedent(source))
    classifier = ScriptIOCalls(tree)
    call = next(node for node in ast.walk(tree) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute) and node.func.attr == "save")
    assert classifier.save_destination(call) is None


def test_non_io_call_does_not_get_classified_by_its_argument_shape():
    tree = ast.parse("library.compute(path, mode='w')")
    assert ScriptIOCalls(tree).classify_open(tree.body[0].value) is None


@pytest.mark.parametrize("imports,body", [
    ("import io", "with io.open(file=input_path, mode='r') as source:\n    text = source.read()\nwith io.open(file=output_path, mode='w') as destination:\n    destination.write(text)"),
    ("from io import open as file_open", "with file_open(input_path) as source:\n    text = source.read()\nwith file_open(output_path, 'w') as destination:\n    destination.write(text)"),
    ("from PIL import Image", "image = Image.open(fp=input_path)\nimage.convert('RGB').save(fp=output_path)"),
    ("from PIL import Image", "with Image.open(input_path) as image:\n    image.save(fp=output_path)"),
    ("from PIL.Image import open as load_image", "with load_image(input_path) as image:\n    image.save(fp=output_path)"),
])
def test_static_admission_accepts_varied_io_syntax(imports, body):
    validate_script(script(body, imports=imports))


@pytest.mark.parametrize("imports,body", [
    ("import io", "io.open(file=input_path, mode='w')"),
    ("from io import open as write", "write(input_path, 'w')"),
    ("import builtins", "builtins.open(input_path, 'r+')"),
    ("from PIL import Image", "image = Image.open(input_path)\nimage.save(fp=input_path)"),
    ("from PIL import Image", "with Image.open(input_path) as image:\n    image.convert('RGB').save(fp=input_path)"),
    ("from PIL import Image", "with Image.open(input_path) as image:\n    image.save(fp=input_path.parent / 'new.png')"),
])
def test_static_admission_does_not_infer_input_safety_from_recognized_io(imports, body):
    candidate = script(body, imports=imports)
    original = candidate.content
    validate_script(candidate)
    # No generated code runs here. The real worker must independently reject
    # input mutation; static admission must not claim that a write is safe.
    assert candidate.content == original


@pytest.mark.parametrize("body", ["open(input_path, mode=chosen_mode)",
                                 "open(input_path, **options)"])
def test_static_admission_leaves_dynamic_mode_behavior_to_real_execution(body):
    validate_script(script(body))


@pytest.mark.parametrize("body", [
    "resource.open(input_path)", "resource.open('w')", "resource.save()",
    "resource.save(input_path)", "resource.save(fp=input_path)",
    "resource.to_csv(input_path)", "resource.write_text(input_path)",
    "from arbitrary_library import open as load; load(input_path)",
    "from arbitrary_library import dump; dump({'value': 1}, input_path)",
    "import numpy as np; writer = external; writer.save(input_path)",
    "import io; io.open = external; io.open(input_path, 'w')",
])
def test_unproven_api_identity_is_admitted_without_inventing_a_file_write(body):
    validate_script(script(body))


@pytest.mark.parametrize("body", [
    "frame.to_csv()", "frame.to_csv(index=False)", "frame.to_csv(path_or_buf=None)",
    "frame.to_csv(**format_options)", "frame.to_csv(*format_arguments)",
    "frame.to_json()", "frame.to_json(None)", "frame.to_parquet(path=None)",
    "frame.to_csv(io.StringIO())", "frame.to_json(io.StringIO())",
    "frame.to_parquet(io.BytesIO())", "buffer = io.StringIO(); frame.to_csv(buffer)",
    "buffer = io.StringIO(); json.dump({'value': 1}, buffer)",
    "buffer = io.BytesIO(); np.save(buffer, [1, 2])",
    "buffer = io.BytesIO(); image = Image.new('RGB', (2, 2)); image.save(buffer, format='PNG')",
])
def test_serialization_to_values_or_memory_buffers_is_not_a_file_write(body):
    validate_script(script("frame = pd.DataFrame({'value': [1]})\n" + body,
        imports="import pandas as pd\nimport io\nimport json\nimport numpy as np\nfrom PIL import Image"))


@pytest.mark.parametrize("statement", [
    "frame.to_csv(DEST)", "frame.to_json(path_or_buf=DEST)", "frame.to_parquet(DEST)",
    "frame.to_excel(excel_writer=DEST)", "frame.to_pickle(DEST)",
    "writer = frame.to_csv; writer(DEST)",
    "figure, axes = plt.subplots(); figure.savefig(DEST)",
    "writer = Path(DEST).write_text; writer('changed')",
])
def test_writer_aliases_do_not_create_an_ast_admission_rule(statement):
    imports = "import pandas as pd\nimport matplotlib.pyplot as plt"
    body = "frame = pd.DataFrame({'value': [1]})\n" + statement
    for destination in ("input_path", "output_path"):
        candidate = script(body.replace("DEST", destination), imports=imports)
        original = candidate.content
        validate_script(candidate)
        assert candidate.content == original
