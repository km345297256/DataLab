"""Opt-in API acceptance against disposable MongoDB and Redis instances.

No application database is used. Every test owns a random database and a Redis
key prefix; cleanup drops/deletes only those resources and never flushes Redis.
"""
import asyncio
from contextlib import asynccontextmanager
import os
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

from beanie import init_beanie
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import jwt
from pymongo.asynchronous.mongo_client import AsyncMongoClient
from pymongo.errors import DuplicateKeyError
import pytest
import pytest_asyncio
from redis.asyncio import Redis

from app.application.errors.exceptions import ConflictError
from app.application.services.auth_service import AuthService
from app.application.services.token_service import TokenService
from app.core.config import get_settings
from app.domain.models.user import RegistrationStatus, User, UserRole
from app.infrastructure.models.documents import RoleTokenQuotaDocument, UserDocument
from app.infrastructure.repositories.user_repository import MongoUserRepository
from app.interfaces.api import admin_routes, auth_routes
from app.interfaces.dependencies import get_auth_service
from app.interfaces.errors.exception_handlers import register_exception_handlers


PASSWORD = "Acceptance-test-password-42"


class NamespacedRedis:
    """Delegate real Redis commands while confining keys to this test run."""

    def __init__(self, client, prefix):
        self.raw, self.prefix = client, prefix

    async def exists(self, key):
        return await self.raw.exists(self.prefix + key)

    async def setex(self, key, ttl, value):
        return await self.raw.setex(self.prefix + key, ttl, value)

    async def delete(self, *keys):
        return await self.raw.delete(*(self.prefix + key for key in keys))

    async def eval(self, script, numkeys, *args):
        return await self.raw.eval(script, numkeys,
                                   *(self.prefix + key for key in args[:numkeys]), *args[numkeys:])

    def lock(self, name, **kwargs):
        return self.raw.lock(self.prefix + name, **kwargs)


@pytest_asyncio.fixture
async def env(monkeypatch):
    mongo_uri = os.getenv("DATALAB_ACCOUNT_TEST_MONGODB_URI")
    redis_uri = os.getenv("DATALAB_ACCOUNT_TEST_REDIS_URL")
    if not mongo_uri or not redis_uri:
        pytest.skip("Set DATALAB_ACCOUNT_TEST_MONGODB_URI and DATALAB_ACCOUNT_TEST_REDIS_URL for disposable integration services")
    # These tests intentionally support only local, explicitly selected test
    # instances. They must never follow the application's configured endpoints.
    for uri in (mongo_uri, redis_uri):
        assert urlsplit(uri).hostname in {"127.0.0.1", "localhost", "::1"}, "Use a disposable local test instance"
    identity = uuid4().hex
    database_name = "datalab_account_test_" + identity
    prefix = "datalab-account-test:" + identity + ":"
    mongo = AsyncMongoClient(mongo_uri, serverSelectionTimeoutMS=3000, tz_aware=True)
    redis = Redis.from_url(redis_uri, decode_responses=True, socket_connect_timeout=3, socket_timeout=5)
    database = mongo[database_name]
    prior_settings = {document: document._document_settings for document in (UserDocument, RoleTokenQuotaDocument)}
    settings = get_settings().model_copy(update={
        "auth_provider": "password", "jwt_secret_key": "integration-test-" + uuid4().hex,
        "bootstrap_admin_email": "", "bootstrap_admin_password": "",
    })
    monkeypatch.setattr("app.application.services.auth_service.get_settings", lambda: settings)
    monkeypatch.setattr("app.application.services.token_service.get_settings", lambda: settings)
    monkeypatch.setattr(auth_routes, "get_settings", lambda: settings)
    async def initialize():
        await redis.ping()
    store = SimpleNamespace(initialize=initialize, client=NamespacedRedis(redis, prefix))
    monkeypatch.setattr("app.infrastructure.storage.redis.get_redis", lambda: store)
    try:
        await init_beanie(database=database, document_models=[UserDocument, RoleTokenQuotaDocument])
        await redis.ping()
        repository, tokens = MongoUserRepository(), TokenService()
        service = AuthService(repository, tokens)
        app = FastAPI()
        app.include_router(auth_routes.router, prefix="/api/v1")
        register_exception_handlers(app)
        # Authentication dependencies, JWT checking, database repository,
        # admission limits and account-management locking stay real.
        app.dependency_overrides[get_auth_service] = lambda: service
        async with AsyncClient(transport=ASGITransport(app=app, client=("127.0.0.1", 12345)), base_url="http://test/api/v1/") as client:
            async def seed(name, *, role=UserRole.USER, active=True, status=RegistrationStatus.APPROVED):
                return await repository.create_user(User(
                    id=uuid4().hex, fullname=name, email=name.lower().replace(" ", "-") + "@example.test",
                    password_hash=service._hash_password(PASSWORD), role=role,
                    is_active=active, registration_status=status,
                ))
            yield SimpleNamespace(client=client, database=database, repository=repository,
                                  service=service, tokens=tokens, settings=settings, seed=seed,
                                  redis=redis, redis_prefix=prefix)
    finally:
        await mongo.drop_database(database_name)
        keys = [key async for key in redis.scan_iter(match=prefix + "*")]
        if keys:
            await redis.delete(*keys)
        await redis.aclose()
        await mongo.close()
        for document, original in prior_settings.items():
            document._document_settings = original


