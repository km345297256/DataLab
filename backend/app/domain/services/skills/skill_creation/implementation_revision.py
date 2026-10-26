"""Literal, transactional source edits against one unchanged implementation.

This module only applies edits. Compilation, privacy, contract coverage and a
new independent method review must still validate the resulting recipe.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError

from app.domain.services.skills.skill_creation_error import SkillPackageValidationError
from .models import SkillRecipe
from .privacy import PrivacyContext


class _EditModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, str_strip_whitespace=False,
        revalidate_instances="always",
    )


class ImplementationEdit(_EditModel):
    script_path: StrictStr = Field(description="Exact path of an existing script in the unchanged recipe.")
    before: StrictStr = Field(min_length=1, description=(
        "Exact nonempty original source text occurring exactly once in this script. "
        "Include enough unchanged surrounding context to make the match unique. "
        "Preserve whitespace and line endings; all edits refer to the original source."
    ))
    after: StrictStr = Field(description=(
        "Complete replacement for that exact original text, including any retained context. "
        "An empty string deletes the selected text."
    ))


class ImplementationEdits(_EditModel):
    edits: list[ImplementationEdit] = Field(min_length=1, description=(
        "Nonoverlapping changes to existing scripts only. Every before must uniquely match "
        "the unchanged original script, never the result of another edit. Do not return "
        "metadata, workflow changes, coordinates, or unchanged replacements."
    ))


EditFailure = Literal[
    "invalid_edits", "invalid_recipe", "unknown_script", "anchor_missing",
    "anchor_ambiguous", "overlapping_edits", "unchanged_edit", "unchanged_result", "invalid_result",
]


class ImplementationEditError(SkillPackageValidationError):
    """Fixed diagnostics contain neither source text nor model-provided paths."""

    def __init__(self, reason: EditFailure, *edit_indexes: int):
        super().__init__("implementation_revision_invalid", details={
            "reason": reason, "edit_indexes": sorted(edit_indexes),
        })


def apply_implementation_edits(
    recipe: SkillRecipe, edits: ImplementationEdits | dict,
) -> SkillRecipe:
    """Apply an entire transaction, preserving all unselected recipe bytes.

    Match every edit before constructing any changed script. Search a second
    occurrence from the next character so overlapping matches are ambiguous,
    too. Reverse splicing then preserves the original offsets for every edit.
    The input objects are never mutated, including when any edit is rejected.
    """
    try:
        transaction = ImplementationEdits.model_validate(edits)
    except (ValidationError, TypeError, ValueError):
        # Pydantic's error may embed model-supplied source. Only this fixed
        # diagnostic belongs in a surfaced exception or repair message.
        raise ImplementationEditError("invalid_edits") from None
    if not isinstance(recipe, SkillRecipe):
        raise ImplementationEditError("invalid_recipe")
    originals = {script.path: script.content for script in recipe.scripts}
    if len(originals) != len(recipe.scripts):
        raise ImplementationEditError("invalid_recipe")

    spans: dict[str, list[tuple[int, int, int, str]]] = defaultdict(list)
    for index, edit in enumerate(transaction.edits):
        if edit.script_path not in originals:
            raise ImplementationEditError("unknown_script", index)
        if edit.before == edit.after:
            raise ImplementationEditError("unchanged_edit", index)
        source = originals[edit.script_path]
        start = source.find(edit.before)
        if start < 0:
            raise ImplementationEditError("anchor_missing", index)
        if source.find(edit.before, start + 1) >= 0:
            raise ImplementationEditError("anchor_ambiguous", index)
        spans[edit.script_path].append((start, start + len(edit.before), index, edit.after))

    replacements: dict[str, str] = {}
    for path, selected in spans.items():
        selected.sort()
        for left, right in zip(selected, selected[1:]):
            if left[1] > right[0]:
                raise ImplementationEditError("overlapping_edits", left[2], right[2])
        source = originals[path]
        for start, end, _index, replacement in reversed(selected):
            source = source[:start] + replacement + source[end:]
        replacements[path] = source
    if all(replacements[path] == originals[path] for path in replacements):
        raise ImplementationEditError("unchanged_result")

    revised = recipe.model_copy(deep=True)
    for script in revised.scripts:
        if script.path in replacements:
            script.content = replacements[script.path]
    return revised


@dataclass(frozen=True)
class _ImplementationRevisionPrivacy(PrivacyContext):
    """Request-owned edit validation against the complete unchanged recipe.

    A partial expression is not prose and may lack the syntax context needed
    for source-literal privacy rules. Only a valid transaction gets that
    context back, by merging into the already checked original recipe.
    """
    _original: SkillRecipe = field(repr=False, compare=False, kw_only=True)
    _base: PrivacyContext = field(repr=False, compare=False, kw_only=True)

    def scan_document(self, value, *, limit=32):
        if type(value) is ImplementationEdits:
            merged = apply_implementation_edits(self._original, value)
            return self._base.scan_document(merged, limit=limit)
        return self._base.scan_document(value, limit=limit)

    def redact_document(self, value):
        if type(value) is ImplementationEdits or isinstance(value, dict) and "edits" in value:
            try:
                transaction = ImplementationEdits.model_validate(value)
                merged = apply_implementation_edits(self._original, transaction)
                if self._base.scan_document(merged, limit=None):
                    return None
            except (ValidationError, ImplementationEditError):
                return None
            # These exact anchors belong to a fully public merged document.
            # Redacting them as prose would corrupt the next literal match.
            return transaction.model_dump(mode="json")
        return self._base.redact_document(value)


def implementation_revision_privacy(context: PrivacyContext, previous) -> PrivacyContext:
    """Bind an isolated privacy view to a host-supplied public original."""
    from .package import validate_portable_document

    base = PrivacyContext(context.entries,
                          generated_output_filenames=context.generated_output_filenames)
    try:
        original = SkillRecipe.model_validate(previous).model_copy(deep=True)
    except (ValidationError, TypeError, ValueError):
        raise ImplementationEditError("invalid_recipe") from None
    validate_portable_document(original, base)
    return _ImplementationRevisionPrivacy(
        entries=base.entries, generated_output_filenames=base.generated_output_filenames,
        _original=original, _base=base,
    )
