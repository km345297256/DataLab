"""One privacy boundary for skill evidence, generated documents and repairs.

The context remembers *provenance*, not a bag of words.  Detection operates on
individual fields (never serialized JSON or concatenated fields); diagnostics
contain only schema locations and fixed categories.  Redaction is for model
evidence/repair only.  A generated package must still pass detection unchanged.
"""
from __future__ import annotations

import ast
from bisect import bisect_left
import io
import json
import math
import re
import tokenize
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Literal

Category = Literal["identity", "host_path", "source_filename", "credential", "url"]
_HINTS: dict[str, str] = {
    "identity": "Remove the original identity; bind identity-dependent behavior to the current invocation.",
    "host_path": "Replace the original absolute path with an explicit current-input or output parameter.",
    "source_filename": "Replace the source filename with an input parameter; preserve its format and scientific role.",
    "credential": "Remove literal credentials; retain only a named runtime parameter if authentication is necessary.",
    "url": "Replace the concrete source URL with a current-input parameter; do not retain query credentials.",
}
_REPLACEMENTS = {
    "identity": "{source_identity}", "host_path": "{input_path}",
    "source_filename": "{input_file}", "credential": "{credential_parameter}",
    "url": "{source_url}",
}
_IDENTITY_KEY = re.compile(r"^(?:(?:file|dataset|session|user|workspace|owner|run|task)[_-]?id)$", re.I)
_CREDENTIAL_KEY = re.compile(r"^(?:api[_-]?key|password|passwd|secret|(?:access|refresh|auth)[_-]?token|token|authorization|credential)$", re.I)
_PATH_KEY = re.compile(r"^(?:file[_-]?path|host[_-]?path|input[_-]?path|output[_-]?path|path)$", re.I)
_FILENAME_KEY = re.compile(r"^(?:file[_-]?name|filename)$", re.I)
_URL_KEY = re.compile(r"^(?:file[_-]?url|source[_-]?url|url)$", re.I)
_URL = re.compile(r"(?:https?|s3|file)://[^\s<>\]\[\"'`]+", re.I)
_PATH = re.compile(
    r"(?<![\w])(?:[A-Za-z]:[\\/][^\s<>\]\[\"'`,;()，。；（）]+|\\\\[^\s<>\]\[\"'`,;()，。；（）]+|"
    r"~/(?:[^\s<>\]\[\"'`,;()，。；（）]+)|/(?:[\w.][\w.-]*/)+[^\s<>\]\[\"'`,;()，。；（）]*|"
    r"/(?:Users|home|root|private|tmp|var|mnt|workspace|data|app|opt|srv|etc)(?=[\s\"'`]|$))"
)
# Generic path syntax needs a Unicode lexical boundary: a slash within a
# multilingual list of scientific terms is not the start of an absolute path.
# Familiar host roots can still be recognized after adjacent CJK prose. All
# exact paths collected from typed source metadata are matched independently.
_HOST_ROOT_PATH = re.compile(
    r"(?<![A-Za-z0-9_])/(?:Users|home|root|private|tmp|var|mnt|workspace|data|app|opt|srv|etc|usr)"
    r"(?:/[^\s<>\]\[\"'`,;()，。；（）]*|(?=[\s\"'`]|$))"
)
_OPAQUE_ID = re.compile(r"(?<![\w])(?:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|[0-9a-f]{24,64})(?![\w])", re.I)
_SECRET = re.compile(r"\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{8,}|\bBearer[ \t]+[A-Za-z0-9._~+/=-]{8,}", re.I)
_ASSIGNMENT = re.compile(
    r"\b(?P<key>(?:api[_-]?key|password|passwd|secret|token|authorization|"
    r"(?:access|refresh|auth)[_-]?token|(?:file|dataset|session|user|workspace|owner)[_-]?id))"
    r"[\"']?[ \t]*[:=][ \t]*(?P<value>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;`]+)", re.I)
# These are grammar forms, not a list of permissible secret values.  Every
# explicit context value is checked before grammar exemptions are considered.
_PLACEHOLDER = re.compile(r"(?:\$?\{[A-Za-z_][\w:.-]*\}|<[A-Za-z_][\w:.-]*>|\[(?:CURRENT|SOURCE|RUNTIME)_[A-Z_]+\])")
_REFERENCE = re.compile(r"(?:args|params|parameters|options)\.[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*")
_SAFE_FIELDS = frozenset("""
name description goal triggers applicability parameters value_type required default constraints
steps action decision verification outputs format content acceptance validation_cases kind input
expected limitations data_requirements dependencies scripts path purpose invocation implementation
reason requirement_ids implementation_source_ids coverage locations requirements id category statement
source_ids excluded_sources source_id explanation title sources research_request verified_steps
requirement_id implementation_appropriate implementation_reason
method evidence_summary successful_tool_capabilities delivery_summary artifact_formats source
language provenance reference_only metadata files fixtures filename rows columns cases assertions
pointer tolerance expected_exit_code arguments expected_outputs fields tests schema status checks
contracts versions implementation_provenance tool_name function_name message notes details
input_files argv data oracle_explanation row_count match count value atol rtol type
input_unchanged script_path support version cli_options output_mode json_outputs input_contract output_contract
assertion_additions assertion new_cases
interface_contract interface_version inputs argument structure missing_semantics meaning shape
properties items additional_keys flags choices filename_argument dynamic_columns repair_scope script_paths
""".split())
_CODE_FIELDS = frozenset({"content", "source", "code", "python_code", "script", "program"})
_PYTHON_SHEBANG = re.compile(r"\A#![ \t]*(?:/usr/bin/env[ \t]+python3?|/usr/bin/python3?)[ \t]*(?:\r?\n|$)")
_REVIEW_POINTER = re.compile(
    r"/(?:parameters/[0-9]+(?:/(?:name|description|value_type|required|default|constraints))?|"
    r"steps/[0-9]+(?:/(?:action|decision|verification))?|"
    r"outputs/[0-9]+(?:/(?:name|format|content|acceptance))?|"
    r"validation_cases/[0-9]+(?:/(?:kind|input|expected))?|"
    r"scripts/[0-9]+(?:/(?:path|purpose|invocation|content))?|"
    r"(?:limitations|data_requirements|dependencies)/[0-9]+)"
)
_REVIEW_COMPARISON = re.compile(
    r"(?:==|!=|<=|>=|=|<|>)(?P<value>true|false|null|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)"
)
_HOST_ROOT_NAMES = frozenset("""
users home root private tmp var mnt workspace data app opt srv etc usr dev proc sys run
bin sbin lib lib64 volumes applications library system network cores net
""".split())


