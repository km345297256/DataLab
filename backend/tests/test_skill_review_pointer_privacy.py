"""Public oracle target citations are local to one independently checked plan."""
import json
from unittest.mock import AsyncMock

import pytest

from app.domain.services.skills.skill_creation.checkpoints import BuildCheckpoints, content_digest
from app.domain.services.skills.skill_creation.evidence import AnalysisContext
from app.domain.services.skills.skill_creation.pipeline import SkillCreationPipeline
from app.domain.services.skills.skill_creation.privacy import PrivacyContext, PrivateValue, generation_privacy
from app.domain.services.skills.skill_creation.package import validate_portable_document
from app.domain.services.skills.skill_creation_error import SkillCreationError, SkillPackageValidationError
from app.domain.services.skills.skill_validation import SyntheticValidationPlan, validate_validation_plan
from app.domain.services.skills.skill_validation_review import ValidationPlanReview, validate_oracle_review
from test_skill_creation_checkpoints import MemoryCache, scope
from test_skill_creation_gateway import make_gateway, requests
from test_skill_creation_pipeline import oracle_review_payload
from test_skill_validation_plan import interfaces, method_contract, plan_payload, sources


POINTER = "/paired/0/group"


def plan_for(pointer=POINTER):
    value = plan_payload()
    assertion = next(item for item in value["scripts"][0]["cases"][0]["assertions"] if item["type"] == "json_value")
    assertion["pointer"] = pointer
    plan = SyntheticValidationPlan.model_validate(value)
    validate_validation_plan(plan, method_contract(), sources(), interfaces())
    return plan


def review_for(plan, *, status="accepted", citation=POINTER):
    document = oracle_review_payload(plan)
    document["scripts"][0]["cases"][0]["assertions"][0]["correctness"] = {
        "status": status, "explanation": f"Independent calculation compares `{citation}` with its expected value."}
    return document


def check_review(plan, privacy):
    def check(value):
        validate_portable_document(value, privacy)
        validate_oracle_review(value, plan)
    return check


def test_exact_current_target_is_public_only_inside_oracle_explanations():
    context = PrivacyContext()
    scoped = context.for_validation_plan(plan_for())
    text = f"Independently verified '{POINTER}' against the synthetic input."
    assert not scoped.scan_document({"correctness": {"explanation": text}})
    assert scoped.redact_document({"correctness": {"explanation": text}}) == {"correctness": {"explanation": text}}
    assert context.scan_document({"explanation": text})
    assert context.review_json_pointers == frozenset()
    for field in ("content", "default", "description", "implementation_reason", "path"):
        assert scoped.scan_document({field: text})
    other = scoped.for_validation_plan(plan_for("/another/0/metric"))
    assert other.scan_document({"explanation": text})
    assert generation_privacy(scoped, ValidationPlanReview, "workflow", {}).scan_document({"explanation": text})


@pytest.mark.parametrize("pointer", ["/summary/count", "/statistics/0/mean", "/a~1b/~0metric", "/统计/0/均值"])
def test_current_schema_targets_are_not_bound_to_a_specific_output_name(pointer):
    scoped = PrivacyContext().for_validation_plan(plan_for(pointer))
    assert not scoped.scan_document({"explanation": f"Value at `{pointer}` agrees with the independent computation."})


@pytest.mark.parametrize("citation", [POINTER + "s", POINTER + "/private.csv", POINTER + "-private.csv",
                                      "/elsewhere" + POINTER, "/paired/1/group", "/unlisted/0/group"])
def test_similar_or_unlisted_paths_never_inherit_a_target_exception(citation):
    scoped = PrivacyContext().for_validation_plan(plan_for())
    assert scoped.scan_document({"explanation": f"Read `{citation}`."})
    assert citation not in scoped.redact_document({"explanation": f"Read `{citation}`."})["explanation"]


@pytest.mark.parametrize("pointer", ["/Users/name/file", "/private/work/file", "/tmp/result", "/home/person/file",
                                     "/Volumes/data/file", "/etc/passwd", "/proc/self/environ", "/usr/local/file"])
def test_host_roots_never_become_public_prose_by_declaring_them_as_json_targets(pointer):
    scoped = PrivacyContext().for_validation_plan(plan_for(pointer))
    assert pointer not in scoped.review_json_pointers
    assert scoped.scan_document({"explanation": f"Read `{pointer}`."})


@pytest.mark.parametrize("category", ["host_path", "credential", "identity", "source_filename"])
def test_known_private_value_always_wins_over_even_an_exact_public_target(category):
    context = PrivacyContext((PrivateValue(POINTER, category),))
    with pytest.raises(SkillPackageValidationError):
        context.for_validation_plan(plan_for())
    # Even an explicitly constructed scoped context cannot suppress strong
    # evidence, because the exemption affects only generic path recognition.
    scoped = PrivacyContext(context.entries, frozenset({POINTER}))
    assert scoped.scan_document({"explanation": f"Read `{POINTER}`."})
    assert POINTER not in scoped.redact_document({"explanation": f"Read `{POINTER}`."})["explanation"]


