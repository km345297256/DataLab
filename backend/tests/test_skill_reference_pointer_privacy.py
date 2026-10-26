"""Reference review citations share the exact host-projected target boundary."""
import json
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.domain.services.skills.method_contract import MethodReview, validate_review
from app.domain.services.skills.skill_creation.checkpoints import BuildCheckpoints
from app.domain.services.skills.skill_creation.evidence import AnalysisContext
from app.domain.services.skills.skill_creation.package import validate_portable_document
from app.domain.services.skills.skill_creation.pipeline import SkillCreationPipeline
from app.domain.services.skills.skill_creation.privacy import PrivacyContext, PrivateValue, generation_privacy
from app.domain.services.skills.skill_creation_error import SkillCreationError, SkillPackageValidationError
from app.domain.services.skills.skill_reference_verification import reference_generation_payload
from test_session_skill_creator import helper_script
from test_skill_creation_checkpoints import MemoryCache, scope
from test_skill_creation_gateway import make_gateway, requests
from test_skill_reference_verification import method_review
from test_skill_review_pointer_privacy import POINTER, plan_for
from test_skill_review_form_gateway import as_form
from test_skill_validation_plan import interfaces, method_contract, sources


def reference_payload(pointer=POINTER):
    return {**reference_generation_payload(plan_for(pointer), method_contract(), sources(), interfaces()),
            "reference_scripts": [helper_script()]}


def reference_review(payload, *, verdict="preserved"):
    document = method_review(payload, verdict=verdict)
    document["requirements"][0]["explanation"] = f"The computation preserves the declared `{POINTER}` output contract."
    return document


def test_reference_scope_is_exact_request_local_and_explanations_only():
    payload, base = reference_payload(), PrivacyContext()
    scoped = generation_privacy(base, MethodReview, "synthetic_reference_review", payload)
    text = f"Compare the declared `{POINTER}` with the original method."
    assert not scoped.scan_document({"explanation": text})
    assert scoped.redact_document({"explanation": text}) == {"explanation": text}
    assert not base.review_json_pointers
    for field in ("content", "default", "description", "implementation_reason", "path"):
        assert scoped.scan_document({field: text})
    for stage in ("method_review", "synthetic_reference_implementation", "synthetic_script_repair"):
        assert generation_privacy(scoped, MethodReview, stage, payload).scan_document({"explanation": text})
    other = generation_privacy(scoped, MethodReview, "synthetic_reference_review", reference_payload("/other/0/group"))
    assert other.scan_document({"explanation": text})
    for citation in (POINTER + "s", POINTER + "/secret", "/elsewhere" + POINTER, "/paired/1/group"):
        assert scoped.scan_document({"explanation": f"Read `{citation}`."})


@pytest.mark.parametrize("category", ["host_path", "credential", "identity", "source_filename"])
def test_known_private_reference_target_still_blocks_before_scope_creation(category):
    with pytest.raises(SkillPackageValidationError):
        generation_privacy(PrivacyContext((PrivateValue(POINTER, category),)), MethodReview,
                           "synthetic_reference_review", reference_payload())


@pytest.mark.parametrize("pointer", ["/private/work/file", "/Users/name/file", "/tmp/result", "/etc/passwd"])
def test_reference_host_roots_never_gain_an_explanation_exception(pointer):
    scoped = generation_privacy(PrivacyContext(), MethodReview, "synthetic_reference_review", reference_payload(pointer))
    assert pointer not in scoped.review_json_pointers
    assert scoped.scan_document({"explanation": f"Read `{pointer}`."})


def test_reference_scope_revalidates_locator_syntax_and_ignores_csv_field_names():
    payload = reference_payload()
    csv_target = payload["blind_cases"][0]["cases"][0]["output_targets"][0]
    csv_target["match_fields"].append("/unlisted/0/column")
    with pytest.raises(SkillPackageValidationError):
        generation_privacy(PrivacyContext(), MethodReview, "synthetic_reference_review", payload)
    payload = reference_payload()
    target = payload["blind_cases"][0]["cases"][0]["output_targets"][1]
    target["pointer"] = "/invalid/~2escape"
    with pytest.raises((ValidationError, SkillPackageValidationError)):
        generation_privacy(PrivacyContext(), MethodReview, "synthetic_reference_review", payload)


@pytest.mark.asyncio
async def test_reference_pointer_survives_pipeline_gateway_checkpoint_save_and_restore():
    payload, cache = reference_payload(), MemoryCache()
    document = reference_review(payload)
    gateway, calls, _ = make_gateway([as_form(document)])
    context = AnalysisContext(privacy=gateway.privacy, evidence={}, sources=sources(), programs=[])
    for _ in range(2):
        pipeline = SkillCreationPipeline(context, gateway, script_verifier=AsyncMock(),
            checkpoints=BuildCheckpoints(cache, scope(), privacy=gateway.privacy))
        pipeline.contract = method_contract()
        result = await pipeline.generate(MethodReview, "synthetic_reference_review", "Review independently.", payload,
            lambda value: validate_review(value, method_contract(), sources()))
        assert result.model_dump() == document
    assert len(calls) == 1
    assert not gateway.privacy.review_json_pointers


@pytest.mark.asyncio
async def test_reference_negative_explanation_repair_keeps_target_and_adverse_verdict():
    payload = reference_payload()
    document = reference_review(payload, verdict="changed")
    document["sources"][0]["explanation"] = ""
    patch = {"repairs": [{"field_id": "F001", "action": "set",
                          "value": "The original source is completely represented by the method requirements."}]}
    gateway, calls, _ = make_gateway([as_form(document), patch])
    scoped = generation_privacy(gateway.privacy, MethodReview, "synthetic_reference_review", payload)
    result = await gateway.generate(MethodReview, "synthetic_reference_review", "Review independently.", payload,
                                   lambda value: validate_portable_document(value, scoped))
    assert result.requirements[0].status == "changed"
    assert POINTER in json.dumps(requests(calls)[1]["previous_candidate"])
    assert not gateway.privacy.review_json_pointers


@pytest.mark.asyncio
async def test_reference_adverse_cached_review_cannot_revote_due_to_pointer_privacy():
    payload, cache = reference_payload(), MemoryCache()
    document = reference_review(payload, verdict="changed")
    gateway, calls, _ = make_gateway([as_form(document)])
    context = AnalysisContext(privacy=gateway.privacy, evidence={}, sources=sources(), programs=[])
    for _ in range(2):
        pipeline = SkillCreationPipeline(context, gateway, script_verifier=AsyncMock(),
            checkpoints=BuildCheckpoints(cache, scope(), privacy=gateway.privacy))
        pipeline.contract = method_contract()
        def check(value):
            if value.requirements[0].status != "preserved":
                raise SkillCreationError("synthetic_reference_review", "reference_calculation_unconfirmed")
        with pytest.raises(SkillCreationError) as failure:
            await pipeline.generate(MethodReview, "synthetic_reference_review", "Review independently.", payload, check)
        assert failure.value.code == "reference_calculation_unconfirmed"
    assert len(calls) == 1
