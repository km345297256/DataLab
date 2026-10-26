"""Account lifecycle and authorization regression tests without production I/O."""
import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import jwt
import pytest
from fastapi import FastAPI
from pymongo.errors import DuplicateKeyError

from app.application.errors.exceptions import BadRequestError, ConflictError, ForbiddenError, NotFoundError, UnauthorizedError, ValidationError
from app.application.services.auth_service import AuthService
from app.application.services.auth_rate_limit import AuthRateLimiter, get_auth_rate_limiter
from app.application.services.token_service import TokenService
from app.core.config import Settings
from app.domain.models.session import SessionStatus
from app.domain.models.user import RegistrationStatus, User, UserRole
from app.domain.models.workspace import WorkspaceRole
from app.domain.repositories.user_repository import UserRepository
from app.domain.services.permission_service import PermissionService
from app.infrastructure.repositories.user_repository import MongoUserRepository
from app.interfaces.api import admin_routes, auth_routes
from app.interfaces.dependencies import get_auth_service, get_current_user
from app.interfaces.errors.exception_handlers import register_exception_handlers


class Users:
    def __init__(self):
        self.users = {}
        self.lock = asyncio.Lock()

    async def get_user_by_id(self, user_id):
        user = self.users.get(user_id)
        return user.model_copy(deep=True) if user else None

    async def get_user_by_email(self, email):
        return next((user.model_copy(deep=True) for user in self.users.values() if user.email == email.strip().lower()), None)

    async def email_exists(self, email):
        return await self.get_user_by_email(email) is not None

    async def create_user(self, user):
        if await self.email_exists(user.email):
            raise DuplicateKeyError("synthetic duplicate")
        return await self.update_user(user)

    async def update_user(self, user):
        self.users[user.id] = user.model_copy(deep=True)
        return user.model_copy(deep=True)

    async def list_users(self, limit=100, offset=0):
        return [user.model_copy(deep=True) for user in list(self.users.values())[offset:offset + limit]]

    async def patch_user(self, user_id, changes, *, expected_auth_version=None, revoke_sessions=False):
        # No await between expected-version check and the in-memory update.
        user = self.users[user_id].model_copy(deep=True)
        if expected_auth_version is not None and user.auth_version != expected_auth_version:
            raise ConflictError("Account changed")
        for key, value in changes.items():
            setattr(user, key, value)
        if revoke_sessions:
            user.auth_version += 1
        self.users[user_id] = user
        return user.model_copy(deep=True)

    @asynccontextmanager
    async def account_management_lock(self):
        async with self.lock:
            yield


@pytest.fixture
def auth(monkeypatch):
    settings = Settings(_env_file=None, auth_provider="password", jwt_secret_key="test-account-secret" * 4)
    tokens = TokenService()
    tokens.settings = settings
    service = AuthService(Users(), tokens)
    service.settings = settings
    service._is_revoked = AsyncMock(return_value=False)
    password = service._hash_password("research-password")
    for key in ("alice", "bob", "admin", "second-admin"):
        service.user_repository.users[key] = User(
            id=key, fullname=key.title(), email=f"{key}@example.com", password_hash=password,
            role=UserRole.ADMIN if "admin" in key else UserRole.USER,
        )
    monkeypatch.setattr("app.application.services.auth_service.UserDocument", SimpleNamespace(find_one=AsyncMock(return_value=None)))
    return service


@pytest.fixture
def app(auth):
    application = FastAPI()
    application.include_router(auth_routes.router, prefix="/api/v1")
    register_exception_handlers(application)
    application.dependency_overrides[get_auth_service] = lambda: auth
    application.dependency_overrides[get_auth_rate_limiter] = lambda: SimpleNamespace(check=AsyncMock(), login_succeeded=AsyncMock())
    return application


def headers(auth, owner):
    return {"Authorization": "Bearer " + auth.token_service.create_access_token(auth.user_repository.users[owner])}