@pytest.mark.asyncio
async def test_pipeline_gateway_checkpoint_save_and_hit_share_the_same_target_scope():
    plan, cache = plan_for(), MemoryCache()
    payload = {"validation_plan": plan.model_dump(mode="json"), "script_interfaces": interfaces()}
    document = review_for(plan)
    gateway, calls, _ = make_gateway([document])
    context = AnalysisContext(privacy=gateway.privacy, evidence={}, sources=sources(), programs=[])
    for _ in range(2):
        pipeline = SkillCreationPipeline(context, gateway, script_verifier=AsyncMock(),
            checkpoints=BuildCheckpoints(cache, scope(), privacy=gateway.privacy))
        pipeline.contract = method_contract()
        result = await pipeline.generate(ValidationPlanReview, "synthetic_plan_review", "Review independently.",
                                        payload, lambda value: validate_oracle_review(value, plan))
        assert result.model_dump() == document
    assert len(calls) == 1  # The cached artifact is rechecked without a false path rejection.
    assert not gateway.privacy.review_json_pointers


@pytest.mark.asyncio
async def test_repair_candidate_preserves_legal_target_citations_but_redacts_private_values():
    plan = plan_for()
    bad = review_for(plan, status="rejected")
    bad["scripts"][0]["support"]["explanation"] = ""
    # Missing explanation repair must keep the already adverse numerical
    # assessment, including its legitimate JSON target, byte for byte.
    patch = {"repairs": [{"field_id": "F001", "action": "set",
                           "value": "The public input and output interfaces support these synthetic checks."}]}
    gateway, calls, _ = make_gateway([bad, patch])
    scoped = gateway.privacy.for_validation_plan(plan)
    result = await gateway.generate(ValidationPlanReview, "synthetic_plan_review", "Review independently.",
        {"validation_plan": plan.model_dump()}, check_review(plan, scoped))
    assert result.verdict() == "rejected"
    assert POINTER in json.dumps(requests(calls)[1]["previous_candidate"])
    assert "{input_path}" not in json.dumps(requests(calls)[1]["previous_candidate"])
    sensitive = "original-private-owner-value"
    bad = review_for(plan)
    bad["scripts"][0]["support"]["explanation"] += " " + sensitive
    gateway, calls, _ = make_gateway([bad, review_for(plan)])
    await gateway.generate(ValidationPlanReview, "synthetic_plan_review", "Review independently.",
        {"validation_plan": plan.model_dump()}, check_review(plan, gateway.privacy.for_validation_plan(plan)))
    repaired = json.dumps(requests(calls)[1]["previous_candidate"])
    assert POINTER in repaired and sensitive not in repaired and "{source_identity}" in repaired


@pytest.mark.parametrize("status", ["rejected", "uncertain"])
@pytest.mark.asyncio
async def test_adverse_cache_and_oracle_revision_roundtrip_preserve_citations_and_verdict(status):
    plan, cache = plan_for(), MemoryCache()
    document = review_for(plan, status=status)
    review = ValidationPlanReview.model_validate(document)
    producer = AsyncMock(return_value=review)
    payload = {"validation_plan": plan.model_dump()}
    def validate_revision(revision):
        validate_oracle_review(revision.review, revision.plan)
        assert revision.plan == plan
    record = {"plan": plan, "review": review, "contract_digest": content_digest(method_contract()),
              "interfaces_digest": content_digest(interfaces())}
    for _ in range(2):
        build = BuildCheckpoints(cache, scope(), privacy=PrivacyContext())
        result = await build.run(ValidationPlanReview, "synthetic_plan_review", "Review independently.",
            payload, lambda value: validate_oracle_review(value, plan), producer)
        assert result.verdict() == status
        await build.save_oracle_revision(record, check=validate_revision)
    assert producer.await_count == 1
    restored = await BuildCheckpoints(cache, scope(), privacy=PrivacyContext()).get_oracle_revision(check=validate_revision)
    assert restored is not None and restored.review.model_dump() == document
    # A later context learning this exact value is private must invalidate the
    # saved revision and cannot return the stored adverse review's contents.
    private = BuildCheckpoints(cache, scope(), privacy=PrivacyContext((PrivateValue(POINTER, "host_path"),)))
    assert await private.get_oracle_revision(check=validate_revision) is None
    with pytest.raises(SkillPackageValidationError):
        await private.save_oracle_revision(record, check=validate_revision)