@dataclass(frozen=True)
class PrivateValue:
    value: str = field(repr=False)
    category: Category


@dataclass(frozen=True)
class PrivacyIssue:
    location: tuple[str | int, ...]
    category: Category
    line: int | None = None
    role: Literal["generated_output"] | None = None

    @property
    def hint(self) -> str:
        if self.role == "generated_output":
            return ("This is a proven historical generated output, not an analysis input. "
                    "Use a portable filename for the same output role in the current output space; "
                    "preserve its contents and consumers. Do not turn it into a required input.")
        return _HINTS[self.category]

    def as_dict(self) -> dict[str, Any]:
        result = {"location": list(self.location), "category": self.category, "hint": self.hint}
        if self.line is not None:
            result["line"] = self.line
        if self.role is not None:
            result["role"] = self.role
        return result


@dataclass(frozen=True)
class _Match:
    start: int
    end: int
    category: Category


def _document(value: Any) -> Any:
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def _placeholder(value: str) -> bool:
    return bool(_PLACEHOLDER.fullmatch(value.strip().strip("\"'")))


def _safe_location(location: tuple[str | int, ...]) -> tuple[str | int, ...]:
    return tuple(part if isinstance(part, int) or part in _SAFE_FIELDS else "<field>" for part in location)


def _review_pointer_comparison(text: str, pointers: frozenset[str]) -> bool:
    """Recognize one exact scoped target followed by a finite JSON scalar.

    This applies only to generic path recognition. Known private values and
    secret/identity patterns are still independently checked by the caller.
    Full matching prevents a comparison from granting scope to a path suffix.
    """
    for pointer in pointers:
        if not text.startswith(pointer):
            continue
        match = _REVIEW_COMPARISON.fullmatch(text[len(pointer):])
        if match is not None:
            value = match.group("value")
            if value in {"true", "false", "null"} or math.isfinite(float(value)):
                return True
    return False


def _review_string_comparison_spans(text: str, targets: frozenset[tuple[str, str]]) -> list[tuple[int, int]]:
    """Recognize only a current target compared with its declared string value.

    Bare labels support ordinary citations such as ``/groups/0/group=A``.
    Quoted values use JSON string semantics. Full lexical termination keeps a
    public comparison from granting permission to a following path suffix.
    """
    spans = []
    for pointer, expected in targets:
        pattern = (re.escape(pointer) + r'(?:==|!=|=)(?P<value>"(?:\\.|[^"\\])*"|[\w.-]+)'
                   + r"(?=$|[\s`'，。；,;()（）\]])")
        for match in re.finditer(pattern, text):
            raw = match.group("value")
            try:
                value = json.loads(raw) if raw.startswith('"') else raw
            except ValueError:
                continue
            if type(value) is str and value == expected:
                spans.append(match.span())
    return spans


@dataclass(frozen=True)
class _PythonCode:
    tree: ast.AST
    lines: tuple[str, ...]
    starts: tuple[int, ...]

    def offset(self, position: tuple[int, int], *, utf8: bool = False) -> int:
        row, column = position
        line = self.lines[row - 1]
        if utf8:
            # AST columns are UTF-8 bytes; tokenize columns are codepoints.
            column = len(line.encode()[:column].decode())
        return self.starts[row - 1] + column

    def span(self, node: ast.AST) -> tuple[int, int]:
        return (self.offset((node.lineno, node.col_offset), utf8=True),
                self.offset((node.end_lineno, node.end_col_offset), utf8=True))


def _python_code(text: str) -> _PythonCode | None:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    # Only physical Python line endings affect AST/token positions. Unicode
    # U+0085/U+2028/U+2029 inside strings are not source line separators.
    ends = [match.end() for match in re.finditer(r"\r\n|\r|\n", text)]
    starts = (0, *ends)
    lines = tuple(text[start:end] for start, end in zip(starts, (*ends, len(text))))
    return _PythonCode(tree, lines, starts)


