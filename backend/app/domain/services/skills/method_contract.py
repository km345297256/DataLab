"""Traceable method requirements, independent of model output length.

Source text stays private model context. Only reviewed, portable requirements
and their package locations are delivered; neither results nor commands are a
reusable method contract.
"""
from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


COVERAGE_ROOTS = ("parameters", "steps", "outputs", "validation_cases", "limitations",
                  "data_requirements", "dependencies", "scripts")
COVERAGE_LOCATION_PATTERN = r"^/(?:" + "|".join(COVERAGE_ROOTS) + r")/\d+(?:/[a-z_]+)?$"
COVERAGE_SECTIONS_BY_KIND = {
    "method": ("steps", "scripts"), "output": ("outputs",),
    "validation": ("validation_cases", "steps"),
    "input": ("parameters", "data_requirements", "steps"),
    "constraint": ("steps", "limitations", "parameters", "data_requirements"),
}
COVERAGE_FIELDS_BY_ROOT = {
    "parameters": ("name", "description", "value_type", "required", "default", "constraints"),
    "steps": ("action", "decision", "verification"),
    "outputs": ("name", "format", "content", "acceptance"),
    "validation_cases": ("kind", "input", "expected"),
    "limitations": (), "data_requirements": (), "dependencies": (),
    "scripts": ("path", "purpose", "invocation", "content"),
}


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class MethodRequirement(ContractModel):
    id: str = Field(pattern=r"^R[0-9]{3,}$")
    kind: Literal["input", "method", "output", "validation", "constraint"]
    # A complete source obligation may be a short phrase or formula. Its
    # meaning must never be expanded merely to satisfy a prose length floor.
    statement: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)


class SourceExclusion(ContractModel):
    source_id: str
    reason: Literal["observed_result", "one_time_instruction", "duplicate"]
    explanation: str = Field(min_length=8)


class MethodContract(ContractModel):
    requirements: list[MethodRequirement] = Field(min_length=1)
    excluded_sources: list[SourceExclusion] = Field(default_factory=list)


class RequirementCoverage(ContractModel):
    requirement_id: str
    # Advertise the same grammar before generation, while keeping runtime
    # validation in _location so existing callers receive CoverageError.
    locations: list[Annotated[str, Field(json_schema_extra={"pattern": COVERAGE_LOCATION_PATTERN})]] = Field(
        min_length=1, description="JSON pointers to actual method content; array indices are zero-based. "
        "For an array with N items, valid indices are 0 through N-1. A field suffix must exist on that item.")


class RequirementReview(ContractModel):
    requirement_id: str
    status: Literal["preserved", "missing", "changed", "unsupported"]
    explanation: str = Field(min_length=8)


class SourceReview(ContractModel):
    source_id: str
    status: Literal["preserved", "valid_exclusion", "missing", "changed"]
    explanation: str = Field(min_length=8)


class MethodReview(ContractModel):
    sources: list[SourceReview]
    requirements: list[RequirementReview]
    implementation_appropriate: bool
    implementation_reason: str = Field(min_length=8)


class CoverageError(ValueError):
    def __init__(self, code: str, identifiers: list[str] | None = None, *, details: dict | None = None):
        self.code = code
        self.identifiers = identifiers or []
        self.details = details or {}
        super().__init__(code)


def evidence_sources(evidence: dict) -> list[dict]:
    """Keep every paragraph; no character, step-count, or capability slicing."""
    sources = []

    def add(kind, text):
        for paragraph in re.split(r"\n\s*\n|\n(?=\s*(?:\d+[.)、]|[-*]\s))", text or ""):
            if paragraph.strip():
                sources.append({"id": f"S{len(sources) + 1:03d}", "kind": kind,
                                "text": paragraph.strip()})

    add("request", evidence.get("research_request", ""))
    add("goal", evidence.get("goal", ""))
    for step in evidence.get("verified_steps", []):
        add("method", step.get("method", ""))
        add("observed_summary", step.get("evidence_summary", ""))
    add("observed_summary", evidence.get("delivery_summary", ""))
    return sources


