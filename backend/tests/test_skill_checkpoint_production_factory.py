"""Exercise the real factory's types/routing without opening Redis or a model."""
from app.core.config import Settings
from app.domain.services.skills.skill_creation.evidence import AnalysisContext
from app.domain.services.skills.skill_creation.model_policy import resolve_skill_model_policy
from app.domain.services.skills.skill_creation.privacy import PrivacyContext, PrivateValue
from app.domain.services.skills.skill_creation.service import get_build_checkpoints


def test_real_factory_accepts_production_context_and_native_capacity_without_mutating_settings(monkeypatch):
    cache = object()
    monkeypatch.setattr("app.infrastructure.external.cache.get_cache", lambda: cache)
    settings = Settings(_env_file=None, model_provider="deepseek", model_name="deepseek-flash",
                        api_base="https://api.deepseek.com", api_key="test-private-credential")
    before = settings.model_dump(mode="json")
    policy = resolve_skill_model_policy(settings, {})
    privacy = PrivacyContext((PrivateValue("test-private-source", "source_filename"),))
    context = AnalysisContext(privacy=privacy, evidence={"method": "Retain complete computation"}, sources=[], programs=[])
    overrides = {"temperature": 0, "api_base": policy.api_base}
    first = get_build_checkpoints(context=context, policy=policy, overrides=overrides,
                                  owner_id="scientist-one", session_id="analysis-one")
    same = get_build_checkpoints(context=context, policy=policy, overrides=overrides,
                                 owner_id="scientist-one", session_id="analysis-one")
    other = get_build_checkpoints(context=context, policy=policy, overrides=overrides,
                                  owner_id="scientist-two", session_id="analysis-one")
    assert first.cache is cache and first.privacy is privacy
    assert first.scope == same.scope and first.scope != other.scope
    assert len(first.scope.identity) == len(first.scope.artifacts) == 64
    assert policy.capacity.max_output_tokens == 393216 and policy.capacity.context_tokens == 1048576
    assert settings.model_dump(mode="json") == before
    assert "private" not in repr(first.scope)