def headers(env, user):
    return {"Authorization": "Bearer " + env.tokens.create_access_token(user)}


async def login(env, email):
    response = await env.client.post("auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()["data"]


@pytest.mark.asyncio
async def test_registration_unique_index_approval_and_role_boundary(env):
    admin = await env.seed("Review Admin", role=UserRole.ADMIN)
    payload = {"fullname": "Researcher", "email": "Researcher@Example.test", "password": PASSWORD, "role": "admin"}
    responses = await asyncio.gather(*(env.client.post("auth/register", json=payload) for _ in range(2)))
    assert sorted(item.status_code for item in responses) == [200, 422]
    created = next(item.json()["data"]["user"] for item in responses if item.status_code == 200)
    assert created["email"] == "researcher@example.test"
    assert created["role"] == "user" and created["registration_status"] == "pending" and not created["is_active"]
    assert "password_hash" not in created and "auth_version" not in created
    assert await env.database.users.count_documents({"email": created["email"]}) == 1
    indexes = await env.database.users.index_information()
    assert any(index.get("unique") and index["key"] == [("email", 1)] for index in indexes.values())
    duplicate = await env.database.users.find_one({"user_id": created["id"]})
    duplicate.pop("_id")
    duplicate["user_id"] = uuid4().hex
    with pytest.raises(DuplicateKeyError):
        await env.database.users.insert_one(duplicate)
    blocked = await env.client.post("auth/login", json={"email": created["email"], "password": PASSWORD})
    assert blocked.status_code == 403
    approved = await env.client.post(f"auth/registrations/{created['id']}/decision", headers=headers(env, admin),
                                     json={"status": "approved", "decision_note": "Integration approval"})
    assert approved.status_code == 200, approved.text
    assert approved.json()["data"]["is_active"] is True
    account = await env.repository.get_user_by_id(created["id"])
    assert account.registration_reviewed_by == admin.id and account.auth_version == 1
    credentials = await login(env, created["email"])
    bearer = {"Authorization": "Bearer " + credentials["access_token"]}
    assert (await env.client.get("auth/me", headers=bearer)).status_code == 200
    forbidden = await env.client.patch(f"auth/user/{created['id']}", headers=bearer, json={"role": "admin"})
    assert forbidden.status_code == 403
    assert (await env.repository.get_user_by_id(created["id"])).role == UserRole.USER


@pytest.mark.asyncio
async def test_users_filters_and_pagination_query_real_mongo(env):
    admin = await env.seed("Directory Admin", role=UserRole.ADMIN)
    active = [await env.seed(f"Project Member {index}") for index in range(3)]
    await env.seed("Project Disabled", active=False)
    pending = await env.seed("Project Pending", active=False, status=RegistrationStatus.PENDING)
    await env.seed("Project Rejected", active=False, status=RegistrationStatus.REJECTED)
    bearer = headers(env, admin)
    pages = []
    for offset in (0, 2):
        response = await env.client.get("auth/users", headers=bearer,
                                        params={"query": "PROJECT MEMBER", "role": "user", "status": "active", "limit": 2, "offset": offset})
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["total"] == 3
        pages.extend(data["users"])
    assert {row["id"] for row in pages} == {user.id for user in active} and len(pages) == 3
    for row in pages:
        assert "password_hash" not in row
    pending_page = await env.client.get("auth/users", headers=bearer, params={"status": "pending"})
    assert [row["id"] for row in pending_page.json()["data"]["users"]] == [pending.id]
    inactive = await env.client.get("auth/users", headers=bearer, params={"status": "inactive"})
    assert inactive.json()["data"]["total"] == 1
    literal = await env.client.get("auth/users", headers=bearer, params={"query": ".*"})
    assert literal.json()["data"] == {"users": [], "total": 0}
    assert (await env.client.get("auth/users", headers=headers(env, active[0]))).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True], ids=["versioned", "missing-version-compatible"])
