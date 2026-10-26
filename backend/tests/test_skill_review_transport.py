"""Closure-only recovery preserves the original negative, without a new vote."""
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from langchain.messages import AIMessage
from pydantic import ValidationError

from app.domain.services.skills.skill_creation.checkpoints import RejectedOracleRevision, content_digest
from app.domain.services.skills.skill_creation.gateway import _raw_review_negative_code
from app.domain.services.skills.skill_creation.review_transport import (
    ReviewTransportRejected, bind_review_transport_proof, recover_review_closures, review_transport_proof,
)
from app.domain.services.skills.skill_creation_error import SkillCreationError
from app.domain.services.skills.skill_validation import SyntheticValidationPlan
from app.domain.services.skills.skill_validation_review import ValidationPlanReview, _validate_review_targets, validate_oracle_review
from test_skill_creation_checkpoints import MemoryCache, builds
from test_skill_creation_gateway import make_gateway, requests
from test_skill_review_json_boundary import candidate
from test_skill_validation_plan import plan_payload

FIXTURE = Path(__file__).parent / "fixtures" / "skill_creator_review_json_boundary"


def actual():
    answer = json.loads((FIXTURE / "138-synthetic_plan_review-response.json").read_text())["content"]
    targets = json.loads((FIXTURE / "138-review-targets.json").read_text())
    return answer, lambda value: _validate_review_targets(value, targets)


def recover(answer, schema=ValidationPlanReview, stage="synthetic_plan_review"):
    return recover_review_closures(answer, schema, negative_code=_raw_review_negative_code(answer, schema, stage))


def apply_edits(answer, proof):
    repaired = answer
    for edit in reversed(proof.edits):
        repaired = (repaired[:edit.offset] + edit.token + repaired[edit.offset:] if edit.operation == "insert"
                    else repaired[:edit.offset] + repaired[edit.offset + 1:])
    return repaired


def test_actual_138_is_unique_one_character_recovery_preserving_all_original_values_and_targets():
    answer, check = actual()
    value, proof = recover(answer)
    check(value)
    assert value.verdict() == "rejected"
    assert proof.original_response_sha256 == hashlib.sha256(answer.encode()).hexdigest()
    assert [edit.model_dump() for edit in proof.edits] == [{"operation": "insert", "offset": 650, "token": "}"}]
    assert apply_edits(answer, proof) == answer[:650] + "}" + answer[650:]
    assert value.model_dump(mode="json") == json.loads(apply_edits(answer, proof))
    assert value.scripts[0].cases[0].method_coverage.status == "rejected"
    assert "NaN/Infinity" in value.scripts[0].cases[0].method_coverage.explanation
    assert proof.repaired_document_sha256 == content_digest(value)


@pytest.mark.asyncio
async def test_actual_138_reaches_the_substantive_revision_boundary_once_and_caches_original_proof():
    answer, check = actual()
    gateway, calls, _ = make_gateway([AIMessage(content=answer)])
    cache = MemoryCache()
    async def produce():
        return await gateway.generate(ValidationPlanReview, "synthetic_plan_review", "Independent review", {}, check)
    producer = AsyncMock(side_effect=produce)
    for _ in range(2):
        review = await builds(cache).run(ValidationPlanReview, "synthetic_plan_review", "Independent review", {}, check,
                                        producer, decision_identity="actual-original-138-scope")
        assert review.verdict() == "rejected"
        assert review_transport_proof(review).original_response_sha256 == hashlib.sha256(answer.encode()).hexdigest()
        assert "_host_review_transport_proof" not in review.model_dump()
    assert producer.await_count == len(calls) == 1
    assert len(cache.entries) == 2
    for record in cache.entries.values():
        assert record["review_transport"]["negative_code"] == "validation_oracle_rejected"
        assert "original_response" not in record and "review_transport" not in record["value"]
    assert "review_transport" not in json.dumps(requests(calls))


@pytest.mark.parametrize("kind", ["method", "reference", "oracle"])
@pytest.mark.parametrize("fence", [False, True])
@pytest.mark.asyncio
async def test_surplus_terminal_closers_only_can_be_removed_without_a_second_vote(kind, fence):
    document, schema, stage, check, code = candidate(kind)
    answer = json.dumps(document, ensure_ascii=False) + "}]"
    if fence: answer = "```json\n" + answer + "\n```"
    gateway, calls, _ = make_gateway([AIMessage(content=answer)])
    result = await gateway.generate(schema, stage, "Independent review", {}, check)
    proof = review_transport_proof(result)
    assert result.model_dump() == document and len(calls) == 1
    assert proof.negative_code == code and all(edit.operation == "delete" for edit in proof.edits)


@pytest.mark.parametrize("damage", ["key", "string", "primitive", "comma", "duplicate", "multi_document", "unknown",
    "unfinished_fence", "missing_eof", "extra_internal_closer", "normalization"])