def _ordinary_string_spans(text: str, code: _PythonCode) -> list[tuple[int, int]]:
    """Exact ordinary str tokens whose decoded values are scanned separately.

    AST Constant spans may cover adjacent tokens and intervening comments;
    they must never grant raw path exemptions. F-string expressions remain
    excluded even on Python versions that tokenize their inner strings.
    """
    interpolated = [code.span(node) for node in ast.walk(code.tree) if isinstance(node, ast.JoinedStr)]
    result = []
    try:
        # Universal physical-newline handling matches ast.parse without
        # treating Unicode text separators as new source rows.
        tokens = tokenize.generate_tokens(io.StringIO(text, newline=None).readline)
        for token in tokens:
            if token.type != tokenize.STRING:
                continue
            try:
                value = ast.literal_eval(token.string)
            except (SyntaxError, ValueError):
                continue
            if not isinstance(value, str):
                continue
            start, end = code.offset(token.start), code.offset(token.end)
            if any(start < right and end > left for left, right in interpolated):
                continue
            result.append((start, end))
    except (tokenize.TokenError, IndentationError):
        return []
    return result


def _code_value_spans(code: _PythonCode) -> list[tuple[int, int]]:
    """Locations of nonliteral credential/identity assignment RHS in Python.

    A variable named token is not itself a credential.  The AST distinguishes
    token = args.token from token = 'an-actual-secret'.  Syntax-invalid code
    receives no exception; strong/context markers are never exempted.
    """
    spans = []
    for node in ast.walk(code.tree):
        pairs = []
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            pairs = [(target.id, node.value) for target in targets if isinstance(target, ast.Name)]
        elif isinstance(node, ast.keyword) and node.arg:
            pairs = [(node.arg, node.value)]
        for key, value in pairs:
            if not (_CREDENTIAL_KEY.fullmatch(key) or _IDENTITY_KEY.fullmatch(key)) or value is None:
                continue
            # Composite values can hide literals: never exempt concatenation,
            # f-strings or arbitrary calls, even if most operands are variables.
            if (isinstance(value, (ast.Name, ast.Attribute))
                    or isinstance(value, ast.Constant) and (value.value is None or type(value.value) is bool or value.value == "")
                    or isinstance(value, ast.Subscript) and isinstance(value.value, (ast.Name, ast.Attribute))
                    or isinstance(value, ast.Call) and not any(isinstance(item, ast.Constant) and isinstance(item.value, str)
                                                              for item in ast.walk(value))):
                spans.append(code.span(value))
    return spans


def _code_annotation_keys(code: _PythonCode) -> frozenset[int]:
    """Exact credential-like parameters with a pure annotation and no default.

    An annotation names a type, not a credential. Restrict this distinction to
    AST-proven function parameter keys; an annotation's source span may contain
    comments, which must retain their independent assignment scans. Literal
    annotations/defaults, arbitrary expressions and known secrets stay scanned.
    """
    def pure_type(node):
        if isinstance(node, ast.Name):
            return True
        if isinstance(node, ast.Attribute):
            return pure_type(node.value)
        if isinstance(node, ast.Subscript):
            return pure_type(node.value) and pure_type(node.slice)
        if isinstance(node, (ast.Tuple, ast.List)):
            return all(pure_type(item) for item in node.elts)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return pure_type(node.left) and pure_type(node.right)
        return isinstance(node, ast.Constant) and (node.value is None or node.value is Ellipsis)

    keys = set()
    def add(name, node, annotation):
        if ((_CREDENTIAL_KEY.fullmatch(name) or _IDENTITY_KEY.fullmatch(name))
                and annotation is not None and pure_type(annotation)):
            keys.add(code.span(node)[0])

    for node in ast.walk(code.tree):
        if isinstance(node, ast.arguments):
            positional = [*node.posonlyargs, *node.args]
            # Positional defaults belong to the trailing arguments. A None
            # kw_default means absence; ast.Constant(None) is a real default.
            unbound = positional[:len(positional) - len(node.defaults)]
            unbound += [argument for argument, default in zip(node.kwonlyargs, node.kw_defaults)
                        if default is None]
            unbound += [argument for argument in (node.vararg, node.kwarg) if argument is not None]
            for argument in unbound:
                add(argument.arg, argument, argument.annotation)
    return frozenset(keys)


