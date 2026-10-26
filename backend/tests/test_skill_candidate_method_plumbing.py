"""Scientific audit transport, sticky decisions and frozen recovery storage."""
import copy
import json
from unittest.mock import AsyncMock, Mock

import pytest
from langchain.messages import AIMessage

from app.domain.services.skills.method_contract import MethodReview
from app.domain.services.skills.skill_candidate_method_audit import CandidateMethodAudit
from app.domain.services.skills.skill_creation.checkpoints import BuildCheckpoints, content_digest
from app.domain.services.skills.skill_creation.gateway import _raw_review_negative_code
from app.domain.services.skills.skill_creation.package import validate_portable_document
from app.domain.services.skills.skill_creation.privacy import PrivacyContext, PrivateValue
from app.domain.services.skills.skill_creation.review_transport import review_transport_proof
from app.domain.services.skills.skill_creation_error import SkillCreationError, SkillPackageValidationError, SkillRepairReviewRejected
from test_skill_creation_checkpoints import MemoryCache, builds, execution_revision, scope
from test_skill_creation_gateway import make_gateway, requests
from test_skill_creation_pipeline import review_payload


STAGE = "synthetic_method_audit"


def document(status="source_violation"):
    return {"requirements": [{"requirement_id": "R001", "status": status,
        "source_id": "S001", "source_quote": "Compute the mean of retained values.",
        "script_path": "scripts/analyze.py", "code_quote": "return sum(values)",
        "explanation": "The implementation returns a total instead of the required mean."}]}


def check(value):
    assert [(r.script_path, r.requirement_id) for r in value.requirements] == [("scripts/analyze.py", "R001")]


@pytest.mark.asyncio
@pytest.mark.parametrize("status,code", [("source_violation", "candidate_method_rejected"),
                                        ("uncertain", "candidate_method_uncertain")])
async def test_exact_audit_negative_is_reused_across_model_and_code_changes(status, code):
    value = document(status)
    gateway, calls, _ = make_gateway([value])
    cache = MemoryCache()
    async def produce():
        return await gateway.generate(CandidateMethodAudit, STAGE, "Audit source only.", {}, check)
    producer = AsyncMock(side_effect=produce)
    for change in [{}, {"configuration": {"model": "other"}, "code_version": "new"}]:
        result = await builds(cache, **change).run(CandidateMethodAudit, STAGE, "Audit source only.", {}, check,
            producer, decision_identity={"source_and_candidate": "unchanged"})
        assert result.model_dump() == value
    assert len(calls) == producer.await_count == 1
    assert any(record.get("code") == code for record in cache.entries.values())


@pytest.mark.parametrize("status,code", [("source_violation", "candidate_method_rejected"),
                                        ("uncertain", "candidate_method_uncertain")])
@pytest.mark.parametrize("damage", ["unclosed", "duplicate", "multidocument", "length", "unknown_key"])
@pytest.mark.asyncio
async def test_malformed_adverse_audit_cannot_be_rewritten_as_accepted(status, code, damage):
    raw = json.dumps(document(status))
    metadata = {}
    if damage == "unclosed": raw = raw[:-1]
    if damage == "duplicate": raw = raw.replace('"status": "' + status + '"', '"status": "' + status + '", "status": "preserved"')
    if damage == "multidocument": raw = "{}\n" + raw
    if damage == "length": metadata = {"finish_reason": "length"}
    if damage == "unknown_key": raw = raw[:-1] + ', "undeclared": true}'
    gateway, calls, _ = make_gateway([AIMessage(content=raw, response_metadata=metadata), document("preserved")])
    with pytest.raises(SkillCreationError) as failure:
        await gateway.generate(CandidateMethodAudit, STAGE, "Audit source only.", {}, check)
    assert failure.value.code == code and len(calls) == 1


@pytest.mark.parametrize("raw", [
    'The explanation mentions "status": "source_violation".',
    json.dumps({"explanation": document()}),
    json.dumps({"requirements": [{"explanation": {"status": "source_violation"}}]}),
    json.dumps({"requirements": [{"status": "preserved", "explanation": "Earlier source_violation is an example."}]}),
    '{"requirements":[{"status":"source_violation_incomplete',
])
def test_prose_unknown_paths_and_partial_tokens_do_not_invent_a_negative(raw):
    assert _raw_review_negative_code(raw, CandidateMethodAudit, STAGE) is None


