"""Completion diagnostics through the physical driver, audit and skill boundary."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain.messages import AIMessage, HumanMessage

from app.core.config import Settings
from app.domain.external.model_driver import ModelCapabilities, ModelIdentity
from app.domain.services import model_runtime as runtime
from app.domain.services.skills.skill_creation_error import SkillCreationError
from app.domain.utils.model_response import (
    ModelEmptyResponseError, ModelIncompleteResponseError, normalize_incomplete_finish_reason,
    validate_model_response,
)
from app.infrastructure.external.llm.chat_model import LangChainModelDriver
from test_model_driver import FakeClient
from test_skill_creation_gateway import SmallRecipe, VALID, make_gateway


CANARY = "synthetic-private-provider-payload-canary"


class TraceStore:
    def __init__(self):
        self.records = {}

    async def put(self, record):
        self.records[record.trace_id] = record.model_copy(deep=True)


@pytest.mark.parametrize("reason,expected", [
    (None, None), ("length", "length"), ("LENGTH", "length"), (" max_tokens ", "max_tokens"),
    ("max_output_tokens", "max_output_tokens"), ("incomplete", "incomplete"),
    ("failed", "failed"), ("error", "error"), ("cancelled", "cancelled"),
    ("tool_calls", "tool_calls"), ("function_call", "function_call"), ("pause_turn", "pause_turn"),
    ("model_context_window_exceeded", "model_context_window_exceeded"),
    (CANARY, "unknown"), ("/private/" + CANARY, "unknown"), ("", "unknown"), (42, "unknown"),
])
def test_incomplete_finish_reason_is_an_optional_fixed_safe_enum(reason, expected):
    error = ModelIncompleteResponseError(reason)
    assert error.finish_reason == normalize_incomplete_finish_reason(reason) == expected
    assert CANARY not in str(error) and CANARY not in repr(vars(error))
    assert str(error) == "Model response ended without a usable completion"


@pytest.mark.parametrize("key,reason,expected", [
    ("finish_reason", "length", "length"), ("stop_reason", "max_tokens", "max_tokens"),
    ("status", "incomplete", "incomplete"), ("finish_reason", CANARY, "unknown"),
])
def test_empty_abnormal_finish_preserves_classification_without_provider_payload(key, reason, expected):
    response = AIMessage(content="", response_metadata={key: reason, "provider_diagnostic": CANARY})
    with pytest.raises(ModelIncompleteResponseError) as error:
        validate_model_response(response)
    assert error.value.finish_reason == expected
    assert CANARY not in str(error.value) and CANARY not in repr(vars(error.value))


def test_normal_empty_response_and_partial_transport_keep_distinct_contracts():
    with pytest.raises(ModelEmptyResponseError):
        validate_model_response(AIMessage(content="", response_metadata={"finish_reason": "stop"}))
    # The shared transport boundary still permits nonempty partial content;
    # artifact-specific complete-response validation rejects it inside audit.
    validate_model_response(AIMessage(content='{"name":', response_metadata={"finish_reason": "length"}))


def setup_boundary(monkeypatch, responses, capacity):
    settings = Settings(_env_file=None, api_key="synthetic-unused-credential", model_provider="openai",
                        model_name="synthetic-model", model_request_capabilities=(
                            {"openai": {"synthetic-model": {"max_output_tokens": capacity}}} if capacity else {}))
    monkeypatch.setattr(runtime, "get_settings", lambda: settings)
    monkeypatch.setattr("app.domain.services.execution_identity.get_settings", lambda: settings)
    monkeypatch.setattr(runtime.TokenUsageService, "record_from_message", AsyncMock())
    client = FakeClient(cache=False)
    client._responses = responses
    driver = LangChainModelDriver(client=client, identity=ModelIdentity(provider="openai", model_name="synthetic-model"),
        capabilities=ModelCapabilities(), max_output_tokens=capacity, native_output_policy=True)
    gateway, _, _ = make_gateway([])
    gateway.model = driver.bind(max_tokens=1)
    gateway.response_validation = runtime.model_response_validation
    return driver, client, gateway


def reply(content, finish="length"):
    return AIMessage(content=content, response_metadata={"finish_reason": finish, "provider_diagnostic": CANARY},
                     usage_metadata={"input_tokens": 8, "output_tokens": 64, "total_tokens": 72})


@pytest.mark.asyncio
@pytest.mark.parametrize("capacity", [None, 65536])
async def test_real_driver_preserves_empty_length_metadata_and_actual_configured_wire_capacity(monkeypatch, capacity):
    driver, client, _ = setup_boundary(monkeypatch, [reply("")], capacity)
    store = TraceStore()
    with runtime.model_execution_scope(user_id="synthetic-owner", session_id="synthetic-session", task_id="synthetic-task", store=store):
        with pytest.raises(ModelIncompleteResponseError) as error:
            await driver.bind(max_tokens=1).ainvoke([HumanMessage(content="Produce the synthetic artifact.")])
    assert error.value.finish_reason == "length"
    record, = store.records.values()
    assert record.status == "failed" and record.error_code == "provider_error"
    assert record.output_token_limit == capacity and record.actual_output_tokens == 64
    if capacity is None:
        assert "max_tokens" not in client._requests[0]
    else:
        assert client._requests[0]["max_tokens"] == capacity
    assert CANARY not in record.model_dump_json() and CANARY not in str(error.value)
    assert "synthetic-unused-credential" not in json.dumps(client._requests, default=str)


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["", '{"name":"' + CANARY])
@pytest.mark.parametrize("capacity", [None, 65536])
async def test_real_skill_gateway_classifies_both_length_forms_and_audits_every_failure(monkeypatch, content, capacity):
    _, client, gateway = setup_boundary(monkeypatch, [reply(content), reply(content)], capacity)
    store = TraceStore()
    with runtime.model_execution_scope(user_id="synthetic-owner", session_id="synthetic-session", task_id="synthetic-task", store=store):
        with pytest.raises(SkillCreationError) as error:
            await gateway.generate(SmallRecipe, "workflow", "Preserve the complete scientific method.", {})
    assert error.value.code == "output_truncated"
    records = list(store.records.values())
    assert len(records) == len(client._requests) and 1 <= len(records) <= 2
    assert all(record.status == "failed" and record.error_code == "provider_error" for record in records)
    assert all(record.output_token_limit == capacity and record.actual_output_tokens == 64 for record in records)
    assert all(request.get("max_tokens") == capacity for request in client._requests)
    safe_artifacts = str(error.value) + json.dumps([record.model_dump(mode="json") for record in records])
    assert CANARY not in safe_artifacts and "synthetic-unused-credential" not in safe_artifacts
    assert all(CANARY not in message.content for request in client._requests for message in request["messages"])


@pytest.mark.asyncio
async def test_normal_empty_retry_is_still_distinct_from_truncated_or_refused_completion(monkeypatch):
    _, client, gateway = setup_boundary(monkeypatch, [reply("", "stop"), reply(json.dumps(VALID), "stop")], None)
    store = TraceStore()
    with runtime.model_execution_scope(user_id="synthetic-owner", session_id="synthetic-session", task_id="synthetic-task", store=store):
        result = await gateway.generate(SmallRecipe, "workflow", "Return the requested structure.", {})
    assert result.model_dump() == VALID and len(client._requests) == 2
    records = list(store.records.values())
    assert [record.status for record in records] == ["failed", "succeeded"]
    assert all(record.output_token_limit is None for record in records)
    assert json.loads(client._requests[1]["messages"][1].content)["repair_feedback"]["reason"] == "incomplete_response"


@pytest.mark.asyncio
async def test_service_pins_verified_official_route_before_real_sdk_factory(monkeypatch):
    """An ambient gateway cannot inherit the official service capacity policy."""
    from app.domain.services.skills.skill_creation import service
    from app.infrastructure.external.llm.chat_model import create_chat_model

    monkeypatch.setenv("DEEPSEEK_API_BASE", "https://synthetic-gateway.invalid/v1")
    settings = Settings(_env_file=None, model_provider="deepseek", model_name="deepseek-flash",
                        api_base=None, api_key="synthetic-unused-credential", model_request_capabilities={})
    original_settings = settings.model_dump()
    context = SimpleNamespace(privacy=None)
    monkeypatch.setattr(service, "prepare_analysis_context", AsyncMock(return_value=context))
    result = SimpleNamespace(recipe=SimpleNamespace(name="synthetic-skill", description="Synthetic method",
        triggers=["synthetic"], applicability=["Synthetic analysis"], steps=[]),
        execution={"status": "not_run", "cases": []})
    pipeline = SimpleNamespace(run=AsyncMock(return_value=result))
    captured = {}

    def model_factory(configured, *, overrides):
        captured["settings"], captured["overrides"] = configured, dict(overrides)
        captured["driver"] = create_chat_model(configured, overrides=overrides)
        return captured["driver"]

    def pipeline_factory(actual_context, gateway, **kwargs):
        assert actual_context is context
        captured["policy"] = gateway.model_policy
        return pipeline

    monkeypatch.setattr(service, "SkillCreationPipeline", pipeline_factory)
    monkeypatch.setattr(service, "render_result", lambda _: ("# Synthetic skill", {}, {}))
    draft = SimpleNamespace(name="synthetic-draft")
    storage = SimpleNamespace(create=AsyncMock(return_value=draft))
    dependencies = service.CreationDependencies(
        settings_factory=lambda: settings, model_factory=model_factory,
        draft_service_factory=lambda: storage,
        usage_recorder_factory=lambda: SimpleNamespace(record_from_message=AsyncMock()),
        response_validation=runtime.model_response_validation, script_verifier=AsyncMock())
    try:
        saved = await service.create_skill_from_session("synthetic-session", "synthetic-owner",
            SimpleNamespace(), SimpleNamespace(), dependencies=dependencies)
        assert saved is draft
        storage.create.assert_awaited_once()
        pipeline.run.assert_awaited_once()
        assert captured["overrides"]["api_base"] == "https://api.deepseek.com"
        assert captured["policy"].api_base == "https://api.deepseek.com"
        client = captured["driver"]._client
        assert str(client.root_client.base_url).rstrip("/") == "https://api.deepseek.com"
        assert str(client.root_async_client.base_url).rstrip("/") == "https://api.deepseek.com"
        capacity = captured["policy"].capacity.max_output_tokens
        assert capacity == client.max_tokens == 393216
        assert captured["settings"].model_request_capabilities["deepseek"]["deepseek-flash"].max_output_tokens == capacity
        assert settings.api_base is None and settings.model_dump() == original_settings
    finally:
        if "driver" in captured:
            captured["driver"]._client.root_client.close()
            await captured["driver"]._client.root_async_client.close()