@pytest.mark.asyncio
async def test_user_management_is_admin_only_and_uses_server_side_filters(app, auth):
    pending = User(id="pending", fullname="Pending researcher", email="pending@example.com", is_active=False, registration_status="pending")
    auth.user_repository.users[pending.id] = pending
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/v1/auth/users")).status_code == 401
        assert (await client.get("/api/v1/auth/users", headers=headers(auth, "alice"))).status_code == 401
        denied = await client.patch("/api/v1/auth/user/alice", json={"role": "admin"}, headers=headers(auth, "alice"))
        assert denied.status_code == 403
        response = await client.get("/api/v1/auth/users", params={"role": "user", "status": "active", "query": "example", "limit": 1}, headers=headers(auth, "admin"))
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["total"] == 2 and len(data["users"]) == 1
        assert "password_hash" not in response.text and "auth_version" not in response.text
        for change in ({"role": "user"}, {"is_active": False}):
            assert (await client.patch("/api/v1/auth/user/admin", json=change, headers=headers(auth, "admin"))).status_code == 400
        for change in ({"is_active": True}, {"role": "admin"}):
            assert (await client.patch("/api/v1/auth/user/pending", json=change, headers=headers(auth, "admin"))).status_code == 400
        assert (await client.patch("/api/v1/auth/user/alice", json={}, headers=headers(auth, "admin"))).status_code == 422


@pytest.mark.asyncio
async def test_disable_reenable_and_role_changes_do_not_resurrect_credentials(auth):
    original = await auth.user_repository.get_user_by_id("alice")
    access = auth.token_service.create_access_token(original)
    refresh = auth.token_service.create_refresh_token(original)
    await auth.deactivate_user("alice", "admin")
    await auth.activate_user("alice", "admin")
    assert await auth.verify_token(access) is None
    with pytest.raises(UnauthorizedError):
        await auth.refresh_access_token(refresh)
    login = await auth.login_with_tokens("alice@example.com", "research-password")
    assert await auth.verify_token(login.access_token)
    await auth.update_account("alice", "admin", role=UserRole.SOFTWARE)
    assert await auth.verify_token(login.access_token) is None
    with pytest.raises(UnauthorizedError):
        await auth.refresh_access_token(login.refresh_token)
    assert (await auth.login_with_tokens("alice@example.com", "research-password")).user.role == UserRole.SOFTWARE


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["disable", "demote"])
async def test_concurrent_admin_changes_leave_an_active_administrator(auth, operation):
    changes = {"is_active": False} if operation == "disable" else {"role": UserRole.USER}
    outcomes = await asyncio.gather(
        auth.update_account("admin", "second-admin", **changes),
        auth.update_account("second-admin", "admin", **changes),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ForbiddenError) for result in outcomes) == 1
    assert len([user for user in auth.user_repository.users.values() if user.is_active and user.role == UserRole.ADMIN]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("reset", [False, True])
async def test_password_change_and_reset_revoke_access_and_refresh(auth, reset):
    login = await auth.login_with_tokens("alice@example.com", "research-password")
    if reset:
        await auth.reset_password("alice@example.com", "new-research-password")
    else:
        await auth.change_password("alice", "research-password", "new-research-password")
    assert await auth.verify_token(login.access_token) is None
    with pytest.raises(UnauthorizedError):
        await auth.refresh_access_token(login.refresh_token)
    assert (await auth.login_with_tokens("alice@example.com", "new-research-password")).user.id == "alice"


@pytest.mark.asyncio
async def test_legacy_tokens_work_only_until_the_first_security_change(auth):
    token = auth.token_service.create_access_token(auth.user_repository.users["alice"])
    payload = auth.token_service.verify_token(token)
    payload.pop("auth_version")
    legacy = jwt.encode(payload, auth.settings.jwt_secret_key, algorithm=auth.settings.jwt_algorithm)
    assert (await auth.verify_token(legacy)).id == "alice"
    await auth.update_account("alice", "admin", role=UserRole.SOFTWARE)
    assert await auth.verify_token(legacy) is None


@pytest.mark.asyncio
async def test_registration_cannot_elevate_roles_or_hide_duplicate_races(auth):
    with pytest.raises(ForbiddenError):
        await auth.register_user("Researcher", "research-password", "new@example.com", role=UserRole.ADMIN)
    auth.user_repository.create_user = AsyncMock(side_effect=DuplicateKeyError("duplicate email"))
    with pytest.raises(ValidationError, match="Email already exists"):
        await auth.register_user("Researcher", "research-password", " new@example.com ")


@pytest.mark.asyncio
async def test_registration_review_rechecks_actor_and_rejects_stale_decisions(auth):
    user = await auth.register_user("Researcher", "research-password", "new@example.com")
    with pytest.raises(ForbiddenError):
        await auth.decide_registration(user.id, RegistrationStatus.APPROVED, "alice")
    outcomes = await asyncio.gather(
        auth.decide_registration(user.id, RegistrationStatus.APPROVED, "admin"),
        auth.decide_registration(user.id, RegistrationStatus.REJECTED, "second-admin"),
        return_exceptions=True,
    )
    assert len([result for result in outcomes if isinstance(result, User)]) == 1
    assert isinstance(next(result for result in outcomes if not isinstance(result, User)), (BadRequestError, ConflictError))


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"is_active": False}, {"role": UserRole.USER}])
async def test_registration_review_and_reviewer_revocation_are_serialized(auth, changes):
    pending = await auth.register_user("Researcher", "research-password", "new@example.com")
    target_read_started = asyncio.Event()
    resume_review = asyncio.Event()
    get_user = auth.user_repository.get_user_by_id

    async def pause_target_read(user_id):
        if user_id == pending.id and not target_read_started.is_set():
            target_read_started.set()
            await resume_review.wait()
        return await get_user(user_id)

    auth.user_repository.get_user_by_id = pause_target_read
    review = asyncio.create_task(auth.decide_registration(pending.id, RegistrationStatus.APPROVED, "admin"))
    await target_read_started.wait()
    revoke = asyncio.create_task(auth.update_account("admin", "second-admin", **changes))
    try:
        # Let the competing mutation reach the same management lock. It must
        # wait for approval instead of revoking a reviewer midway through it.
        await asyncio.sleep(0)
        assert not revoke.done()
    finally:
        resume_review.set()
        await asyncio.gather(review, revoke)
    assert (await get_user(pending.id)).registration_status == RegistrationStatus.APPROVED
    with pytest.raises(ForbiddenError):
        await auth.decide_registration(pending.id, RegistrationStatus.REJECTED, "admin")


