"""Private, non-replayable method references from host-verified Python exits."""
from __future__ import annotations

import ast

from app.domain.services.program_execution import validated_source_snapshot


class ProgramMethodEvidenceError(ValueError):
    """A safe source-boundary diagnostic; never include private source text."""

    REASONS = frozenset({"invalid_record", "integrity_unavailable", "unsupported_syntax",
                         "invalid_projection", "private_identifiers"})

    def __init__(self, reason: str):
        if reason not in self.REASONS:
            raise ValueError("Unknown program method evidence reason")
        self.reason = reason
        super().__init__(f"Program method evidence unavailable: {reason}")


def _parameter(node: ast.AST, name: str) -> ast.Name:
    return ast.copy_location(ast.Name(id=name, ctx=ast.Load()), node)


class _MethodProjection(ast.NodeTransformer):
    """Remove data while respecting each AST field's grammar.

    Expression slots accept a runtime name; f-string segments and match values
    do not. Projection is a method reference, never a runnable historical job.
    """

    def __init__(self):
        self.pattern_parameter = 0

    def visit_Expr(self, node):
        # Retain a statement even when prose is the only member of a suite.
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, (str, bytes)):
            return ast.copy_location(ast.Pass(), node)
        return self.generic_visit(node)

    def visit_Constant(self, node):
        if isinstance(node.value, bytes):
            return _parameter(node, "CURRENT_TASK_BYTES")
        if isinstance(node.value, str):
            return _parameter(node, "CURRENT_TASK_PARAMETER")
        if type(node.value) in {int, float, complex} and node.value not in {0, 1, 2}:
            return _parameter(node, "CURRENT_TASK_NUMBER")
        return node

    def visit_JoinedStr(self, node):
        # JoinedStr.values accepts only string Constants / FormattedValues.
        # A fixed literal also works inside deeply nested format specifications
        # without introducing another level of replacement-field nesting.
        node.values = [ast.copy_location(ast.Constant(value="CURRENT_TASK_PARAMETER"), value)
                       if isinstance(value, ast.Constant) else self.visit(value)
                       for value in node.values]
        return node

    def _pattern_value(self, node):
        if isinstance(node, ast.Attribute):
            return self.visit(node)
        if isinstance(node, ast.Constant) and (
            node.value is None or type(node.value) is bool
            or type(node.value) in {int, float} and node.value in {0, 1, 2}
        ):
            return node
        # Bare names become capture patterns; signed/complex literals must be
        # replaced as a whole. Distinct dotted values preserve separate cases.
        self.pattern_parameter += 1
        return ast.copy_location(ast.Attribute(
            value=_parameter(node, "CURRENT_TASK_PARAMETERS"),
            attr=f"value_{self.pattern_parameter}", ctx=ast.Load()), node)

    def visit_MatchValue(self, node):
        node.value = self._pattern_value(node.value)
        return node

    def visit_MatchMapping(self, node):
        node.keys = [self._pattern_value(key) for key in node.keys]
        node.patterns = [self.visit(pattern) for pattern in node.patterns]
        return node

    def _literal_rows(self, node):
        # Assignment/deletion targets are syntax, not inline datasets. This
        # matters for empty targets which literal_eval also accepts.
        if hasattr(node, "ctx") and not isinstance(node.ctx, ast.Load):
            return self.generic_visit(node)
        try:
            ast.literal_eval(node)
        except (ValueError, TypeError):
            return self.generic_visit(node)
        return _parameter(node, "CURRENT_TASK_VALUES")

    visit_List = _literal_rows
    visit_Tuple = _literal_rows
    visit_Dict = _literal_rows
    visit_Set = _literal_rows


_SYNTAX_ERRORS = (SyntaxError, ValueError, TypeError, RecursionError, OverflowError)


def _project_source(content: str) -> str:
    try:
        tree = ast.parse(content)
        # Parsing alone accepts e.g. a top-level return or invalid patterns.
        # Compilation validates grammar only; no original code is executed.
        compile(tree, "<method-source>", "exec")
    except _SYNTAX_ERRORS:
        raise ProgramMethodEvidenceError("unsupported_syntax") from None
    try:
        transformed = ast.fix_missing_locations(_MethodProjection().visit(tree))
        compile(transformed, "<method-reference>", "exec")
        source = ast.unparse(transformed)
        compile(source, "<method-reference>", "exec")
    except _SYNTAX_ERRORS:
        raise ProgramMethodEvidenceError("invalid_projection") from None
    return source


def method_program_references(records: list[dict] | None, *, private_values: set[str] | None = None,
                              private_pattern=None, privacy_context=None) -> list[dict]:
    """Caller supplies successful native ledger records, never public tool JSON.

    This is a second byte-integrity check, not a substitute for the caller's
    owner, turn and native-operation checks. Return no partial batch or raw
    source on failure; the caller must surface a skill-creation diagnostic.
    """
    if records is not None and not isinstance(records, list):
        raise ProgramMethodEvidenceError("invalid_record")
    result = []
    seen = set()
    for record in records or []:
        if not isinstance(record, dict) or record.get("schema") != "program-method-evidence/v1":
            raise ProgramMethodEvidenceError("invalid_record")
        snapshot = validated_source_snapshot(record.get("source_snapshot"), record.get("source_digest"))
        if snapshot is None:
            raise ProgramMethodEvidenceError("integrity_unavailable")
        if record["source_digest"] in seen:
            continue
        seen.add(record["source_digest"])
        source = _project_source(snapshot["content"])
        if privacy_context is not None:
            private_content = privacy_context.scan_document({"content": source})
        else:
            private_content = ((private_pattern is not None and private_pattern.search(source))
                               or any(item and len(item) >= 4 and item in source for item in private_values or set()))
        if private_content:
            raise ProgramMethodEvidenceError("private_identifiers")
        result.append({"id": f"P{len(result) + 1:03d}", "language": "python",
                       "provenance": "host_verified_successful_source",
                       "reference_only": True, "source": source,
                       "limitations": "String and byte literals, non-structural numbers, comments and inline literal collections were removed. Rebind all CURRENT_TASK placeholders from the method contract and new input; do not execute this reference. A newly generated script has not been run."})
    return result