_DIRECT_PROHIBITION = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    r"(?:do\s+not|don['’]t|must\s+not|mustn['’]t|shall\s+not|should\s+not|shouldn['’]t|never)"
    r"\s+(?:use|include|insert|call|apply|add)\b|"
    r"(?:禁止|严禁)(?:使用|采用|调用|添加|包含|加入|用)?|"
    r"(?:不得|不可|不要|不能|不允许|不应)(?:使用|采用|调用|添加|包含|加入|用))",
    re.IGNORECASE,
)
_PROHIBITED_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:-[A-Za-z0-9_]+)*")
_NAME_WRAPPING = re.compile(r"[\s`\"'“”‘’]*")
_PROHIBITED_LIST_SEPARATOR = re.compile(r"\s*(?:、|/|以及|或者|或|和|与|及|(?:,\s*)?(?:and|or|nor)\b)\s*", re.IGNORECASE)


def _prohibition_prefixes(text: str):
    for match in _DIRECT_PROHIBITION.finditer(text):
        # A negated prohibition is not a prohibition. Do not infer semantics
        # from a distant negation or swallow a whole sentence after one word.
        if re.search(r"(?:并非|不是|并不是|不再|不能|不得|不应|不要|不允许|没有)\s*$", text[:match.start()]):
            continue
        yield match


def _prohibited_name_spans(text: str) -> list[tuple[int, int]]:
    """Only direct named objects and explicit lists, never whole clauses.

    Ambiguous comma-separated prose stays mandatory. Double negatives,
    instructions not to omit a method, and 'not only' do not match this grammar.
    Scientific review remains responsible for enforcing the actual constraint.
    """
    spans = []
    for prefix in _prohibition_prefixes(text):
        cursor = prefix.end()
        qualifier = re.match(r"\s*(?:(?:a|an|any|the)\s+|任何)?", text[cursor:], re.IGNORECASE)
        cursor += qualifier.end()
        while True:
            cursor = _NAME_WRAPPING.match(text, cursor).end()
            name = _PROHIBITED_NAME.match(text, cursor)
            if name is None:
                break
            end = _NAME_WRAPPING.match(text, name.end()).end()
            # 'Do not use methods other than HC3' is already outside the
            # direct-object grammar; the corresponding Chinese suffix needs
            # an explicit check because it immediately follows the identifier.
            if re.match(r"(?:以外|之外|外的)", text[end:]):
                break
            spans.append(name.span())
            separator = _PROHIBITED_LIST_SEPARATOR.match(text, end)
            if separator is None:
                break
            cursor = separator.end()
    return spans


# These are conventional programmer annotations, not scientific method names.
# Their appearance in an instruction (including a mixed-language prohibition)
# must not force an otherwise complete implementation to contain placeholders.
# Keep the full instruction in the contract; semantic review, not a lexical
# occurrence check, decides whether actual computation satisfies it. Match only
# whole markers: identifiers such as TODO_SCORE still carry their own meaning.
_PROGRAMMER_ANNOTATION_MARKERS = frozenset({"todo", "fixme"})


