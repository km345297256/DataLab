"""Public authentication throttles, client attribution and retry responses."""
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.application.errors.exceptions import AppException, UnauthorizedError
from app.application.services.auth_rate_limit import AuthRateLimiter, get_auth_rate_limiter
from app.interfaces.api import auth_routes
from app.interfaces.dependencies import get_auth_service
from app.interfaces.errors.exception_handlers import register_exception_handlers


class BudgetStore:
    """Atomic in-memory equivalent; live Redis script coverage is opt-in below."""
    def __init__(self):
        self.counts = {}
        self.expiries = {}

    async def eval(self, script, count, *args):
        keys = args[:count]
        budgets = args[count:]
        for index, key in enumerate(keys):
            if self.counts.get(key, 0) >= budgets[2 * index]:
                return self.expiries[key]
        for index, key in enumerate(keys):
            self.counts[key] = self.counts.get(key, 0) + 1
            self.expiries.setdefault(key, budgets[2 * index + 1])
        return 0

    async def delete(self, key):
        self.counts.pop(key, None)
        self.expiries.pop(key, None)


@pytest.fixture
def store(monkeypatch):
    client = BudgetStore()
    monkeypatch.setattr("app.infrastructure.storage.redis.get_redis", lambda: SimpleNamespace(initialize=AsyncMock(), client=client))
    return client


@pytest.mark.asyncio
async def test_login_email_budget_is_normalized_concurrent_and_cleared_on_success(store):
    limiter = AuthRateLimiter()
    results = await asyncio.gather(*(limiter.check("login", " Alice@Example.com ", f"peer-{index}") for index in range(25)), return_exceptions=True)
    assert results.count(None) == 10
    denied = [result for result in results if isinstance(result, HTTPException)]
    assert len(denied) == 15
    assert all(result.status_code == 429 and result.headers == {"Retry-After": "300"} for result in denied)
    with pytest.raises(HTTPException):
        await limiter.check("login", "alice@example.com", "another-peer")
    assert not any("alice" in key or "example.com" in key for key in store.counts)
    await limiter.login_succeeded("alice@example.com")
    await limiter.check("login", "ALICE@example.com", "peer-0")


@pytest.mark.asyncio
async def test_shared_peer_budget_limits_distinct_accounts_without_tight_per_user_cap(store):
    limiter = AuthRateLimiter()
    for index in range(300):
        await limiter.check("login", f"researcher{index}@example.com", "reverse-proxy")
    with pytest.raises(HTTPException) as caught:
        await limiter.check("login", "another@example.com", "reverse-proxy")
    assert caught.value.status_code == 429
    await limiter.check("login", "another@example.com", "different-peer")


@pytest.mark.asyncio
async def test_registration_uses_separate_account_and_client_budgets(store):
    limiter = AuthRateLimiter()
    for _ in range(3):
        await limiter.check("register", "alice@example.com", "peer")
    with pytest.raises(HTTPException) as caught:
        await limiter.check("register", "ALICE@example.com", "another-peer")
    assert caught.value.headers == {"Retry-After": "3600"}
    await limiter.check("login", "alice@example.com", "peer")


@pytest.mark.asyncio
async def test_rate_limit_storage_outages_fail_closed(monkeypatch):
    monkeypatch.setattr("app.infrastructure.storage.redis.get_redis", lambda: SimpleNamespace(initialize=AsyncMock(side_effect=ConnectionError())))
    limiter = AuthRateLimiter()
    for request in (limiter.check("login", "alice@example.com", "peer"), limiter.login_succeeded("alice@example.com")):
        with pytest.raises(AppException) as caught:
            await request
        assert caught.value.status_code == 503


@pytest.mark.asyncio
async def test_http_throttles_ignore_spoofed_forwarded_addresses_and_keep_retry_header(store):
    app = FastAPI()
    app.include_router(auth_routes.router, prefix="/api/v1")
    register_exception_handlers(app)
    service = SimpleNamespace(login_with_tokens=AsyncMock(side_effect=UnauthorizedError("Invalid email or password")))
    app.dependency_overrides[get_auth_service] = lambda: service
    limiter = AuthRateLimiter()
    app.dependency_overrides[get_auth_rate_limiter] = lambda: limiter
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("192.0.2.2", 1000)), base_url="http://test") as client:
        for index in range(11):
            response = await client.post("/api/v1/auth/login", json={"email": "alice@example.com", "password": "wrong-password"},
                                         headers={"X-Forwarded-For": f"198.51.100.{index}"})
        assert response.status_code == 429
        assert response.headers["retry-after"] == "300"
        assert "Too many" in response.json()["msg"]
    assert service.login_with_tokens.await_count == 10
    client_keys = [key for key in store.counts if ":client:" in key]
    assert client_keys == [limiter._key("login", "client", "192.0.2.2")]


@pytest.mark.asyncio
async def test_atomic_budget_script_against_disposable_redis(monkeypatch):
    url = os.environ.get("AUTH_TEST_REDIS_URL")
    if not url:
        pytest.skip("Set AUTH_TEST_REDIS_URL to a disposable Redis for Lua execution")
    from redis.asyncio import Redis
    client = Redis.from_url(url, decode_responses=True)
    prefix = "datalab-auth-test:" + uuid4().hex + ":"
    limiter = AuthRateLimiter()
    original_key = limiter._key
    monkeypatch.setattr(limiter, "_key", lambda *args: prefix + original_key(*args))
    monkeypatch.setattr("app.infrastructure.storage.redis.get_redis", lambda: SimpleNamespace(initialize=AsyncMock(), client=client))
    try:
        outcomes = await asyncio.gather(*(limiter.check("login", "alice@example.com", f"peer-{index}") for index in range(30)), return_exceptions=True)
        assert outcomes.count(None) == 10
        assert sum(isinstance(outcome, HTTPException) and outcome.status_code == 429 for outcome in outcomes) == 20
        key = limiter._key("login", "account", "alice@example.com")
        assert await client.get(key) == "10"
        assert 0 < await client.ttl(key) <= 300
        await limiter.login_succeeded("alice@example.com")
        await limiter.check("login", "alice@example.com", "new-peer")
    finally:
        keys = [key async for key in client.scan_iter(match=prefix + "*")]
        if keys:
            await client.delete(*keys)
        await client.aclose()
