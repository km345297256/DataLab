"""Append observable coverage without rewriting an independently frozen oracle.

This boundary permits structural/diagnostic checks and additional input probes.
It cannot amend an existing datum, expected scientific value, or tolerance. The
caller must still validate method/source coverage and the public interface, then
execute the unchanged independently reviewed reference against the extended plan.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Annotated

from pydantic import Field, ValidationError, model_validator

from app.domain.services.skills.skill_creation_error import SkillPackageValidationError
from app.domain.services.skills.skill_validation import (
    CSVHeaderAssertion, CSVRowsAssertion, JSONArrayLengthAssertion,
    JSONObjectKeysAssertion, JSONValueAssertion, OutputEmptyAssertion, StderrContainsAssertion,
    SyntheticValidationCase, SyntheticValidationPlan, ValidationModel,
)

CoverageAssertion = Annotated[
    OutputEmptyAssertion | JSONObjectKeysAssertion | CSVHeaderAssertion | StderrContainsAssertion,
    Field(discriminator="type"),
]


class VerificationAssertionAddition(ValidationModel):
    script_path: str = Field(pattern=r"^scripts/[a-z][a-z0-9_-]{0,47}\.py$")
    case_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    assertion: CoverageAssertion


def _ordinary_rejection(case: SyntheticValidationCase) -> bool:
    return case.kind == "invalid" and (
        case.expected_exit_code == "nonzero"
        or type(case.expected_exit_code) is int and 0 < case.expected_exit_code < 124
    ) and all(isinstance(item, (OutputEmptyAssertion, StderrContainsAssertion)) for item in case.assertions)


def _ordinary_probe(case: SyntheticValidationCase) -> bool:
    return _ordinary_rejection(case) or (
        case.kind in {"normal", "boundary"} and type(case.expected_exit_code) is int
        and case.expected_exit_code == 0 and bool(case.assertions)
        and all(not isinstance(item, (OutputEmptyAssertion, StderrContainsAssertion)) for item in case.assertions)
    )


class VerificationNewCase(ValidationModel):
    script_path: str = Field(pattern=r"^scripts/[a-z][a-z0-9_-]{0,47}\.py$")
    case: SyntheticValidationCase

    @model_validator(mode="after")
    def ordinary_observable_probe(self):
        if not _ordinary_probe(self.case):
            raise ValueError("New coverage cases must be observable successful probes or ordinary invalid-input rejections")
        return self


class CoverageRemediation(ValidationModel):
    script_path: str = Field(pattern=r"^scripts/[a-z][a-z0-9_-]{0,47}\.py$")
    case_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    new_case_ids: list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_case_ids(self):
        if len(set(self.new_case_ids)) != len(self.new_case_ids):
            raise ValueError("Remediation case IDs must be unique")
        return self


class VerificationCoverageAmendment(ValidationModel):
    assertion_additions: list[VerificationAssertionAddition] = Field(default_factory=list)
    new_cases: list[VerificationNewCase] = Field(default_factory=list)
    remediations: list[CoverageRemediation] = Field(default_factory=list)

    @model_validator(mode="after")
    def contains_addition(self):
        if not self.assertion_additions and not self.new_cases:
            raise ValueError("Coverage amendment must add an observable check or new input case")
        return self


@dataclass(frozen=True)
class CoverageReferenceConfirmation:
    plan: SyntheticValidationPlan
    evidence: dict


def _canonical(value) -> bytes:
    # JSON preserves scalar types (including 0 versus 0.0 and true versus 1).
    # Lists retain their original order; object insertion order is immaterial.
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(",", ":")).encode()


def _require(condition):
    if not condition:
        raise SkillPackageValidationError("validation_oracle_revision_invalid")


def _pointer(pointer: str) -> tuple[str, ...]:
    return tuple(part.replace("~1", "/").replace("~0", "~")
                 for part in pointer[1:].split("/")) if pointer else ()


def _check_json_relation(parent, child):
    """Reject a newly imposed shape that contradicts an existing target."""
    ancestor, descendant = _pointer(parent.pointer), _pointer(child.pointer)
    if len(descendant) <= len(ancestor) or descendant[:len(ancestor)] != ancestor:
        return
    key = descendant[len(ancestor)]
    if isinstance(parent, JSONObjectKeysAssertion):
        _require(key in parent.keys)
    elif isinstance(parent, JSONValueAssertion):
        _require(False)  # A scalar cannot contain a nested output target.
    elif isinstance(parent, JSONArrayLengthAssertion):
        _require(key.isascii() and key.isdigit() and (key == "0" or not key.startswith("0"))
                 and len(key) <= len(str(parent.length)) and int(key) < parent.length)


def _check_assertion_addition(case, addition, previous):
    _require(isinstance(addition, (OutputEmptyAssertion, JSONObjectKeysAssertion, CSVHeaderAssertion, StderrContainsAssertion)))
    if isinstance(addition, (OutputEmptyAssertion, StderrContainsAssertion)):
        _require(_ordinary_rejection(case))
        _require(not any(type(item) is type(addition) and
            (not isinstance(addition, StderrContainsAssertion) or item.expected == addition.expected)
            for item in previous))
        return
    _require(case.expected_exit_code == 0)
    json_types = (JSONValueAssertion, JSONArrayLengthAssertion, JSONObjectKeysAssertion)
    for old in previous:
        if isinstance(old, (OutputEmptyAssertion, StderrContainsAssertion)) or old.path != addition.path:
            continue
        if isinstance(addition, CSVHeaderAssertion):
            _require(not isinstance(old, (CSVHeaderAssertion, *json_types)))
            if isinstance(old, CSVRowsAssertion):
                _require((set(old.match) | set(old.fields)) <= set(addition.columns))
        else:
            _require(not isinstance(old, (CSVRowsAssertion, CSVHeaderAssertion)))
            if isinstance(old, json_types):
                # Same target cannot be two objects with different key sets,
                # or both an object and a scalar/array. Identical duplicates
                # also add no coverage and are not allowed.
                _require(_pointer(old.pointer) != _pointer(addition.pointer))
                _check_json_relation(addition, old)
                _check_json_relation(old, addition)


def _case_observation(case):
    value = case.model_dump(mode="python")
    # A new label, kind, explanation, exit-code spelling or assertion does not
    # make the same input and invocation a new probe. Changed actual arguments
    # are meaningful (for example, checking a different threshold on the data).
    return _canonical({key: value[key] for key in ("input_files", "argv")})


def validate_coverage_extension(original: SyntheticValidationPlan,
                                revised: SyntheticValidationPlan) -> None:
    """Validate one or cumulative append-only extensions; never normalize input.

    Existing scripts/cases/assertions keep their order and every semantic JSON
    byte, including old defaults' scalar types. New cases may be appended only
    to existing supported scripts. New scientific expectations need separate
    blind reference confirmation; they cannot replace any original assertion.
    """
    try:
        SyntheticValidationPlan.model_validate(revised.model_dump(mode="json"))
    except (ValidationError, TypeError, ValueError) as exc:
        raise SkillPackageValidationError("validation_oracle_revision_invalid") from exc
    # Pydantic's JSON mode coerces float-typed defaults from 0 to 0.0.
    # Dump Python scalars first and let our strict canonical encoder preserve
    # their actual types. These schemas contain only JSON-compatible values.
    before = original.model_dump(mode="python")
    after = revised.model_dump(mode="python")
    _require(len(original.scripts) == len(revised.scripts))
    _require(len({item.script_path for item in original.scripts}) == len(original.scripts))
    additions = 0
    for old_script, new_script, trimmed in zip(original.scripts, revised.scripts, after["scripts"]):
        _require(len(new_script.cases) >= len(old_script.cases))
        for old_case, new_case, trim_case in zip(old_script.cases, new_script.cases, trimmed["cases"]):
            _require(len(new_case.assertions) >= len(old_case.assertions))
            previous = list(old_case.assertions)
            for addition in new_case.assertions[len(old_case.assertions):]:
                _check_assertion_addition(old_case, addition, previous)
                previous.append(addition)
                additions += 1
            trim_case["assertions"] = trim_case["assertions"][:len(old_case.assertions)]
        observed = {_case_observation(case) for case in new_script.cases[:len(old_script.cases)]}
        for case in new_script.cases[len(old_script.cases):]:
            _require(old_script.support == "supported" and _ordinary_probe(case))
            identity = _case_observation(case)
            _require(identity not in observed)
            observed.add(identity)
            additions += 1
        trimmed["cases"] = trimmed["cases"][:len(old_script.cases)]
    _require(_canonical(before) == _canonical(after))
    if not additions:
        raise SkillPackageValidationError("validation_oracle_revision_unchanged")


def _validate_remediations(original: SyntheticValidationPlan,
                           amendment: VerificationCoverageAmendment) -> set[tuple[str, str]]:
    original_cases = {(script.script_path, case.id): case for script in original.scripts for case in script.cases}
    added = {(item.script_path, item.case.id): item.case for item in amendment.new_cases}
    _require(len(added) == len(amendment.new_cases))
    mapped = set()
    for item in amendment.remediations:
        key = (item.script_path, item.case_id)
        _require(key in original_cases and key not in mapped)
        mapped.add(key)
        old_case = original_cases[key]
        _require(bool(item.new_case_ids) and len(set(item.new_case_ids)) == len(item.new_case_ids))
        for identifier in item.new_case_ids:
            new_key = (item.script_path, identifier)
            _require(new_key in added and new_key not in original_cases)
            new_case = added[new_key]
            _require(bool(set(new_case.requirement_ids) & set(old_case.requirement_ids)))
            _require(bool(set(new_case.source_ids) & set(old_case.source_ids)))
    return mapped


def validate_required_remediations(original: SyntheticValidationPlan,
                                   revised: SyntheticValidationPlan,
                                   amendment: VerificationCoverageAmendment,
                                   required_cases: set[tuple[str, str]]) -> None:
    """Require a real addition for each old gap; mapping never grants a verdict."""
    applied = apply_coverage_amendment(original, amendment)
    _require(_canonical(applied.model_dump(mode="python")) == _canonical(revised.model_dump(mode="python")))
    old_keys = {(script.script_path, case.id) for script in original.scripts for case in script.cases}
    _require(set(required_cases) <= old_keys)
    resolved = _validate_remediations(original, amendment)
    resolved.update((item.script_path, item.case_id) for item in amendment.assertion_additions)
    _require(set(required_cases) <= resolved)


def validate_reference_coverage_confirmation(original: SyntheticValidationPlan,
                                             proposed: SyntheticValidationPlan,
                                             confirmed: SyntheticValidationPlan) -> None:
    """Allow the blind reference to correct only new finite numeric answers.

    Input data, invocations, target locations, tolerance, case metadata and all
    original plan bytes stay fixed. Structural/diagnostic answers are never
    learned from observed outputs. This validates changes, not their accuracy;
    the caller still needs real same-source execution and provenance receipts.
    """
    if _canonical(original.model_dump(mode="python")) == _canonical(proposed.model_dump(mode="python")):
        _require(_canonical(proposed.model_dump(mode="python")) == _canonical(confirmed.model_dump(mode="python")))
        return
    validate_coverage_extension(original, proposed)
    validate_coverage_extension(original, confirmed)
    before, after = proposed.model_dump(mode="python"), confirmed.model_dump(mode="python")
    for initial, wanted, actual in zip(original.scripts, before["scripts"], after["scripts"]):
        _require(len(wanted["cases"]) == len(actual["cases"]))
        for previous_case, current_case in zip(wanted["cases"][len(initial.cases):], actual["cases"][len(initial.cases):]):
            _require(len(previous_case["assertions"]) == len(current_case["assertions"]))
            changed = False
            for previous, current in zip(previous_case["assertions"], current_case["assertions"]):
                _require(previous["type"] == current["type"])
                pairs = []
                if previous["type"] == "json_value":
                    pairs.append((previous["expected"], current["expected"], "value"))
                elif previous["type"] == "csv_rows":
                    _require(set(previous["fields"]) == set(current["fields"]))
                    pairs.append((previous, current, "count"))
                    pairs.extend((value, current["fields"][key], "value") for key, value in previous["fields"].items())
                elif previous["type"] == "json_array_length":
                    pairs.append((previous, current, "length"))
                for expected, observed, key in pairs:
                    if _canonical(expected[key]) == _canonical(observed[key]):
                        continue
                    def finite_number(value):
                        return type(value) is int or type(value) is float and math.isfinite(value)
                    _require(finite_number(expected[key]) and finite_number(observed[key]))
                    observed[key] = expected[key]
                    changed = True
            if changed:
                current_case["oracle_explanation"] = previous_case["oracle_explanation"]
    _require(_canonical(before) == _canonical(after))


def apply_coverage_amendment(original: SyntheticValidationPlan,
                             amendment: VerificationCoverageAmendment) -> SyntheticValidationPlan:
    """Copy the original and append typed additions, without round-tripping it."""
    revised = original.model_copy(deep=True)
    scripts = {item.script_path: item for item in revised.scripts}
    _require(len(scripts) == len(revised.scripts))
    for addition in amendment.assertion_additions:
        _require(addition.script_path in scripts)
        script = scripts[addition.script_path]
        cases = {item.id: item for item in script.cases}
        _require(addition.case_id in cases)
        cases[addition.case_id].assertions.append(addition.assertion.model_copy(deep=True))
    for addition in amendment.new_cases:
        _require(addition.script_path in scripts)
        scripts[addition.script_path].cases.append(addition.case.model_copy(deep=True))
    validate_coverage_extension(original, revised)
    _validate_remediations(original, amendment)
    return revised