def method_anchors(text: str) -> set[str]:
    """General scientific identifiers, not an allowlist of known methods.

Preserving an acronym/name is necessary, not sufficient: semantic review also
checks the direction, population, covariance, correction and decision rules.
"""
    common = {"The", "This", "That", "These", "Those", "When", "Then", "With", "From", "Only",
              "Please", "Analyze", "Analysis", "Data", "Input", "Output", "Check", "Report",
              "Generate", "Create", "Save", "Read", "Compare", "Use", "Using", "Keep", "Each",
              # Sentence-initial workflow verbs are not scientific identifiers.
              # Preserve the original case here: e.g. the acronym CLEAN must
              # not disappear merely because the ordinary verb Clean can.
              "Clean", "Validate", "Summarize", "Inspect", "Calculate", "Compute", "Fit",
              "Plot", "Draw", "Visualize", "Extract", "Load", "Parse", "Convert", "Transform",
              "Filter", "Select", "Group", "Aggregate", "Sort", "Merge", "Join", "Combine",
              "Remove", "Retain", "Preserve", "Reject", "Handle", "Verify", "Ensure", "Record",
              "Return", "Write", "Export", "Describe", "Explain", "Document", "Identify",
              "Determine", "Evaluate", "Estimate", "Apply", "Perform", "Run", "Test",
              "First", "Next", "Finally", "For", "If", "Otherwise", "Before", "After"}
    prefixes = [match.span() for match in _prohibition_prefixes(text)]
    return {match.group().casefold() for match in re.finditer(
        r"(?<![A-Za-z0-9_])(?:[A-Z][A-Z0-9_-]+|[A-Z][a-z]{2,}(?:-[A-Za-z0-9]+)*)(?![A-Za-z0-9_]|['’][tT]\b)", text)
            if match.group() not in common
            and match.group().casefold() not in _PROGRAMMER_ANNOTATION_MARKERS
            and not any(start <= match.start() < end for start, end in prefixes)}


def implementation_anchors(text: str) -> set[str]:
    """Names that must occur in implementation, excluding prohibited uses.

    Extraction still preserves every scientific name in the contract. If a
    name has any non-prohibited occurrence, its implementation anchor remains.
    """
    prohibited = _prohibited_name_spans(text)
    return {token for token in method_anchors(text) if any(
        not any(start <= occurrence.start() and occurrence.end() <= end for start, end in prohibited)
        for occurrence in re.finditer(r"(?<![A-Za-z0-9_])" + re.escape(token) + r"(?![A-Za-z0-9_])", text, re.IGNORECASE))}


def _missing_anchors(statement: str, target: str, *, implementation: bool = False) -> list[str]:
    # Extraction recognizes name-shaped tokens, but their use in code or prose
    # may be lower case or snake_case components (hedges_g, HC3_cov,
    # compute_holm_adjusted). Underscores separate those name components;
    # letters and digits do not (tools is not OLS, HC30 is not HC3).
    # This checks lexical traceability only; independent review still checks
    # whether the actual computation preserves the required scientific method.
    anchors = implementation_anchors(statement) if implementation else method_anchors(statement)
    return sorted(token for token in anchors
                  if not re.search(r"(?<![A-Za-z0-9])" + re.escape(token) + r"(?![A-Za-z0-9])",
                                   target, re.IGNORECASE))


def _preserves_anchors(statement: str, target: str, *, implementation: bool = False) -> bool:
    return not _missing_anchors(statement, target, implementation=implementation)


def _missing_anchor_feedback(identifier: str, statement: str, target: str, *, locations=None, implementation: bool = False) -> dict:
    # Only bounded scientific name tokens, generated IDs and validated JSON
    # pointers enter repair feedback. Never echo surrounding source prose.
    item = {"identifier": identifier, "anchors": [token for token in _missing_anchors(statement, target, implementation=implementation)
             if re.fullmatch(r"[a-z][a-z0-9_-]{1,79}", token)]}
    if locations is not None:
        item["locations"] = list(locations)
    return item


def _unique(values: list[str], code: str) -> set[str]:
    if len(set(values)) != len(values):
        raise CoverageError(code)
    return set(values)