def test_any_information_or_ambiguous_structure_damage_is_not_recovered(damage):
    document, schema, stage, _, _ = candidate("oracle")
    answer = json.dumps(document) + "]"
    if damage == "key": answer = answer.replace('"scripts"', 'scripts', 1)
    elif damage == "string": answer = answer.replace('"Independent evidence identifies a concrete method error."', '"unfinished', 1)
    elif damage == "primitive": answer = answer.replace('"rejected"', 'tru', 1)
    elif damage == "comma": answer = answer.replace(', "cases"', ' "cases"', 1)
    elif damage == "duplicate": answer = answer.replace('"status": "rejected"', '"status": "rejected", "status": "accepted"', 1)
    elif damage == "multi_document": answer += '{}'
    elif damage == "unknown": answer = answer.replace('"case_id"', '"foreign_case_id"', 1)
    elif damage == "unfinished_fence": answer = '```json\n' + answer
    elif damage == "missing_eof": answer = answer[:-2]
    elif damage == "extra_internal_closer": answer = answer.replace(', "cases"', '}, "cases"', 1)
    elif damage == "normalization": answer = answer.replace('Independent evidence identifies a concrete method error.', ' Independent evidence identifies a concrete method error. ')
    with pytest.raises(ReviewTransportRejected): recover(answer, schema, stage)


@pytest.mark.parametrize("kind", ["method", "oracle"])
def test_recovery_cannot_promote_accepted_values(kind):
    document, schema, stage, _, _ = candidate(kind)
    if kind == "method": document["requirements"][0]["status"] = "preserved"
    else: document["scripts"][0]["cases"][0]["assertions"][0]["correctness"]["status"] = "accepted"
    with pytest.raises(ReviewTransportRejected):
        recover_review_closures(json.dumps(document) + "}", schema,
            negative_code="validation_oracle_rejected" if kind == "oracle" else "method_review_rejected")


@pytest.mark.parametrize("failure", ["missing_check", "wrong_targets", "truncated_finish", "private"])
@pytest.mark.asyncio
async def test_transport_recovery_cannot_skip_exact_targets_privacy_or_finish_boundary(failure):
    from app.domain.services.skills.skill_creation.privacy import PrivacyContext, PrivateValue
    answer, check = actual()
    metadata = {"finish_reason": "length"} if failure == "truncated_finish" else {}
    if failure == "missing_check": check = None
    if failure == "wrong_targets": check = lambda value: _validate_review_targets(value, [])
    gateway, calls, _ = make_gateway([AIMessage(content=answer, response_metadata=metadata)])
    if failure == "private": gateway.privacy = PrivacyContext((PrivateValue("longitudinal_report.py", "identity"),))
    with pytest.raises(SkillCreationError) as failure_info:
        await gateway.generate(ValidationPlanReview, "synthetic_plan_review", "Review", {}, check)
    assert failure_info.value.code == "validation_oracle_rejected" and len(calls) == 1


@pytest.mark.parametrize("corruption", ["document_hash", "unknown_proof_field", "wrong_verdict", "edit_operation", "positive_value"])
@pytest.mark.asyncio
async def test_bad_persisted_proof_retains_the_original_negative_without_regeneration(corruption):
    answer, check = actual()
    value, proof = recover(answer)
    bind_review_transport_proof(value, proof, negative_code="validation_oracle_rejected")
    producer, cache = AsyncMock(return_value=value), MemoryCache()
    kwargs = dict(schema=ValidationPlanReview, stage="synthetic_plan_review", instruction="Review", payload={}, check=check, produce=producer)
    await builds(cache).run(**kwargs)
    record = next(value for key, value in cache.entries.items() if ":adverse:" in key)
    if corruption == "document_hash": record["review_transport"]["repaired_document_sha256"] = "0" * 64
    elif corruption == "unknown_proof_field": record["review_transport"]["raw"] = answer
    elif corruption == "wrong_verdict": record["review_transport"]["negative_code"] = "validation_oracle_uncertain"
    elif corruption == "edit_operation": record["review_transport"]["edits"][0]["operation"] = "replace"
    else: record["value"]["scripts"][0]["cases"][0]["method_coverage"]["status"] = "accepted"
    with pytest.raises(SkillCreationError) as failure: await builds(cache).run(**kwargs)
    assert failure.value.code == "validation_oracle_rejected" and producer.await_count == 1


@pytest.mark.asyncio
async def test_oracle_revision_slot_preserves_host_sidecar_and_revalidates_binding():
    document, schema, stage, check, code = candidate("oracle")
    value, proof = recover(json.dumps(document) + "}")
    bind_review_transport_proof(value, proof, negative_code=code)
    plan = SyntheticValidationPlan.model_validate(plan_payload())
    revision = RejectedOracleRevision(plan=plan, review=value, contract_digest="a" * 64, interfaces_digest="b" * 64)
    cache = MemoryCache()
    await builds(cache).save_oracle_revision(revision)
    saved = await builds(cache).get_oracle_revision(check=lambda item: validate_oracle_review(item.review, plan))
    assert review_transport_proof(saved.review) == proof
    record = next(iter(cache.entries.values()))
    record["review_transport"]["repaired_document_sha256"] = "0" * 64
    with pytest.raises(SkillCreationError) as failure:
        await builds(cache).get_oracle_revision()
    assert failure.value.code == "validation_oracle_rejected"


