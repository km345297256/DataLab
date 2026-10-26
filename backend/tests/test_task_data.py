"""Task-data discovery must never make a historical reference an access grant."""
from datetime import UTC, datetime, timedelta
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Header
from httpx import ASGITransport, AsyncClient

from app.application.errors.exceptions import NotFoundError
from app.application.services.task_data_service import TaskDataService
from app.domain.models.event import MessageEvent
from app.domain.models.file import FileInfo
from app.domain.models.session import Session
from app.domain.services.data_runs import data_run_id
from app.interfaces.api import session_routes
from app.interfaces.dependencies import get_current_user
from app.interfaces.errors.exception_handlers import register_exception_handlers

BASE = datetime(2026, 10, 22, 8, tzinfo=UTC)


class Files:
    def __init__(self):
        self.infos, self.bytes = {}, {}

    def add(self, key, *, owner="alice", name="table.csv", metadata=None, minute=0, data=b"x,y\n1,2\n"):
        info = FileInfo(file_id=key, filename=name, user_id=owner, size=len(data),
                        content_type="text/csv", upload_date=BASE + timedelta(minutes=minute),
                        metadata=metadata, file_path="/private/SECRET/output.csv",
                        file_url="https://user:SECRET@example.test/file?token=SECRET")
        self.infos[key], self.bytes[key] = info, data
        return info

    async def get_file_info(self, key, owner):
        info = self.infos.get(key)
        return info.model_copy(deep=True) if info and info.user_id == owner else None

    async def download_file_range(self, key, owner, *, offset, length):
        if key not in self.bytes:
            raise FileNotFoundError("/private/SECRET")
        return self.bytes[key][offset:offset + length], await self.get_file_info(key, owner)

    async def download_file(self, key, owner):
        if key not in self.bytes:
            raise FileNotFoundError("/private/SECRET")
        return BytesIO(self.bytes[key]), await self.get_file_info(key, owner)


@pytest.fixture
def env():
    files = Files()
    session = Session(id="task", user_id="alice", agent_id="agent", is_shared=True, collaborator_user_ids=["bob"])
    async def owned(key, owner):
        return session if key == "task" and owner == "alice" else None
    sessions = SimpleNamespace(find_owned_by_id_and_user_id=AsyncMock(side_effect=owned),
                               get_events=AsyncMock(return_value=[]))
    library = SimpleNamespace(get_context=AsyncMock(return_value=[]), file_locations=AsyncMock(return_value={}))
    provenance = SimpleNamespace(list_acquisitions=AsyncMock(return_value=[]))
    return SimpleNamespace(files=files, session=session, sessions=sessions, library=library, provenance=provenance,
                           service=TaskDataService(sessions, files, library, provenance))


def user(seq, minute, *files, selected=None):
    return MessageEvent(role="user", message="分析", seq=seq, timestamp=BASE + timedelta(minutes=minute),
                        attachments=list(files), metadata={"analysis_input_file_ids": selected} if selected is not None else {})


def assistant(seq, minute, *files, run_seq=None):
    return MessageEvent(role="assistant", message="结果", seq=seq, timestamp=BASE + timedelta(minutes=minute),
                        attachments=list(files), run_id=data_run_id("task", run_seq) if run_seq else None)


@pytest.mark.asyncio
async def test_current_task_origin_is_independent_of_selection_and_same_name_versions_survive(env):
    upload = env.files.add("input", minute=-60)
    old = env.files.add("old", name="chart.csv", minute=2, metadata={"source": "sandbox_artifact", "session_id": "task"})
    new = env.files.add("new", name="chart.csv", minute=12, metadata={"source": "sandbox_artifact", "session_id": "task"})
    download = env.files.add("download", minute=3, metadata={"source": "data_connector", "sha256": "a" * 64})
    imported = env.files.add("imported", minute=-90, metadata={"source": "sandbox_artifact", "session_id": "previous"})
    old_download = env.files.add("old-download", minute=-90, metadata={"source": "data_connector", "sha256": "b" * 64})
    env.session.files = [new, download]
    env.sessions.get_events.return_value = [user(1, 0, upload, imported, old_download),
        assistant(2, 2, old), user(3, 10, download, old, selected=["old", "download"]), assistant(4, 12, new)]
    env.provenance.list_acquisitions.return_value = [{"file_id": "download", "size": download.size, "sha256": "a" * 64}]
    response = await env.service.list("alice", "task")
    items = {item.file.file_id: item for item in response.items}
    assert {key: item.category for key, item in items.items()} == {
        "input": "upload", "old": "analysis", "new": "analysis", "download": "download",
        "imported": "upload", "old-download": "upload"}
    assert items["old"].round_number == 1 and items["new"].round_number == 2
    assert items["download"].round_number == 1
    assert items["input"].created_at == BASE  # brought into this task, not original upload time
    assert response.selected_file_ids == ["old", "download"]
    assert response.unavailable_count == 0


