"""Failure categories need a bound operation, not an exception-name guess."""
import hashlib

import pytest

from app.domain.services.analysis_failure import classify_program_failure


def category(source, exception, line, **diagnostic):
    digest = hashlib.sha256(source.encode()).hexdigest()
    snapshot = {"version": 1, "encoding": "utf-8", "size_bytes": len(source.encode()),
                "sha256": digest, "content": source}
    return classify_program_failure({"exception_type": exception, "line": line, **diagnostic},
                                    source_snapshot=snapshot, source_digest=digest)


@pytest.mark.parametrize("source,line", [
    ("import sklearn\n", 1), ("from scipy.stats import fisher_exact\n", 1),
])
def test_missing_dependency_is_bound_to_import_statement(source, line):
    assert category(source, "ModuleNotFoundError", line) == "dependency"


@pytest.mark.parametrize("source,line", [
    ("import json\nencoded = json.dumps(payload)\n", 2),
    ("import json as j\nj.dump(payload, handle)\n", 2),
    ("from json import dumps as encode\nresult = encode(payload)\n", 2),
    ("import json\nresult = json.dumps(\n    payload,\n    allow_nan=False,\n)\n", 3),
])
@pytest.mark.parametrize("exception", ["TypeError", "ValueError", "OverflowError", "RecursionError"])
def test_standard_json_direct_operation_has_serialization_category(source, line, exception):
    assert category(source, exception, line) == "serialization"


@pytest.mark.parametrize("source,line", [
    ("import json\nvalue = parse(payload)\n", 2),
    ("import json\nfor (a,b,c), value in table.stack().items():\n    pass\n", 2),
    ("import json\nvalue = json.dumps(build())\n", 2),
    ("import json\njson.dump(payload, open(path, 'w'))\n", 2),
    ("json.dumps(payload)\n", 1),
    ("import json\njson = custom\njson.dumps(payload)\n", 3),
    ("import json\njson.dumps = custom\njson.dumps(payload)\n", 3),
    ("import json\ndef run(json):\n    return json.dumps(payload)\n", 3),
    ("import json\nimport fake as json\njson.dumps(payload)\n", 3),
    ("from json import dumps\ndef dumps(payload):\n    pass\ndumps(payload)\n", 4),
    ("import json\nvalue = custom() + json.dumps(payload)\n", 2),
    ("import json\nvalue = json.loads(payload)\n", 2),
])
@pytest.mark.parametrize("exception", ["TypeError", "ValueError"])
def test_similar_names_unrelated_lines_nested_calls_and_shadowing_are_not_serialization(source, line, exception):
    assert category(source, exception, line) == "runtime"


@pytest.mark.parametrize("fault", ["missing-snapshot", "wrong-digest", "wrong-line", "bool-line", "missing-line"])
def test_unbound_diagnostics_never_infer_serialization_or_dependency(fault):
    for source, exception, expected in [("import json\njson.dumps(payload)\n", "TypeError", "runtime"),
                                        ("import sklearn\n", "ModuleNotFoundError", "runtime")]:
        digest = hashlib.sha256(source.encode()).hexdigest()
        snapshot = {"version": 1, "encoding": "utf-8", "size_bytes": len(source.encode()),
                    "sha256": digest, "content": source}
        diagnostic = {"exception_type": exception, "line": len(source.splitlines())}
        if fault == "missing-snapshot": snapshot = None
        if fault == "wrong-digest": digest = "0" * 64
        if fault == "wrong-line": diagnostic["line"] = 999
        if fault == "bool-line": diagnostic["line"] = True
        if fault == "missing-line": diagnostic.pop("line")
        assert classify_program_failure(diagnostic, source_snapshot=snapshot, source_digest=digest) == expected


@pytest.mark.parametrize("source,expected", [
    ("handle = open(path, 'w')\n", "publication"),
    ("handle = open(path, mode='wb')\n", "publication"),
    ("handle = open(path, 'r')\n", "data"),
    ("handle = open(path)\n", "runtime"),
    ("handle = open(path, mode)\n", "runtime"),
    ("handle = open(resolve(), 'w')\n", "runtime"),
    ("open = custom\nhandle = open(path, 'w')\n", "runtime"),
    ("from custom import open\nhandle = open(path, 'w')\n", "runtime"),
])
def test_output_write_error_requires_explicit_write_operation(source, expected):
    assert category(source, "PermissionError", len(source.splitlines())) == expected


def test_structured_names_or_messages_cannot_self_attest_category():
    assert category("compute()\n", "ValueError", 1, category="serialization", stage="json") == "runtime"
    assert category("compute()\n", "ModuleNotFoundError", 1) == "runtime"


def test_context_manager_output_open_is_publication_stage():
    assert category("with open(path, 'w') as handle:\n    pass\n", "PermissionError", 1) == "publication"


@pytest.mark.parametrize("function,arguments", [
    ("write_json", "path, payload"), ("normalize_json", "payload"), ("not_estimable", "reason"),
])
def test_sandbox_structured_json_helpers_are_explicit_serialization_operations(function, arguments):
    for source in [f"import datalab_json as result_json\nresult_json.{function}({arguments})\n",
                   f"from datalab_json import {function} as convert\nconvert({arguments})\n"]:
        assert category(source, "ValueError", 2) == "serialization"
        assert category(source, "OSError", 2) == "runtime"
    assert category(f"import custom as datalab_json\ndatalab_json.{function}({arguments})\n", "ValueError", 2) == "runtime"


@pytest.mark.parametrize("exception", [None, [], {}, 1, True])
def test_malformed_exception_type_has_no_inferred_category(exception):
    assert classify_program_failure({"exception_type": exception}) == "runtime"


@pytest.mark.parametrize("exception", ["TypeError", "ValueError", "AttributeError", "AssertionError", "IndexError", "KeyError"])
def test_generic_failure_does_not_blame_data_or_require_input_reread(exception):
    from app.domain.services.analysis_failure import SOURCE_DIAGNOSTIC_CATEGORIES
    assert category("result = calculate(frame)\n", exception, 1) == "runtime"
    assert "runtime" in SOURCE_DIAGNOSTIC_CATEGORIES


def test_numpy_pandas_api_mismatch_requires_actual_receiver_and_source():
    evidence = {"attribute_name": "values", "object_type": "ndarray", "object_module": "numpy"}
    assert category("result = residuals.values\n", "AttributeError", 1, **evidence) == "code"
    assert category("result = residuals.other\n", "AttributeError", 1, **evidence) == "runtime"
    assert category("result = residuals.values\n", "AttributeError", 1) == "runtime"
    assert category("result = residuals.values\n", "AttributeError", 1,
                    **{**evidence, "object_module": "custom"}) == "runtime"


def test_transpose_syntax_is_not_itself_evidence_of_bad_data_or_code():
    # dat.T can be deliberate; its type failure needs diagnosis instead of a
    # guessed change of analysis method or reread of all original files.
    assert category("result = np.where(dat.T == 'T2', 1, -1)\n", "TypeError", 1) == "runtime"
