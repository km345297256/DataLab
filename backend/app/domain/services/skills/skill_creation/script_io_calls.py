"""Resolve distinct open/save API signatures without executing generated code.

An import establishes an API identity, not filesystem permission. Callers still
check destination provenance, input/output guards and real sandbox execution.
Unknown receivers do not acquire filesystem effects from their method names.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass


@dataclass(frozen=True)
class OpenCall:
    kind: str
    mode: ast.expr
    destination: ast.expr | None


@dataclass(frozen=True)
class WriteCall:
    destination: ast.expr | None


@dataclass(frozen=True)
class _Binding:
    position: tuple[int, int]
    imported: str | None = None
    expression: ast.expr | None = None


_SCOPES = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
_PATH_TYPES = {"pathlib.Path", "pathlib.PosixPath", "pathlib.WindowsPath"}
_PATH_METHODS = {"resolve", "absolute", "expanduser", "with_name", "with_suffix", "with_stem", "joinpath"}
_IMAGE_FACTORIES = {"PIL.Image.open", "PIL.Image.new", "PIL.Image.fromarray", "PIL.Image.frombytes",
                    "PIL.Image.frombuffer"}
_IMAGE_METHODS = {"convert", "copy", "resize", "rotate", "crop", "transpose", "filter", "quantize",
                  "transform", "reduce", "getchannel"}
# These signatures describe known effects, not API admission. Unrecognized
# libraries and receivers remain admissible and run under filesystem isolation.
_TABLE_FACTORIES = {f"pandas.{name}" for name in (
    "DataFrame", "Series", "read_csv", "read_table", "read_excel", "read_json",
    "read_parquet", "read_feather", "read_pickle", "concat", "merge", "crosstab")}
_TABLE_METHODS = {"copy", "rename", "assign", "drop", "dropna", "drop_duplicates", "fillna",
                  "replace", "astype", "reset_index", "set_index", "sort_values", "sort_index",
                  "merge", "join", "reindex", "head", "tail", "query", "filter", "select_dtypes"}
_TABLE_WRITERS = {"to_csv": "path_or_buf", "to_json": "path_or_buf", "to_parquet": "path",
                  "to_excel": "excel_writer", "to_pickle": "path", "to_feather": "path"}
_MEMORY_RETURNING_WRITERS = {"to_csv", "to_json", "to_parquet"}
_FUNCTION_WRITERS = {
    "numpy.savetxt": (0, "fname"), "numpy.save": (0, "file"),
    "numpy.savez": (0, "file"), "numpy.savez_compressed": (0, "file"),
    "matplotlib.pyplot.savefig": (0, "fname"), "json.dump": (1, "fp"),
    "pickle.dump": (1, "file"), "datalab_json.write_json": (0, "path"),
}


class ScriptIOCalls:
    def __init__(self, tree: ast.Module):
        self.tree = tree
        self.parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        self.bindings: dict[ast.AST, dict[str, list[_Binding]]] = {}
        self.global_names: dict[ast.AST, set[str]] = {}
        self.nonlocal_names: dict[ast.AST, set[str]] = {}
        self.global_mutations: set[str] = set()
        self.mutated_qualified: set[str] = set()
        self.mutated_objects: set[tuple[ast.AST, str]] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Global):
                self.global_names.setdefault(self._scope(node), set()).update(node.names)
            elif isinstance(node, ast.Nonlocal):
                self.nonlocal_names.setdefault(self._scope(node), set()).update(node.names)
        for node in ast.walk(tree):
            scope = self._scope(node)
            line = (getattr(node, "end_lineno", getattr(node, "lineno", 0)),
                    getattr(node, "end_col_offset", getattr(node, "col_offset", 0)))
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    name = alias.asname or (alias.name.split(".")[0] if isinstance(node, ast.Import) else alias.name)
                    imported = (alias.name if alias.asname else alias.name.split(".")[0]) if isinstance(node, ast.Import) else (
                        f"{node.module}.{alias.name}" if not node.level and alias.name != "*" else None)
                    self._add(scope, name, _Binding(line, imported=imported))
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self._add(scope, target.id, _Binding(line, expression=node.value))
                    elif isinstance(target, (ast.Tuple, ast.List)):
                        for index, item in enumerate(target.elts):
                            if isinstance(item, ast.Name):
                                self._add(scope, item.id, _Binding(line, expression=ast.Subscript(
                                    value=node.value, slice=ast.Constant(index), ctx=ast.Load())))
                            else:
                                self._unknown_targets(scope, item, line)
                    else:
                        self._unknown_targets(scope, target, line)
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name):
                    self._add(scope, node.target.id, _Binding(line, expression=node.value))
                else:
                    self._unknown_targets(scope, node.target, line)
            elif isinstance(node, (ast.AugAssign, ast.NamedExpr)):
                self._unknown_targets(scope, node.target, line)
            elif isinstance(node, ast.Delete):
                for target in node.targets:
                    self._unknown_targets(scope, target, line)
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                self._unknown_targets(scope, node.target, line)
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    # __enter__ assigns the target before the body (and before
                    # the next context manager), not after the whole With.
                    entered = (item.context_expr.end_lineno, item.context_expr.end_col_offset)
                    if isinstance(item.optional_vars, ast.Name):
                        self._add(scope, item.optional_vars.id, _Binding(entered, expression=item.context_expr))
                    elif item.optional_vars:
                        self._unknown_targets(scope, item.optional_vars, entered)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self._add(scope, node.name, _Binding(line))
                if not isinstance(node, ast.ClassDef):
                    for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs,
                                *([node.args.vararg] if node.args.vararg else []),
                                *([node.args.kwarg] if node.args.kwarg else [])):
                        self._add(node, arg.arg, _Binding((-1, -1)))
            elif isinstance(node, ast.Lambda):
                for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs,
                            *([node.args.vararg] if node.args.vararg else []),
                            *([node.args.kwarg] if node.args.kwarg else [])):
                    self._add(node, arg.arg, _Binding((-1, -1)))
            elif isinstance(node, ast.ExceptHandler) and node.name:
                self._add(scope, node.name, _Binding(line))
            elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
                self._add(scope, node.name, _Binding(line))
            elif isinstance(node, ast.MatchMapping) and node.rest:
                self._add(scope, node.rest, _Binding(line))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
                qualified = self._qualified(node)
                if qualified:
                    self.mutated_qualified.add(qualified)
                if isinstance(node.value, ast.Name):
                    self.mutated_objects.add((self._scope(node), node.value.id))

    def _scope(self, node):
        parent = self.parents.get(node)
        while parent is not None and not isinstance(parent, _SCOPES):
            parent = self.parents.get(parent)
        return parent or self.tree

    def _add(self, scope, name, binding):
        if name in self.global_names.get(scope, ()) and scope is not self.tree:
            self.global_mutations.add(name)
        self.bindings.setdefault(scope, {}).setdefault(name, []).append(binding)

    def _unknown_targets(self, scope, target, line):
        for node in ast.walk(target):
            if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                self._add(scope, node.id, _Binding(line))

    def _lookup(self, node: ast.Name):
        scope = self._scope(node)
        origin_scope = scope
        if node.id in self.global_mutations:
            return []
        if node.id in self.global_names.get(scope, ()):
            scope = self.tree
        elif node.id in self.nonlocal_names.get(scope, ()):
            # General nonlocal mutation is intentionally not a proof of API
            # identity; a caller may still use a direct imported API instead.
            return []
        while scope is not None:
            # A function can run after later module/outer-scope assignments.
            # Their source position cannot prove an earlier binding unchanged.
            deferred = origin_scope is not scope
            available = lambda record: deferred or record.position < (node.lineno, node.col_offset)
            if any(available(record) for record in self.bindings.get(scope, {}).get("*", ())):
                return []
            records = self.bindings.get(scope, {}).get(node.id)
            if records is not None:
                return [record for record in records if available(record)]
            if scope is self.tree:
                break
            scope = self._scope(scope)
            # Function lexical lookup does not inherit a class namespace.
            if isinstance(scope, ast.ClassDef):
                scope = self._scope(scope)
        return None

    def _name_value(self, node, resolver, seen):
        records = self._lookup(node)
        if records is None:
            return "builtins.open" if resolver == "qualified" and node.id == "open" else None
        if not records:
            return None
        values = []
        for record in records:
            if record.imported is not None:
                value = record.imported if resolver == "qualified" else None
            elif record.expression is not None:
                value = (self._qualified(record.expression, seen) if resolver == "qualified"
                         else self._kind(record.expression, seen))
            else:
                value = None
            values.append(value)
        return values[0] if values[0] is not None and all(value == values[0] for value in values) else None

    def _qualified(self, node, seen=frozenset()):
        if id(node) in seen:
            return None
        seen = seen | {id(node)}
        if isinstance(node, ast.Name):
            return self._name_value(node, "qualified", seen)
        if isinstance(node, ast.Attribute):
            parent = self._qualified(node.value, seen)
            qualified = f"{parent}.{node.attr}" if parent else None
            return None if qualified in self.mutated_qualified else qualified
        return None

    def _kind(self, node, seen=frozenset()):
        if id(node) in seen:
            return None
        seen = seen | {id(node)}
        if isinstance(node, ast.Name):
            if (self._scope(node), node.id) in self.mutated_objects:
                return None
            return self._name_value(node, "kind", seen)
        if isinstance(node, ast.Call):
            qualified = self._qualified(node.func)
            if qualified in _PATH_TYPES:
                return "path"
            if qualified in _IMAGE_FACTORIES:
                return "image"
            if qualified in _TABLE_FACTORIES:
                return "table"
            if qualified in {"io.StringIO", "io.BytesIO"}:
                return "memory"
            if qualified in {"matplotlib.pyplot.figure", "matplotlib.pyplot.gcf", "matplotlib.figure.Figure"}:
                return "figure"
            if isinstance(node.func, ast.Attribute):
                parent = self._kind(node.func.value, seen)
                if parent == "path" and node.func.attr in _PATH_METHODS:
                    return "path"
                if parent == "image" and node.func.attr in _IMAGE_METHODS:
                    return "image"
                if parent == "table" and node.func.attr in _TABLE_METHODS:
                    return "table"
        if isinstance(node, ast.Subscript):
            if self._kind(node.value, seen) == "table":
                return "table"
            if (isinstance(node.value, ast.Call) and self._qualified(node.value.func) == "matplotlib.pyplot.subplots"
                    and isinstance(node.slice, ast.Constant) and node.slice.value == 0):
                return "figure"
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div) and self._kind(node.left, seen) == "path":
            return "path"
        if isinstance(node, ast.Attribute) and node.attr == "parent" and self._kind(node.value, seen) == "path":
            return "path"
        return None

    @staticmethod
    def _argument(call, index, keyword, default=None):
        if len(call.args) > index:
            return call.args[index]
        return next((item.value for item in call.keywords if item.arg == keyword), default)

    def classify_open(self, call: ast.Call) -> OpenCall | None:
        qualified = self._qualified(call.func)
        expanded = any(isinstance(item, ast.Starred) for item in call.args) or any(
            item.arg is None for item in call.keywords)
        if qualified in {"builtins.open", "io.open", "PIL.Image.open"}:
            if expanded:
                return OpenCall("unknown", ast.Constant(None), None)
            kind, keyword = ("image", "fp") if qualified == "PIL.Image.open" else ("file", "file")
            destination = self._argument(call, 0, keyword)
            mode = self._argument(call, 1, "mode", ast.Constant("r"))
            return OpenCall(kind if destination is not None else "unknown", mode, destination)
        if isinstance(call.func, ast.Attribute) and call.func.attr == "open":
            if (self._kind(call.func.value) == "path"
                    and not any(f"{name}.open" in self.mutated_qualified for name in _PATH_TYPES)):
                if expanded:
                    return OpenCall("unknown", ast.Constant(None), None)
                return OpenCall("path", self._argument(call, 0, "mode", ast.Constant("r")), call.func.value)
        return None

    def classify_write(self, call: ast.Call) -> WriteCall | None:
        """Identify a proven file writer; unknown API names confer no effects."""
        qualified = self._qualified(call.func)
        signature = _FUNCTION_WRITERS.get(qualified)
        function = self._callable_expression(call.func)
        if signature is not None:
            destination = self._argument(call, *signature)
        elif isinstance(function, ast.Attribute):
            receiver, name = function.value, function.attr
            kind = self._kind(receiver)
            if (kind == "path" and name in {"write_text", "write_bytes", "mkdir", "touch"}
                    and not any(f"{path_type}.{name}" in self.mutated_qualified for path_type in _PATH_TYPES)):
                destination = receiver
            elif kind == "image" and name == "save":
                destination = self._argument(call, 0, "fp")
            elif kind == "figure" and name == "savefig":
                destination = self._argument(call, 0, "fname")
            elif kind == "table" and name in _TABLE_WRITERS:
                destination = self._argument(call, 0, _TABLE_WRITERS[name])
                if (name in _MEMORY_RETURNING_WRITERS
                        and (destination is None or isinstance(destination, ast.Starred)
                             or isinstance(destination, ast.Constant) and destination.value is None)):
                    # Unpacked options without an explicit destination do not
                    # prove a file write; execution determines the real effect.
                    return None
            else:
                return None
        else:
            return None
        if destination is not None and self._kind(destination) == "memory":
            return None
        return WriteCall(destination)

    def _callable_expression(self, node: ast.expr, seen=frozenset()) -> ast.expr:
        if not isinstance(node, ast.Name) or id(node) in seen:
            return node
        records = self._lookup(node)
        if records and len(records) == 1 and records[0].expression is not None:
            return self._callable_expression(records[0].expression, seen | {id(node)})
        return node

    def save_destination(self, call: ast.Call) -> ast.expr | None:
        if (isinstance(call.func, ast.Attribute) and call.func.attr == "save"
                and self._kind(call.func.value) == "image"):
            return self._argument(call, 0, "fp")
        return None