def test_raw_audit_precedence_matches_typed_verdict():
    data = document("uncertain")
    data["requirements"].append({**data["requirements"][0], "requirement_id": "R002", "status": "source_violation"})
    assert CandidateMethodAudit.model_validate(data).verdict() == "rejected"
    assert _raw_review_negative_code(json.dumps(data), CandidateMethodAudit, STAGE) == "candidate_method_rejected"


@pytest.mark.asyncio
async def test_negative_explanation_only_schema_repair_preserves_the_original_judgment():
    data = document()
    data["requirements"][0]["explanation"] = 42
    gateway, calls, _ = make_gateway([data, {"repairs": [{"field_id": "F001", "action": "set", "value": "The required mean is not implemented."}]}])
    result = await gateway.generate(CandidateMethodAudit, STAGE, "Audit source only.", {}, check)
    assert result.verdict() == "rejected" and len(calls) == 2
    assert requests(calls)[1]["previous_candidate"]["requirements"][0]["status"] == "source_violation"
    assert requests(calls)[1]["schema"]["title"] == "SchemaFieldRepairs"


@pytest.mark.asyncio
async def test_bad_scope_or_privacy_of_an_adverse_audit_stops_without_new_vote():
    for validation in [Mock(side_effect=ValueError("Scope differs")),
                       lambda value: validate_portable_document(value, PrivacyContext((PrivateValue("required mean", "identity"),)))]:
        gateway, calls, _ = make_gateway([document(), document("preserved")])
        with pytest.raises(SkillCreationError) as failure:
            await gateway.generate(CandidateMethodAudit, STAGE, "Audit source only.", {}, validation)
        assert failure.value.code == "candidate_method_rejected" and len(calls) == 1


async def closure_audit():
    gateway, _, _ = make_gateway([AIMessage(content=json.dumps(document()) + "}")])
    value = await gateway.generate(CandidateMethodAudit, STAGE, "Audit source only.", {}, check)
    assert review_transport_proof(value) is not None
    return value


@pytest.mark.asyncio
async def test_transport_proof_roundtrips_in_adverse_ledger_and_dict_execution_revision():
    value = await closure_audit()
    cache = MemoryCache()
    producer = AsyncMock(return_value=value)
    for _ in range(2):
        restored = await builds(cache).run(CandidateMethodAudit, STAGE, "Audit.", {}, check, producer)
        assert review_transport_proof(restored) == review_transport_proof(value)
    original = execution_revision()
    record = {**original.model_dump(), "method_audit": value, "repair_used": True,
              "pending_recipe": original.recipe}
    assert await builds(cache).save_execution_revision(record)
    loaded = await builds(cache).get_execution_revision()
    assert loaded.repair_used is True and loaded.pending_recipe == original.recipe
    assert loaded.method_audit == value and review_transport_proof(loaded.method_audit) == review_transport_proof(value)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["binding", "schema", "repair_used", "audit_proof", "target_check"])
async def test_execution_revision_damage_keeps_evidence_and_never_resets_the_attempt(mutation):
    original, cache = execution_revision(), MemoryCache()
    original.method_audit = await closure_audit()
    await builds(cache).save_execution_revision(original)
    record = next(iter(cache.entries.values()))
    if mutation == "binding": record["binding"] = "f" * 64
    if mutation == "schema": record["value"]["recipe"] = {}
    if mutation == "repair_used": record["value"]["repair_used"] = "false"
    if mutation == "audit_proof": record["method_audit_transport"]["repaired_document_sha256"] = "f" * 64
    before = copy.deepcopy(cache.entries)
    validator = Mock(side_effect=ValueError("Frozen target changed")) if mutation == "target_check" else None
    with pytest.raises(SkillCreationError) as failure:
        await builds(cache).get_execution_revision(check=validator)
    assert (failure.value.stage, failure.value.code) == ("synthetic_execution", "synthetic_execution_failed")
    assert cache.entries == before


@pytest.mark.asyncio
async def test_old_execution_revision_defaults_and_real_expiry_are_preserved():
    cache = MemoryCache()
    await builds(cache).save_execution_revision(execution_revision())
    record = next(iter(cache.entries.values()))
    for field in ("repair_used", "method_audit", "pending_recipe", "pending_review"):
        record["value"].pop(field)
    value = await builds(cache).get_execution_revision()
    assert value.repair_used is False and value.method_audit is value.pending_recipe is value.pending_review is None
    record["expires_at"] = 0
    assert await builds(cache).get_execution_revision() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["method_audit", "pending_recipe", "pending_review"])
