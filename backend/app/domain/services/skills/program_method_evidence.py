"""Private, non-replayable method references from host-verified Python exits."""
from __future__ import annotations

import ast
import re
import symtable

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

    def __init__(self, *, supported=None):
        self.pattern_parameter = 0
        self.supported = supported or (lambda value: False)

    def visit_Expr(self, node):
        # Retain a statement even when prose is the only member of a suite.
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, (str, bytes)):
            return ast.copy_location(ast.Pass(), node)
        return self.generic_visit(node)

    def visit_Constant(self, node):
        if isinstance(node.value, bytes):
            return _parameter(node, "CURRENT_TASK_BYTES")
        if isinstance(node.value, str):
            if self.supported(node.value):
                return node
            return _parameter(node, "CURRENT_TASK_PARAMETER")
        if type(node.value) in {int, float, complex} and node.value not in {0, 1, 2}:
            if self.supported(node.value):
                return node
            return _parameter(node, "CURRENT_TASK_NUMBER")
        return node

    def visit_JoinedStr(self, node):
        # JoinedStr.values accepts only string Constants / FormattedValues.
        # A fixed literal also works inside deeply nested format specifications
        # without introducing another level of replacement-field nesting.
        node.values = [ast.copy_location(ast.Constant(value=(value.value if self.supported(value.value)
                                                           else "CURRENT_TASK_PARAMETER")), value)
                       if isinstance(value, ast.Constant) else self.visit(value)
                       for value in node.values]
        return node

    def _pattern_value(self, node):
        if isinstance(node, ast.Attribute):
            return self.visit(node)
        if isinstance(node, ast.Constant) and (
            node.value is None or type(node.value) is bool
            or type(node.value) in {int, float} and node.value in {0, 1, 2}
            or self.supported(node.value)
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


def _supported_literals(method_sources, private):
    # Results and failed historical experiments cannot establish reusable
    # constants. Keep the source relation, not a library/API admission list.
    texts = [source["text"] for source in method_sources or []
             if isinstance(source, dict) and source.get("kind") in {"request", "goal", "method"}
             and isinstance(source.get("text"), str)
             and source.get("execution_status", "completed") == "completed"]

    def supported(value):
        if type(value) not in {str, int, float} or isinstance(value, str) and not value.strip():
            return False
        token = str(value)
        if private(token):
            return False
        # Exact scalar evidence only. For example 'HC3' can be supported by
        # 'use HC3', while a shorter substring of another term is insufficient.
        left = r"(?<!\w)" if isinstance(value, str) else r"(?<![\w.])"
        right = r"(?!\w)" if isinstance(value, str) else r"(?!\w|\.[0-9])"
        pattern = re.compile(left + re.escape(token) + right, re.IGNORECASE)
        return any(pattern.search(text) for text in texts)
    return supported


def _anonymize_local_identifiers(tree, private, *, unresolved_members=None):
    """Rename local bindings and mark private members as unresolved references.

    A member placeholder does not name a real API. This non-runnable projection
    preserves the operation's shape for source-backed rebinding and review.
    """
    bound = set()
    protected = set()
    member_names = set()
    member_roles = {}
    parents = {id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.alias) and node.asname:
            bound.add(node.asname)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar, ast.ExceptHandler)) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bound.add(node.rest)
        # External imports and keyword interfaces must not be mistaken for
        # lexical bindings. Private attributes get an explicit unresolved role.
        if isinstance(node, ast.Attribute):
            if private(node.attr):
                member_names.add(node.attr)
                parent = parents.get(id(node))
                role = ("call" if isinstance(parent, ast.Call) and parent.func is node
                        else "write" if isinstance(node.ctx, ast.Store)
                        else "delete" if isinstance(node.ctx, ast.Del) else "read")
                member_roles.setdefault(node.attr, set()).add(role)
            else:
                protected.add(node.attr)
        elif isinstance(node, ast.keyword) and node.arg:
            protected.add(node.arg)
        elif isinstance(node, ast.alias):
            protected.update(node.name.split("."))
    # A spelling bound inside one function can still name an unrelated global
    # in another scope. A consistent lexical rename must not rewrite that
    # unresolved global (which may be supplied by an external API or import *).
    symbols = symtable.symtable(ast.unparse(tree), "<method-reference>", "exec")
    module_bound = {symbol.get_name() for symbol in symbols.get_symbols()
                    if symbol.is_assigned() or symbol.is_imported() or symbol.is_namespace()}
    pending = [symbols]
    while pending:
        scope = pending.pop()
        protected.update(symbol.get_name() for symbol in scope.get_symbols()
                         if symbol.is_global() and symbol.is_referenced()
                         and symbol.get_name() not in module_bound)
        pending.extend(scope.get_children())
    all_names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | bound | protected | member_names
    member_replacements = {}
    for name in sorted(member_names):
        for index in range(1, len(all_names) + len(member_names) + 2):
            candidate = f"CURRENT_TASK_MEMBER_{index:03d}"
            if candidate not in all_names and not private(candidate):
                member_replacements[name] = candidate
                all_names.add(candidate)
                break
        else:
            raise ProgramMethodEvidenceError("private_identifiers")
    if unresolved_members is not None:
        unresolved_members.extend({"placeholder": member_replacements[name],
                                   "roles": sorted(member_roles[name]), "binding_required": True}
                                  for name in sorted(member_names))
    replacements = {}
    for name in sorted(bound - protected):
        if not private(name):
            continue
        if name in member_replacements:
            replacements[name] = member_replacements[name]
            continue
        # The finite occupied set bounds search; an unusable privacy policy
        # yields the existing safe failure instead of a fallback to raw names.
        for index in range(1, len(all_names) + len(bound) + 2):
            candidate = f"binding_{index}"
            if candidate not in all_names and not private(candidate):
                replacements[name] = candidate
                all_names.add(candidate)
                break
        else:
            raise ProgramMethodEvidenceError("private_identifiers")
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            node.attr = member_replacements.get(node.attr, node.attr)
        elif isinstance(node, ast.Name):
            node.id = replacements.get(node.id, node.id)
        elif isinstance(node, ast.arg):
            node.arg = replacements.get(node.arg, node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                               ast.MatchAs, ast.MatchStar, ast.ExceptHandler)) and node.name:
            node.name = replacements.get(node.name, node.name)
        elif isinstance(node, ast.alias) and node.asname:
            node.asname = replacements.get(node.asname, node.asname)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            node.rest = replacements.get(node.rest, node.rest)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            node.names = [replacements.get(name, name) for name in node.names]
    return tree


