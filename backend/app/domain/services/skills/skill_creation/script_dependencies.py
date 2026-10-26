"""Dependency admission is open; availability is verified by the real runtime.

This module does not import candidate dependencies or probe the API process's
installed packages. Neither would establish availability in the isolated skill
execution environment.
"""
from __future__ import annotations

import ast


def dependency_capabilities() -> dict:
    return {
        "admission": "open",
        "api_admission": "open",
        "availability": "not_established_by_static_validation",
        "runtime_check": "Imports and required tools must work in the actual isolated execution environment. "
            "An import failure requires runtime diagnosis: the dependency may be missing, incompatible, or fail during loading. The exception type alone does not prove a package is absent; it never implies a prohibited library.",
        "authoring": "Use suitable existing libraries and their documented APIs. Do not reimplement library "
            "functionality to satisfy an import or API allowlist: library names, module exports, attributes and "
            "dynamic calls are unrestricted. Declare dependencies honestly; do not claim they are installed. "
            "Actual execution must preserve read-only inputs and isolated outputs.",
    }


def validate_python_module_syntax(tree: ast.AST) -> None:
    """Check Python's own compilation rules without executing or importing it.

    ``ast.parse`` alone accepts misplaced future imports and function-local
    wildcard imports. These are Python syntax errors, not dependency decisions.
    """
    compile(tree, "<generated-skill>", "exec", dont_inherit=True)
