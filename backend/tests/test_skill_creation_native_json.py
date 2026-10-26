"""Exercise JSON mode through the real governed driver, not prompt assertions."""
import json
from types import SimpleNamespace

import pytest
from langchain.messages import AIMessage

from app.core.config import ModelRequestCapability
from app.domain.external.model_driver import ModelCapabilities
from app.domain.services import model_runtime as runtime
from app.domain.services.skills.skill_creation.models import HelperScript
from app.domain.services.skills.skill_creation.script_policy import validate_script
from test_skill_creation_gateway import SmallRecipe, VALID, make_gateway
from test_skill_creation_source_response import SOURCE
from test_skill_creation_script_authoring import REQUEST
from test_skill_model_completion_boundary import setup_boundary, TraceStore


def response(content):
    return AIMessage(content=content, response_metadata={"finish_reason": "stop"},
        usage_metadata={"input_tokens": 16, "output_tokens": 24, "total_tokens": 40})


def real_gateway(monkeypatch, responses, *, capability="adapter", bound=True):
    driver, client, gateway = setup_boundary(monkeypatch, responses, 393216)
    driver.capabilities = ModelCapabilities(json_object=capability)
    gateway.model = driver.bind(max_tokens=1) if bound else driver
    gateway.model_policy = SimpleNamespace(provider="openai", model_name="synthetic-model",
        capacity=ModelRequestCapability(context_tokens=1048576, max_output_tokens=393216))
    return driver, client, gateway


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize("source_stage", ["implementation", "synthetic_reference_implementation", "synthetic_method_repair"])
async def test_structured_stage_uses_native_json_and_source_stage_uses_text_without_capacity_changes(monkeypatch, bound, source_stage):
    driver, client, gateway = real_gateway(monkeypatch,
        [response(json.dumps(VALID)), response(SOURCE)], bound=bound)
    store = TraceStore()
    with runtime.model_execution_scope(user_id="synthetic-owner", session_id="synthetic-session",
                                       task_id="synthetic-task", store=store):
        result = await gateway.generate(SmallRecipe, "workflow", "Return valid JSON preserving methods.", {})
        script = await gateway.generate(HelperScript, source_stage, "Return full Python source.",
                                       {"script_request": REQUEST}, validate_script)
    assert result.model_dump() == VALID and script.content == SOURCE.strip()
    assert client._requests[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in client._requests[1]
    assert all(request["max_tokens"] == 393216 for request in client._requests)
    assert driver.max_output_tokens == 393216
    assert len(store.records) == 2
    assert all(record.output_token_limit == 393216 and record.actual_output_tokens == 24
               for record in store.records.values())


@pytest.mark.asyncio
async def test_source_explicitly_clears_inherited_json_binding(monkeypatch):
    driver, client, gateway = real_gateway(monkeypatch, [response(SOURCE)])
    gateway.model = driver.bind(response_format={"type": "json_object"}, max_tokens=1)
    await gateway.generate(HelperScript, "implementation", "Return complete Python.",
                           {"script_request": REQUEST}, validate_script)
    assert "response_format" not in client._requests[0]
    assert client._requests[0]["max_tokens"] == 393216


@pytest.mark.asyncio
async def test_native_json_is_not_a_substitute_for_schema_validation_or_physical_failure_audit(monkeypatch):
    _, client, gateway = real_gateway(monkeypatch,
        [response(json.dumps({"name": "partial"})), response(json.dumps(VALID))])
    store = TraceStore()
    with runtime.model_execution_scope(user_id="synthetic-owner", session_id="synthetic-session",
                                       task_id="synthetic-task", store=store):
        value = await gateway.generate(SmallRecipe, "workflow", "Return a full JSON document.", {})
    assert value.model_dump() == VALID
    assert [request["response_format"] for request in client._requests] == [{"type": "json_object"}] * 2
    assert [record.status for record in store.records.values()] == ["failed", "succeeded"]
    assert len(client._requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", ["unknown", "unsupported"])
async def test_unknown_or_unsupported_real_driver_does_not_receive_native_json(monkeypatch, capability):
    _, client, gateway = real_gateway(monkeypatch, [response(json.dumps(VALID))], capability=capability)
    assert (await gateway.generate(SmallRecipe, "workflow", "Return valid JSON.", {})).model_dump() == VALID
    assert "response_format" not in client._requests[0]
    assert client._requests[0]["max_tokens"] == 393216


@pytest.mark.asyncio
async def test_existing_custom_model_without_capabilities_or_keyword_support_remains_compatible():
    gateway, calls, _ = make_gateway([VALID])
    assert (await gateway.generate(SmallRecipe, "workflow", "Return JSON.", {})).model_dump() == VALID
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "deepseek"])
async def test_gateway_format_reaches_real_sdk_http_payload_and_is_removed_for_python(provider):
    """Stub HTTP only: run the production driver and installed provider SDK."""
    import httpx
    from app.core.config import Settings
    from app.infrastructure.external.llm.chat_model import create_chat_model
    from test_model_driver import RecordingMiddleware

    captured = []
    def transport(request):
        captured.append(json.loads(request.content))
        content = json.dumps(VALID) if len(captured) == 1 else SOURCE
        return httpx.Response(200, json={
            "id": "offline-skill-source", "object": "chat.completion", "created": 1,
            "model": "synthetic-model", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 30, "total_tokens": 50}})

    middleware = RecordingMiddleware()
    middleware.output = 393216
    model = create_chat_model(Settings(_env_file=None, model_provider=provider,
        model_name="synthetic-model", api_key="offline-test-key", api_base="https://offline.example.test/v1",
        model_request_capabilities={provider: {"synthetic-model": {
            "context_tokens": 1048576, "max_output_tokens": 393216}}}), request_middleware=middleware)
    gateway, _, _ = make_gateway([])
    gateway.model = model
    await model._client.root_async_client._client.aclose()
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http_client:
            model._client.root_async_client._client = http_client
            await gateway.generate(SmallRecipe, "workflow", "Return a complete JSON object.", {})
            await gateway.generate(HelperScript, "implementation", "Return complete Python.",
                                   {"script_request": REQUEST}, validate_script)
    finally:
        model._client.root_client.close()
        await model._client.root_async_client.close()
    assert len(captured) == 2
    assert captured[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in captured[1]
    assert all(request.get("max_tokens", request.get("max_completion_tokens")) == 393216 for request in captured)
    assert "extra_body" not in captured[0]  # SDK merged the actual wire field