def _project_source(content: str, *, supported=None, private=None, unresolved_members=None) -> str:
    try:
        tree = ast.parse(content)
        # Parsing alone accepts e.g. a top-level return or invalid patterns.
        # Compilation validates grammar only; no original code is executed.
        compile(tree, "<method-source>", "exec")
    except _SYNTAX_ERRORS:
        raise ProgramMethodEvidenceError("unsupported_syntax") from None
    try:
        transformed = _MethodProjection(supported=supported).visit(tree)
        if private is not None:
            transformed = _anonymize_local_identifiers(transformed, private, unresolved_members=unresolved_members)
        transformed = ast.fix_missing_locations(transformed)
        compile(transformed, "<method-reference>", "exec")
        source = ast.unparse(transformed)
        compile(source, "<method-reference>", "exec")
    except _SYNTAX_ERRORS:
        raise ProgramMethodEvidenceError("invalid_projection") from None
    return source


def method_program_references(records: list[dict] | None, *, private_values: set[str] | None = None,
                              private_pattern=None, privacy_context=None, method_sources=None) -> list[dict]:
    """Caller supplies successful native ledger records, never public tool JSON.

    This is a second byte-integrity check, not a substitute for the caller's
    owner, turn and native-operation checks. Return no partial batch or raw
    source on failure; the caller must surface a skill-creation diagnostic.
    """
    if records is not None and not isinstance(records, list):
        raise ProgramMethodEvidenceError("invalid_record")
    result = []
    seen = set()
    from .skill_creation.privacy import ensure_context
    privacy = ensure_context(privacy_context if privacy_context is not None else private_values)
    def private(value):
        return bool(privacy.scan_document({"content": value}) or
                    private_pattern is not None and private_pattern.search(value))
    supported = _supported_literals(method_sources, private)
    for record in records or []:
        if not isinstance(record, dict) or record.get("schema") != "program-method-evidence/v1":
            raise ProgramMethodEvidenceError("invalid_record")
        snapshot = validated_source_snapshot(record.get("source_snapshot"), record.get("source_digest"))
        if snapshot is None:
            raise ProgramMethodEvidenceError("integrity_unavailable")
        if record["source_digest"] in seen:
            continue
        seen.add(record["source_digest"])
        unresolved_members = []
        source = _project_source(snapshot["content"], supported=supported, private=private,
                                 unresolved_members=unresolved_members)
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
                       "projection_version": 2,
                       **({"unresolved_member_roles": unresolved_members} if unresolved_members else {}),
                       "limitations": "Beyond structural constants, only non-private scalar literals explicitly supported by request/method sources were retained; observed data, unsupported strings/numbers, comments and literal collections were removed. Private local bindings may be consistently renamed. Non-private external APIs are preserved; CURRENT_TASK_MEMBER placeholders have unresolved member roles and do not name known APIs. Rebind every CURRENT_TASK placeholder from the original method contract and current input, then independently review the new implementation; do not execute this reference. A successful process exit is not scientific acceptance; newly generated scripts have not been run."})
    return result