def validate_contract(contract: MethodContract, sources: list[dict]) -> None:
    source_map = {item["id"]: item for item in sources}
    _unique([item.id for item in contract.requirements], "duplicate_requirements")
    covered = set()
    for requirement in contract.requirements:
        refs = _unique(requirement.source_ids, "duplicate_source_references")
        if not refs <= source_map.keys():
            raise CoverageError("unknown_requirement_source", [requirement.id])
        covered.update(refs)
    excluded = _unique([item.source_id for item in contract.excluded_sources], "duplicate_exclusions")
    unknown = excluded - source_map.keys()
    overlap = covered & excluded
    missing = set(source_map) - covered - excluded
    # Give one retry all structural issues, not only the first one. Feedback
    # contains generated source identifiers, never arbitrary model strings.
    details = {key: sorted(value) for key, value in {
        "unknown_source_ids": {item for item in unknown if re.fullmatch(r"S[0-9]{3,}", item)},
        "overlapping_source_ids": overlap,
        "missing_source_ids": missing,
    }.items() if value}
    if unknown:
        raise CoverageError("unknown_source_exclusion", details.get("unknown_source_ids"), details=details)
    if overlap:
        raise CoverageError("source_exclusion_overlap", sorted(overlap), details=details)
    if missing:
        raise CoverageError("missing_source_coverage", sorted(missing), details=details)
    missing_anchors = []
    for source in sources:
        # The requested method names must survive extraction even if a model
        # labels a paragraph duplicate or a one-time instruction.
        if source["kind"] not in {"request", "method"}:
            continue
        target = "\n".join(item.statement for item in contract.requirements
                           if source["id"] in item.source_ids)
        if source["id"] in excluded:
            target = "\n".join(item.statement for item in contract.requirements)
        if not _preserves_anchors(source["text"], target):
            missing_anchors.append(_missing_anchor_feedback(source["id"], source["text"], target))
    if missing_anchors:
        raise CoverageError("method_identifiers_lost", [item["identifier"] for item in missing_anchors],
                            details={"missing_anchors": missing_anchors})


def _location(document: dict, pointer: str):
    if not re.fullmatch(COVERAGE_LOCATION_PATTERN, pointer):
        raise CoverageError("invalid_coverage_location")
    value = document
    try:
        for part in pointer.strip("/").split("/"):
            value = value[int(part)] if isinstance(value, list) else value[part]
    except (KeyError, ValueError, IndexError, TypeError):
        raise CoverageError("unknown_coverage_location") from None
    return value


