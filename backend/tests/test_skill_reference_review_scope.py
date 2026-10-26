"""Withheld observation metadata cannot hide an actual reference rejection."""
import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.domain.services.skills.method_contract import (
    CoverageError, MethodContract, MethodReview, SourceExclusion, validate_review,
)
from app.domain.services.skills.skill_creation.checkpoints import content_digest
from app.domain.services.skills.skill_creation_error import SkillCreationError
from app.domain.services.skills.skill_reference_verification import (
    REFERENCE_REVIEW_PROMPT, _validate_reference_review, reference_generation_payload,
    reference_review_identity, verify_reference_plan,
)
from test_skill_creation_checkpoint_integration import Gateway, pipeline, verifier
from test_skill_creation_checkpoints import MemoryCache, builds
from test_skill_reference_revisions import program
from test_skill_reference_verification import method_review
from test_skill_validation_plan import interfaces, method_contract, sources
from test_skill_validation_review import typed


FIXTURE = Path(__file__).parent / "fixtures" / "skill_creator_reference_review_scope"


def scope_case():
    contract = method_contract()
    contract.excluded_sources = [SourceExclusion(source_id=identifier, reason="observed_result",
        explanation="This source is excluded from reusable method requirements.") for identifier in ("S004", "S005")]
    context = reference_generation_payload(typed(), contract, sources(), interfaces())
    document = method_review(context)
    document["sources"].extend({"source_id": identifier, "status": "valid_exclusion",
        "explanation": "Only excluded metadata is available; no observation text was supplied."}
        for identifier in ("S004", "S005"))
    return contract, context, document


def recorded_case():
    raw = (FIXTURE / "review043.json").read_bytes()
    provenance = json.loads((FIXTURE / "provenance.json").read_text())
    assert hashlib.sha256(raw).hexdigest() == provenance["response_sha256"]
    context = json.loads((FIXTURE / "context.json").read_text())
    return context, json.loads(json.loads(raw)["content"])


@pytest.mark.parametrize("optional_ids", [[], ["S004"], ["S005"], ["S004", "S005"]])
def test_declared_withheld_exclusions_are_optional_without_mutating_review_or_context(optional_ids):
    contract, context, document = scope_case()
    document["sources"] = [item for item in document["sources"]
                           if item["source_id"] == "S001" or item["source_id"] in optional_ids]
    review = MethodReview.model_validate(document)
    original, original_context = review.model_dump(), copy.deepcopy(context)
    _validate_reference_review(review, contract, context["sources"])
    assert review.model_dump() == original and context == original_context


@pytest.mark.parametrize("field", ["source", "requirement", "implementation"])
def test_optional_exclusions_do_not_erase_real_negative_judgments(field):
    contract, context, document = scope_case()
    if field == "source":
        document["sources"][0]["status"] = "changed"
    elif field == "requirement":
        document["requirements"][0]["status"] = "unsupported"
    else:
        document["implementation_appropriate"] = False
    review = MethodReview.model_validate(document)
    with pytest.raises(CoverageError) as failure:
        _validate_reference_review(review, contract, context["sources"])
    assert failure.value.code == "method_review_rejected"
    assert review.model_dump() == document


@pytest.mark.parametrize("defect,code", [
    ("duplicate_actual", "duplicate_source_review"),
    ("duplicate_withheld", "duplicate_source_review"),
    ("duplicate_requirement", "duplicate_requirement_review"),
    ("foreign_source", "incomplete_method_review"),
    ("foreign_requirement", "incomplete_method_review"),
    ("missing_actual", "incomplete_method_review"),
    ("missing_requirement", "incomplete_method_review"),
    ("withheld_changed", "incomplete_method_review"),
    ("withheld_missing", "incomplete_method_review"),
    ("withheld_preserved", "incomplete_method_review"),
])
def test_scope_projection_cannot_hide_duplicate_unknown_incomplete_or_unjustified_coverage(defect, code):
    contract, context, document = scope_case()
    if defect == "duplicate_actual":
        document["sources"].append(copy.deepcopy(document["sources"][0]))
    elif defect == "duplicate_withheld":
        document["sources"].append({**document["sources"][-1], "status": "changed"})
    elif defect == "duplicate_requirement":
        document["requirements"] *= 2
    elif defect == "foreign_source":
        document["sources"][-1]["source_id"] = "S999"
    elif defect == "foreign_requirement":
        document["requirements"][0]["requirement_id"] = "R999"
    elif defect == "missing_actual":
        document["sources"].pop(0)
    elif defect == "missing_requirement":
        document["requirements"] = []
    else:
        document["sources"][-1]["status"] = defect.removeprefix("withheld_")
    with pytest.raises(CoverageError) as failure:
        _validate_reference_review(MethodReview.model_validate(document), contract, context["sources"])
    assert failure.value.code == code