async def test_all_new_execution_state_obeys_existing_privacy_boundary(target):
    marker = "private-owned-research-marker"
    original = execution_revision()
    if target == "method_audit":
        data = document(); data["requirements"][0]["explanation"] = marker
        original.method_audit = CandidateMethodAudit.model_validate(data)
    else:
        original.pending_recipe = original.recipe.model_copy(deep=True)
        original.repair_used = True
        if target == "pending_recipe": original.pending_recipe.description = marker
        else:
            data = review_payload(); data["implementation_appropriate"] = False; data["implementation_reason"] = marker
            original.pending_review = MethodReview.model_validate(data)
    cache = MemoryCache()
    with pytest.raises(SkillPackageValidationError):
        await BuildCheckpoints(cache, scope(), privacy=PrivacyContext((PrivateValue(marker, "identity"),))).save_execution_revision(original)
    assert not cache.entries


@pytest.mark.asyncio
async def test_real_pending_review_retains_transport_and_cannot_be_replaced_with_accepted_review():
    data = review_payload(); data["implementation_appropriate"] = False
    gateway, _, _ = make_gateway([AIMessage(content=json.dumps(data) + "}")])
    negative = await gateway.generate(MethodReview, "method_review", "Review.", {}, lambda value: None)
    error = SkillRepairReviewRejected(negative)
    assert error.review is negative and error.code == "method_review_rejected"
    original, cache = execution_revision(), MemoryCache()
    record = {**original.model_dump(), "pending_recipe": original.recipe, "pending_review": error.review, "repair_used": True}
    await builds(cache).save_execution_revision(record)
    loaded = await builds(cache).get_execution_revision()
    assert loaded.pending_review == negative
    assert review_transport_proof(loaded.pending_review) == review_transport_proof(negative)
    accepted = MethodReview.model_validate(review_payload())
    with pytest.raises(ValueError): SkillRepairReviewRejected(accepted)
    with pytest.raises(ValueError): await builds(MemoryCache()).save_execution_revision({**record, "pending_review": accepted})
    with pytest.raises(ValueError): await builds(MemoryCache()).save_execution_revision({**record, "pending_recipe": None})
    with pytest.raises(ValueError): await builds(MemoryCache()).save_execution_revision({**record, "repair_used": False})


@pytest.mark.asyncio
async def test_audit_and_repair_keep_real_driver_native_output_contract(monkeypatch):
    from test_skill_creation_native_json import real_gateway, response
    from test_skill_creation_script_authoring import REQUEST
    from test_skill_creation_source_response import SOURCE
    from app.domain.services.skills.skill_creation.models import HelperScript
    _, client, gateway = real_gateway(monkeypatch, [response(json.dumps(document())), response(SOURCE)])
    assert (await gateway.generate(CandidateMethodAudit, STAGE, "Audit source only.", {}, check)).verdict() == "rejected"
    await gateway.generate(HelperScript, "synthetic_method_repair", "Repair source only.", {"script_request": REQUEST})
    assert client._requests[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in client._requests[1]
    assert all(item["max_tokens"] == 393216 for item in client._requests)


@pytest.mark.asyncio
async def test_new_repair_uses_modular_fallback_after_true_truncation(monkeypatch):
    from app.domain.services.skills.skill_creation.models import HelperScript
    from app.domain.services.skills.skill_creation import script_authoring
    from test_skill_creation_script_authoring import REQUEST
    from test_skill_creation_source_response import SOURCE
    script = HelperScript.model_validate({**{k: REQUEST[k] for k in ("path", "purpose", "invocation")}, "content": SOURCE})
    fallback = AsyncMock(return_value=script)
    monkeypatch.setattr(script_authoring, "generate_modular_script", fallback)
    gateway, calls, _ = make_gateway([AIMessage(content=SOURCE, response_metadata={"finish_reason": "length"})])
    assert await gateway.generate(HelperScript, "synthetic_method_repair", "Repair.", {"script_request": REQUEST}) == script
    assert fallback.await_count == len(calls) == 1
    assert fallback.call_args.args[2] == "synthetic_method_repair"
