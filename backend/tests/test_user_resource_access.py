"""Multi-user boundaries remain enforced below UI and storage adapters."""
from datetime import UTC, datetime
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.application.errors.exceptions import NotFoundError
from app.application.services.agent_service import AgentService
from app.application.services.file_preview import preview_version
from app.application.services.file_service import FileService
from app.domain.models.event import MessageEvent
from app.domain.models.file import FileInfo
from app.domain.models.session import Session
from app.domain.models.user import User
from app.interfaces.api import data_connector_routes, file_routes, session_routes
from app.interfaces.api.data_candidate_routes import owned_session
from app.interfaces.dependencies import get_agent_service, get_current_user, get_file_service, get_optional_current_user
from app.interfaces.errors.exception_handlers import register_exception_handlers


def file_info(owner="alice"):
    return FileInfo(file_id="private-file", filename="private.csv", user_id=owner,
                    size=4, content_type="text/csv", upload_date=datetime(2026, 10, 21, tzinfo=UTC))


def unchecked_storage(info):
    """An adapter regression must not turn the public service into an ID lookup."""
    return SimpleNamespace(
        get_file_info=AsyncMock(return_value=info),
        download_file=AsyncMock(return_value=(BytesIO(b"a,b\n"), info)),
        download_file_range=AsyncMock(return_value=(b"a,b\n", info)),
        delete_file=AsyncMock(return_value=True),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["bob", "administrator", ""])
async def test_foreign_file_metadata_cannot_grant_read_delete_or_signing(actor):
    info = file_info()
    storage = unchecked_storage(info)
    tokens = SimpleNamespace(create_signed_url=lambda **kwargs: "/signed")
    service = FileService(storage, tokens)
    assert await service.get_file_info(info.file_id, actor) is None
    assert await service.delete_file(info.file_id, actor) is False
    for action in (
        service.download_file(info.file_id, actor),
        service.preview_file(info.file_id, actor),
        service.create_signed_url(info.file_id, actor),
    ):
        with pytest.raises(FileNotFoundError):
            await action
    storage.download_file.assert_not_awaited()
    storage.download_file_range.assert_not_awaited()
    storage.delete_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_owned_download_and_explicit_signed_capability_still_work():
    info = file_info()
    service = FileService(unchecked_storage(info))
    assert await service.get_file_info(info.file_id, "alice") == info
    assert (await service.preview_file(info.file_id, "alice")).text == "a,b\n"
    stream, actual = await service.download_file(info.file_id, "alice")
    assert stream.read() == b"a,b\n" and actual.user_id == "alice"
    # None is the internal boundary used only after signature verification.
    assert await service.get_file_info(info.file_id, None) == info
    assert b"".join([chunk async for chunk in service.download_chunks(
        info.file_id, None, offset=0, length=4, version=preview_version(info))]) == b"a,b\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["download", "preview", "range"])
async def test_ownership_change_during_read_never_returns_foreign_bytes(operation):
    owned, foreign = file_info(), file_info("bob")
    storage = unchecked_storage(owned)
    stream = BytesIO(b"a,b\n")
    storage.download_file.return_value = stream, foreign
    storage.download_file_range.return_value = b"a,b\n", foreign
    service = FileService(storage)
    with pytest.raises(FileNotFoundError):
        if operation == "download":
            await service.download_file(owned.file_id, "alice")
        elif operation == "preview":
            await service.preview_file(owned.file_id, "alice")
        else:
            await anext(service.download_chunks(owned.file_id, "alice", offset=0,
                                               length=4, version=preview_version(owned)))
    if operation == "download":
        assert stream.closed


def sessions():
    session = Session(id="private-session", agent_id="agent", user_id="alice",
                      collaborator_user_ids=["collaborator"], files=[file_info()])
    async def find(key):
        return session if key == session.id else None
    async def accessible(key, actor):
        return await find(key) if actor in (session.user_id, *session.collaborator_user_ids) else None
    events = [MessageEvent(role="user", message="private research")]
    repository = SimpleNamespace(find_by_id=AsyncMock(side_effect=find),
                                 find_by_id_and_user_id=AsyncMock(side_effect=accessible),
                                 get_events=AsyncMock(return_value=events))
    service = AgentService.__new__(AgentService)
    service._session_repository = repository
    return service, session, repository


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", [None, "", "bob", "administrator"])
async def test_private_session_events_and_files_require_an_authorized_principal(actor):
    service, session, repository = sessions()
    assert await service.get_session(session.id, actor) is None
    with pytest.raises(NotFoundError):
        await service.get_session_events(session.id, actor)
    with pytest.raises(NotFoundError):
        await service.get_session_files(session.id, actor)
    repository.get_events.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_collaborator_and_revocable_public_session_reads():
    service, session, repository = sessions()
    for actor in ("alice", "collaborator"):
        assert await service.get_session(session.id, actor) is session
        assert await service.get_session_events(session.id, actor)
        assert await service.get_session_files(session.id, actor) == session.files
    session.is_shared = True
    assert await service.get_session(session.id) is session
    assert await service.get_session_events(session.id)
    assert await service.get_shared_session_files(session.id) == session.files
    session.is_shared = False
    with pytest.raises(NotFoundError):
        await service.get_session_events(session.id)
    with pytest.raises(NotFoundError):
        await service.get_shared_session_files(session.id)
    assert await service.is_session_shared("missing-session") is False