def test_proof_cannot_be_supplied_as_a_model_field_or_promote_another_review():
    document, schema, _, _, code = candidate("oracle")
    value, proof = recover(json.dumps(document) + "}")
    document["_host_review_transport_proof"] = proof.model_dump(mode="json")
    with pytest.raises(ValidationError): schema.model_validate(document)
    changed = value.model_copy(deep=True)
    changed.scripts[0].cases[0].assertions[0].correctness.explanation = "Changed explanation."
    with pytest.raises(ReviewTransportRejected): bind_review_transport_proof(changed, proof, negative_code=code)


@pytest.mark.parametrize("kind", ["method", "oracle", "reference"])
@pytest.mark.asyncio
async def test_real_dict_calling_convention_retains_host_proof_in_every_revision_slot(kind):
    from app.domain.services.skills.skill_creation.models import SkillRecipe
    from test_session_skill_creator import recipe_payload, helper_script
    document, schema, stage, check, code = candidate(kind)
    value, proof = recover(json.dumps(document) + "}", schema, stage)
    bind_review_transport_proof(value, proof, negative_code=code)
    cache = MemoryCache()
    if kind == "oracle":
        name, record = "oracle_revision", dict(plan=SyntheticValidationPlan.model_validate(plan_payload()), review=value,
            contract_digest="a" * 64, interfaces_digest="b" * 64)
    elif kind == "method":
        name, record = "revision", dict(recipe=SkillRecipe.model_validate(recipe_payload()), review=value, contract_digest="a" * 64)
    else:
        name, record = "reference_revision", dict(reference_scripts=[helper_script()], review=value, blind_context_digest="a" * 64)
    await getattr(builds(cache), "save_" + name)(record)
    loaded = await getattr(builds(cache), "get_" + name)()
    assert review_transport_proof(loaded.review) == proof
    stored = next(iter(cache.entries.values()))
    assert stored["review_transport"]["original_response_sha256"] == proof.original_response_sha256
    stored["value"]["review"] = {"malformed": True}
    with pytest.raises(SkillCreationError): await getattr(builds(cache), "get_" + name)()
    assert cache.entries


@pytest.mark.parametrize("version", [True, 1.0, "1", None])
def test_persisted_proof_version_requires_exact_integer(version):
    value, proof = recover(actual()[0])
    document = proof.model_dump(mode="json")
    document["version"] = version
    with pytest.raises(ValueError): bind_review_transport_proof(value, document, negative_code="validation_oracle_rejected")


def test_missing_ancestor_object_closure_cannot_choose_between_two_schema_owners():
    from app.domain.services.skills.skill_creation.review_transport import _ClosureParser, _tokens
    assessment = {"type": "object", "required": ["status"], "properties": {"status": {"type": "string"}}}
    middle = {"type": "object", "properties": {"inner": assessment, "sibling": {"type": "string"}}}
    root = {"type": "object", "properties": {"outer": middle, "sibling": {"type": "string"}}}
    parser = _ClosureParser(_tokens('{"outer":{"inner":{"status":"rejected","sibling":"ambiguous"}}}'), root)
    with pytest.raises(ReviewTransportRejected): parser.value(root)


def test_missing_array_closer_is_never_inferred_from_a_later_object_key():
    answer, _ = actual()
    # Even with an existing negative, array delimiters and commas are frozen.
    answer = answer.replace('"assertions":[{', '"assertions":{', 1)
    with pytest.raises(ReviewTransportRejected): recover(answer)


@pytest.mark.asyncio
async def test_actual_negative_closure_recovery_crosses_production_driver_validation_once(monkeypatch):
    from test_skill_model_completion_boundary import setup_boundary
    answer, check = actual()
    _, client, gateway = setup_boundary(monkeypatch, [AIMessage(content=answer)], 393216)
    result = await gateway.generate(ValidationPlanReview, "synthetic_plan_review", "Review original targets", {}, check)
    assert result.verdict() == "rejected" and len(client._requests) == 1
    assert review_transport_proof(result).original_response_sha256 == hashlib.sha256(answer.encode()).hexdigest()


def test_typed_host_proof_is_revalidated_instead_of_trusting_unchecked_model_copy():
    value, proof = recover(actual()[0])
    altered = proof.model_copy(update={"edits": ()})
    with pytest.raises(ValueError): bind_review_transport_proof(value, altered, negative_code="validation_oracle_rejected")


def test_one_missing_object_and_surplus_terminal_closers_preserve_every_original_value():
    answer, check = actual()
    value, proof = recover(answer + "}]}")
    check(value)
    assert len(proof.edits) == 4 and proof.edits[0].operation == "insert"
    assert all(edit.operation == "delete" for edit in proof.edits[1:])
    assert value.model_dump(mode="json") == json.loads(apply_edits(answer + "}]}", proof))
