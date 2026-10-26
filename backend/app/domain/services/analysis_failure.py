"""Conservative categories from trusted execution diagnostics and saved bytes.

Categories describe the observed failing operation, not a model's explanation.
In particular, TypeError/ValueError alone never attest a serialization failure.
This module does not retain paths, source text, or exception messages.
"""
from __future__ import annotations

import ast
from typing import Literal

ProgramFailureCategory = Literal["dependency", "code", "data", "serialization", "publication", "runtime"]
CODE_DIAGNOSTIC_ERRORS = frozenset({"SyntaxError", "IndentationError", "TabError", "NameError", "UnboundLocalError"})
# Unknown runtime errors require diagnosis, not an unsupported claim that the
# user's data is wrong. Source recovery still needs the latest bound failing
# line, an actual inspection, and an unused trial in the progress guard.
SOURCE_DIAGNOSTIC_CATEGORIES = frozenset({"dependency", "code", "serialization", "publication", "runtime"})
_DATA_ERRORS = frozenset({"ParserError", "EmptyDataError", "UnicodeDecodeError"})
_IO_ERRORS = frozenset({"OSError", "IOError", "FileNotFoundError", "PermissionError", "IsADirectoryError",
                       "NotADirectoryError", "FileExistsError"})


def _direct_call(statement: ast.stmt) -> ast.Call | None:
    value = getattr(statement, "value", None)
    if isinstance(statement, ast.With) and len(statement.items) == 1:
        value = statement.items[0].context_expr
    if not isinstance(value, ast.Call):
        return None
    # Argument evaluation can fail before the named operation starts. Do not
    # label json.dump(data, open(...)) or json.dumps(build()) as JSON errors.
    if any(isinstance(node, ast.Call) for argument in [*value.args, *(kw.value for kw in value.keywords)]
           for node in ast.walk(argument)):
        return None
    return value


_SERIALIZATION_FUNCTIONS = {"json": {"dump", "dumps"},
                            "datalab_json": {"write_json", "normalize_json", "not_estimable"}}


def _serialization_callable(tree: ast.AST, function: ast.expr) -> bool:
    aliases: dict[str, tuple[str, str | None]] = {}
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.Import):
            aliases.update({item.asname or item.name: (item.name, None) for item in node.names
                            if item.name in _SERIALIZATION_FUNCTIONS})
        elif isinstance(node, ast.ImportFrom) and node.module in _SERIALIZATION_FUNCTIONS and node.level == 0:
            aliases.update({item.asname or item.name: (node.module, item.name) for item in node.names
                            if item.name in _SERIALIZATION_FUNCTIONS[node.module]})
    if isinstance(function, ast.Name):
        root = function.id
        binding = aliases.get(root)
        expected = binding and binding[1] in _SERIALIZATION_FUNCTIONS[binding[0]]
    elif isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
        root = function.value.id
        binding = aliases.get(root)
        expected = binding and binding[1] is None and function.attr in _SERIALIZATION_FUNCTIONS[binding[0]]
    else:
        return False
    if not expected:
        return False
    # Conservatively reject shadowed aliases, including local parameters,
    # monkeypatched module attributes, and imports under the same name.
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == root and isinstance(node.ctx, (ast.Store, ast.Del)):
            return False
        if isinstance(node, ast.arg) and node.arg == root:
            return False
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == root:
            return False
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
            value = node.value
            while isinstance(value, ast.Attribute):
                value = value.value
            if isinstance(value, ast.Name) and value.id == root:
                return False
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for item in node.names:
                bound = item.asname or item.name.split(".")[0]
                if bound == root and not ((isinstance(node, ast.Import) and item.name in _SERIALIZATION_FUNCTIONS) or
                    (isinstance(node, ast.ImportFrom) and node.module in _SERIALIZATION_FUNCTIONS and node.level == 0
                     and item.name in _SERIALIZATION_FUNCTIONS[node.module])):
                    return False
    return True


def classify_program_failure(diagnostic: dict | None, *, source_snapshot: dict | None = None,
                             source_digest: str | None = None) -> ProgramFailureCategory:
    """Classify host-validated feedback; callers must authenticate its receipt.

    A category never grants recovery by itself. The progress guard additionally
    requires inspection of the latest failed source and a still-unused trial.
    """
    exception = diagnostic.get("exception_type") if isinstance(diagnostic, dict) else None
    if not isinstance(exception, str):
        return "runtime"
    if exception in CODE_DIAGNOSTIC_ERRORS:
        return "code"
    fallback = "data" if exception in _DATA_ERRORS else "runtime"
    from app.domain.services.program_execution import validated_source_snapshot
    snapshot = validated_source_snapshot(source_snapshot, source_digest) if isinstance(source_digest, str) else None
    line = diagnostic.get("line") if isinstance(diagnostic, dict) else None
    if snapshot is None or type(line) is not int or not 1 <= line <= len(snapshot["content"].splitlines()):
        return fallback
    try:
        tree = ast.parse(snapshot["content"])
    except (SyntaxError, ValueError, RecursionError):
        return fallback
    statements = [node for node in ast.walk(tree) if isinstance(node, ast.stmt)
                  and node.lineno <= line <= (node.end_lineno or node.lineno)]
    if not statements:
        return fallback
    statement = min(statements, key=lambda node: (node.end_lineno or node.lineno) - node.lineno)
    # Python's AttributeError carries the actual receiver type/name. Match that
    # host-captured evidence to the bound source, not arbitrary exception text.
    # Arrays do not implement pandas' .values/.to_numpy/.iloc API; this is a
    # program API mismatch, not evidence that source data needs rereading.
    if (exception == "AttributeError" and diagnostic.get("object_module") == "numpy"
            and diagnostic.get("object_type") == "ndarray"
            and diagnostic.get("attribute_name") in {"values", "to_numpy", "iloc", "loc", "index"}
            and any(isinstance(node, ast.Attribute)
                    and node.attr == diagnostic["attribute_name"]
                    and node.lineno <= line <= (node.end_lineno or node.lineno)
                    for node in ast.walk(statement))):
        return "code"
    if exception in {"ImportError", "ModuleNotFoundError"} and isinstance(statement, (ast.Import, ast.ImportFrom)):
        return "dependency"
    call = _direct_call(statement)
    if call is None or not call.lineno <= line <= (call.end_lineno or call.lineno):
        return fallback
    if exception in {"TypeError", "ValueError", "OverflowError", "RecursionError", "UnicodeError",
                     "UnicodeEncodeError", "UnicodeDecodeError"} and _serialization_callable(tree, call.func):
        return "serialization"
    if exception in _IO_ERRORS and isinstance(call.func, ast.Name) and call.func.id == "open":
        # Only an explicit write mode establishes the output stage. Default or
        # dynamic modes may read inputs and must not gain a source-only retry.
        modes = [kw.value for kw in call.keywords if kw.arg == "mode"] or call.args[1:2]
        shadowed = any(isinstance(node, ast.Name) and node.id == "open" and isinstance(node.ctx, (ast.Store, ast.Del))
                       or isinstance(node, ast.arg) and node.arg == "open"
                       or isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == "open"
                       or isinstance(node, (ast.Import, ast.ImportFrom)) and any(
                           (item.asname or item.name.split(".")[0]) == "open" for item in node.names)
                       for node in ast.walk(tree))
        if not shadowed and len(modes) == 1 and isinstance(modes[0], ast.Constant) and isinstance(modes[0].value, str):
            return "publication" if any(mode in modes[0].value for mode in "wax+") else "data"
    return fallback
