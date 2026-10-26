"""Skill authoring uses native capacity, not role token caps."""
import asyncio
import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain.messages import AIMessage

from app.core.config import Settings
from app.domain.services import model_runtime
from app.domain.services.skills.skill_creation.gateway import SynthesisGateway
from app.domain.services.skills.skill_creation.model_policy import resolve_skill_model_policy
from app.infrastructure.external.llm.chat_model import create_chat_model
from test_model_driver import FakeClient
from test_skill_creation_gateway import SmallRecipe, VALID


def settings(**updates):
    updates.setdefault('model_request_capabilities', {})
    return Settings(_env_file=None, model_provider='deepseek', model_name='deepseek-flash',
                    api_base='https://api.deepseek.com', api_key='synthetic-test-key', **updates)


@pytest.mark.parametrize('name', ['deepseek-flash', 'deepseek-v4-pro'])
@pytest.mark.parametrize('base', ['https://api.deepseek.com', 'https://api.deepseek.com/v1/', None])
def test_official_native_capacity_does_not_mutate_settings(name, base):
    original = settings(model_request_capabilities={'deepseek': {'deepseek-flash': {'vision': True}}})
    original = original.model_copy(update={'model_name':name,'api_base':base})
    snapshot = original.model_dump()
    policy = resolve_skill_model_policy(original, {})
    assert policy.capacity.context_tokens == 1_048_576
    assert policy.capacity.max_output_tokens == 393_216
    assert policy.settings.model_request_capabilities['deepseek'][name] == policy.capacity
    assert original.model_dump() == snapshot
    if name == 'deepseek-flash':
        assert policy.capacity.vision is True


@pytest.mark.parametrize('base', ['https://gateway.invalid/v1', 'http://api.deepseek.com',
    'https://api.deepseek.com:8443', 'https://api.deepseek.com/other',
    'https://api.deepseek.com.attacker.invalid'])
def test_other_routes_do_not_inherit_official_capacity(base):
    original = settings().model_copy(update={'api_base':base})
    policy = resolve_skill_model_policy(original, {})
    assert policy.capacity is None and policy.settings is original


@pytest.mark.parametrize('base', ['https://api.deepseek.com?private=parameter', 'https://api.deepseek.com/#fragment'])
def test_invalid_routes_are_rejected_before_capacity_selection(base):
    from app.domain.services.model_route import ModelRouteConfigurationError
    with pytest.raises(ModelRouteConfigurationError):
        resolve_skill_model_policy(settings().model_copy(update={'api_base':base}), {})


def test_overrides_and_explicit_verified_capacity_are_respected():
    original = settings(model_request_capabilities={'deepseek': {'deepseek-flash': {
        'context_tokens': 65536, 'max_output_tokens':16384}}})
    policy = resolve_skill_model_policy(original, {})
    assert policy.capacity.context_tokens == 65536 and policy.capacity.max_output_tokens == 16384
    for name in ['deepseek-chat', 'unverified-model']:
        assert resolve_skill_model_policy(original, {'model_name':name}).capacity is None
    assert resolve_skill_model_policy(original, {'api_base':'https://gateway.invalid',
                                                'api_key':'synthetic-other-key'}).capacity is None


@pytest.mark.asyncio
async def test_physical_driver_receives_native_capacity_above_legacy_context(monkeypatch):
    original = settings()
    before = original.model_dump()
    monkeypatch.setattr(model_runtime, 'get_settings', lambda: original)
    client = FakeClient(cache=False)
    client._responses.append(AIMessage(content=json.dumps(VALID)))
    monkeypatch.setattr('langchain_deepseek.ChatDeepSeek', lambda **kwargs: client)
    policy = resolve_skill_model_policy(original, {})
    model = create_chat_model(policy.settings, overrides={'max_tokens':2000})
    gateway = SynthesisGateway(model=model, system='Return JSON.', privacy=None,
        response_validation=model_runtime.model_response_validation,
        usage_recorder=SimpleNamespace(record_from_message=AsyncMock()), user_id='test-owner', session_id='test-session',
        settings=policy.settings, overrides={}, model_policy=policy)
    await gateway.generate(SmallRecipe, 'workflow', 'Preserve all methods.', {})
    assert client._requests[0]['max_tokens'] == 393_216
    assert original.model_context_capacity_tokens == 131_072
    assert original.model_dump() == before
    assert model_runtime._REQUEST_CAPACITY.get() is None


@pytest.mark.asyncio
async def test_capacity_scope_isolated_between_concurrent_calls_and_model_identities(monkeypatch):
    original = settings()
    monkeypatch.setattr(model_runtime, 'get_settings', lambda: original)
    policy = resolve_skill_model_policy(original, {})
    output = {}
    async def call(label, name, scoped):
        async def invoke(messages, maximum):
            output[label] = maximum
            return AIMessage(content='complete')
        with (model_runtime.model_request_capacity(policy.provider,policy.model_name,policy.capacity) if scoped else nullcontext()):
            await asyncio.sleep(0)
            await model_runtime.invoke_model_request(messages=[], provider='deepseek', model_name=name,
                max_output_tokens=393_216, invoke=invoke)
    await asyncio.gather(call('verified','deepseek-flash',True),call('ordinary','deepseek-flash',False),
                         call('other-model','different-model',True))
    assert output['verified'] == 393_216
    assert output['ordinary'] < 131_072 and output['other-model'] < 131_072
    assert model_runtime._REQUEST_CAPACITY.get() is None
