"""Official skill requests opt into thinking without changing ordinary calls."""
import json
from types import SimpleNamespace

import httpx
import pytest
from langchain.messages import HumanMessage
from langchain.messages import AIMessage

from app.core.config import Settings
from app.domain.services.skills.skill_creation.model_policy import resolve_skill_model_policy
from app.domain.services.skills.skill_creation.models import HelperScript
from app.domain.services.skills.skill_creation.script_policy import validate_script
from app.infrastructure.external.llm.chat_model import create_chat_model
from test_model_driver import RecordingMiddleware
from test_skill_creation_gateway import SmallRecipe, VALID, make_gateway
from test_skill_creation_script_authoring import REQUEST
from test_skill_creation_source_response import SOURCE


def configured(provider="deepseek", name="deepseek-flash", base="https://api.deepseek.com"):
    return Settings(_env_file=None, model_provider=provider, model_name=name,
        api_base=base, api_key="offline-skill-key", model_request_capabilities={
            provider: {name: {"context_tokens": 1048576, "max_output_tokens": 393216}}})


@pytest.mark.parametrize("name", ["deepseek-flash", "deepseek-v4-pro"])
@pytest.mark.parametrize("base", [None, "https://api.deepseek.com", "https://api.deepseek.com/v1/"])
def test_verified_policy_returns_fresh_isolated_thinking_options(name, base):
    settings = configured(name=name, base=base)
    before = settings.model_dump()
    policy = resolve_skill_model_policy(settings, {})
    expected = {"extra_body": {"thinking": {"type": "enabled"}}, "reasoning_effort": "high"}
    assert policy.invocation_options() == expected
    changed = policy.invocation_options()
    changed["extra_body"]["thinking"]["type"] = "disabled"
    assert policy.invocation_options() == expected
    assert policy.capacity.max_output_tokens == 393216
    assert settings.model_dump() == before


@pytest.mark.parametrize("provider,name,base", [
    ("deepseek", "deepseek-chat", "https://api.deepseek.com"),
    ("deepseek", "deepseek-flash", "https://compatible.example.test/v1"),
    ("deepseek", "deepseek-flash", "https://api.deepseek.com/other"),
    ("deepseek", "deepseek-v4-pro", "https://api.deepseek.com.attacker.invalid"),
    ("openai", "deepseek-flash", "https://api.deepseek.com"),
])
def test_unverified_routes_and_other_providers_receive_no_thinking_override(provider, name, base):
    assert resolve_skill_model_policy(configured(provider, name, base), {}).invocation_options() == {}


