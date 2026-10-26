"""Private, expiring checkpoints for validated skill authoring work.

The cache is an optimization, never a validation authority. Reads run the
current schema, privacy and domain checks again. Source text, prompts, raw
responses and provider credentials only contribute to hashes; they are not
stored. This state is not a skill draft and has no public download/install API.
"""
from __future__ import annotations

from dataclasses import dataclass
import asyncio
import hashlib
import inspect
import json
import logging
import math
import time
from typing import Any, Awaitable, Callable, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from app.domain.external.cache import Cache
from app.domain.services.skills.method_contract import MethodReview
from app.domain.services.skills.skill_candidate_method_audit import CandidateMethodAudit
from app.domain.services.skills.skill_creation_error import SkillCreationError
from app.domain.services.skills.skill_validation import SyntheticValidationPlan
from app.domain.services.skills.skill_validation_review import ValidationPlanReview
from .models import HelperScript, SkillRecipe
from .package import validate_portable_document
from .privacy import ensure_context, generation_privacy
from .review_transport import ReviewTransportRejected, bind_review_transport_proof, review_transport_proof

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)
DEFAULT_TTL_SECONDS = 24 * 60 * 60
CHECKPOINT_STAGES = frozenset({
    "method_requirements", "workflow", "implementation", "implementation_revision", "method_review",
    "synthetic_validation_plan", "synthetic_plan_review", "synthetic_script_repair", "synthetic_build_repair",
    "synthetic_method_audit", "synthetic_method_repair",
    "synthetic_reference_implementation", "synthetic_reference_review",
})
ADVERSE_CODES = frozenset({
    "method_review_rejected", "validation_oracle_rejected", "validation_oracle_uncertain",
    "reference_calculation_unconfirmed",
    "candidate_method_rejected", "candidate_method_uncertain",
})
REVIEW_STAGES = frozenset({"method_review", "synthetic_plan_review", "synthetic_reference_review", "synthetic_method_audit"})


def content_digest(value: Any) -> str:
    """Digest locally; neither unhashed inputs nor credentials enter storage."""
    def encode(item):
        if isinstance(item, BaseModel):
            return item.model_dump(mode="json")
        raise TypeError("Checkpoint identity must contain JSON-compatible values")
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False, default=encode).encode()).hexdigest()


@dataclass(frozen=True)
class BuildScope:
    identity: str
    artifacts: str

    @classmethod
    def create(cls, *, owner_id: str, session_id: str, source: Any,
               configuration: Any, code_version: str) -> "BuildScope":
        if not owner_id or not session_id or not code_version:
            raise ValueError("Checkpoint scope requires owner, session and code version")
        # Negative scientific decisions belong to the source and candidate,
        # not the provider/settings. Changing a model must not erase a verdict.
        identity = content_digest({"owner": owner_id, "session": session_id, "source": source})
        return cls(identity, content_digest({"identity": identity,
            "configuration": configuration, "code_version": code_version}))


class RejectedMethodRevision(BaseModel):
    """Latest rejected portable candidate, retained only for substantive repair."""
    model_config = ConfigDict(extra="forbid")
    recipe: SkillRecipe
    review: MethodReview
    contract_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class RejectedOracleRevision(BaseModel):
    """Private frozen test intent and the exact independent adverse assessment."""
    model_config = ConfigDict(extra="forbid")
    plan: SyntheticValidationPlan
    review: ValidationPlanReview
    contract_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    interfaces_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class RejectedReferenceRevision(BaseModel):
    """Latest blind reference and its real adverse review, never test answers."""
    model_config = ConfigDict(extra="forbid")
    reference_scripts: list[HelperScript] = Field(min_length=1)
    review: MethodReview
    blind_context_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class FailedExecutionRevision(BaseModel):
    """Static-valid script candidate and a frozen independent synthetic oracle."""
    model_config = ConfigDict(extra="forbid")
    recipe: SkillRecipe
    plan: SyntheticValidationPlan
    execution: dict
    contract_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    original_recipe_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    interfaces_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    repair_used: StrictBool = False
    method_audit: CandidateMethodAudit | None = None
    pending_recipe: SkillRecipe | None = None
    pending_review: MethodReview | None = None


