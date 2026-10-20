from unittest.mock import AsyncMock

import httpx
import pytest

from app.application.errors.exceptions import UpstreamServiceError
from app.infrastructure.external import sso_client


class FakeAsyncClient:
    def __init__(self, response):
        self.response = response
        self.post = AsyncMock(return_value=response)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


def response(payload, status=200):
    return httpx.Response(
        status,
        json=payload,
        request=httpx.Request("POST", "https://space.4fair.cn/test"),
    )


@pytest.mark.asyncio
async def test_resolve_sso_uid_passes_token_in_authorization_header(monkeypatch):
    client = FakeAsyncClient(response({"code": 200, "data": {"uid": "user-1"}}))
    monkeypatch.setattr(sso_client.httpx, "AsyncClient", lambda **_kwargs: client)

    uid = await sso_client.resolve_sso_uid(" sso-token ")

    assert uid == "user-1"
    _, kwargs = client.post.await_args
    assert kwargs["headers"] == {"Authorization": "sso-token"}
    assert kwargs.get("params") is None


@pytest.mark.asyncio
async def test_resolve_sso_uid_rejects_missing_uid(monkeypatch):
    client = FakeAsyncClient(response({"code": 200, "data": {}}))
    monkeypatch.setattr(sso_client.httpx, "AsyncClient", lambda **_kwargs: client)

    with pytest.raises(UpstreamServiceError):
        await sso_client.resolve_sso_uid("sso-token")


@pytest.mark.asyncio
async def test_record_analysis_tool_usage_uses_required_query_parameters(monkeypatch):
    client = FakeAsyncClient(response({"code": 200, "data": None}))
    monkeypatch.setattr(sso_client.httpx, "AsyncClient", lambda **_kwargs: client)

    await sso_client.record_analysis_tool_usage(
        uid="user-1",
        title="气象数据集",
        tool_id="geoscience_trend",
    )

    _, kwargs = client.post.await_args
    assert kwargs["params"] == {
        "uid": "user-1",
        "title": "气象数据集",
        "toolId": "geoscience_trend",
        "source": "数据中心",
    }