@pytest.mark.asyncio
async def test_alias_dedupe_saved_locations_and_unsent_files_are_read_only(env):
    info = env.files.add("input", metadata={"token": "SECRET", "file_path": "/private/SECRET"})
    alias = info.model_copy(update={"file_id": "gridfs:input"})
    env.session.files = [info, alias]
    env.sessions.get_events.return_value = [user(1, 0, info, alias, selected=["gridfs:input", "input"])]
    draft = env.files.add("draft", name="/private/SECRET/draft.csv")
    location = {"asset_id": "a" * 32, "asset_name": "数据", "folder_id": None, "folder_path": "", "file_path": "draft.csv"}
    env.library.file_locations.return_value = {"draft": [location]}
    response = await env.service.list("alice", "task", file_ids=["draft", "gridfs:draft", "forbidden"])
    assert len(response.items) == 2 and response.unavailable_count == 1
    assert response.selected_file_ids == ["input"]
    assert next(item for item in response.items if item.file.file_id == "draft").saved_locations[0].file_path == "draft.csv"
    assert {info.file_id for info in env.session.files} == {"input", "gridfs:input"}
    payload = response.model_dump_json()
    assert all(value not in payload for value in ("SECRET", "private", "token", "storage_key", "https:"))
    assert '"filename":"draft.csv"' in payload


@pytest.mark.asyncio
async def test_missing_bytes_foreign_files_spills_and_broken_receipts_cannot_escape(env):
    foreign = env.files.add("foreign", owner="bob", name="secret.csv")
    missing = env.files.add("missing")
    del env.files.bytes["missing"]
    truncated = env.files.add("truncated")
    env.files.bytes["truncated"] = b"x"
    spill = env.files.add("spill", metadata={"source": "tool_output_spill"})
    untrusted = env.files.add("untrusted", metadata={"user_metadata": {"source": "sandbox_artifact", "session_id": "task"}})
    env.session.files = [foreign, missing, truncated, spill, untrusted]
    env.provenance.list_acquisitions.return_value = [{"file_id": "untrusted", "size": untrusted.size, "sha256": "a" * 64}]
    env.sessions.get_events.return_value = [user(1, 0, foreign, missing, spill)]
    response = await env.service.list("alice", "task")
    assert response.unavailable_count == 4 and response.selected_file_ids == []
    assert [item.file.file_id for item in response.items] == ["untrusted"]
    assert response.items[0].category == "upload"
    assert "secret.csv" not in response.model_dump_json()


@pytest.mark.asyncio
async def test_empty_file_existence_and_download_not_yet_in_session_are_supported(env):
    empty = env.files.add("empty", data=b"")
    downloaded = env.files.add("downloaded", metadata={"sha256": "d" * 64})
    env.session.files = [empty]
    env.provenance.list_acquisitions.return_value = [{"file_id": "downloaded", "size": downloaded.size, "sha256": "d" * 64}]
    response = await env.service.list("alice", "task")
    assert {item.file.file_id for item in response.items} == {"empty", "downloaded"}
    assert all(item.available for item in response.items)
    del env.files.bytes["empty"]
    assert (await env.service.list("alice", "task")).unavailable_count == 1


@pytest.mark.asyncio
async def test_delayed_output_uses_its_original_run_round(env):
    old = env.files.add("old", minute=20, metadata={"source": "sandbox_artifact", "session_id": "task"})
    env.sessions.get_events.return_value = [user(1, 0), user(2, 10), assistant(3, 20, old, run_seq=1)]
    assert (await env.service.list("alice", "task")).items[0].round_number == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("owner,task", [("bob", "task"), ("alice", "unknown")])
async def test_private_view_excludes_shared_sessions_and_collaborators(env, owner, task):
    with pytest.raises(NotFoundError):
        await env.service.list(owner, task, file_ids=["foreign"])
    env.sessions.get_events.assert_not_awaited()
    env.provenance.list_acquisitions.assert_not_awaited()
    env.library.file_locations.assert_not_awaited()


@pytest.mark.asyncio
async def test_route_contract_owner_scope_and_draft_query_limits(env):
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(session_routes.router)
    async def current(x_user: str = Header()):
        return SimpleNamespace(id=x_user)
    app.dependency_overrides[get_current_user] = current
    app.dependency_overrides[session_routes.get_task_data_service] = lambda: env.service
    env.files.add("draft")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = await client.get("/sessions/task/task-data", params=[("file_ids", "draft")], headers={"x-user": "alice"})
        assert result.status_code == 200
        assert result.json()["data"]["items"][0]["category"] == "upload"
        assert (await client.get("/sessions/task/task-data", headers={"x-user": "bob"})).status_code == 404
        assert (await client.get("/sessions/task/task-data")).status_code == 422
        for params in [[("file_ids", "/private/SECRET")], [("file_ids", "draft")] * 33]:
            bad = await client.get("/sessions/task/task-data", params=params, headers={"x-user": "alice"})
            assert bad.status_code == 422 and "SECRET" not in bad.text