async def test_disable_reactivate_never_revives_old_access_or_refresh(env, legacy):
    admin = await env.seed("Security Admin", role=UserRole.ADMIN)
    member = await env.seed("Security Member")
    if legacy:
        await env.database.users.update_one({"user_id": member.id}, {"$unset": {"auth_version": "", "registration_status": ""}})
        member = await env.repository.get_user_by_id(member.id)
        assert member.auth_version == 0 and member.registration_status == RegistrationStatus.APPROVED
    access, refresh = env.tokens.create_access_token(member), env.tokens.create_refresh_token(member)
    if legacy:
        def remove_version(token):
            payload = env.tokens.verify_token(token)
            payload.pop("auth_version")
            return jwt.encode(payload, env.settings.jwt_secret_key, algorithm=env.settings.jwt_algorithm)
        access, refresh = remove_version(access), remove_version(refresh)
    bearer = {"Authorization": "Bearer " + access}
    assert (await env.client.get("auth/me", headers=bearer)).status_code == 200
    assert (await env.client.post("auth/refresh", json={"refresh_token": refresh})).status_code == 200
    admin_headers = headers(env, admin)
    disabled = await env.client.patch(f"auth/user/{member.id}", headers=admin_headers, json={"is_active": False})
    assert disabled.status_code == 200, disabled.text
    assert (await env.repository.get_user_by_id(member.id)).auth_version == 1
    for restored in (False, True):
        if restored:
            response = await env.client.patch(f"auth/user/{member.id}", headers=admin_headers, json={"is_active": True})
            assert response.status_code == 200
        assert (await env.client.get("auth/me", headers=bearer)).status_code == 401
        assert (await env.client.post("auth/refresh", json={"refresh_token": refresh})).status_code == 401
    fresh = await login(env, member.email)
    assert (await env.client.get("auth/me", headers={"Authorization": "Bearer " + fresh["access_token"]})).status_code == 200


@pytest.mark.asyncio
async def test_stale_preference_update_cannot_restore_disabled_user(env):
    admin = await env.seed("CAS Admin", role=UserRole.ADMIN)
    member = await env.seed("CAS Member")
    stale = await env.repository.get_user_by_id(member.id)
    response = await env.client.patch(f"auth/user/{member.id}", headers=headers(env, admin), json={"is_active": False})
    assert response.status_code == 200
    stale.installed_renderer_ids = ["stale-renderer"]
    stale.role, stale.is_active = UserRole.ADMIN, True
    with pytest.raises(ConflictError):
        await env.repository.update_user(stale)
    current = await env.repository.get_user_by_id(member.id)
    assert current.is_active is False and current.role == UserRole.USER and current.auth_version == 1
    assert current.installed_renderer_ids == []
    # Even a current document cannot use the preference writer to alter security.
    current.is_active, current.role = True, UserRole.ADMIN
    current.installed_renderer_ids = ["current-renderer"]
    await env.repository.update_user(current)
    saved = await env.repository.get_user_by_id(member.id)
    assert saved.is_active is False and saved.role == UserRole.USER and saved.auth_version == 1
    assert saved.installed_renderer_ids == ["current-renderer"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"is_active": False}, {"role": "user"}], ids=["disable", "demote"])
async def test_two_admins_cannot_concurrently_remove_each_other(env, change):
    first = await env.seed("First Admin", role=UserRole.ADMIN)
    second = await env.seed("Second Admin", role=UserRole.ADMIN)
    results = await asyncio.gather(
        env.client.patch(f"auth/user/{second.id}", headers=headers(env, first), json=change),
        env.client.patch(f"auth/user/{first.id}", headers=headers(env, second), json=change),
    )
    assert sum(response.status_code == 200 for response in results) == 1, [response.text for response in results]
    assert all(response.status_code in {200, 401, 403} for response in results)
    assert await env.database.users.count_documents({"role": "admin", "is_active": True}) == 1
    remaining = await env.database.users.find_one({"role": "admin", "is_active": True})
    assert (await login(env, remaining["email"]))["user"]["role"] == "admin"