def _decoded_code_literals(code: _PythonCode) -> list[tuple[str, int, int]]:
    """Escaped Python strings must not conceal known values or host paths."""
    result = []
    for node in ast.walk(code.tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        start, end = code.span(node)
        result.append((node.value, start, end))
    return result


@dataclass(frozen=True)
class PrivacyContext:
    entries: tuple[PrivateValue, ...] = field(default=(), repr=False)
    review_json_pointers: frozenset[str] = field(default_factory=frozenset, repr=False)
    coverage_amendment_pointers: bool = field(default=False, repr=False)
    # Host provenance only; these names remain private to every scanner. The
    # workflow authoring adapter may assign new names before source generation.
    generated_output_filenames: frozenset[str] = field(default_factory=frozenset, repr=False)
    # Request-owned roles are derived afresh from a complete typed plan. A
    # dictionary merely containing data/user_id never grants these roles.
    synthetic_plan_inputs: bool = field(default=False, repr=False)
    synthetic_identity_roots: frozenset[tuple[str | int, ...]] = field(default_factory=frozenset, repr=False)
    review_json_string_values: frozenset[tuple[str, str]] = field(default_factory=frozenset, repr=False)

    def document_scope(self, value) -> "PrivacyContext":
        if not self.synthetic_plan_inputs:
            return self
        from app.domain.services.skills.skill_validation import SyntheticValidationPlan
        try:
            plan = SyntheticValidationPlan.model_validate(_document(value))
        except (ValueError, TypeError):
            return replace(self, synthetic_identity_roots=frozenset(),
                           review_json_pointers=frozenset(), review_json_string_values=frozenset())
        roots = frozenset(("scripts", si, "cases", ci, "input_files", fi, "data")
            for si, script in enumerate(plan.scripts) for ci, case in enumerate(script.cases)
            for fi, fixture in enumerate(case.input_files) if fixture.format == "json")
        pointers, strings = self._plan_targets(plan)
        return replace(self, synthetic_identity_roots=roots, review_json_pointers=pointers,
                       review_json_string_values=strings)

    @staticmethod
    def _plan_targets(plan):
        assertions = [item for script in plan.scripts for case in script.cases for item in case.assertions
                      if item.type in {"json_value", "json_array_length", "json_object_keys"}]
        pointers = frozenset(item.pointer for item in assertions if item.pointer
            and item.pointer.split("/", 2)[1].casefold() not in _HOST_ROOT_NAMES)
        strings = frozenset((item.pointer, item.expected.value) for item in assertions
            if item.type == "json_value" and item.pointer in pointers and type(item.expected.value) is str)
        return pointers, strings

    def _synthetic_identity(self, location) -> bool:
        return any(location[:len(root)] == root for root in self.synthetic_identity_roots)

    def _amendment_pointer_locations(self, value) -> frozenset[tuple[str | int, ...]]:
        """Only a request-owned amendment schema grants this locator role.

        Do not infer a role from an arbitrary key named pointer or assertion.
        The complete parent assertion must also satisfy its declared schema;
        all its other fields are still scanned normally.
        """
        if not self.coverage_amendment_pointers:
            return frozenset()
        from app.domain.services.skills.skill_validation import JSONObjectKeysAssertion
        value = _document(value)
        additions = value.get("assertion_additions") if isinstance(value, dict) else None
        if not isinstance(additions, list):
            return frozenset()
        locations = set()
        for index, addition in enumerate(additions):
            if not isinstance(addition, dict):
                continue
            try:
                JSONObjectKeysAssertion.model_validate(addition.get("assertion"))
            except ValueError:
                continue
            locations.add(("assertion_additions", index, "assertion", "pointer"))
        return frozenset(locations)

    def for_validation_plan(self, plan) -> "PrivacyContext":
        """Scope prose citations to assertion targets of this validated plan.

        Callers validate the plan's method/interface contract before review.
        Reconstruct its schema and recheck its privacy here; a model cannot
        manufacture permission through an explanation or a repair response.
        Never carry a previous request's public targets into a new scope.
        """
        from app.domain.services.skills.skill_validation import SyntheticValidationPlan
        from .package import validate_portable_document
        plan = SyntheticValidationPlan.model_validate(_document(plan))
        base = PrivacyContext(self.entries, generated_output_filenames=self.generated_output_filenames,
                              synthetic_plan_inputs=True)
        validate_portable_document(plan, base)
        pointers, strings = self._plan_targets(plan)
        return self._with_review_pointers(pointers, strings=strings)

    def for_reference_targets(self, blind_cases) -> "PrivacyContext":
        """Permit only host-projected JSON targets in reference review prose.

        These locators come from the validated plan, without its expectations.
        Revalidate their locator schema and all private values before deriving
        a fresh scope; never take permissions from the model's review itself.
        """
        from app.domain.services.skills.skill_validation import JSONOutputAssertion
        from .package import validate_portable_document
        blind_cases = _document(blind_cases)
        # Restore only the host field name used by the existing declarative
        # locator scanner. Every value remains present for private-value,
        # secret and unrelated-path checks; no response field grants scope.
        declaration = [{**script, "cases": [
            {**{key: value for key, value in case.items() if key != "output_targets"},
             "assertions": case["output_targets"]} for case in script["cases"]]}
            for script in blind_cases]
        # This is the host's blind projection of typed synthetic fixtures, not
        # permission for the reference author's returned source or prose.
        from app.domain.services.skills.skill_validation import SyntheticJSONInput
        roots = set()
        for si, script in enumerate(blind_cases):
            if set(script) != {"script_path", "cases"}:
                continue
            for ci, case in enumerate(script["cases"]):
                if set(case) != {"case_id", "input_files", "argv", "requirement_ids", "output_targets"}:
                    continue
                for fi, fixture in enumerate(case["input_files"]):
                    try:
                        SyntheticJSONInput.model_validate(fixture)
                    except (ValueError, TypeError):
                        continue
                    roots.add((si, "cases", ci, "input_files", fi, "data"))
        validate_portable_document(declaration, PrivacyContext(self.entries,
            generated_output_filenames=self.generated_output_filenames,
            synthetic_identity_roots=frozenset(roots)))
        targets = [JSONOutputAssertion.model_validate({"path": target["path"], "pointer": target["pointer"]})
            for script in blind_cases for case in script["cases"] for target in case["output_targets"]
            if target["type"] in {"json_value", "json_array_length", "json_object_keys"}]
        return self._with_review_pointers(target.pointer for target in targets)

    def _with_review_pointers(self, values, *, strings=frozenset()) -> "PrivacyContext":
        pointers = frozenset(value for value in values if value
            and value.split("/", 2)[1].casefold() not in _HOST_ROOT_NAMES)
        return PrivacyContext(self.entries, pointers,
                              generated_output_filenames=self.generated_output_filenames,
                              review_json_string_values=frozenset((pointer, value) for pointer, value in strings
                                                                 if pointer in pointers))

    @classmethod
    def from_legacy(cls, values: Iterable[str] | None) -> "PrivacyContext":
        entries = []
        for value in values or ():
            if not isinstance(value, str) or not value:
                continue
            category: Category = ("url" if _URL.search(value) else "host_path" if _PATH.search(value)
                                  else "source_filename" if re.search(r"\.[A-Za-z][A-Za-z0-9]{0,9}$", value)
                                  else "identity")
            entries.append(PrivateValue(value, category))
        return cls(tuple(entries))

    @classmethod
    def collect(cls, events: Iterable[Any], *, session_id: str, user_id: str,
                workspace_id: str | None = None) -> "PrivacyContext":
        events = list(events)
        entries: set[tuple[str, Category]] = set()
        def add(value: Any, category: Category) -> None:
            if isinstance(value, str) and value:
                entries.add((value, category))
        for value in (session_id, user_id, workspace_id or f"personal-{user_id}"):
            add(value, "identity")
        def collect(value: Any, *, attachment: bool = False, location: tuple[str | int, ...] = ()) -> None:
            value = _document(value)
            if isinstance(value, dict):
                for key, nested in value.items():
                    key = str(key)
                    if _CREDENTIAL_KEY.fullmatch(key):
                        add(nested, "credential")
                    elif _IDENTITY_KEY.fullmatch(key) or attachment and key == "id":
                        add(nested, "identity")
                    elif _FILENAME_KEY.fullmatch(key):
                        add(nested, "source_filename")
                    elif _PATH_KEY.fullmatch(key):
                        add(nested, "host_path")
                    elif _URL_KEY.fullmatch(key):
                        add(nested, "url")
                    collect(nested, attachment=attachment, location=location + (key,))
            elif isinstance(value, (list, tuple)):
                for index, nested in enumerate(value):
                    collect(nested, attachment=attachment, location=location + (index,))
            elif isinstance(value, str):
                # This extracts only independently recognizable private spans,
                # never arbitrary filename-looking scientific identifiers.
                for match in cls()._matches(value, location):
                    add(value[match.start:match.end], match.category)
        for event in events:
            data = _document(event)
            if not isinstance(data, dict):
                data = vars(event)
            add(data.get("id"), "identity")
            for key in ("run_id", "task_id", "tool_call_id"):
                add(data.get(key), "identity")
            collect(data.get("attachments") or [], attachment=True)
            collect(data.get("function_args") or {})
            # Results are inspected locally for typed sensitive markers only;
            # neither raw result data nor this context becomes model evidence.
            collect(data.get("function_result") or {})
            for key in ("message", "plan", "step"):
                collect(data.get(key))
        return cls(tuple(PrivateValue(value, category) for value, category in sorted(entries)),
                   generated_output_filenames=_generated_output_filenames(events, session_id, user_id))

    @property
    def private_values(self) -> frozenset[str]:
        return frozenset(entry.value for entry in self.entries)

    def _file_aliases(self) -> dict[str, str]:
        """Opaque stable aliases preserve input roles without keeping names."""
        filenames = sorted({entry.value for entry in self.entries if entry.category == "source_filename"})
        return {name: f"{{input_file_{index}}}" for index, name in enumerate(filenames, 1)}

    def _replacement(self, match: _Match, original: str, aliases: dict[str, str]) -> str:
        literal = original
        if literal not in aliases:
            try:
                decoded = ast.literal_eval("(" + original + ")")
            except (SyntaxError, ValueError, TypeError, RecursionError):
                pass
            else:
                if isinstance(decoded, str):
                    literal = decoded
        if match.category == "source_filename" and literal in aliases:
            return aliases[literal]
        if match.category == "host_path":
            basename = literal.replace("\\", "/").rsplit("/", 1)[-1]
            same_basename = [alias for name, alias in aliases.items()
                             if name.replace("\\", "/").rsplit("/", 1)[-1] == basename]
            # An ambiguous basename is never used to merge two input roles.
            if len(same_basename) == 1:
                return same_basename[0]
        return _REPLACEMENTS[match.category]

    def _output_match_name(self, text: str, match: _Match) -> str | None:
        """Classify a precise filename match from host provenance only.

        Decoded Python literals can be reported using their whole token span.
        Never infer an output role from a variable name, prose or a path.
        Classification changes repair guidance, not detection or redaction.
        """
        if match.category != "source_filename":
            return None
        literal = text[match.start:match.end]
        if literal not in self.generated_output_filenames:
            try:
                # The AST span of adjacent string tokens omits their outer
                # grouping parentheses; retain multiline literal semantics.
                literal = ast.literal_eval("(" + literal + ")")
            except (SyntaxError, ValueError, TypeError, RecursionError):
                return None
        if not isinstance(literal, str) or literal not in self.generated_output_filenames:
            return None
        # Proven output ownership cannot erase another sensitive meaning of
        # the same spelling. Keep ambiguous credentials, identities and paths
        # under their ordinary guidance instead of suggesting a file rename.
        if any(issue.category != "source_filename" for issue in self._matches(literal)):
            return None
        return literal

    def _output_match_names(self, text: str, matches: list[_Match]) -> dict[_Match, str]:
        """Keep output guidance away from overlapping sensitive meanings.

        A filename inside a credential/path span cannot prove that redaction
        actually emitted its filename alias. Model-authored copies of that
        alias elsewhere must not manufacture an output binding.
        """
        protected = []
        for match in matches:
            if match.category == "source_filename":
                continue
            if protected and match.start < protected[-1][1]:
                protected[-1] = (protected[-1][0], max(protected[-1][1], match.end))
            else:
                protected.append((match.start, match.end))
        starts = [start for start, _ in protected]
        result = {}
        for match in matches:
            index = bisect_left(starts, match.end) - 1
            if index >= 0 and protected[index][1] > match.start:
                continue
            name = self._output_match_name(text, match)
            if name is not None:
                result[match] = name
        return result

    def output_repair_references(self, document: Any) -> list[dict[str, str]]:
        """Describe historical output roles without exposing or rewriting them.

        Proposed names are suggestions for the author, never privacy exemptions
        or changes to a frozen output contract. Full checks still inspect the
        author's actual source. Input and ambiguous provenance get no binding.
        """
        texts: list[str] = []
        names: set[str] = set()

        def walk(value, location=()):
            value = _document(value)
            if isinstance(value, str):
                texts.append(value)
                # Escaped and adjacent Python string tokens can spell a
                # public output name without that spelling in raw source.
                # Treat their decoded values as occupied too; parsing never
                # executes source or changes the authored program.
                code = _python_code(value) if location and location[-1] in _CODE_FIELDS else None
                if code is not None:
                    texts.extend(literal for literal, _, _ in _decoded_code_literals(code))
                names.update(self._output_match_names(value, self._matches(value, location)).values())
            elif isinstance(value, dict):
                for key, nested in value.items():
                    texts.append(str(key))
                    walk(nested, (*location, str(key)))
            elif isinstance(value, (list, tuple)):
                for index, nested in enumerate(value):
                    walk(nested, (*location, index))

        walk(document)
        aliases = self._file_aliases()
        folded_texts = [text.casefold() for text in texts]
        occupied_candidate_count = sum(text.count("artifact_") for text in folded_texts)
        result = []
        occupied: set[str] = set()
        for name in sorted(names):
            if name not in aliases or self.scan_document(aliases[name]):
                continue
            item = {"reference": aliases[name], "role": "generated_output"}
            extension = name.rsplit(".", 1)[-1] if "." in name else ""
            if re.fullmatch(r"[A-Za-z][A-Za-z0-9]{0,11}", extension):
                # Existing source references bound collisions without imposing a
                # filename list or guessing a new scientific role/format.
                for serial in range(1, occupied_candidate_count + len(self.entries) + len(names) + 2):
                    suggested = f"artifact_{serial:03d}.{extension}"
                    if (suggested.casefold() not in occupied
                            and not any(suggested.casefold() in text for text in folded_texts)
                            and not self.scan_document(suggested)):
                        item["suggested_portable_name"] = suggested
                        occupied.add(suggested.casefold())
                        break
            result.append(item)
        return result

    def _matches(self, text: str, location: tuple[str | int, ...] = (), *, assertion_pointer: bool = False) -> list[_Match]:
        matches = []
        synthetic_identity = self._synthetic_identity(location)
        code = _python_code(text) if location and location[-1] in _CODE_FIELDS else None
        for entry in self.entries:
            # Identifiers and filenames are lexical values, not substrings of
            # unrelated scientific terms (e.g. user 'ols' vs method 'tools').
            boundary = entry.category in {"identity", "source_filename"}
            pattern = (r"(?<![A-Za-z0-9_])" if boundary and entry.value[0].isascii() and entry.value[0].isalnum() else "") + re.escape(entry.value)
            pattern += r"(?![A-Za-z0-9_])" if boundary and entry.value[-1].isascii() and entry.value[-1].isalnum() else ""
            matches.extend(_Match(match.start(), match.end(), entry.category) for match in re.finditer(pattern, text))
        for pattern, category in ((_URL, "url"), (_OPAQUE_ID, "identity"), (_SECRET, "credential")):
            if category == "identity" and synthetic_identity:
                continue
            matches.extend(_Match(match.start(), match.end(), category) for match in pattern.finditer(text))
        # JSON pointers are a declared contract syntax, not host paths.  All
        # other fields (including code/comments/defaults) retain path checks.
        pointer = ("coverage" in location and "locations" in location
                   and re.fullmatch(r"/(?:name|description|triggers|goal|applicability|parameters|steps|outputs|validation_cases|limitations|data_requirements|dependencies|scripts|coverage|implementation)(?:/(?:[a-z_]+|[0-9]+))*", text))
        pointer = pointer or (("assertions" in location and location[-1] == "pointer" or assertion_pointer)
                              and re.fullmatch(r"(?:/(?:[^~]|~[01])*)*", text))
        if not pointer:
            shebang = _PYTHON_SHEBANG.match(text) if location and location[-1] in _CODE_FIELDS else None
            string_spans = _ordinary_string_spans(text, code) if code is not None else []
            comparison_spans = (_review_string_comparison_spans(text, self.review_json_string_values)
                if location and location[-1] in {"explanation", "oracle_explanation"} else [])
            for match in (*_PATH.finditer(text), *_HOST_ROOT_PATH.finditer(text)):
                # Source escape syntax may resemble a UNC path. Scan actual
                # decoded strings below; only this raw generic path guess is
                # suppressed. Context/URL/secret/identity matches remain.
                if any(start <= match.start() and match.end() <= end for start, end in string_spans):
                    continue
                # The exact first-line interpreter directive has public syntax
                # semantics. Known-context matches above are never exempted.
                if shebang and match.end() <= shebang.end():
                    continue
                # Reviews cite exact workflow locations to justify their
                # verdict. This recognizes only their schema grammar, not a
                # generic slash exemption or a filesystem path allowlist.
                if (location and location[-1] in {"explanation", "implementation_reason"}
                        and _REVIEW_POINTER.fullmatch(match.group())):
                    continue
                # Only exact targets from the current plan may be cited in
                # oracle explanations. Prefixes/suffixes, arbitrary paths,
                # code and defaults receive no exception. Known private
                # values, URLs and secrets above are never suppressed.
                if (location and location[-1] in {"explanation", "oracle_explanation"}
                        and (match.group() in self.review_json_pointers
                             or _review_pointer_comparison(match.group(), self.review_json_pointers)
                             or any(start <= match.start() and match.end() <= end for start, end in comparison_spans))):
                    continue
                # A suffix of an explicit path parameter is not an absolute
                # path.  Known private values above still take precedence.
                prefix = text[:match.start()]
                parameter_suffix = re.search(r"(?:\$?\{[A-Za-z_][\w:.-]*\}|<[A-Za-z_][\w:.-]*>)$", prefix)
                if not parameter_suffix:
                    matches.append(_Match(match.start(), match.end(), "host_path"))
        code_spans: list[tuple[int, int]] | None = None
        annotation_keys: frozenset[int] = frozenset()
        for match in _ASSIGNMENT.finditer(text):
            rhs = match.group("value")
            if _placeholder(rhs) or _REFERENCE.fullmatch(rhs):
                continue
            if code_spans is None:
                code_spans = _code_value_spans(code) if code is not None else []
                annotation_keys = _code_annotation_keys(code) if code is not None else frozenset()
            if match.start("key") in annotation_keys:
                continue
            if any(start <= match.start("value") < end for start, end in code_spans):
                continue
            category = "credential" if _CREDENTIAL_KEY.fullmatch(match.group("key")) else "identity"
            if category == "identity" and synthetic_identity:
                continue
            matches.append(_Match(match.start("value"), match.end("value"), category))
        if location and isinstance(location[-1], str) and text and not _placeholder(text):
            key = location[-1]
            if _CREDENTIAL_KEY.fullmatch(key) or (_IDENTITY_KEY.fullmatch(key) and not synthetic_identity):
                matches.append(_Match(0, len(text), "credential" if _CREDENTIAL_KEY.fullmatch(key) else "identity"))
        if code is not None:
            for literal, start, end in _decoded_code_literals(code):
                matches.extend(_Match(start, end, match.category) for match in self._matches(literal))
        return sorted(set(matches), key=lambda match: (match.start, -match.end, match.category))

    def scan_document(self, value: Any, *, limit: int | None = 32) -> list[PrivacyIssue]:
        """Find private values; ``None`` requests complete host-side scope.

        The bounded default remains suitable for public diagnostics. Mutation
        authorization must use a complete scan rather than those diagnostics.
        """
        self = self.document_scope(value)
        issues = []
        pointer_locations = self._amendment_pointer_locations(value)
        def walk(node: Any, location: tuple[str | int, ...]) -> None:
            node = _document(node)
            if isinstance(node, str):
                matches = self._matches(node, location, assertion_pointer=location in pointer_locations)
                outputs = self._output_match_names(node, matches)
                for match in matches:
                    issue = PrivacyIssue(_safe_location(location), match.category,
                        node.count("\n", 0, match.start) + 1 if "\n" in node else None,
                        "generated_output" if match in outputs else None)
                    if issue not in issues and (limit is None or len(issues) < limit):
                        issues.append(issue)
            elif isinstance(node, dict):
                for index, (key, nested) in enumerate(node.items()):
                    walk(str(key), location + ("<key>", index))
                    walk(nested, location + (str(key),))
            elif isinstance(node, (list, tuple)):
                for index, nested in enumerate(node):
                    walk(nested, location + (index,))
        walk(value, ())
        return issues

    def redact_text(self, text: str, *, location: tuple[str | int, ...] = (), assertion_pointer: bool = False) -> str:
        value = str(text or "")
        matches = self._matches(value, location, assertion_pointer=assertion_pointer)
        merged: list[_Match] = []
        for match in matches:
            if merged and match.start < merged[-1].end:
                prior = merged[-1]
                merged[-1] = _Match(prior.start, max(prior.end, match.end), prior.category)
            else:
                merged.append(match)
        aliases = self._file_aliases()
        for match in reversed(merged):
            replacement = self._replacement(match, value[match.start:match.end], aliases)
            value = value[:match.start] + replacement + value[match.end:]
        return value

    def redact_document(self, value: Any) -> Any:
        self = self.document_scope(value)
        pointer_locations = self._amendment_pointer_locations(value)
        def walk(node: Any, location: tuple[str | int, ...]) -> Any:
            node = _document(node)
            if isinstance(node, str):
                return self.redact_text(node, location=location, assertion_pointer=location in pointer_locations)
            if isinstance(node, dict):
                result = {}
                for index, (key, nested) in enumerate(node.items()):
                    safe_key = f"<private_field_{index}>" if self._matches(str(key), location + ("<key>", index)) else key
                    result[safe_key] = walk(nested, location + (str(key),))
                return result
            if isinstance(node, (list, tuple)):
                return [walk(nested, location + (index,)) for index, nested in enumerate(node)]
            return node
        return walk(value, ())


def _generated_output_filenames(events, session_id, user_id) -> frozenset[str]:
    """Classify only owned host-published outputs, excluding selected inputs.

    No model text or tool result grants a role. Old or incomplete attachment
    provenance simply retains the existing private-filename behavior.
    """
    generated = []
    blocked = set()
    selected_ids = set()
    for event in events:
        data = _document(event)
        if not isinstance(data, dict):
            data = vars(event)
        metadata = data.get("metadata") or {}
        if data.get("type") == "message" and data.get("role") == "user":
            for key in ("analysis_input_file_ids", "requested_input_file_ids", "input_file_ids"):
                selected_ids.update(value for value in metadata.get(key, []) or [] if isinstance(value, str))
        for attachment in data.get("attachments") or []:
            attachment = _document(attachment)
            if not isinstance(attachment, dict):
                continue
            name = attachment.get("filename")
            if not isinstance(name, str) or not name:
                continue
            meta = attachment.get("metadata") or {}
            owned_output = (data.get("type") == "message" and data.get("role") == "assistant"
                and isinstance(attachment.get("file_id"), str) and bool(attachment["file_id"])
                and attachment.get("user_id") == user_id
                and meta.get("source") == "sandbox_artifact" and meta.get("session_id") == session_id
                and meta.get("user_id", user_id) == user_id
                and meta.get("filename", name) == name
                and type(meta.get("artifact_size")) is int and meta["artifact_size"] >= 0
                and meta["artifact_size"] == attachment.get("size")
                and isinstance(meta.get("artifact_sha256"), str)
                and re.fullmatch(r"[a-f0-9]{64}", meta["artifact_sha256"]) is not None)
            if owned_output:
                generated.append((name, attachment.get("file_id")))
            else:
                blocked.add(name.replace("\\", "/").rsplit("/", 1)[-1].casefold())
    for name, file_id in generated:
        if file_id in selected_ids:
            blocked.add(name.casefold())
    return frozenset(name for name, _ in generated if name.casefold() not in blocked)


def ensure_context(value: PrivacyContext | Iterable[str] | None = None) -> PrivacyContext:
    return value if isinstance(value, PrivacyContext) else PrivacyContext.from_legacy(value)


def generation_privacy(context, schema, stage, payload) -> PrivacyContext:
    """Use the same request boundary for generation, repairs and checkpoints."""
    context = ensure_context(context)
    if stage == "implementation_revision":
        from .implementation_revision import ImplementationEdits, implementation_revision_privacy
        if schema is ImplementationEdits:
            return implementation_revision_privacy(context, payload["previous_skill_package"])
    if stage == "workflow":
        from .workflow_authoring import WorkflowDraft, WorkflowDraftV2
        if schema in {WorkflowDraft, WorkflowDraftV2}:
            return PrivacyContext(context.entries, generated_output_filenames=context.generated_output_filenames)
    if stage == "synthetic_validation_plan":
        from app.domain.services.skills.skill_validation import SyntheticValidationPlan
        if schema is SyntheticValidationPlan:
            return PrivacyContext(context.entries, generated_output_filenames=context.generated_output_filenames,
                                  synthetic_plan_inputs=True)
        from app.domain.services.skills.skill_validation_extension import VerificationCoverageAmendment
        if schema is VerificationCoverageAmendment:
            return PrivacyContext(context.entries, coverage_amendment_pointers=True,
                                  generated_output_filenames=context.generated_output_filenames)
    if stage == "synthetic_plan_review" and "validation_plan" in payload:
        from app.domain.services.skills.skill_validation_review import ValidationPlanReview
        if schema is ValidationPlanReview:
            return context.for_validation_plan(payload["validation_plan"])
    if stage == "synthetic_reference_review" and "blind_cases" in payload:
        from app.domain.services.skills.method_contract import MethodReview
        if schema is MethodReview:
            return context.for_reference_targets(payload["blind_cases"])
    # A scoped object is not an ambient/global exemption for other stages.
    return PrivacyContext(context.entries, generated_output_filenames=context.generated_output_filenames)


def scan_document(value: Any, context: PrivacyContext | Iterable[str] | None = None,
                  *, limit: int | None = 32) -> list[PrivacyIssue]:
    return ensure_context(context).scan_document(value, limit=limit)


def redact_document(value: Any, context: PrivacyContext | Iterable[str] | None = None) -> Any:
    return ensure_context(context).redact_document(value)


def redact_text(text: str, context: PrivacyContext | Iterable[str] | None = None) -> str:
    return ensure_context(context).redact_text(text)
