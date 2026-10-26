"""Explicit creator commands must execute without weakening front-door safety."""
import json
from types import SimpleNamespace

import pytest

from app.application.services import dataset_request_resolver as module


async def _resolve(monkeypatch, question, *, allowed=True):
    payload = {
        "safety": {"decision": "allow" if allowed else "reject", "risk_level": "low"},
        "execution": {"mode": "direct", "required_evidence": "user_message"},
        "answer": "可以根据分析流程编写技能。",
        "catalog_queries": [],
    }

    class Model:
        def bind(self, **kwargs):
            return self

        async def ainvoke(self, messages):
            return SimpleNamespace(content=json.dumps(payload))

    async def empty_rules():
        return []

    async def record(*args, **kwargs):
        pass

    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(module, "create_chat_model", lambda *args, **kwargs: Model())
    resolver = module.DatasetRequestResolver()
    resolver._policy_store = SimpleNamespace(list_enabled=empty_rules)
    resolver._record_usage = record
    return await resolver.resolve(question=question, datasets=[], events=[])


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [
    "/skill-creator 将这个流程保存为可复用的技能",
    "使用 /skill-creator 将这个流程保存为可复用的技能",
    "/skill-create save this workflow",
])
async def test_allowed_explicit_command_reaches_creator_instead_of_text_reply(monkeypatch, command):
    resolution = await _resolve(monkeypatch, command)
    assert resolution.mode == "sandbox"
    assert resolution.controller_metadata["source"] == "builtin_skill_creator"
    assert resolution.decision.execution.required_capabilities == ["skill_create_from_session"]
    assert resolution.decision.execution.required_evidence == "conversation"
    assert resolution.answer == ""
    assert resolution.target_files == []


@pytest.mark.asyncio
async def test_creator_command_cannot_override_model_safety_rejection(monkeypatch):
    resolution = await _resolve(monkeypatch, "/skill-creator 保存这个流程", allowed=False)
    assert resolution.mode == "reject"


@pytest.mark.asyncio
@pytest.mark.parametrize("question", [
    "解释一下 /skill-creator 是什么", "不要使用 /skill-creator 保存流程",
    "/skill-creator-example 的作用是什么？",
])
async def test_command_mention_or_negation_does_not_execute_creator(monkeypatch, question):
    resolution = await _resolve(monkeypatch, question)
    assert resolution.mode == "direct"
