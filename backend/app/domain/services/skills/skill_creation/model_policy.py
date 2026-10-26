"""Verified skill-authoring request policy, isolated from ordinary model calls."""
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.core.config import ModelRequestCapability, Settings
from app.domain.services.model_route import resolve_model_route


@dataclass(frozen=True)
class SkillModelPolicy:
    settings: Settings
    provider: str
    model_name: str
    capacity: ModelRequestCapability | None = None
    api_base: str | None = None
    deepseek_thinking: bool = False

    def invocation_options(self) -> dict:
        # Fresh per-call values override the ordinary DeepSeek client's
        # disabled default without mutating that client or shared settings.
        if self.deepseek_thinking:
            return {"extra_body": {"thinking": {"type": "enabled"}}, "reasoning_effort": "high"}
        return {}


def resolve_skill_model_policy(settings: Settings, overrides: dict) -> SkillModelPolicy:
    route = resolve_model_route(settings, overrides)
    name = overrides.get("model_name") or settings.model_name
    policy = SkillModelPolicy(settings, route.provider, name)
    # Verified 2026-10-23: official Chat Completions max_tokens=393216,
    # context=1M. Omitting max_tokens uses 8K in non-thinking mode.
    # https://api-docs.deepseek.com/api/create-chat-completion/
    # https://api-docs.deepseek.com/quick_start/pricing/
    # Thinking toggle/effort verified against the official thinking-mode guide:
    # https://api-docs.deepseek.com/guides/thinking_mode/
    # Compatible gateways/model aliases inherit neither capacity nor options.
    if route.provider != "deepseek" or name not in {"deepseek-flash", "deepseek-v4-pro"}:
        return policy
    endpoint = urlsplit(route.api_base or "https://api.deepseek.com")
    if (endpoint.scheme != "https" or endpoint.hostname != "api.deepseek.com"
            or endpoint.port not in {None, 443} or endpoint.path.rstrip("/") not in {"", "/v1"}
            or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment):
        return policy
    existing = settings.model_request_capabilities.get(route.provider, {}).get(name, ModelRequestCapability())
    capacity = existing.model_copy(update={
        "context_tokens": existing.context_tokens or 1_048_576,
        "max_output_tokens": existing.max_output_tokens or 393_216,
    })
    capabilities = {provider: dict(models) for provider, models in settings.model_request_capabilities.items()}
    capabilities.setdefault(route.provider, {})[name] = capacity
    # The adapter and runtime must agree. A max_tokens bind is intentionally
    # ignored by production adapters; provide verified capacity at creation.
    configured = settings.model_copy(update={"model_request_capabilities": capabilities})
    return SkillModelPolicy(configured, route.provider, name, capacity,
                            route.api_base or "https://api.deepseek.com", deepseek_thinking=True)
