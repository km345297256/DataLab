"""Validate declarative argv without expanding or rewriting argument values.

The worker substitutes only complete top-level input/output references and
passes every other value as one unchanged subprocess argument. Content, JSON
syntax and configuration semantics belong to the tested CLI, not this transport
boundary. The original strings remain part of the frozen validation plan.
"""
from __future__ import annotations

import re
from typing import Iterable, Literal

from pydantic_core import PydanticCustomError

ArgumentKind = Literal["input", "output", "literal"]
_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")
_INPUT = re.compile(r"\{input:([A-Za-z0-9][A-Za-z0-9_.-]{0,95})\}\Z")
ARGUMENT_ERROR_HINTS = {
    "validation_argument_control_character": "CLI argument strings cannot contain NUL; preserve all other intended data, including empty strings and line breaks.",
    "validation_argument_unicode": "CLI argument strings must contain valid Unicode encodable as UTF-8; preserve their intended text without normalization.",
    "validation_argument_input_reference": "Use an exact top-level {input:filename} reference naming a declared fixture; do not append paths or surrounding text.",
    "validation_argument_output_reference": "Use {output} or {output}/safe-relative-filename; remove traversal, absolute paths and empty path segments.",
    "validation_argument_io_binding": "Include --input and --output exactly once, bound to their corresponding top-level placeholders. Only an invalid case expecting a nonzero exit with no assertions or only output_empty/stderr_contains rejection postconditions may bind --output to exactly the same declared fixture as --input to test rejection. Scientific CSV/JSON output assertions are not allowed in this probe. output_empty checks the isolated output workspace, never the read-only input mount. No other input alias or real path is allowed. JSON configuration cannot replace either binding.",
}


def _error(rule: str, message: str) -> None:
    # Both arguments are fixed developer-authored strings. Never include a
    # rejected argument or source filename in repair feedback.
    raise PydanticCustomError("validation_argument_" + rule, message)


def argument_reference(token: str) -> tuple[Literal["input", "output"], str] | None:
    """Recognize the reserved top-level reference grammar, never substrings.

    The returned suffix is a bare input filename or a safe output-relative
    suffix (empty or beginning with '/'). Both validation and the worker use
    this function so incomplete references cannot be partially materialized.
    """
    if token.startswith("{input:"):
        match = _INPUT.fullmatch(token)
        if match is None:
            _error("input_reference", "A top-level input placeholder must exactly name one declared fixture.")
        return "input", match.group(1)
    if token.startswith("{output}"):
        if token != "{output}":
            if not token.startswith("{output}/"):
                _error("output_reference", "An output placeholder must be exact or followed by a safe relative output path.")
            parts = token[len("{output}/"):].split("/")
            if any(not _FILE.fullmatch(part) or part in {".", ".."} for part in parts):
                _error("output_reference", "An output placeholder must be exact or followed by a safe relative output path.")
        return "output", token[len("{output}"):]
    return None


def classify_argument(token: str, input_filenames: Iterable[str]) -> ArgumentKind:
    """Check transport and explicit references without interpreting CLI data."""
    if "\x00" in token:
        _error("control_character", "CLI arguments must not contain NUL characters.")
    try:
        token.encode("utf-8")
    except UnicodeEncodeError:
        _error("unicode", "CLI arguments must be encodable as UTF-8.")
    reference = argument_reference(token)
    if reference is None:
        return "literal"
    kind, value = reference
    if kind == "input" and value not in input_filenames:
        _error("input_reference", "A top-level input placeholder must exactly name one declared fixture.")
    return kind


def input_output_reference_alias(arguments: list[str], input_filenames: Iterable[str]) -> bool:
    """Recognize exact fixture identity without granting permission to write it."""
    if any(arguments.count(flag) != 1 for flag in ("--input", "--output")):
        return False
    input_index, output_index = arguments.index("--input"), arguments.index("--output")
    if input_index + 1 >= len(arguments) or output_index + 1 >= len(arguments):
        return False
    input_token, output_token = arguments[input_index + 1], arguments[output_index + 1]
    match = _INPUT.fullmatch(input_token)
    return bool(match and match.group(1) in input_filenames and input_token == output_token)


def validate_arguments(arguments: list[str], input_filenames: Iterable[str], *,
                       reject_input_alias_only: bool = False) -> None:
    """Validate argv, allowing an explicit rejection probe of one input alias.

    Callers may enable the probe only for an invalid case expecting a nonzero
    exit with no assertions or only output_empty/stderr_contains rejection
    postconditions, never scientific output assertions. This does not authorize
    writing: execution keeps every input read-only and still verifies unchanged
    input hashes. Output emptiness refers only to the isolated output workspace.
    """
    names = frozenset(input_filenames)
    kinds = [classify_argument(token, names) for token in arguments]
    alias_rejection = (reject_input_alias_only is True
                       and input_output_reference_alias(arguments, names))
    for flag, required_kind in (("--input", "input"), ("--output", "output")):
        if arguments.count(flag) != 1:
            _error("io_binding", "Declare exactly one --input and one --output, each followed by its corresponding top-level placeholder.")
        index = arguments.index(flag)
        if (index + 1 >= len(arguments)
                or kinds[index + 1] != required_kind and not (flag == "--output" and alias_rejection)):
            _error("io_binding", "Declare exactly one --input and one --output, each followed by its corresponding top-level placeholder.")