def test_source_with_provided_body_remains_mandatory_even_when_excluded():
    contract, context, document = scope_case()
    supplied = [*context["sources"], {"id": "S004", "kind": "request", "text": "A duplicate method request."}]
    _validate_reference_review(MethodReview.model_validate(document), contract, supplied)
    document["sources"] = [item for item in document["sources"] if item["source_id"] != "S004"]
    with pytest.raises(CoverageError, match="incomplete_method_review"):
        _validate_reference_review(MethodReview.model_validate(document), contract, supplied)


def test_general_method_review_scope_is_not_broadened():
    contract, context, document = scope_case()
    with pytest.raises(CoverageError, match="incomplete_method_review"):
        validate_review(MethodReview.model_validate(document), contract, context["sources"])
    assert "未提供正文的来源仅是已排除的元数据" in REFERENCE_REVIEW_PROMPT


def test_actual_043_bytes_preserve_real_r006_r019_and_source_rejections():
    context, document = recorded_case()
    review = MethodReview.model_validate(document)
    with pytest.raises(CoverageError) as failure:
        _validate_reference_review(review, MethodContract.model_validate(context["method_contract"]), context["sources"])
    assert failure.value.code == "method_review_rejected"
    assert set(failure.value.identifiers) == {"S001", "S002", "S003", "R006", "R019"}
    assert review.model_dump(mode="json") == document
    assert review.implementation_appropriate is False


@pytest.mark.asyncio
async def test_actual_043_is_saved_and_sent_unchanged_to_substantive_revision_and_resume():
    original_context, document = recorded_case()
    contract = MethodContract.model_validate(original_context["method_contract"])
    original_sources = original_context["sources"]
    cache, execute = MemoryCache(), AsyncMock()
    implementations = iter([program(-2), program(-1), program(0)])
    def reply(stage, payload):
        if stage == "synthetic_reference_implementation":
            return {**payload["script_request"], "content": next(implementations)}
        assert stage == "synthetic_reference_review"
        return copy.deepcopy(document)
    gateway = Gateway(reply)
    blind = reference_generation_payload(typed(), contract, original_sources, interfaces())
    async def run():
        build = pipeline(cache, gateway, verifier())
        build.contract = contract
        await verify_reference_plan(typed(), contract, original_sources, interfaces(),
            generate=build.generate, execute=execute, checkpoints=build.checkpoints)
    for count in (4, 6):
        with pytest.raises(SkillCreationError) as failure:
            await run()
        assert (failure.value.stage, failure.value.code) == ("synthetic_reference_review", "reference_calculation_unconfirmed")
        assert len(gateway.calls) == count
        saved = await builds(cache).get_reference_revision(blind_cases=blind["blind_cases"])
        assert saved.review.model_dump(mode="json") == document
        assert saved.blind_context_digest == content_digest(blind)
    execute.assert_not_awaited()
    revisions = [payload for _, payload in gateway.calls if "reference_revision" in payload]
    assert len(revisions) == 2
    for payload in revisions:
        assert payload["reference_revision"]["review"] == document
        assert {key: payload[key] for key in blind} == blind
    assert revisions[0]["reference_revision"]["reference_scripts"][0]["content"] == program(-2)
    assert revisions[1]["reference_revision"]["reference_scripts"][0]["content"] == program(-1)
    first_review = gateway.calls[1][1]
    # Scope handling changes neither the original blind context nor semantic
    # decision identity. It only validates an in-memory view of review sources.
    identity_before = reference_review_identity(first_review)
    review = MethodReview.model_validate(document)
    with pytest.raises(CoverageError, match="method_review_rejected"):
        _validate_reference_review(review, contract, first_review["sources"])
    assert reference_review_identity(first_review) == identity_before
    assert review.model_dump(mode="json") == document