@pytest.mark.asyncio
async def test_connector_operations_require_owner_even_for_explicit_collaborators():
    service, session, _ = sessions()
    assert await owned_session(session.id, SimpleNamespace(id="alice"), service) == ("alice", session.id)
    session.is_shared = True
    for actor in ("bob", "administrator", "collaborator"):
        with pytest.raises(NotFoundError):
            await owned_session(session.id, SimpleNamespace(id=actor), service)


@pytest.mark.asyncio
@pytest.mark.parametrize("actor,expected_owner", [("alice", True), ("collaborator", False)])
async def test_session_responses_report_ownership_without_promoting_collaborators(actor, expected_owner):
    agent, session, _ = sessions()
    agent.get_session_history = AsyncMock(return_value=SimpleNamespace(events=[], has_more=False, next_before_seq=None))
    identity = User(id=actor, fullname="Test User", email=f"{actor}@example.test")
    response = await session_routes.get_session(session.id, identity, agent)
    assert response.data.is_owner is expected_owner
    history = await session_routes.get_session_history(session.id, 5, None, identity, agent)
    assert history.data.is_owner is expected_owner


@pytest.mark.asyncio
@pytest.mark.parametrize("actor,role", [("bob", "user"), ("administrator", "admin")])
async def test_http_resource_routes_do_not_grant_admin_an_implicit_data_bypass(actor, role):
    agent, session, repository = sessions()
    storage = unchecked_storage(file_info())
    files = FileService(storage)
    connectors = SimpleNamespace(call=AsyncMock())
    app = FastAPI()
    for router in (session_routes.router, file_routes.router, data_connector_routes.router):
        app.include_router(router)
    register_exception_handlers(app)
    identity = User(id=actor, fullname="Test User", email=f"{actor}@example.test", role=role)
    app.dependency_overrides[get_current_user] = lambda: identity
    app.dependency_overrides[get_optional_current_user] = lambda: identity
    app.dependency_overrides[get_agent_service] = lambda: agent
    app.dependency_overrides[get_file_service] = lambda: files
    app.dependency_overrides[data_connector_routes.get_data_connector_service] = lambda: connectors
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for path in (f"/sessions/{session.id}", f"/sessions/{session.id}/files",
                     f"/sessions/{session.id}/analysis-inputs", "/files/private-file/info",
                     "/files/private-file/download"):
            response = await client.get(path)
            assert response.status_code == 404, (path, response.text)
            assert "private research" not in response.text
        response = await client.post(f"/sessions/{session.id}/data-connectors/zenodo/call",
                                     json={"operation": "search", "arguments": {"query": "climate"}})
        assert response.status_code == 404
    connectors.call.assert_not_awaited()
    repository.get_events.assert_not_awaited()
    storage.download_file_range.assert_not_awaited()


@pytest.mark.asyncio
async def test_analysis_inputs_recheck_ownership_without_losing_valid_owned_selection():
    agent, session, repository = sessions()
    repository.get_events.return_value = [MessageEvent(role="user", message="analyze", attachments=[file_info()])]
    storage = unchecked_storage(file_info())
    files = FileService(storage, SimpleNamespace(create_signed_url=lambda **kwargs: "/api/v1/files/private-file?signature=test"))
    identity = User(id="alice", fullname="Test User", email="alice@example.test")
    result = await session_routes.get_analysis_inputs(session.id, identity, agent, files)
    assert result.data["selected_file_ids"] == ["private-file"]
    assert result.data["files"][0]["file_url"] == "/api/v1/files/private-file?signature=test"
    storage.get_file_info.return_value = file_info("bob")
    result = await session_routes.get_analysis_inputs(session.id, identity, agent, files)
    assert result.data == {"files": [], "selected_file_ids": []}
