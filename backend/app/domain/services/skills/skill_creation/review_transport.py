"""Deterministic transport recovery for an already adverse independent review.

Only missing object closers at a uniquely owned schema sibling and surplus
terminal closers are repairable. No token carrying information is changed.
The schema and the caller's exact coverage check remain authoritative; this
module never asks a reviewer to vote again or repairs an accepted response.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator


class ReviewTransportRejected(ValueError):
    pass


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


class ClosureEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    operation: Literal["insert", "delete"]
    offset: StrictInt = Field(ge=0, le=4 * 1024 * 1024)
    token: Literal["}", "]"]

    @model_validator(mode="after")
    def insertion_is_object_only(self):
        if self.operation == "insert" and self.token != "}":
            raise ValueError("Only a missing object closer can be inserted")
        return self


class ReviewTransportProof(BaseModel):
    """Host-only provenance. It is never a field in a model response schema."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[1]
    original_response_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    repaired_document_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    negative_code: Literal["method_review_rejected", "reference_calculation_unconfirmed",
        "validation_oracle_rejected", "validation_oracle_uncertain",
        "candidate_method_rejected", "candidate_method_uncertain"]
    edits: tuple[ClosureEdit, ...] = Field(min_length=1, max_length=128)

    @field_validator("version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("Unsupported transport proof version")
        return value

    @model_validator(mode="after")
    def ordered_edits(self):
        offsets = [edit.offset for edit in self.edits]
        if offsets != sorted(offsets) or len(offsets) != len(set(offsets)):
            raise ValueError("Closure edits must have unique ordered offsets")
        deleting = False
        for edit in self.edits:
            if edit.operation == "delete":
                deleting = True
            elif deleting:
                raise ValueError("Only terminal closers may be deleted")
        return self


_PROOF_ATTRIBUTE = "_host_review_transport_proof"


def review_transport_proof(value):
    proof = getattr(value, _PROOF_ATTRIBUTE, None)
    if proof is not None and not isinstance(proof, ReviewTransportProof):
        raise ReviewTransportRejected("Untrusted transport provenance")
    return proof


def _has_adverse_code(value, code):
    from app.domain.services.skills.skill_candidate_method_audit import CandidateMethodAudit
    from app.domain.services.skills.method_contract import MethodReview
    from app.domain.services.skills.skill_validation_review import ValidationPlanReview
    if isinstance(value, CandidateMethodAudit):
        return value.verdict() in {"rejected", "uncertain"} and code == "candidate_method_" + value.verdict()
    if isinstance(value, ValidationPlanReview):
        return value.verdict() in {"rejected", "uncertain"} and code == "validation_oracle_" + value.verdict()
    if isinstance(value, MethodReview):
        return (code in {"method_review_rejected", "reference_calculation_unconfirmed"}
                and (not value.implementation_appropriate
                     or any(item.status != "preserved" for item in value.requirements)
                     or any(item.status not in {"preserved", "valid_exclusion"} for item in value.sources)))
    return False


def bind_review_transport_proof(value, proof, *, negative_code):
    """Revalidate a host sidecar against the typed, still adverse document."""
    proof = ReviewTransportProof.model_validate(proof.model_dump(mode="json") if isinstance(proof, ReviewTransportProof) else proof)
    if (proof.negative_code != negative_code or not _has_adverse_code(value, negative_code)
            or proof.repaired_document_sha256 != _digest(value.model_dump(mode="json"))):
        raise ReviewTransportRejected("Transport provenance does not bind this adverse review")
    object.__setattr__(value, _PROOF_ATTRIBUTE, proof)
    return value


@dataclass(frozen=True)
class _Token:
    raw: str
    value: object
    start: int
    end: int


def _tokens(text, offset=0):
    decoder = json.JSONDecoder()
    tokens = []
    position = 0
    while position < len(text):
        if text[position] in " \t\r\n":
            position += 1
            continue
        start = position
        if text[position] in "{}[]:,":
            position += 1
            value = text[start:position]
        else:
            try:
                value, position = decoder.raw_decode(text, position)
            except (ValueError, RecursionError) as exc:
                raise ReviewTransportRejected("Invalid JSON token") from exc
            if isinstance(value, (dict, list)) or (isinstance(value, float) and not math.isfinite(value)):
                raise ReviewTransportRejected("Invalid JSON scalar")
            if position < len(text) and text[position] not in " \t\r\n{}[]:,":
                raise ReviewTransportRejected("Invalid JSON token boundary")
        tokens.append(_Token(text[start:position], value, offset + start, offset + position))
    return tokens


@dataclass
class _Object:
    properties: dict
    required: set
    seen: set


class _ClosureParser:
    def __init__(self, tokens, schema):
        self.tokens, self.schema, self.position, self.edits = tokens, schema, 0, []

    def peek(self, offset=0):
        index = self.position + offset
        return self.tokens[index] if index < len(self.tokens) else None

    def take(self, raw):
        token = self.peek()
        if token is None or token.raw != raw:
            raise ReviewTransportRejected("Unexpected JSON structure")
        self.position += 1
        return token

    def resolve(self, schema):
        if "$ref" in schema:
            reference = schema["$ref"]
            if not reference.startswith("#/$defs/"):
                raise ReviewTransportRejected("Unsupported review schema")
            return self.schema["$defs"][reference.removeprefix("#/$defs/")]
        return schema

    def value(self, schema, ancestors=(), depth=0):
        if depth > 64 or len(self.edits) >= 128:
            raise ReviewTransportRejected("Review recovery bound exceeded")
        schema = self.resolve(schema)
        kind = schema.get("type")
        if kind == "object":
            self.take("{")
            frame = _Object(schema["properties"], set(schema.get("required", [])), set())
            result = {}
            if self.peek() and self.peek().raw == "}":
                self.take("}")
                return result
            while True:
                key = self.peek()
                if key is None or not key.raw.startswith('"') or not isinstance(key.value, str):
                    raise ReviewTransportRejected("Missing object key")
                if key.value not in frame.properties or key.value in frame.seen:
                    raise ReviewTransportRejected("Unknown or duplicate review key")
                self.position += 1
                self.take(":")
                frame.seen.add(key.value)
                result[key.value] = self.value(frame.properties[key.value], (*ancestors, frame), depth + 1)
                separator = self.peek()
                if separator is not None and separator.raw == "}":
                    self.take("}")
                    return result
                if separator is None or separator.raw != ",":
                    raise ReviewTransportRejected("Missing object separator or closer")
                next_key = self.peek(1)
                if next_key is None or not next_key.raw.startswith('"') or not isinstance(next_key.value, str):
                    raise ReviewTransportRejected("Missing sibling key")
                if next_key.value not in frame.properties:
                    # A missing } can only expose a sibling of an open object.
                    # Array boundaries cannot be crossed: no ] is inserted.
                    possible = []
                    for ancestor in reversed(ancestors):
                        if ancestor is None:
                            break
                        if next_key.value in ancestor.properties:
                            possible.append(ancestor)
                    if (len(possible) != 1 or next_key.value in possible[0].seen
                            or not frame.required <= frame.seen):
                        raise ReviewTransportRejected("Ambiguous or incomplete missing object closer")
                    self.edits.append(ClosureEdit(operation="insert", offset=separator.start, token="}"))
                    return result  # The unchanged comma belongs to the ancestor.
                self.take(",")
        if kind == "array":
            self.take("[")
            result = []
            if self.peek() and self.peek().raw == "]":
                self.take("]")
                return result
            while True:
                result.append(self.value(schema["items"], (*ancestors, None), depth + 1))
                if self.peek() and self.peek().raw == "]":
                    self.take("]")
                    return result
                self.take(",")
        token = self.peek()
        if token is None or token.raw in "{}[]:,":
            raise ReviewTransportRejected("Missing review scalar")
        # Final strict schema validation checks enums, formats and scalar types.
        self.position += 1
        return token.value


def recover_review_closures(answer, schema, *, negative_code):
    """Return an unchanged-information typed adverse review and host proof.

    The caller must first observe a known adverse judgment, and must still
    apply its exact target/privacy check before making this review usable.
    Missing EOF closers, commas, quotes, values, array closers, and internal
    surplus closers are deliberately outside this grammar.
    """
    from app.domain.services.skills.method_contract import MethodReview
    from app.domain.services.skills.skill_candidate_method_audit import CandidateMethodAudit
    from app.domain.services.skills.skill_validation_review import ValidationPlanReview
    if schema not in (MethodReview, ValidationPlanReview, CandidateMethodAudit) or negative_code is None or len(answer) > 4 * 1024 * 1024:
        raise ReviewTransportRejected("Unsupported review recovery")
    fenced = re.fullmatch(r"\s*```(?:json)?[ \t]*\r?\n(.*?)\r?\n?```\s*", answer,
                         flags=re.IGNORECASE | re.DOTALL)
    text, offset = (fenced.group(1), fenced.start(1)) if fenced else (answer, 0)
    tokens = _tokens(text, offset)
    parser = _ClosureParser(tokens, schema.model_json_schema())
    try:
        document = parser.value(parser.schema)
        for token in tokens[parser.position:]:
            if token.raw not in ("}", "]"):
                raise ReviewTransportRejected("Multiple documents or nonterminal surplus tokens")
            parser.edits.append(ClosureEdit(operation="delete", offset=token.start, token=token.raw))
        if not parser.edits or len(parser.edits) > 128:
            raise ReviewTransportRejected("No bounded closure-only recovery")
        # Independently decode the edited bytes. The parser is not allowed to
        # silently discard a field or structural token while building its tree.
        repaired = text
        for edit in reversed(parser.edits):
            position = edit.offset - offset
            repaired = (repaired[:position] + "}" + repaired[position:] if edit.operation == "insert"
                        else repaired[:position] + repaired[position + 1:])
        if _canonical(json.loads(repaired)) != _canonical(document):
            raise ReviewTransportRejected("Closure edits do not reproduce the document")
        value = schema.model_validate(document, strict=True)
        if not _has_adverse_code(value, negative_code):
            raise ReviewTransportRejected("Recovered judgment differs from the observed adverse review")
        # Validators may trim/coerce values; a transport repair may not do so.
        if _canonical(document) != _canonical(value.model_dump(mode="json")):
            raise ReviewTransportRejected("Schema normalization would change the original information")
        proof = ReviewTransportProof(version=1, original_response_sha256=hashlib.sha256(answer.encode()).hexdigest(),
            repaired_document_sha256=_digest(document), negative_code=negative_code, edits=tuple(parser.edits))
    except (ValueError, KeyError, RecursionError, TypeError) as exc:
        raise ReviewTransportRejected("No unique complete schema-preserving recovery") from exc
    return value, proof
