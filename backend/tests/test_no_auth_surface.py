"""Account boundaries replacing the former shared-anonymous administrator."""
import hashlib
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, WebSocket, WebSocketException
from fastapi.middleware.cors import CORSMiddleware
from starlette.requests import Request

from app.application.errors.exceptions import AppException, UnauthorizedError, ForbiddenError
from app.application.services.auth_service import AuthService
from app.application.services.auth_rate_limit import get_auth_rate_limiter
from app.application.services.file_service import FileService
from app.application.services.token_service import TokenService
from app.core.config import Settings
from app.domain.models.file import FileInfo
from app.domain.models.user import User, UserRole, RegistrationStatus
from app.interfaces.api.routes import create_api_router
from app.interfaces.dependencies import get_auth_service, get_current_user, get_file_service, get_token_service, verify_signature_websocket
from app.interfaces.errors.exception_handlers import register_exception_handlers
from app.main import app as deployment_app


class MemoryUsers:
    def __init__(self):
        self.users = {}
    async def get_user_by_id(self, user_id):
        return self.users.get(user_id)
    async def get_user_by_email(self, email):
        return next((user for user in self.users.values() if user.email == email.lower()), None)
    async def email_exists(self, email):
        return await self.get_user_by_email(email) is not None
    async def create_user(self, user):
        self.users[user.id] = user
        return user
    async def update_user(self, user):
        self.users[user.id] = user
        return user
    async def list_users(self, limit=100, offset=0):
        return list(self.users.values())[offset:offset + limit]


@pytest.fixture
def auth(monkeypatch):
    settings = Settings(_env_file=None, auth_provider="password", jwt_secret_key="test-secret-" * 8)
    tokens = TokenService()
    tokens.settings = settings
    repo = MemoryUsers()
    service = AuthService(repo, tokens)
    service.settings = settings
    cache = {}
    async def exists(key): return key in cache
    async def set_value(key, ttl, value):
        cache[key] = value
        return True
    store = SimpleNamespace(initialize=AsyncMock(), client=SimpleNamespace(exists=exists, setex=set_value))
    monkeypatch.setattr("app.infrastructure.storage.redis.get_redis", lambda: store)
    for user_id in ("alice", "bob", "admin"):
        repo.users[user_id] = User(id=user_id, fullname=user_id.title(), email=f"{user_id}@test.local",
                                  password_hash=service._hash_password("secret-password"),
                                  role=UserRole.ADMIN if user_id == "admin" else UserRole.USER)
    return service


@pytest.fixture
def app(auth):
    application = FastAPI()
    application.include_router(create_api_router(), prefix="/api/v1")
    register_exception_handlers(application)
    application.dependency_overrides[get_auth_service] = lambda: auth
    application.dependency_overrides[get_auth_rate_limiter] = lambda: SimpleNamespace(check=AsyncMock(), login_succeeded=AsyncMock())
    application.dependency_overrides[get_token_service] = lambda: auth.token_service
    return application


def headers(auth, user_id):
    return {"Authorization": "Bearer " + auth.token_service.create_access_token(auth.user_repository.users[user_id])}