def _coverage_text(value):
    """Read actual content rather than JSON's escaped representation of it.

    Structural field names are not evidence of a scientific implementation.
    Separate leaves with real newlines so adjacent fields cannot invent a name.
    """
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _coverage_text(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _coverage_text(child)


def _coverage_location_inventory(document):
    """Expose generated structure sizes and fixed schema fields, never values.

    Unknown roots, pointer text and arbitrary object keys must not enter repair
    feedback. Pending scripts have already been projected to declared slots.
    """
    result = {}
    for root in COVERAGE_ROOTS:
        items = document.get(root)
        count = len(items) if isinstance(items, list) else 0
        result[f"/{root}"] = {
            "item_count": count,
            "valid_index_range": [0, count - 1] if count else None,
            "fields": list(COVERAGE_FIELDS_BY_ROOT[root]),
        }
    return result


def validate_coverage(contract: MethodContract, document: dict, *, pending_scripts: list[dict] | None = None) -> None:
    """Validate declared locations now, deferring only not-yet-written code.

    Workflow generation may declare helper bodies that will be generated next.
    Their path, index and public fields already have a concrete schema. Only
    the anchors in their missing content can wait, and only for requirements
    explicitly assigned to that helper. Final validation omits this parameter.
    """
    pending = {}
    if pending_scripts and not document.get("scripts"):
        scripts = []
        for index, declaration in enumerate(pending_scripts):
            if (not isinstance(declaration, dict)
                    or not isinstance(declaration.get("path"), str)
                    or not re.fullmatch(r"scripts/[a-z][a-z0-9_-]{0,47}\.py", declaration["path"])
                    or any(not isinstance(declaration.get(field), str) for field in ("purpose", "invocation"))
                    or not isinstance(declaration.get("requirement_ids"), list)):
                raise CoverageError("invalid_pending_script_declaration")
            scripts.append({field: declaration[field] for field in ("path", "purpose", "invocation")}
                           | {"content": ""})
            pending[index] = declaration
        _unique([script["path"] for script in scripts], "duplicate_script_plan")
        document = {**document, "scripts": scripts}
    coverage = [RequirementCoverage.model_validate(item) for item in document.get("coverage", [])]
    ids = _unique([item.requirement_id for item in coverage], "duplicate_requirement_coverage")
    required = {item.id for item in contract.requirements}
    if ids != required:
        raise CoverageError("incomplete_requirement_coverage", sorted(required - ids))
    requirements = {item.id: item for item in contract.requirements}
    coverage_issues = []
    resolved = []
    for coverage_index, item in enumerate(coverage):
        requirement = requirements[item.requirement_id]
        sections = COVERAGE_SECTIONS_BY_KIND[requirement.kind]
        prefixes = tuple(f"/{section}/" for section in sections)
        wrong_section = not any(path.startswith(prefixes) for path in item.locations)
        def record_issue(code, location_index):
            coverage_issues.append({
                "code": code, "requirement_id": requirement.id,
                "location": ["coverage", coverage_index, "locations", location_index],
                "allowed_sections": [f"/{section}" for section in sections],
            })
        if wrong_section:
            for location_index in range(len(item.locations)):
                record_issue("requirement_wrong_section", location_index)
        values = []
        for location_index, pointer in enumerate(item.locations):
            try:
                values.append(_location(document, pointer))
            except CoverageError as exc:
                record_issue(exc.code, location_index)
        resolved.append(values)
    if coverage_issues:
        # Invalid model pointers and user-controlled field names never enter
        # feedback. Indices identify every broken slot in the redacted candidate.
        raise CoverageError(coverage_issues[0]["code"],
            sorted({issue["requirement_id"] for issue in coverage_issues}),
            details={"coverage_issues": coverage_issues,
                     "allowed_roots": [f"/{root}" for root in COVERAGE_ROOTS],
                     "available_locations": _coverage_location_inventory(document)})
    missing_anchors = []
    for item, values in zip(coverage, resolved):
        requirement = requirements[item.requirement_id]
        pending_content = False
        for pointer in item.locations:
            # Even a future script reference must point to a real declared
            # slot and one of HelperScript's fields, never arbitrary metadata.
            match = re.fullmatch(r"/scripts/(\d+)(?:/content)?", pointer)
            if match and (declaration := pending.get(int(match[1]))) is not None:
                if requirement.id not in declaration["requirement_ids"]:
                    raise CoverageError("undeclared_script_requirement", [requirement.id])
                pending_content = True
        text = "\n".join(_coverage_text(values))
        # Anchors may be split between an existing step and the pending helper.
        # An unrelated script declaration cannot defer this requirement's check.
        if not _preserves_anchors(requirement.statement, text, implementation=True) and not pending_content:
            missing_anchors.append(_missing_anchor_feedback(requirement.id, requirement.statement, text,
                                                            locations=item.locations, implementation=True))
    if missing_anchors:
        raise CoverageError("method_identifiers_lost", [item["identifier"] for item in missing_anchors],
                            details={"missing_anchors": missing_anchors})


def validate_review(review: MethodReview, contract: MethodContract, sources: list[dict]) -> None:
    source_ids = _unique([item.source_id for item in review.sources], "duplicate_source_review")
    requirement_ids = _unique([item.requirement_id for item in review.requirements], "duplicate_requirement_review")
    if source_ids != {item["id"] for item in sources} or requirement_ids != {item.id for item in contract.requirements}:
        raise CoverageError("incomplete_method_review")
    excluded = {item.source_id for item in contract.excluded_sources}
    rejected = [item.source_id for item in review.sources
                if item.status != ("valid_exclusion" if item.source_id in excluded else "preserved")]
    rejected += [item.requirement_id for item in review.requirements if item.status != "preserved"]
    if rejected or not review.implementation_appropriate:
        raise CoverageError("method_review_rejected", rejected)
