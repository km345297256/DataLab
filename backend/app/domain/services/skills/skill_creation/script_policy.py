"""Static portability checks for generated skill scripts.

This module accepts only the three source fields needed for validation. It has
no model, persistence or recipe dependencies and must never execute the script.
Static acceptance does not certify scientific correctness or runtime safety;
the independent isolated-worker verification remains required.
"""
from __future__ import annotations

import ast
import shlex
from typing import Protocol

from .script_dependencies import (
    dependency_capabilities, validate_python_module_syntax,
)


class ScriptSource(Protocol):
    path: str
    content: str
    invocation: str


class SkillScriptValidationError(ValueError):
    """Fixed repair feedback; never includes generated code or private values."""

    # Historical codes remain readable for previously persisted failures. New
    # admission does not infer filesystem safety from source-code shape.
    HINTS = {
        "script_syntax_invalid": "Return a complete Python module that parses and compiles without executing it.",
        "script_dependency_unsupported": "Dependency admission is open. Check actual dependency availability in the isolated execution environment.",
        "script_arguments_missing": "Preserve the declared public input/output parameters and verify their actual behavior in isolated execution; no CLI parsing library is required.",
        "script_distinct_path_guard_missing": "Ensure selected inputs remain unchanged and reject output aliases of inputs before writing. Verify this behavior through isolated execution, independently of the code structure.",
        "script_output_destination_unknown": "Use the declared output location and verify that generated artifacts remain within its isolated writable space.",
        "script_open_mode_unknown": "Verify that input access is read-only and writes target the declared isolated output space.",
        "script_output_not_isolated": "Preserve read-only inputs and keep generated outputs in the declared isolated writable space. Verify actual file effects; helper layout and variable names are not safety evidence.",
        "script_invocation_invalid": "Provide a shell-tokenizable invocation containing the exact declared script path and the --input and --output options, with either separate values or --option=value syntax.",
    }

    def __init__(self, code: str, message: str, *, details: dict | None = None):
        self.code = code
        self.details = details or {}
        self.repair_hint = self.HINTS[code]
        super().__init__(message)


def script_authoring_capabilities(source_form: str) -> dict:
    """Public static policy, derived once for every source-authoring stage.

    Declaring support is not execution evidence. Installed dependencies, full
    method fidelity and real outputs still need the normal isolated checks.
    """
    if source_form not in {"complete_script", "module_manifest", "function", "repair_selection"}:
        raise ValueError("Unknown source authoring form")
    return {
        "version": 4,
        "source_form": source_form,
        "dependency_policy": dependency_capabilities(),
        "future_imports": {"mode": "python_syntax", "placement": "module_prefix"}
            if source_form == "complete_script" else {"mode": "excluded_from_modular_assembly"},
        "static_rules": {key: SkillScriptValidationError.HINTS[key] for key in (
            "script_syntax_invalid", "script_invocation_invalid")},
        "runtime_contract": {
            "static_acceptance": "Only Python compilation and the declared invocation are checked statically; this does not establish dependency availability, installation readiness, or successful execution.",
            "inputs": "Expose only selected inputs through read-only mounts and verify their content hashes remain unchanged during isolated execution.",
            "outputs": "Keep writable output space separate from mounted inputs; verify real outputs and input/output alias rejection in isolated execution.",
            "parameters": "Verify that declared parameters are actually read and affect behavior according to the original input contract; any CLI parsing library and helper organization may be used.",
            "multiple_files": "For a newly designed multi-file entry point, pass one JSON input manifest through --input; role values bind currently selected files. Synthetic JSON fixtures may use exact {input:filename} references recursively materialized by the worker. Do not concatenate role={input:filename} argv values or repeat --input. The manifest binds files only and must preserve original data fields and configuration locations. Do not rewrite an existing explicit CLI contract; report unsupported verification interfaces instead.",
            "method": "Independent source/method review and real synthetic behavior checks remain required; neither source shape nor model self-report proves correctness.",
        },
        "imports_in_function_body_allowed": source_form == "complete_script",
        "execution_validation_required": True,
    }


def validate_supported_imports(tree: ast.AST) -> None:
    """Compatibility entry point: every dependency is admitted.

    Only Python language syntax is checked here. Static validation cannot know
    whether a library is installed in the separate execution environment.
    """
    try:
        validate_python_module_syntax(tree)
    except (SyntaxError, ValueError, TypeError, RecursionError) as exc:
        raise SkillScriptValidationError(
            "script_syntax_invalid", "生成的辅助脚本不符合 Python 语法，未保存技能。"
        ) from exc


def invocation_arguments(script: ScriptSource) -> list[str]:
    """Read only declared argv tokens, without interpreting or running a shell.

    This describes the public invocation, not the implementation's parser or
    parameter effects. Those remain independent review/execution obligations.
    Only tokens after the exact script path contribute options; ``--`` ends
    option parsing. Text containing a flag as a substring is not a declaration.
    """
    try:
        tokens = shlex.split(script.invocation, comments=False, posix=True)
        start = tokens.index(script.path) + 1
    except (ValueError, TypeError) as exc:
        raise SkillScriptValidationError(
            "script_invocation_invalid", "生成的辅助脚本缺少有效的参数化调用说明，未保存技能。"
        ) from exc
    arguments = set()
    for index, token in enumerate(tokens[start:], start=start):
        if token == "--":
            break
        option, separator, value = token.partition("=")
        if (option.startswith("--") and len(option) > 2
                and all(character.isascii() and (character.isalnum() or character in "-_")
                        for character in option[2:]) and (not separator or value)):
            if option in {"--input", "--output"} and not separator:
                if index + 1 >= len(tokens) or not tokens[index + 1] or tokens[index + 1].startswith("--"):
                    raise SkillScriptValidationError(
                        "script_invocation_invalid", "生成的辅助脚本缺少有效的参数化调用说明，未保存技能。"
                    )
            arguments.add(option)
    if not {"--input", "--output"}.issubset(arguments):
        raise SkillScriptValidationError(
            "script_invocation_invalid", "生成的辅助脚本缺少有效的参数化调用说明，未保存技能。"
        )
    return sorted(arguments)


def validate_script(script: ScriptSource) -> None:
    """Check compilation and declared invocation, never execute source code.

    No control-flow, alias, parser, writer or helper pattern is a safety proof.
    Input immutability, output isolation, real parameter behavior and scientific
    fidelity must pass the existing isolated worker and independent reviews.
    """
    try:
        tree = ast.parse(script.content, filename=script.path)
    except (SyntaxError, ValueError, TypeError, RecursionError) as exc:
        raise SkillScriptValidationError("script_syntax_invalid", "生成的辅助脚本语法无效，未保存技能。") from exc
    validate_supported_imports(tree)
    invocation_arguments(script)


# Compatibility for the existing caller/test contract during module migration.
_validate_script = validate_script