@pytest.mark.asyncio
async def test_preference_updates_never_save_stale_passwords_or_roles():
    repository = MongoUserRepository()
    repository.patch_user = AsyncMock()
    user = User(id="alice", fullname="Alice", email="alice@example.com", password_hash="stale", role="admin", auth_version=2, installed_skill_ids=["mine"])
    await repository.update_user(user)
    call = repository.patch_user.await_args
    assert call.args[0] == "alice"
    assert set(call.args[1]) == {"auto_enabled_skills", "installed_skill_ids", "installed_mcp_names", "installed_renderer_ids"}
    assert call.kwargs == {"expected_auth_version": 2}


@pytest.mark.asyncio
async def test_mongo_security_patch_uses_atomic_version_predicate_and_increment(monkeypatch):
    from app.infrastructure.repositories import user_repository
    writes = []
    current = User(id="alice", fullname="Alice", email="alice@example.com", auth_version=1, is_active=False)
    async def update(query, changes, **options):
        writes.append((query, changes))
        return current.model_dump()
    document = SimpleNamespace(get_pymongo_collection=lambda: SimpleNamespace(find_one_and_update=update),
                               model_validate=lambda raw: SimpleNamespace(to_domain=lambda: User.model_validate(raw)))
    monkeypatch.setattr(user_repository, "UserDocument", document)
    result = await MongoUserRepository().patch_user("alice", {"is_active": False}, expected_auth_version=0, revoke_sessions=True)
    assert result.auth_version == 1
    query, update_values = writes[0]
    assert query == {"user_id": "alice", "$or": [{"auth_version": 0}, {"auth_version": {"$exists": False}}]}
    assert update_values["$inc"] == {"auth_version": 1}
    assert set(update_values["$set"]) == {"is_active", "updated_at"}


