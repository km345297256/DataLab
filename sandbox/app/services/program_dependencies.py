"""Conservative import resolution for the interpreter that launches programs.

This is a prerequisite check, not a claim that imports or scientific methods
will succeed. It never executes the script or imports a discovered package.
Conditional, guarded and lazy imports are informational, never launch blockers.
"""
from __future__ import annotations

import ast
import importlib.machinery
import importlib.metadata
from pathlib import Path
import platform
import sys
import warnings


_CATALOG = (
    ("numpy", "numpy"), ("pandas", "pandas"), ("scipy", "scipy"),
    ("sklearn", "scikit-learn"), ("matplotlib", "matplotlib"),
    ("seaborn", "seaborn"), ("xarray", "xarray"),
    ("pyarrow", "pyarrow"), ("statsmodels", "statsmodels"), ("patsy", "patsy"),
)
_MAX_IMPORTS = 256


def _module_available(name: str, search_path: list[str]) -> bool:
    """Resolve dotted modules without find_spec's implicit parent imports."""
    if (name in {"__main__", "__future__"} or name in sys.builtin_module_names
            or sys.modules.get(name) is not None):
        return True
    parts = name.split(".")
    paths = search_path
    for index in range(len(parts)):
        qualified = ".".join(parts[:index + 1])
        spec = importlib.machinery.PathFinder.find_spec(qualified, paths)
        if spec is None:
            return False
        if index < len(parts) - 1:
            if spec.submodule_search_locations is None:
                return False
            paths = list(spec.submodule_search_locations)
    return True


def _runtime(search_path: list[str]) -> dict:
    packages = []
    for module, distribution in _CATALOG:
        try:
            version = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            version = None
        packages.append({"module": module, "distribution": distribution,
                         "version": version,
                         "available": _module_available(module, search_path)})
    return {"python_version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "packages": packages, "json_writer": "datalab_json.write_json"}


def unchecked_dependencies(*, state: str = "not_checked") -> dict:
    return {"version": 1, "check": "static_import_resolution", "state": state,
            "required_modules": [], "missing_modules": [],
            "optional_modules": [], "optional_missing_modules": [],
            "dynamic_imports": False, "runtime": _runtime(list(sys.path))}


def runtime_capabilities(*, cancelled=None) -> dict:
    """Describe this actual interpreter before authoring; never run user code.

    This is installation/import resolution, not a scientific validation or a
    promise that every submodule can import. The per-program preflight remains
    authoritative for the script directory and requested modules.
    """
    paths = list(sys.path)
    return {"version": 1, "check": "runtime_catalog", "state": "observed",
            **_runtime(paths), "analysis_helpers": {
                name: _module_available(name, paths)
                for name in ("datalab_analysis", "datalab_statistics", "datalab_results")}}


def _catches_import_error(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    names = {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}
    return any(isinstance(node, ast.Name) and node.id in names
               or isinstance(node, ast.Attribute) and node.attr in names
               for node in ast.walk(handler.type))


def inspect_program_dependencies(source: bytes, script_path: str) -> dict:
    with warnings.catch_warnings():
        # Syntax warnings can echo source; the API exposes classifications only.
        warnings.simplefilter("ignore", SyntaxWarning)
        warnings.simplefilter("ignore", DeprecationWarning)
        tree = ast.parse(source, filename="<analysis-program>")
    required: set[str] = set()
    optional: set[str] = set()

    def visit(node: ast.AST, conditional: bool = False) -> None:
        target = optional if conditional else required
        if isinstance(node, ast.Import):
            target.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if not node.level and node.module:
                target.add(node.module)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            for child in ast.iter_child_nodes(node):
                visit(child, True)
        elif isinstance(node, ast.If):
            # A literal condition is safe to evaluate without executing source.
            literal = node.test.value if isinstance(node.test, ast.Constant) else None
            for child in node.body:
                visit(child, conditional or literal is not True)
            for child in node.orelse:
                visit(child, conditional or literal is not False)
        elif isinstance(node, ast.Try) or type(node).__name__ == "TryStar":
            guarded = any(_catches_import_error(handler) for handler in node.handlers)
            for child in node.body:
                visit(child, conditional or guarded)
            for handler in node.handlers:
                for child in handler.body:
                    visit(child, True)
            for child in node.orelse:
                visit(child, True)
            for child in node.finalbody:
                visit(child, conditional)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Match)):
            for child in ast.iter_child_nodes(node):
                visit(child, True)
        else:
            for child in ast.iter_child_nodes(node):
                visit(child, conditional)
        if len(required | optional) > _MAX_IMPORTS or any(len(name) > 256 for name in required | optional):
            raise ValueError("static imports exceed feedback limits")

    visit(tree)
    optional.difference_update(required)
    # BOOTSTRAP replaces sys.path[0] with the actual source directory.
    search_path = [str(Path(script_path).parent), *sys.path[1:]]
    dynamic = any(isinstance(node, ast.Call) and (
        isinstance(node.func, ast.Name) and node.func.id == "__import__"
        or isinstance(node.func, ast.Attribute) and node.func.attr == "import_module"
    ) for node in ast.walk(tree))
    missing = sorted(name for name in required if not _module_available(name, search_path))
    return {"version": 1, "check": "static_import_resolution",
            "state": "missing" if missing else "ready",
            "required_modules": sorted(required), "missing_modules": missing,
            "optional_modules": sorted(optional),
            "optional_missing_modules": sorted(name for name in optional
                                                if not _module_available(name, search_path)),
            "dynamic_imports": dynamic, "runtime": _runtime(search_path)}