@pytest.mark.asyncio
async def test_real_redis_login_budget_applies_through_api(env):
    email = "missing-" + uuid4().hex + "@example.test"
    for _ in range(10):
        response = await env.client.post("auth/login", json={"email": email, "password": PASSWORD})
        assert response.status_code == 401
    response = await env.client.post("auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 429
    assert 1 <= int(response.headers["retry-after"]) <= 300
    keys = [key async for key in env.redis.scan_iter(match=env.redis_prefix + "auth:limit:*")]
    assert len(keys) == 2 and all(email not in key for key in keys)


@pytest.mark.asyncio
async def test_skill_cleanup_preserves_concurrent_security_and_unrelated_skill_changes(env, monkeypatch):
    admin = await env.seed("Cleanup Admin", role=UserRole.ADMIN)
    member = await env.seed("Cleanup Member")
    collection = env.database.users
    await collection.update_one({"user_id": member.id}, {"$set": {
        "installed_skill_ids": ["removed-skill", "keep-skill"],
        "auto_enabled_skills": ["  MAPPING[1]  ", "mapping1", "keep-auto"],
        "installed_renderer_ids": ["keep-renderer"],
    }})
    started, proceed = asyncio.Event(), asyncio.Event()
    class GatedCollection:
        def __getattr__(self, name):
            return getattr(collection, name)

        async def update_many(self, *args, **kwargs):
            started.set()
            await proceed.wait()
            return await collection.update_many(*args, **kwargs)

    monkeypatch.setattr(UserDocument, "get_pymongo_collection", classmethod(lambda cls: GatedCollection()))
    cleanup = asyncio.create_task(admin_routes._clear_deleted_skill_references("removed-skill", "mapping[1]"))
    try:
        await asyncio.wait_for(started.wait(), 3)
        changed = await env.client.patch(f"auth/user/{member.id}", headers=headers(env, admin),
                                         json={"is_active": False, "role": "software"})
        assert changed.status_code == 200, changed.text
        updated_password = env.service._hash_password("Changed-during-cleanup-42")
        await env.repository.patch_user(member.id, {"password_hash": updated_password},
                                         expected_auth_version=1, revoke_sessions=True)
        await collection.update_one({"user_id": member.id}, {"$addToSet": {
            "installed_skill_ids": "new-skill", "auto_enabled_skills": "new-auto",
        }})
        proceed.set()
        await cleanup
    finally:
        proceed.set()
        await asyncio.gather(cleanup, return_exceptions=True)
    user = await env.repository.get_user_by_id(member.id)
    assert user.is_active is False and user.role == UserRole.SOFTWARE
    assert user.password_hash == updated_password and user.auth_version == 2
    assert user.installed_skill_ids == ["keep-skill", "new-skill"]
    assert user.auto_enabled_skills == ["mapping1", "keep-auto", "new-auto"]
    assert user.installed_renderer_ids == ["keep-renderer"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"is_active": False}, {"role": "user"}], ids=["disable-reviewer", "demote-reviewer"])
async def test_registration_review_waits_for_revocation_and_rechecks_reviewer(env, monkeypatch, change):
    admin = await env.seed("Revoking Admin", role=UserRole.ADMIN)
    reviewer = await env.seed("Reviewing Admin", role=UserRole.ADMIN)
    applicant = await env.seed("Pending Applicant", active=False, status=RegistrationStatus.PENDING)
    updating, release_update, review_waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_patch = env.repository.patch_user
    original_lock = env.repository.account_management_lock
    lock_attempts = 0

    async def gated_patch(user_id, changes, **kwargs):
        if user_id == reviewer.id:
            updating.set()
            await release_update.wait()
        return await original_patch(user_id, changes, **kwargs)

    @asynccontextmanager
    async def observed_real_lock():
        nonlocal lock_attempts
        lock_attempts += 1
        if lock_attempts == 2:
            review_waiting.set()
        async with original_lock():
            yield

    monkeypatch.setattr(env.repository, "patch_user", gated_patch)
    monkeypatch.setattr(env.repository, "account_management_lock", observed_real_lock)
    revocation = asyncio.create_task(env.client.patch(f"auth/user/{reviewer.id}",
        headers=headers(env, admin), json=change))
    review = None
    try:
        await asyncio.wait_for(updating.wait(), 3)
        # The reviewer is still active when the HTTP dependency authenticates;
        # its account changes while the handler waits for the real Redis lock.
        review = asyncio.create_task(env.client.post(f"auth/registrations/{applicant.id}/decision",
            headers=headers(env, reviewer), json={"status": "approved"}))
        await asyncio.wait_for(review_waiting.wait(), 3)
        assert not review.done()
        release_update.set()
        assert (await revocation).status_code == 200
        response = await review
        assert response.status_code == 403, response.text
    finally:
        release_update.set()
        await asyncio.gather(revocation, *([review] if review else []), return_exceptions=True)
    saved = await env.repository.get_user_by_id(applicant.id)
    assert saved.registration_status == RegistrationStatus.PENDING and saved.is_active is False
    assert saved.auth_version == 0 and saved.registration_reviewed_by is None