def _adverse_review(review: MethodReview) -> bool:
    return (not review.implementation_appropriate
        or any(item.status != "preserved" for item in review.requirements)
        or any(item.status not in {"preserved", "valid_exclusion"} for item in review.sources))


def _adverse_code(value, stage):
    if stage == "synthetic_method_audit" and isinstance(value, CandidateMethodAudit):
        if value.verdict() in {"rejected", "uncertain"}:
            return "candidate_method_" + value.verdict()
    if isinstance(value, MethodReview) and _adverse_review(value):
        return ("reference_calculation_unconfirmed" if stage == "synthetic_reference_review"
                else "method_review_rejected")
    if stage == "synthetic_plan_review" and callable(getattr(value, "verdict", None)):
        verdict = value.verdict()
        if verdict in {"rejected", "uncertain"}:
            return "validation_oracle_" + verdict
    return None


def _transport_sidecar(value, stage):
    proof = review_transport_proof(value)
    if proof is None:
        return {}
    bind_review_transport_proof(value, proof, negative_code=_adverse_code(value, stage))
    return {"review_transport": proof.model_dump(mode="json")}


def _restore_transport(value, record, stage):
    if "review_transport" in record:
        try:
            bind_review_transport_proof(value, record["review_transport"], negative_code=_adverse_code(value, stage))
        except ValueError as exc:
            raise ReviewTransportRejected("Invalid persisted review transport provenance") from exc
    return value


async def _check(value, privacy, check):
    validate_portable_document(value, privacy)
    if check is not None:
        result = check(value)
        if inspect.isawaitable(result):
            await result


