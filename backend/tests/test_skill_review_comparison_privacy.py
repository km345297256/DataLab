"""Scoped JSON comparisons grant no permission to paths or private values."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.services.skills.skill_creation import service
from app.domain.services.skills.skill_creation.evidence import AnalysisContext
from app.domain.services.skills.skill_creation.privacy import PrivacyContext, PrivateValue, generation_privacy
from app.domain.services.skills.skill_validation_review import ValidationPlanReview, validate_oracle_review
from app.domain.services.skills.skill_creation.pipeline import SkillCreationPipeline
from app.domain.services.skills.skill_creation.checkpoints import BuildCheckpoints, BuildScope, content_digest
from test_skill_creation_checkpoints import Artifact, MemoryCache, scope
from test_skill_creation_gateway import make_gateway
from test_skill_review_pointer_privacy import plan_for, review_for
from test_skill_validation_plan import interfaces, method_contract, sources


@pytest.mark.parametrize("pointer", ["/profiles/0/rows", "/counts/accepted", "/统计/0/样本量"])
@pytest.mark.parametrize("comparison", ["=2", "==0", "!=-0.5", "=1e-12", "=-2.1E+3", "=true", "!=false", "=null"])
def test_only_declared_targets_gain_finite_scalar_comparison_syntax(pointer, comparison):
    privacy = PrivacyContext().for_validation_plan(plan_for(pointer))
    document = {"correctness": {"explanation": f"Independent calculation confirms `{pointer}{comparison}`."}}
    assert not privacy.scan_document(document)
    assert privacy.redact_document(document) == document
    assert PrivacyContext().scan_document(document)


@pytest.mark.parametrize("suffix", ["=NaN", "=Infinity", "=-Infinity", "=1e999", "=true/private",
    "=null/secret", "=2/private/file", "=2suffix", "=2.0.csv", "=secret", "==", "===2",
    "=01", "=+2", "=2e", "=truefalse", "=2%2fsecret", "=2@host", "=2\\secret"])
def test_invalid_scalar_or_path_tail_never_inherits_declared_pointer_scope(suffix):
    pointer = "/measurements/count"
    privacy = PrivacyContext().for_validation_plan(plan_for(pointer))
    citation = pointer + suffix
    document = {"explanation": f"Check `{citation}` independently."}
    assert privacy.scan_document(document)
    assert citation not in privacy.redact_document(document)["explanation"]


@pytest.mark.parametrize("citation", ["/unlisted/count=2", "/measurements/counts=2",
    "/measurements/count/private=2", "/elsewhere/measurements/count=2"])
def test_full_current_pointer_is_required_not_a_path_prefix(citation):
    privacy = PrivacyContext().for_validation_plan(plan_for("/measurements/count"))
    assert privacy.scan_document({"explanation": f"Check `{citation}`."})


@pytest.mark.parametrize("field", ["content", "default", "description", "implementation_reason", "path", "expected"])
def test_numeric_comparison_exception_does_not_apply_outside_explanations(field):
    privacy = PrivacyContext().for_validation_plan(plan_for("/measurements/count"))
    assert privacy.scan_document({field: "Compare `/measurements/count=2` with the expected value."})


@pytest.mark.parametrize("category,private", [
    ("host_path", "/measurements/count"), ("identity", "2"), ("credential", "2"),
    ("source_filename", "/measurements/count=2"),
])
def test_known_private_markers_still_win_over_public_numeric_comparisons(category, private):
    privacy = PrivacyContext((PrivateValue(private, category),), frozenset({"/measurements/count"}))
    issues = privacy.scan_document({"explanation": "Compare `/measurements/count=2`."})
    assert any(issue.category == category for issue in issues)


@pytest.mark.parametrize("private", ["/private/secret/file", "sk-synthetic-secret-token", "0123456789abcdef01234567"])
def test_allowed_comparison_cannot_swallow_an_adjacent_private_value(private):
    privacy = PrivacyContext().for_validation_plan(plan_for("/measurements/count"))
    document = {"explanation": f"Compare `/measurements/count=2`; additional value `{private}`."}
    assert privacy.scan_document(document)
    redacted = privacy.redact_document(document)["explanation"]
    assert "/measurements/count=2" in redacted and private not in redacted


def test_comparison_scope_is_reset_for_other_plan_or_generation_stage():
    pointer = "/measurements/count"
    privacy = PrivacyContext().for_validation_plan(plan_for(pointer))
    document = {"explanation": f"Compare `{pointer}=2`."}
    assert privacy.for_validation_plan(plan_for("/other/count")).scan_document(document)
    assert generation_privacy(privacy, ValidationPlanReview, "workflow", {}).scan_document(document)


@pytest.mark.asyncio
async def test_negative_comparison_review_survives_real_gateway_and_adverse_cache():
    pointer, cache = "/profiles/0/rows", MemoryCache()
    plan = plan_for(pointer)
    document = review_for(plan, status="rejected", citation=pointer + "=2")
    gateway, calls, _ = make_gateway([document])
    context = AnalysisContext(privacy=gateway.privacy, evidence={}, sources=sources(), programs=[])
    payload = {"validation_plan": plan.model_dump(), "script_interfaces": interfaces()}
    for _ in range(2):
        pipeline = SkillCreationPipeline(context, gateway, script_verifier=AsyncMock(),
            checkpoints=BuildCheckpoints(cache, scope(), privacy=gateway.privacy))
        pipeline.contract = method_contract()
        result = await pipeline.generate(ValidationPlanReview, "synthetic_plan_review", "Review independently.",
            payload, lambda value: validate_oracle_review(value, plan))
        assert result.verdict() == "rejected"
        assert result.model_dump() == document
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["rejected", "uncertain"])
async def test_host_output_role_changes_build_scope_without_storing_names(monkeypatch, status):
    cache = MemoryCache()
    monkeypatch.setattr("app.infrastructure.external.cache.get_cache", lambda: cache)
    monkeypatch.setattr("app.domain.services.model_route.resolve_model_route", lambda *args:
        SimpleNamespace(provider="fixture", api_base="https://example.invalid"))
    monkeypatch.setattr(service, "_creator_code_version", lambda: "role-scope-fixture")
    names = {"private-first-output.csv", "private-second-output.csv"}
    entries = tuple(PrivateValue(name, "source_filename") for name in sorted(names))
    settings = SimpleNamespace(model_dump=lambda **kwargs: {"model_name": "fixture"})
    policy = SimpleNamespace(settings=settings, model_name="fixture", capacity=None)

    def build(roles):
        context = AnalysisContext(privacy=PrivacyContext(entries, generated_output_filenames=frozenset(roles)),
                                  evidence={"method": "unchanged"}, sources=[], programs=[])
        return service.get_build_checkpoints(context=context, policy=policy, overrides={},
            owner_id="owner", session_id="session")

    first, second, same = build(names), build(set()), build(reversed(sorted(names)))
    assert first.scope != second.scope and first.scope == same.scope
    assert first.scope.identity == second.scope.identity
    assert first.scope.artifacts != second.scope.artifacts
    producer = AsyncMock(return_value=Artifact(content="A portable validated authoring artifact."))
    for checkpoint in (first, second, same):
        await checkpoint.run(Artifact, "workflow", "Generate fixture.", {}, None, producer)
    assert producer.await_count == 2

    # The pre-output-role schema never included generated_outputs. Its adverse
    # decisions must survive both the schema upgrade and later role changes.
    legacy = BuildCheckpoints(cache, BuildScope.create(
        owner_id="owner", session_id="session",
        source={"evidence": {"method": "unchanged"}, "sources": [], "programs": [],
                "privacy": content_digest([(entry.category, entry.value) for entry in entries])},
        configuration={"provider": "fixture", "model": "fixture", "api_base": "https://example.invalid",
                       "capacity": None, "overrides": {}, "model_settings": {"model_name": "fixture"}},
        code_version="pre-output-roles-fixture"), privacy=PrivacyContext(entries))
    assert legacy.scope.identity == first.scope.identity == second.scope.identity
    assert len({legacy.scope.artifacts, first.scope.artifacts, second.scope.artifacts}) == 3
    plan = plan_for("/profiles/0/rows")
    document = review_for(plan, status=status, citation="/profiles/0/rows=2")
    review_producer = AsyncMock(return_value=ValidationPlanReview.model_validate(document))
    for checkpoint in (legacy, first, second, same):
        result = await checkpoint.run(ValidationPlanReview, "synthetic_plan_review", "Review independently.",
            {"validation_plan": plan.model_dump()}, lambda value: validate_oracle_review(value, plan), review_producer)
        assert result.verdict() == status
        assert result.model_dump() == document
    assert review_producer.await_count == 1
    serialized = json.dumps(cache.entries) + repr(legacy.scope) + repr(first.scope) + repr(second.scope)
    assert not any(name in serialized for name in names)