@pytest.mark.asyncio
async def test_accounts_are_resolved_from_tokens_and_anonymous_requests_are_rejected(app, auth):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for endpoint in ("/auth/me", "/sessions", "/files/private/download", "/plugins/runtime", "/data-library", "/data-library/stats"):
            assert (await client.get("/api/v1" + endpoint)).status_code == 401
        for owner in ("alice", "bob"):
            response = await client.get("/api/v1/auth/me", headers=headers(auth, owner))
            assert response.json()["data"]["id"] == owner
            assert response.json()["data"]["role"] == "user"
        refresh = auth.token_service.create_refresh_token(auth.user_repository.users["alice"])
        assert (await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer " + refresh})).status_code == 401
        assert (await client.get("/api/v1/auth/registrations", headers=headers(auth, "alice"))).status_code == 401
        assert (await client.get("/api/v1/auth/registrations", headers=headers(auth, "admin"))).status_code == 200


@pytest.mark.asyncio
async def test_private_file_download_and_signing_cannot_cross_user_boundary(app, auth):
    info = FileInfo(file_id="private-file", filename="private.csv", content_type="text/csv", size=7, user_id="alice")
    class Storage:
        async def get_file_info(self, file_id, user_id=None):
            return info if file_id == info.file_id and user_id in (None, "alice") else None
        async def download_file(self, file_id, user_id=None):
            return BytesIO(b"a,b\n1,2"), info
        async def download_file_range(self, file_id, user_id=None, *, offset, length):
            assert user_id in (None, "alice")
            return b"a,b\n1,2"[offset:offset + length], info
    app.dependency_overrides[get_file_service] = lambda: FileService(Storage(), auth.token_service)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        path = "/api/v1/files/private-file"
        assert (await client.get(path + "/download", headers=headers(auth, "alice"))).status_code == 200
        assert (await client.get(path + "/download", headers=headers(auth, "bob"))).status_code == 404
        assert (await client.post(path + "/signed-url", json={"expire_minutes": 5}, headers=headers(auth, "bob"))).status_code == 404
        signed = await client.post(path + "/signed-url", json={"expire_minutes": 5}, headers=headers(auth, "alice"))
        assert signed.status_code == 200
        url = signed.json()["data"]["signed_url"]
        assert (await client.get(url)).status_code == 200
        assert (await client.get(path)).status_code == 401
        assert (await client.get(url.replace("private-file", "another-file"))).status_code == 401
        assert (await client.get(url + "&signature=duplicate")).status_code == 401
        expired = auth.token_service.create_signed_url(path, expire_minutes=-1)
        assert (await client.get(expired)).status_code == 401


@pytest.mark.asyncio
async def test_login_registration_approval_and_logout_revoke_both_tokens(app, auth, monkeypatch):
    monkeypatch.setattr("app.application.services.auth_service.UserDocument", SimpleNamespace(find_one=AsyncMock(return_value=None)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/auth/register", json={"email": "new@test.local", "fullname": "New Researcher", "password": "secret-password", "role": "admin"})
        assert response.status_code == 200
        created = response.json()["data"]["user"]
        assert created["role"] == "user" and created["registration_status"] == "pending"
        assert not created["is_active"]
        credentials = {"email": "new@test.local", "password": "secret-password"}
        assert (await client.post("/api/v1/auth/login", json=credentials)).status_code == 403
        decision = await client.post(f"/api/v1/auth/registrations/{created['id']}/decision", json={"status": "approved"}, headers=headers(auth, "admin"))
        assert decision.status_code == 200
        login = (await client.post("/api/v1/auth/login", json=credentials)).json()["data"]
        access = {"Authorization": "Bearer " + login["access_token"]}
        assert (await client.get("/api/v1/auth/me", headers=access)).status_code == 200
        assert (await client.post("/api/v1/auth/logout", headers=access, json={"refresh_token": login["refresh_token"]})).status_code == 200
        assert (await client.get("/api/v1/auth/me", headers=access)).status_code == 401
        assert (await client.post("/api/v1/auth/refresh", json={"refresh_token": login["refresh_token"]})).status_code == 401


@pytest.mark.asyncio
async def test_deactivation_and_password_changes_invalidate_existing_credentials(auth):
    user = auth.user_repository.users["alice"]
    access = auth.token_service.create_access_token(user)
    refresh = auth.token_service.create_refresh_token(user)
    user.is_active = False
    assert await auth.verify_token(access) is None
    with pytest.raises(UnauthorizedError): await auth.refresh_access_token(refresh)
    user.is_active = True
    user.password_hash = auth._hash_password("another-password")
    assert await auth.verify_token(access) is None
    with pytest.raises(UnauthorizedError): await auth.refresh_access_token(refresh)


@pytest.mark.asyncio
async def test_revocation_read_failure_cannot_reactivate_logged_out_tokens(auth, monkeypatch):
    from app.infrastructure.storage.redis import get_redis
    user = auth.user_repository.users["alice"]
    access = auth.token_service.create_access_token(user)
    refresh = auth.token_service.create_refresh_token(user)
    await auth.logout(access, refresh)
    store = get_redis()
    original = store.client.exists
    monkeypatch.setattr(store.client, "exists", AsyncMock(side_effect=ConnectionError("Redis unavailable")))
    for verify, token in ((auth.verify_token, access), (auth.refresh_access_token, refresh)):
        with pytest.raises(AppException) as failure:
            await verify(token)
        assert failure.value.status_code == 503
    monkeypatch.setattr(store.client, "exists", original)
    assert await auth.verify_token(access) is None
    with pytest.raises(UnauthorizedError): await auth.refresh_access_token(refresh)


@pytest.mark.asyncio
@pytest.mark.parametrize("raises", [True, False])
async def test_logout_does_not_report_success_when_revocation_write_fails(auth, monkeypatch, raises):
    from app.infrastructure.storage.redis import get_redis
    user = auth.user_repository.users["alice"]
    write = AsyncMock(side_effect=ConnectionError("Redis unavailable")) if raises else AsyncMock(return_value=False)
    monkeypatch.setattr(get_redis().client, "setex", write)
    with pytest.raises(AppException) as failure:
        await auth.logout(auth.token_service.create_access_token(user), auth.token_service.create_refresh_token(user))
    assert failure.value.status_code == 503


@pytest.mark.asyncio
async def test_bootstrap_never_promotes_an_existing_account(auth):
    auth.settings.bootstrap_admin_email = "alice@test.local"
    auth.settings.bootstrap_admin_password = "bootstrap-secret-password"
    await auth.bootstrap_admin()
    assert auth.user_repository.users["alice"].role == UserRole.USER
    auth.settings.bootstrap_admin_email = "first-admin@test.local"
    await auth.bootstrap_admin()
    created = await auth.user_repository.get_user_by_email("first-admin@test.local")
    assert created.role == UserRole.ADMIN and created.is_active
    assert auth._verify_password("bootstrap-secret-password", created.password_hash)


def test_password_hashes_use_independent_salts_and_verify_legacy(auth):
    first = auth._hash_password("same-password")
    second = auth._hash_password("same-password")
    assert first != second
    assert auth._verify_password("same-password", first)
    assert not auth._verify_password("wrong-password", first)
    auth.settings.password_salt = "legacy-salt"
    legacy = "legacy-salt" + hashlib.pbkdf2_hmac("sha256", b"old-password", b"legacy-salt", 10).hex()
    assert auth._verify_password("old-password", legacy)


@pytest.mark.asyncio
async def test_websocket_requires_signature_for_exact_session(auth):
    def socket(url):
        path, _, query = url.partition("?")
        return WebSocket({"type": "websocket", "scheme": "ws", "path": path, "query_string": query.encode(), "headers": [], "server": ("test", 80)}, receive=AsyncMock(), send=AsyncMock())
    url = auth.token_service.create_signed_url("/api/v1/sessions/alice-session/vnc")
    assert await verify_signature_websocket(socket(url), auth.token_service)
    with pytest.raises(WebSocketException):
        await verify_signature_websocket(socket(url.replace("alice-session", "bob-session")), auth.token_service)
    with pytest.raises(WebSocketException):
        await verify_signature_websocket(socket("/api/v1/sessions/alice-session/vnc"), auth.token_service)


@pytest.mark.asyncio
async def test_read_only_collaborators_cannot_mint_interactive_vnc_links(app, auth):
    from app.interfaces.dependencies import get_agent_service
    session = SimpleNamespace(user_id="alice")
    agent = SimpleNamespace(get_session=AsyncMock(return_value=session))
    app.dependency_overrides[get_agent_service] = lambda: agent
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        path = "/api/v1/sessions/alice-session/vnc/signed-url"
        assert (await client.post(path, json={}, headers=headers(auth, "bob"))).status_code == 404
        response = await client.post(path, json={}, headers=headers(auth, "alice"))
        assert response.status_code == 200
        url = response.json()["data"]["signed_url"]
        assert "owner=alice" in url
        assert auth.token_service.verify_signed_url(url)
        assert not auth.token_service.verify_signed_url(url.replace("owner=alice", "owner=bob"))


@pytest.mark.asyncio
@pytest.mark.parametrize("active,session_owner,query_owner", [(True, "bob", "alice"), (False, "alice", "alice"), (True, "alice", "")])
async def test_vnc_rechecks_owner_status_before_accepting_a_signed_connection(auth, active, session_owner, query_owner):
    from app.interfaces.api.session_routes import vnc_websocket
    auth.user_repository.users["alice"].is_active = active
    socket = SimpleNamespace(query_params={"owner": query_owner}, accept=AsyncMock(), close=AsyncMock())
    agent = SimpleNamespace(get_session=AsyncMock(return_value=SimpleNamespace(user_id=session_owner)), get_vnc_url=AsyncMock())
    await vnc_websocket(socket, "alice-session", signature="already-verified", agent_service=agent, user_repository=auth.user_repository)
    socket.accept.assert_not_awaited()
    socket.close.assert_awaited_once_with(code=1008, reason="Session owner access required")
    agent.get_vnc_url.assert_not_awaited()


def test_authentication_routes_exist_without_restoring_removed_product_domains(monkeypatch):
    monkeypatch.delenv("AUTH_PROVIDER", raising=False)
    assert Settings(_env_file=None).auth_provider == "password"
    app = FastAPI()
    app.include_router(create_api_router(), prefix="/api/v1")
    schema = app.openapi()
    assert "/api/v1/auth/login" in schema["paths"]
    assert "/api/v1/auth/registrations" in schema["paths"]
    assert "securitySchemes" in schema["components"]
    assert not any("/collaborators" in path or "/api-keys" in path for path in schema["paths"])
    cors = next(m for m in deployment_app.user_middleware if m.cls is CORSMiddleware)
    assert "*" not in cors.kwargs["allow_origins"]


@pytest.mark.asyncio
async def test_validation_response_never_echoes_a_rejected_host_path(app, auth):
    sensitive_path = "/Users/example/private/dataset\\u0001".replace("\\u0001", "\u0001")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/files/large-uploads/init",
            headers=headers(auth, "admin"),
            json={"filename": "private.csv", "size": sensitive_path},
        )
    assert response.status_code == 422
    assert "/Users/example/private/dataset" not in response.text
    assert all(set(error) <= {"loc", "msg", "type"} for error in response.json()["detail"])