class BuildCheckpoints:
    """One owned build attempt; Redis enables continuation on a later retry.

    ``run`` accepts a zero-argument ``produce`` callback around the ordinary
    generator. It must still run the ordinary generation checks on a miss.
    ``decision_identity`` may supply a host-derived semantic candidate digest
    (e.g. normalized Python AST + method contract) for review stages. Otherwise
    the complete immutable review payload identifies the decision.

    The caller owns per-session authoring serialization and chooses when to
    save/reuse a rejected revision. No background execution or installation is
    started here. Redis failures degrade to the normal freshly validated path;
    known adverse decisions remain sticky within this attempt even on outage.
    """
    def __init__(self, cache: Cache | None, scope: BuildScope, *, privacy=None,
                 ttl_seconds: int = DEFAULT_TTL_SECONDS, clock: Callable[[], float] = time.time,
                 cache_io_timeout_seconds: float = 2.0):
        if type(ttl_seconds) is not int or ttl_seconds <= 0:
            raise ValueError("Checkpoint TTL must be a positive number of seconds")
        if (type(cache_io_timeout_seconds) not in (int, float)
                or not math.isfinite(cache_io_timeout_seconds) or cache_io_timeout_seconds <= 0):
            raise ValueError("Checkpoint storage timeout must be a positive finite number")
        self.cache, self.scope, self.privacy = cache, scope, privacy
        self.cache_io_timeout_seconds = cache_io_timeout_seconds
        self._remote_cache_available = cache is not None
        self.ttl_seconds, self.clock = ttl_seconds, clock
        self._local: dict[str, dict] = {}
        self._accessed_artifacts: list[tuple[str, str]] = []

    def mark(self) -> int:
        """Start a caller-owned authoring transaction, including cache hits."""
        return len(self._accessed_artifacts)

    async def discard_since(self, mark: int, *, stages: set[str] | frozenset[str]) -> None:
        """Evict a failed revision's artifacts without deleting review decisions.

        Successful static authoring may still fail the later substantive-change
        gate. Such a candidate must not become a permanent cached retry loop.
        Only artifacts accessed since this local marker can be discarded; no
        cache scan, another account's records or scientific verdict is touched.
        """
        if type(mark) is not int or not 0 <= mark <= len(self._accessed_artifacts):
            raise ValueError("Invalid checkpoint transaction marker")
        if not stages <= CHECKPOINT_STAGES:
            raise ValueError("Unknown checkpoint stage")
        for stage, key in set(self._accessed_artifacts[mark:]):
            if stage in stages:
                await self._discard(key)

    def _key(self, category, binding):
        return f"datalab:skill-build:v1:{category}:{binding}"

    async def _get(self, key, binding, *, strict=False):
        raw = self._local.get(key)
        if raw is None and self._remote_cache_available:
            try:
                async with asyncio.timeout(self.cache_io_timeout_seconds):
                    raw = await self.cache.get(key)
            except Exception as exc:
                self._remote_cache_available = False
                logger.warning("Skill build checkpoint read unavailable error_type=%s", type(exc).__name__)
        def invalid():
            if strict:
                raise SkillCreationError("synthetic_execution", "synthetic_execution_failed")
            return None
        if raw is None:
            return None
        if (not isinstance(raw, dict) or raw.get("version") != 1 or raw.get("binding") != binding
                or strict and type(raw.get("version")) is not int):
            return invalid()
        deadline = raw.get("expires_at")
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            return invalid()
        if not self.clock() < deadline:
            self._local.pop(key, None)
            return None
        # A fresh JSON copy prevents an in-memory cache implementation or a
        # caller's model mutation from modifying the stored checkpoint.
        try:
            return json.loads(json.dumps(raw, allow_nan=False))
        except (TypeError, ValueError):
            return invalid()

    async def _put(self, key, binding, value):
        entry = {"version": 1, "binding": binding,
            "expires_at": self.clock() + self.ttl_seconds, **value}
        entry = json.loads(json.dumps(entry, allow_nan=False))
        self._local[key] = entry
        if self._remote_cache_available:
            try:
                async with asyncio.timeout(self.cache_io_timeout_seconds):
                    stored = bool(await self.cache.set(key, entry, ttl=self.ttl_seconds))
                # The production adapter returns False for a handled outage.
                # A successful read miss remains distinguishable from this.
                if not stored:
                    self._remote_cache_available = False
                return stored
            except Exception as exc:
                self._remote_cache_available = False
                logger.warning("Skill build checkpoint write unavailable error_type=%s", type(exc).__name__)
        return False

    async def _discard(self, key):
        self._local.pop(key, None)
        if self._remote_cache_available:
            try:
                async with asyncio.timeout(self.cache_io_timeout_seconds):
                    await self.cache.delete(key)
            except Exception as exc:
                self._remote_cache_available = False
                logger.warning("Skill build checkpoint discard unavailable error_type=%s", type(exc).__name__)

    def _decision_binding(self, stage, payload, decision_identity):
        return content_digest({"identity": self.scope.identity, "stage": stage,
            "candidate": payload if decision_identity is None else decision_identity})

    async def run(self, schema: type[T], stage: str, instruction: str, payload: Any,
                  check: Callable[[T], Any] | None, produce: Callable[[], Awaitable[T]],
                  *, decision_identity: Any = None) -> T:
        if stage not in CHECKPOINT_STAGES:
            return await produce()
        privacy = generation_privacy(self.privacy, schema, stage, payload)
        binding = content_digest({"scope": self.scope.artifacts, "stage": stage,
            "schema": schema.model_json_schema(), "instruction": instruction, "payload": payload})
        key = self._key("artifact", binding)
        self._accessed_artifacts.append((stage, key))
        decision = self._decision_binding(stage, payload, decision_identity)
        rejection = await self._get(self._key("adverse", decision), decision)
        if rejection and rejection.get("code") in ADVERSE_CODES:
            # A valid adverse MethodReview can drive the normal revision path.
            # If its schema/checks changed, retain the rejection rather than
            # soliciting a different scientific answer for the same candidate.
            if rejection.get("schema") == content_digest(schema.model_json_schema()):
                try:
                    value = schema.model_validate(rejection["value"])
                    if _adverse_code(value, stage) != rejection["code"]:
                        raise ValueError("Inconsistent cached adverse verdict")
                    _restore_transport(value, rejection, stage)
                    await _check(value, privacy, check)
                    return value
                except Exception:
                    pass
            raise SkillCreationError(stage, rejection["code"])
        cached = await self._get(key, binding)
        if cached is not None:
            value = None
            try:
                value = schema.model_validate(cached["value"])
                _restore_transport(value, cached, stage)
                await _check(value, privacy, check)
                return value
            except SkillCreationError as exc:
                if stage in REVIEW_STAGES and exc.code in ADVERSE_CODES:
                    await self._remember_rejection(decision, exc.code)
                    raise
                await self._discard(key)
            except Exception as exc:
                code = _adverse_code(value, stage)
                if code is not None:
                    await self._remember_rejection(decision, code)
                    raise SkillCreationError(stage, code) from exc
                # Old, malformed or newly invalid artifacts cannot bypass
                # current checks. Their contents/exception text never get logged.
                await self._discard(key)
        value = None
        try:
            produced = await produce()
            value = schema.model_validate(produced.model_dump(mode="json"))
            _restore_transport(value, _transport_sidecar(produced, stage), stage)
            await _check(value, privacy, check)
        except SkillCreationError as exc:
            if stage in REVIEW_STAGES and exc.code in ADVERSE_CODES:
                await self._remember_rejection(decision, exc.code)
            raise
        except Exception as exc:
            code = _adverse_code(value, stage)
            if code is not None:
                await self._remember_rejection(decision, code)
                raise SkillCreationError(stage, code) from exc
            raise
        code = _adverse_code(value, stage)
        if code is not None:
            await self._remember_rejection(decision, code, value=value)
        await self._put(key, binding, {"value": value.model_dump(mode="json"), **_transport_sidecar(value, stage)})
        return value

    async def _remember_rejection(self, decision, code, *, value=None):
        # There is no positive-write operation on this key. A fresh model
        # response cannot overwrite a stored adverse scientific decision.
        key = self._key("adverse", decision)
        if await self._get(key, decision) is None:
            record = {"code": code}
            if value is not None:
                record.update(value=value.model_dump(mode="json"),
                              schema=content_digest(type(value).model_json_schema()), **_transport_sidecar(value, "synthetic_method_audit" if isinstance(value, CandidateMethodAudit) else
                                  "synthetic_plan_review" if isinstance(value, ValidationPlanReview) else
                                  "synthetic_reference_review" if code == "reference_calculation_unconfirmed" else "method_review"))
            await self._put(key, decision, record)

    async def get_revision(self, *, check=None) -> RejectedMethodRevision | None:
        binding = content_digest({"scope": self.scope.artifacts, "slot": "method_revision"})
        key = self._key("revision", binding)
        entry = await self._get(key, binding)
        if entry is None:
            return None
        revision = None
        try:
            revision = RejectedMethodRevision.model_validate(entry["value"])
            _restore_transport(revision.review, entry, "method_review")
            await self._check_revision(revision, check)
            return revision
        except Exception as exc:
            if "review_transport" in entry:
                # Corrupt schema/provenance or changed domain checks cannot
                # turn an adverse transport recovery into a fresh vote.
                code = _adverse_code(revision.review, "method_review") if revision is not None else None
                raise SkillCreationError("method_review", code or "method_review_rejected") from exc
            await self._discard(key)
            return None

    async def save_revision(self, revision: RejectedMethodRevision | dict, *, check=None) -> bool:
        review = revision.review if isinstance(revision, RejectedMethodRevision) else revision.get("review")
        transport = _transport_sidecar(review, "method_review") if isinstance(review, MethodReview) else {}
        document = revision.model_dump(mode="json") if isinstance(revision, BaseModel) else revision
        revision = RejectedMethodRevision.model_validate(document)
        _restore_transport(revision.review, transport, "method_review")
        await self._check_revision(revision, check)
        binding = content_digest({"scope": self.scope.artifacts, "slot": "method_revision"})
        return await self._put(self._key("revision", binding), binding,
            {"value": revision.model_dump(mode="json"), **transport})

    async def _check_revision(self, revision, check):
        if not _adverse_review(revision.review):
            raise ValueError("Revision checkpoint requires an adverse method review")
        await _check(revision.recipe, self.privacy, None)
        await _check(revision.review, self.privacy, None)
        if check is not None:
            result = check(revision)
            if inspect.isawaitable(result):
                await result

    async def get_oracle_revision(self, *, check=None) -> RejectedOracleRevision | None:
        binding = content_digest({"scope": self.scope.artifacts, "slot": "oracle_revision"})
        key = self._key("revision", binding)
        entry = await self._get(key, binding)
        if entry is None:
            return None
        revision = None
        try:
            revision = RejectedOracleRevision.model_validate(entry["value"])
            _restore_transport(revision.review, entry, "synthetic_plan_review")
            await self._check_oracle_revision(revision, check)
            return revision
        except Exception as exc:
            if "review_transport" in entry:
                # Corrupt schema/provenance or changed domain checks cannot
                # turn an adverse transport recovery into a fresh vote.
                code = _adverse_code(revision.review, "synthetic_plan_review") if revision is not None else None
                raise SkillCreationError("synthetic_plan_review", code or "validation_oracle_rejected") from exc
            await self._discard(key)
            return None

    async def save_oracle_revision(self, revision: RejectedOracleRevision | dict, *, check=None) -> bool:
        review = revision.review if isinstance(revision, RejectedOracleRevision) else revision.get("review")
        transport = _transport_sidecar(review, "synthetic_plan_review") if isinstance(review, ValidationPlanReview) else {}
        document = revision.model_dump(mode="json") if isinstance(revision, BaseModel) else revision
        revision = RejectedOracleRevision.model_validate(document)
        _restore_transport(revision.review, transport, "synthetic_plan_review")
        await self._check_oracle_revision(revision, check)
        binding = content_digest({"scope": self.scope.artifacts, "slot": "oracle_revision"})
        return await self._put(self._key("revision", binding), binding,
            {"value": revision.model_dump(mode="json"), **transport})

    async def _check_oracle_revision(self, revision, check):
        if revision.review.verdict() not in {"rejected", "uncertain"}:
            raise ValueError("Revision checkpoint requires an adverse oracle review")
        await _check(revision.plan, self.privacy, None)
        privacy = ensure_context(self.privacy).for_validation_plan(revision.plan)
        await _check(revision.review, privacy, None)
        if check is not None:
            result = check(revision)
            if inspect.isawaitable(result):
                await result

    async def get_reference_revision(self, *, check=None, blind_cases=()) -> RejectedReferenceRevision | None:
        binding = content_digest({"scope": self.scope.artifacts, "slot": "reference_revision"})
        key = self._key("revision", binding)
        entry = await self._get(key, binding)
        if entry is None:
            return None
        revision = None
        try:
            revision = RejectedReferenceRevision.model_validate(entry["value"])
            _restore_transport(revision.review, entry, "synthetic_reference_review")
            await self._check_reference_revision(revision, check, blind_cases)
            return revision
        except Exception as exc:
            if "review_transport" in entry:
                # Corrupt schema/provenance or changed domain checks cannot
                # turn an adverse transport recovery into a fresh vote.
                code = _adverse_code(revision.review, "synthetic_reference_review") if revision is not None else None
                raise SkillCreationError("synthetic_reference_review", code or "reference_calculation_unconfirmed") from exc
            await self._discard(key)
            return None

    async def save_reference_revision(self, revision: RejectedReferenceRevision | dict, *, check=None, blind_cases=()) -> bool:
        review = revision.review if isinstance(revision, RejectedReferenceRevision) else revision.get("review")
        transport = _transport_sidecar(review, "synthetic_reference_review") if isinstance(review, MethodReview) else {}
        document = revision.model_dump(mode="json") if isinstance(revision, BaseModel) else revision
        revision = RejectedReferenceRevision.model_validate(document)
        _restore_transport(revision.review, transport, "synthetic_reference_review")
        await self._check_reference_revision(revision, check, blind_cases)
        binding = content_digest({"scope": self.scope.artifacts, "slot": "reference_revision"})
        return await self._put(self._key("revision", binding), binding,
            {"value": revision.model_dump(mode="json"), **transport})

    async def _check_reference_revision(self, revision, check, blind_cases):
        if not _adverse_review(revision.review):
            raise ValueError("Reference revision requires an adverse method review")
        # No input bodies or candidate answers are persisted. The caller binds
        # the saved digest to the current blind context and revalidates every
        # source/interface before the exact scoped review is allowed through.
        if check is not None:
            result = check(revision)
            if inspect.isawaitable(result):
                await result
        await _check(revision.reference_scripts, self.privacy, None)
        privacy = ensure_context(self.privacy).for_reference_targets(blind_cases)
        await _check(revision.review, privacy, None)

    async def get_execution_revision(self, *, check=None) -> FailedExecutionRevision | None:
        binding = content_digest({"scope": self.scope.artifacts, "slot": "execution_revision"})
        key = self._key("revision", binding)
        entry = await self._get(key, binding, strict=True)
        if entry is None:
            return None
        try:
            revision = FailedExecutionRevision.model_validate(entry["value"])
            if "method_audit_transport" in entry:
                _restore_transport(revision.method_audit, {"review_transport": entry["method_audit_transport"]},
                                   "synthetic_method_audit")
            if "pending_review_transport" in entry:
                _restore_transport(revision.pending_review, {"review_transport": entry["pending_review_transport"]},
                                   "method_review")
            await self._check_execution_revision(revision, check)
            return revision
        except Exception as exc:
            # A stored failed candidate pins its original execution target.
            # Incompatibility or corruption cannot silently restart authoring
            # with another plan, reset a repair allowance, or erase evidence.
            raise SkillCreationError("synthetic_execution", "synthetic_execution_failed") from exc

    async def save_execution_revision(self, revision: FailedExecutionRevision | dict, *, check=None) -> bool:
        audit = revision.method_audit if isinstance(revision, FailedExecutionRevision) else revision.get("method_audit")
        transport = (_transport_sidecar(audit, "synthetic_method_audit")
                     if isinstance(audit, CandidateMethodAudit) else {})
        pending_review = revision.pending_review if isinstance(revision, FailedExecutionRevision) else revision.get("pending_review")
        pending_transport = (_transport_sidecar(pending_review, "method_review")
                             if isinstance(pending_review, MethodReview) else {})
        document = revision.model_dump(mode="json") if isinstance(revision, BaseModel) else revision
        revision = FailedExecutionRevision.model_validate(document)
        _restore_transport(revision.method_audit, transport, "synthetic_method_audit")
        _restore_transport(revision.pending_review, pending_transport, "method_review")
        await self._check_execution_revision(revision, check)
        binding = content_digest({"scope": self.scope.artifacts, "slot": "execution_revision"})
        return await self._put(self._key("revision", binding), binding,
            {"value": revision.model_dump(mode="json"), **({"method_audit_transport": transport["review_transport"]}
                if "review_transport" in transport else {}), **({"pending_review_transport": pending_transport["review_transport"]}
                if "review_transport" in pending_transport else {})})

    async def _check_execution_revision(self, revision, check):
        if revision.execution.get("status") != "failed" or revision.execution.get("scope") != "synthetic":
            raise ValueError("Execution checkpoint requires a failed synthetic execution")
        # The worker's hash-only provenance belongs in typed digest fields, not
        # the model-facing observed failure. The caller removes those keys.
        await _check(revision.recipe, self.privacy, None)
        await _check(revision.plan, self.privacy, None)
        await _check(revision.execution, self.privacy, None)
        if revision.pending_recipe is not None:
            if revision.repair_used is not True:
                raise ValueError("Pending repair cannot reset the spent repair allowance")
            await _check(revision.pending_recipe, self.privacy, None)
        if revision.method_audit is not None:
            await _check(revision.method_audit, self.privacy, None)
        if revision.pending_review is not None:
            if revision.pending_recipe is None or not _adverse_review(revision.pending_review):
                raise ValueError("Pending repair review requires its candidate and an adverse verdict")
            await _check(revision.pending_review, self.privacy, None)
        if check is not None:
            result = check(revision)
            if inspect.isawaitable(result):
                await result