@pytest.mark.asyncio
async def test_custom_model_with_explicit_options_receives_entire_policy():
    received = []

    async def invoke(messages, *, extra_body, reasoning_effort):
        received.append({"extra_body": extra_body, "reasoning_effort": reasoning_effort})
        return AIMessage(content=json.dumps(VALID))

    gateway, _, _ = make_gateway([])
    gateway.model = SimpleNamespace(ainvoke=invoke)
    gateway.model_policy = resolve_skill_model_policy(configured(), {})
    await gateway.generate(SmallRecipe, "workflow", "Return a JSON object.", {})
    assert received == [gateway.model_policy.invocation_options()]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,base,bound", [
    ("deepseek-flash", "https://api.deepseek.com", False),
    ("deepseek-flash", "https://api.deepseek.com/v1", True),
    ("deepseek-v4-pro", "https://api.deepseek.com", True),
    ("deepseek-v4-pro", "https://api.deepseek.com/v1/", False),
])
async def test_real_sdk_wire_enables_skill_json_repairs_and_python_but_leaves_ordinary_client_disabled(name, base, bound):
    """Only HTTP is mocked; production driver, SDK serialization and merge run."""
    policy = resolve_skill_model_policy(configured(name=name, base=base), {})
    middleware = RecordingMiddleware()
    middleware.output = 393216
    model = create_chat_model(policy.settings, overrides={"api_base": policy.api_base}, request_middleware=middleware)
    captured, urls = [], []
    def transport(request):
        urls.append(str(request.url))
        captured.append(json.loads(request.content))
        content = (json.dumps({"name": "incomplete"}) if len(captured) == 1 else
                   SOURCE if len(captured) == 3 else json.dumps(VALID))
        # Deliberately do not retrieve, synthesize, log or save reasoning text.
        return httpx.Response(200, json={"id": "offline-skill", "object": "chat.completion",
            "created": 1, "model": name, "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 30, "total_tokens": 50}})
    gateway, _, _ = make_gateway([])
    gateway.model_policy = policy
    gateway.model = (model.bind(extra_body={"thinking": {"type": "disabled"}},
        reasoning_effort="low", response_format={"type": "json_object"}, max_tokens=1) if bound else model)
    await model._client.root_async_client._client.aclose()
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http_client:
            model._client.root_async_client._client = http_client
            assert (await gateway.generate(SmallRecipe, "workflow", "Return a complete JSON object.", {})).model_dump() == VALID
            assert (await gateway.generate(HelperScript, "synthetic_reference_implementation", "Return complete Python.",
                {"script_request": REQUEST}, validate_script)).content == SOURCE.strip()
            await model.ainvoke([HumanMessage(content="An ordinary non-skill request.")])
    finally:
        model._client.root_client.close()
        await model._client.root_async_client.close()
    assert len(captured) == 4
    assert all(url.startswith(base.rstrip("/") + "/chat/completions") for url in urls)
    assert all(request["thinking"] == {"type": "enabled"} and request["reasoning_effort"] == "high"
               for request in captured[:3])
    assert [request["response_format"] for request in captured[:2]] == [{"type": "json_object"}] * 2
    assert "response_format" not in captured[2]
    assert captured[3]["thinking"] == {"type": "disabled"} and "reasoning_effort" not in captured[3]
    assert all(request["max_tokens"] == 393216 and "extra_body" not in request for request in captured)
    assert model._client.extra_body == {"thinking": {"type": "disabled"}}
    assert model.max_output_tokens == 393216
    assert "reasoning_content" not in json.dumps(captured)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,name,base", [
    ("deepseek", "deepseek-flash", "https://compatible.example.test/v1"),
    ("deepseek", "deepseek-chat", "https://api.deepseek.com/v1"),
    ("openai", "synthetic-model", "https://offline.example.test/v1"),
])
async def test_real_sdk_wire_preserves_other_routes_for_json_and_python(provider, name, base):
    policy = resolve_skill_model_policy(configured(provider, name, base), {})
    middleware = RecordingMiddleware()
    middleware.output = 393216
    model = create_chat_model(policy.settings, request_middleware=middleware)
    captured = []
    def transport(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "offline-other", "object": "chat.completion", "created": 1,
            "model": name, "choices": [{"index": 0, "finish_reason": "stop", "message": {
                "role": "assistant", "content": json.dumps(VALID) if len(captured) == 1 else SOURCE}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 30, "total_tokens": 50}})
    gateway, _, _ = make_gateway([])
    gateway.model, gateway.model_policy = model, policy
    await model._client.root_async_client._client.aclose()
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http_client:
            model._client.root_async_client._client = http_client
            await gateway.generate(SmallRecipe, "workflow", "Return a JSON object.", {})
            await gateway.generate(HelperScript, "implementation", "Return complete Python.", {"script_request": REQUEST}, validate_script)
    finally:
        model._client.root_client.close()
        await model._client.root_async_client.close()
    assert len(captured) == 2
    assert all("reasoning_effort" not in request for request in captured)
    assert all(request.get("thinking") == ({"type": "disabled"} if provider == "deepseek" else None) for request in captured)
    assert captured[0]["response_format"] == {"type": "json_object"} and "response_format" not in captured[1]
