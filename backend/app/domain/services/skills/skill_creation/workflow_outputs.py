"""Assign portable names to proven historical outputs before code is authored.

This is a host-owned authoring transformation, never a privacy exemption or a
source-code rewrite. An ambiguous/input reference leaves the original document
intact for the normal privacy checks to reject. Repair feedback exposes only
redacted references and schema coordinates, never the private mapping. All
scientific, coverage and implementation checks still run.
"""
from dataclasses import dataclass
import re

from .privacy import PrivacyContext
from .workflow_authoring import WorkflowDraft

_BASENAME = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z][A-Za-z0-9]{0,11}")
# A declared filename must be a complete prose token, not a substring of an
# undeclared Unicode or punctuation-bearing filename. Unknown separators stay
# private rather than growing an incomplete blacklist of filename characters.
_NON_SEPARATOR = r"[^\s" + re.escape("\"'`()[]<>,;!?，。；、（）【】《》“”‘’：！？") + "]"
_REFERENCE_FIELDS = {
    "outputs": ("name", "content", "acceptance"),
    "steps": ("action", "decision", "verification"),
    "validation_cases": ("expected",),
}
# For locating an invocation reference only, a basename can follow a path or
# assignment separator. This does not establish the path's role or authorize a
# rewrite; the model must resolve the invocation under the ordinary checks.
_INVOCATION_NON_SEPARATOR = r"[^\s" + re.escape("\"'`()[]<>,;!?，。；、（）【】《》“”‘’：！？/\\=") + "]"


@dataclass(frozen=True)
class _OutputNameBinding:
    output_index: int
    original_name: str
    portable_name: str


def _plan_output_names(draft: WorkflowDraft, privacy: PrivacyContext) -> tuple[_OutputNameBinding, ...]:
    """Keep private provenance and collision allocation in one host-only place."""
    if not isinstance(draft, WorkflowDraft) or not privacy.generated_output_filenames:
        return ()
    declared = [output.name for output in draft.outputs]
    if len({name.casefold() for name in declared}) != len(declared):
        return ()
    private_names = {item.value for item in privacy.entries if item.category == "source_filename"}
    candidates = [(index, name) for index, name in enumerate(declared)
                  if name in privacy.generated_output_filenames and name in private_names
                  and _BASENAME.fullmatch(name)
                  and all(issue.category == "source_filename" for issue in privacy.scan_document(name))]
    occupied = {name.casefold() for name in declared}
    result = []
    serial = 1
    for index, name in candidates:
        extension = name.rsplit(".", 1)[1]
        # A fixed bounded namespace prevents hostile names from creating an
        # unbounded search. Partial allocation is never returned.
        while serial <= len(privacy.entries) + len(declared) + len(candidates) + 1:
            alias = f"output_{serial:03d}.{extension}"
            serial += 1
            if alias.casefold() not in occupied and not privacy.scan_document(alias):
                result.append(_OutputNameBinding(index, name, alias))
                occupied.add(alias.casefold())
                break
        else:
            return ()
    return tuple(result)


def portable_output_repair_bindings(draft: WorkflowDraft, privacy: PrivacyContext) -> list[dict]:
    """Describe proven output names without changing inputs, paths or content.

    Coordinates identify authored fields needing a consistent portable name;
    they do not prove producer/consumer relationships or equal directories.
    The original draft still has to pass every existing validation gate.
    """
    bindings = []
    for planned in _plan_output_names(draft, privacy):
        current = privacy.redact_text(planned.original_name)
        if current == planned.original_name or privacy.scan_document(current):
            continue
        # An opaque filename alias is required. Partial redaction or another
        # private category's marker cannot establish this filename identity.
        if not re.fullmatch(r"\{input_file_[1-9][0-9]*\}", current):
            continue
        pattern = re.compile(r"(?<!" + _NON_SEPARATOR + ")" + re.escape(planned.original_name) +
                             r"(?!" + _NON_SEPARATOR + ")")
        locations = []
        for root, fields in _REFERENCE_FIELDS.items():
            for index, item in enumerate(getattr(draft, root)):
                for field in fields:
                    text = getattr(item, field)
                    if "```" not in text and "~~~" not in text and pattern.search(text):
                        locations.append([root, index, field])
        invocation_pattern = re.compile(r"(?<!" + _INVOCATION_NON_SEPARATOR + ")" +
                                        re.escape(planned.original_name) + r"(?!" + _NON_SEPARATOR + ")")
        if draft.implementation:
            for index, script in enumerate(draft.implementation.scripts):
                if invocation_pattern.search(script.invocation):
                    locations.append(["implementation", "scripts", index, "invocation"])
                if script.interface_contract is not None:
                    for output_index, output in enumerate(script.interface_contract.outputs):
                        if output.path.rsplit("/", 1)[-1] == planned.original_name:
                            locations.append(["implementation", "scripts", index,
                                              "interface_contract", "outputs", output_index, "path"])
        bindings.append({"output_index": planned.output_index, "portable_name": planned.portable_name,
                         "current_reference": current, "locations": locations})
    return bindings


def normalize_generated_output_names(draft: WorkflowDraft, privacy: PrivacyContext) -> WorkflowDraft:
    """Copy only when every original privacy issue is a safe output reference."""
    if not isinstance(draft, WorkflowDraft) or not privacy.generated_output_filenames:
        return draft
    before = privacy.scan_document(draft)
    if not before or any(issue.category != "source_filename" for issue in before):
        return draft
    plan = _plan_output_names(draft, privacy)
    if not plan:
        return draft
    mapping = {item.original_name: item.portable_name for item in plan}
    # Match full filename tokens once. Do not rewrite paths, URL components,
    # prefixes/suffixes or cascading aliases, even in an output-oriented field.
    pattern = re.compile(r"(?<!" + _NON_SEPARATOR + r")(?:" +
                         "|".join(re.escape(name) for name in sorted(mapping, key=len, reverse=True)) +
                         r")(?!" + _NON_SEPARATOR + ")")
    document = draft.model_dump(mode="python")
    for root, fields in _REFERENCE_FIELDS.items():
        for item in document[root]:
            for key in fields:
                text = item[key]
                # Workflow prose may mention files in inline code, but fenced
                # source examples are not an authorized code rewrite surface.
                if "```" not in text and "~~~" not in text:
                    item[key] = pattern.sub(lambda match: mapping[match.group()], text)
    candidate = type(draft).model_validate(document)
    if privacy.scan_document(candidate):
        return draft
    return candidate
