"""Complete manifest requests remain cancellable without a batch time ceiling."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse

from app.application.errors.exceptions import BadRequestError, ForbiddenError
from app.interfaces.api import open_data_routes as routes
from app.interfaces.errors.exception_handlers import register_exception_handlers


@pytest.mark.asyncio
async def test_slow_manifest_sends_heartbeats_then_one_complete_json(monkeypatch):
    monkeypatch.setattr(routes, "RESOLUTION_HEARTBEAT_SECONDS", 0.001)
    released = asyncio.Event()

    async def work():
        await released.wait()
        return {"files": [{"id": str(i)} for i in range(101)]}

    response = await routes._long_json(work())
    assert isinstance(response, StreamingResponse)
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["cache-control"] == "no-store"
    chunks = [await anext(response.body_iterator), await anext(response.body_iterator)]
    assert chunks == [b"\n", b"\n"]
    released.set()
    chunks.extend([chunk async for chunk in response.body_iterator])
    result = json.loads(b"".join(chunks))
    assert result["code"] == 0
    assert [file["id"] for file in result["data"]["files"]] == [str(i) for i in range(101)]


@pytest.mark.asyncio
async def test_closing_slow_response_cancels_and_joins_underlying_work(monkeypatch):
    monkeypatch.setattr(routes, "RESOLUTION_HEARTBEAT_SECONDS", 0.001)
    stopped = asyncio.Event()

    async def work():
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    response = await routes._long_json(work())
    assert await anext(response.body_iterator) == b"\n"
    await response.body_iterator.aclose()
    assert stopped.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("error, code, message", [
    (BadRequestError("文件选择已失效，请重新选择"), 400, "文件选择已失效，请重新选择"),
    (RuntimeError("secret /private/provider/path"), 500, "文件处理暂未完成，请稍后重试。"),
])
async def test_slow_failures_finish_the_json_contract_without_leaking_internals(monkeypatch, error, code, message):
    monkeypatch.setattr(routes, "RESOLUTION_HEARTBEAT_SECONDS", 0.001)
    released = asyncio.Event()

    async def work():
        await released.wait()
        raise error

    response = await routes._long_json(work())
    first = await anext(response.body_iterator)
    released.set()
    body = first + b"".join([chunk async for chunk in response.body_iterator])
    assert json.loads(body) == {"code": code, "msg": message, "data": None}
    assert b"/private" not in body


@pytest.mark.asyncio
async def test_fast_failures_keep_normal_http_error_handling():
    async def work():
        raise BadRequestError("文件选择已失效")

    with pytest.raises(BadRequestError, match="文件选择已失效"):
        await routes._long_json(work())


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix, body", [
    ("candidates/candidate/resolve", {}),
    ("acquisitions", {"candidate_id": "candidate", "file_ids": ["file"]}),
])
async def test_ownership_is_checked_before_streaming_or_starting_work(suffix, body):
    service = SimpleNamespace(get_search=AsyncMock(side_effect=ForbiddenError()),
                              resolve=AsyncMock(), create_acquisition=AsyncMock())
    app = FastAPI()
    app.include_router(routes.router)
    register_exception_handlers(app)
    app.dependency_overrides[routes.get_current_user] = lambda: SimpleNamespace(id="owner")
    app.dependency_overrides[routes.get_open_data_service] = lambda: service
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(f"/sessions/session/data-searches/search/{suffix}", json=body)
    assert response.status_code == 403
    assert response.json()["code"] == 403
    service.get_search.assert_awaited_once_with("owner", "session", "search")
    service.resolve.assert_not_called()
    service.create_acquisition.assert_not_called()