@pytest.mark.asyncio
async def test_permission_policy_respects_account_action_resource_and_workspace(monkeypatch, auth):
    from app.domain.services import permission_service
    admin = await auth.user_repository.get_user_by_id("admin")
    alice = await auth.user_repository.get_user_by_id("alice")
    service = PermissionService()
    private = SimpleNamespace(scope="private", owner_user_id="alice")
    assert await service.can(alice, "write", private)
    assert not await service.can(admin, "read", private)
    assert not await service.can(alice, "invented-action", private)
    assert not await service.can(admin, "invented-action", private)
    assert await service.can(admin, "delete", SimpleNamespace(scope="global"))
    assert await service.can(alice, "read", SimpleNamespace(scope="global"))
    assert not await service.can(alice, "delete", SimpleNamespace(scope="global"))
    for values in ({"is_active": False}, {"registration_status": RegistrationStatus.PENDING}):
        assert not await service.can(admin.model_copy(update=values), "read", SimpleNamespace(scope="global"))
    # Query expressions are represented by dict equality in this isolated test.
    class Field:
        def __eq__(self, other): return other
    monkeypatch.setattr(permission_service, "WorkspaceMemberDocument", SimpleNamespace(
        workspace_id=Field(), user_id=Field(), find_one=AsyncMock(return_value=SimpleNamespace(role=WorkspaceRole.DEVELOPER))))
    resource = SimpleNamespace(scope="workspace", workspace_id="lab")
    assert await service.can(alice, "write", resource)
    assert not await service.can(alice, "delete", resource)
    assert not await service.can(alice, "read", SimpleNamespace(scope="unknown"), workspace_id="lab")


def task(owner="alice", shared=False, collaborators=None):
    now = datetime.now(UTC)
    return SimpleNamespace(session_id="session-1", user_id=owner, agent_id="agent", task_id=None, sandbox_id=None,
                           title="Confidential trial", latest_message="Unpublished data", latest_message_at=now,
                           status=SessionStatus.COMPLETED, unread_message_count=0, is_shared=shared,
                           collaborator_user_ids=collaborators or [], created_at=now, updated_at=now)


@pytest.mark.asyncio
async def test_admin_task_overview_never_leaks_or_searches_private_content(monkeypatch, auth):
    session = task()
    document = SimpleNamespace(find=lambda: SimpleNamespace(sort=lambda value: SimpleNamespace(to_list=AsyncMock(return_value=[session]))))
    monkeypatch.setattr(admin_routes, "SessionDocument", document)
    monkeypatch.setattr(admin_routes, "_user_names", AsyncMock(return_value={"alice": "Alice"}))
    arguments = dict(user_id=None, status=None, limit=50, offset=0, current_user=auth.user_repository.users["admin"])
    result = await admin_routes.list_tasks(query=None, **arguments)
    assert result.data.total == 1
    assert result.data.tasks[0].title is None and result.data.tasks[0].latest_message is None
    assert not result.data.tasks[0].can_replay
    assert (await admin_routes.list_tasks(query="Confidential", **arguments)).data.total == 0
    session.is_shared = True
    assert (await admin_routes.list_tasks(query="Confidential", **arguments)).data.tasks[0].title == "Confidential trial"
    assert (await admin_routes.list_tasks(query=None, **arguments)).data.tasks[0].can_replay


@pytest.mark.asyncio
@pytest.mark.parametrize("access", ["private", "owner", "collaborator", "shared"])
async def test_admin_replay_uses_resource_authorization(monkeypatch, auth, access):
    session = task(owner="admin" if access == "owner" else "alice", shared=access == "shared", collaborators=["admin"] if access == "collaborator" else [])
    monkeypatch.setattr(admin_routes, "SessionDocument", SimpleNamespace(session_id="field", find_one=AsyncMock(return_value=session)))
    monkeypatch.setattr(admin_routes.EventMapper, "events_to_sse_events", AsyncMock(return_value=[]))
    service = SimpleNamespace(get_session_events=AsyncMock(return_value=[]))
    admin = auth.user_repository.users["admin"]
    if access == "private":
        with pytest.raises(NotFoundError):
            await admin_routes.get_task_replay(session.session_id, admin, service)
        service.get_session_events.assert_not_awaited()
    else:
        await admin_routes.get_task_replay(session.session_id, admin, service)
        service.get_session_events.assert_awaited_once_with(session.session_id, user_id=None if access == "shared" else "admin")
