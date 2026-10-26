"""Saved data must survive chats and remain private under reuse and deletion."""
import asyncio
import copy
import hashlib
from io import BytesIO
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Header
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.application.errors.exceptions import BadRequestError, NotFoundError
from app.application.services.data_library_service import DataLibraryService
from app.application.services.file_service import FileService
from app.domain.models.data_library import CreateLibraryAsset, SelectedDataInput, UpdateLibraryAsset, canonical_file_id
from app.domain.models.file import FileInfo
from app.infrastructure.external.file.retainedfile import RetainedFileError, RetainedFileStorage
from app.interfaces.api import data_library_routes
from app.interfaces.dependencies import get_current_user
from app.interfaces.errors.exception_handlers import register_exception_handlers


class Repository:
    def __init__(self):
        self.rows, self.claims, self.receipts, self.contexts = {}, {}, {}, {}

    async def insert(self, owner, payload):
        self.rows[(owner, payload["id"])] = copy.deepcopy(payload)
        return payload

    async def get(self, owner, key):
        return copy.deepcopy(self.rows.get((owner, key)))

    async def page(self, owner, *, cursor=None, query="", source_type=None):
        return [copy.deepcopy(row) for (user, key), row in sorted(self.rows.items(), reverse=True)
                if user == owner and (cursor is None or key < cursor)
                and (not source_type or row["source_type"] == source_type)
                and (not query or query.casefold() in (row["name"] + row["description"] + " ".join(row["tags"])).casefold())][:21]

    async def update(self, owner, key, changes):
        row = self.rows.get((owner, key))
        if row is not None:
            row.update(changes)
        return copy.deepcopy(row)

    async def delete(self, owner, key):
        return self.rows.pop((owner, key), None)

    def claim(self, owner, file_id):
        return self.claims.setdefault((owner, canonical_file_id(file_id)), {"assets": set(), "deleting": False})

    async def retain_file(self, owner, file_id, asset_id):
        claim = self.claim(owner, file_id)
        if claim["deleting"]:
            return False
        claim["assets"].add(asset_id)
        return True

    async def release_file(self, owner, file_id, asset_id):
        self.claim(owner, file_id)["assets"].discard(asset_id)

    async def begin_file_delete(self, owner, file_id):
        claim = self.claim(owner, file_id)
        if claim["assets"] or claim["deleting"]:
            return False
        claim["deleting"] = True
        return True

    async def finish_file_delete(self, owner, file_id, *, deleted):
        if not deleted:
            self.claim(owner, file_id)["deleting"] = False

    async def acquisitions(self, owner, file_ids):
        return {key: copy.deepcopy(self.receipts[(owner, key)]) for key in file_ids if (owner, key) in self.receipts}

    async def get_context(self, owner, session):
        return self.contexts.get((owner, session), [])

    async def set_context(self, owner, session, file_ids):
        self.contexts[(owner, session)] = list(file_ids)


class Files:
    def __init__(self):
        self.infos, self.contents, self.reads = {}, {}, []
        self.deletions = []

    def add(self, file_id="file-1", *, owner="alice", data=b"a,b\n1,2\n", metadata=None):
        self.infos[file_id] = FileInfo(
            file_id=file_id, filename="/private/source/table.csv", file_path="/srv/private/table.csv",
            user_id=owner, size=len(data), content_type="text/csv", upload_date=datetime.now(UTC),
            metadata=metadata or {"token": "SECRET", "storage_key": "private/key"},
            file_url="https://user:SECRET@host.test/data?token=SECRET",
        )
        self.contents[file_id] = data
        return self.infos[file_id]

    async def get_file_info(self, file_id, owner):
        info = self.infos.get(canonical_file_id(file_id))
        return info.model_copy(deep=True) if info and info.user_id == owner else None

    async def download_file_range(self, file_id, owner, *, offset, length):
        self.reads.append((file_id, owner, offset, length))
        info = await self.get_file_info(file_id, owner)
        if info is None or file_id not in self.contents:
            raise FileNotFoundError("/private/SECRET")
        return self.contents[file_id][offset:offset + length], info

    async def download_file(self, file_id, owner):
        info = await self.get_file_info(file_id, owner)
        if info is None or file_id not in self.contents:
            raise FileNotFoundError("/private/SECRET")
        return BytesIO(self.contents[file_id]), info

    async def delete_file(self, file_id, owner):
        info = await self.get_file_info(file_id, owner)
        if not info:
            return False
        self.deletions.append(file_id)
        self.infos.pop(file_id)
        self.contents.pop(file_id, None)
        return True


@pytest.fixture
def env():
    repo, files = Repository(), Files()
    info = files.add()
    sessions = {("alice", "first"): SimpleNamespace(files=[info]), ("alice", "second"): SimpleNamespace(files=[])}
    async def find(session_id, owner):
        return sessions.get((owner, session_id))
    session_repo = SimpleNamespace(find_owned_by_id_and_user_id=AsyncMock(side_effect=find),
                                   get_events=AsyncMock(return_value=[]))
    retained = RetainedFileStorage(files, repo)
    return SimpleNamespace(repo=repo, files=files, sessions=sessions, session_repo=session_repo, retained=retained,
                           service=DataLibraryService(repo, retained, session_repo))


def request(**kwargs):
    return CreateLibraryAsset(name=kwargs.pop("name", "研究数据"), file_ids=kwargs.pop("file_ids", ["file-1"]), **kwargs)


@pytest.mark.asyncio
async def test_saved_upload_survives_origin_chat_and_service_restart_and_reuses_in_new_chat(env):
    saved = await env.service.create("alice", request(session_id="first"))
    del env.sessions[("alice", "first")]
    restarted = DataLibraryService(env.repo, env.retained, env.session_repo)
    listed = await restarted.list("alice")
    assert listed.items == [saved] and listed.items[0].status == "ready"
    context = await restarted.set_context("alice", "second", SelectedDataInput(file_ids=["file-1"]))
    assert context.files[0].file_id == "file-1"
    assert (await restarted.get_context("alice", "second")).files == context.files
    assert await env.retained.get_file_info("file-1", "alice") is not None
    assert not env.files.deletions


@pytest.mark.asyncio
async def test_library_never_exposes_paths_storage_metadata_or_signed_credentials(env):
    saved = await env.service.create("alice", request())
    assert saved.files[0].filename == "table.csv"
    serialized = saved.model_dump_json()
    for secret in ("/private", "/srv", "SECRET", "storage_key", "file_path", "file_url", "user_id"):
        assert secret not in serialized


@pytest.mark.asyncio
async def test_explicit_logical_directories_survive_save_reload_and_legacy_records(env):
    env.files.infos["file-1"].metadata["relative_path"] = "/private/do-not-infer/table.csv"
    saved = await env.service.create("alice", request(
        file_ids=["gridfs:file-1"], file_paths={"gridfs:file-1": "实验一/原始数据/table.csv"},
    ))
    assert saved.files[0].relative_path == "实验一/原始数据/table.csv"
    restarted = DataLibraryService(env.repo, env.retained, env.session_repo)
    assert (await restarted.get("alice", saved.id)).files == saved.files
    assert (await restarted.list("alice")).items[0].files == saved.files
    assert "/private" not in saved.model_dump_json()

    # Context is an owned-file selection, not a directory/mount request.
    context = await restarted.set_context("alice", "second", SelectedDataInput(file_ids=["file-1"]))
    assert context.files[0].relative_path is None
    assert context.files[0].filename == "table.csv"
    plain = await restarted.create("alice", request())
    assert plain.files[0].relative_path is None
    del env.repo.rows[("alice", plain.id)]["files"][0]["relative_path"]
    assert (await restarted.get("alice", plain.id)).files[0].relative_path is None


@pytest.mark.asyncio
async def test_logical_path_cannot_rename_files_and_failed_save_releases_claims(env):
    with pytest.raises(BadRequestError, match="原文件名"):
        await env.service.create("alice", request(file_paths={"file-1": "experiment/renamed.csv"}))
    assert not env.repo.rows
    assert not env.repo.claim("alice", "file-1")["assets"]


@pytest.mark.parametrize("path", [
    "", "/private/table.csv", "//host/share/table.csv", "../table.csv", "data/../table.csv",
    "./table.csv", "data/./table.csv", "data//table.csv", "data/table.csv/",
    r"C:\data\table.csv", "C:/data/table.csv", "C:table.csv", r"\\host\share\table.csv",
    "data/\x00table.csv", "data/\ntable.csv", "data/\x7ftable.csv", "data/\x85table.csv",
    "data/ table.csv", " data/table.csv", "data/table.csv ",
    "/".join(["folder"] * 16 + ["table.csv"]), "a" * 256 + "/table.csv",
    "/".join(["a" * 255] * 4 + ["table.csv"]), "https://host/table.csv",
])
def test_save_rejects_unsafe_or_ambiguous_logical_paths(path):
    with pytest.raises(ValidationError):
        request(file_paths={"file-1": path})


@pytest.mark.parametrize("payload", [
    {"file_paths": {"file-2": "table.csv"}},
    {"file_paths": {"file-1": "one/table.csv", "gridfs:file-1": "two/table.csv"}},
    {"file_ids": ["file-1", "file-2"], "file_paths": {"file-1": "data/table.csv", "file-2": "data/table.csv"}},
    {"file_ids": ["file-1", "file-2"], "file_paths": {"file-1": "data/table.csv", "file-2": "data/table.csv/child.csv"}},
    {"file_paths": {"file-1": None}},
])
def test_directory_mapping_requires_selected_unique_file_ids_and_unique_paths(payload):
    with pytest.raises(ValidationError):
        request(**payload)


@pytest.mark.asyncio
async def test_two_users_cannot_read_modify_delete_or_select_each_others_assets(env):
    saved = await env.service.create("alice", request())
    assert (await env.service.list("bob")).items == []
    for operation in [
        env.service.get("bob", saved.id),
        env.service.update("bob", saved.id, UpdateLibraryAsset(name="stolen")),
        env.service.delete("bob", saved.id),
        env.service.create("bob", request()),
        env.service.get_context("bob", "first"),
        env.service.set_context("bob", "first", SelectedDataInput(file_ids=["file-1"])),
        env.service.create("bob", request(session_id="first")),
    ]:
        with pytest.raises(NotFoundError):
            await operation
    assert (await env.service.get("alice", saved.id)).name == "研究数据"
    assert env.repo.claim("bob", "file-1")["assets"] == set()


@pytest.mark.asyncio
async def test_removing_one_of_two_library_entries_retains_file_until_last_reference_removed(env):
    first = await env.service.create("alice", request())
    second = await env.service.create("alice", request(name="第二份记录"))
    file_api = FileService(file_storage=env.retained)
    with pytest.raises(RetainedFileError):
        await file_api.delete_file("file-1", "alice")
    await env.service.delete("alice", first.id)
    with pytest.raises(RetainedFileError):
        await env.retained.delete_file("file-1", "alice")
    await env.service.delete("alice", second.id)
    assert not env.files.deletions  # Removing entries never removes bytes.
    assert await file_api.delete_file("file-1", "alice") is True


@pytest.mark.asyncio
async def test_artifact_replacement_cleanup_retains_saved_old_version_without_failing_task(env):
    from app.domain.services.agent_task_runner import AgentTaskRunner
    await env.service.create("alice", request())
    runner = object.__new__(AgentTaskRunner)
    runner._file_storage, runner._user_id = env.retained, "alice"
    runner._session_id, runner._agent_id = "first", "agent"
    old = env.files.infos["file-1"].model_copy(update={"metadata": {"source": "sandbox_artifact", "session_id": "first"}})
    await runner._delete_replaced_storage_file(old)
    assert env.files.deletions == []
    assert (await env.service.list("alice")).items[0].status == "ready"


@pytest.mark.asyncio
async def test_save_reservation_prevents_concurrent_artifact_cleanup_before_commit(env):
    reading, resume = asyncio.Event(), asyncio.Event()
    original_read = env.files.download_file_range
    async def delayed_read(*args, **kwargs):
        reading.set()
        await resume.wait()
        return await original_read(*args, **kwargs)
    env.files.download_file_range = delayed_read
    pending = asyncio.create_task(env.service.create("alice", request()))
    await reading.wait()
    with pytest.raises(RetainedFileError):
        await env.retained.delete_file("file-1", "alice")
    resume.set()
    saved = await pending
    assert saved.status == "ready" and not env.files.deletions


@pytest.mark.asyncio
async def test_delete_claim_prevents_concurrent_save_and_gridfs_alias_bypass(env):
    assert await env.repo.begin_file_delete("alice", "gridfs:file-1")
    with pytest.raises(NotFoundError):
        await env.service.create("alice", request())
    assert (await env.service.list("alice")).items == []
    await env.repo.finish_file_delete("alice", "file-1", deleted=False)
    saved = await env.service.create("alice", request(file_ids=["gridfs:file-1"]))
    assert saved.files[0].file_id == "file-1"
    with pytest.raises(RetainedFileError):
        await env.retained.delete_file("gridfs:file-1", "alice")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing_blob", "truncated", "spill", "missing_size", "foreign"])
async def test_save_requires_owned_completed_real_files_and_rolls_back_reservations(env, failure):
    if failure == "missing_blob": env.files.contents.clear()
    if failure == "truncated": env.files.contents["file-1"] = b"a"
    if failure == "spill": env.files.infos["file-1"].metadata = {"source": "tool_output_spill"}
    if failure == "missing_size": env.files.infos["file-1"].size = None
    if failure == "foreign": env.files.infos["file-1"].user_id = "bob"
    with pytest.raises(NotFoundError) as caught:
        await env.service.create("alice", request())
    assert "private" not in str(caught.value)
    assert not env.repo.rows and not env.repo.claim("alice", "file-1")["assets"]


@pytest.mark.asyncio
async def test_empty_and_large_files_use_bounded_owned_reads(env):
    env.files.add("empty", data=b"")
    env.files.add("large", data=b"x" * 10000)
    saved = await env.service.create("alice", request(file_ids=["empty", "large"]))
    assert [file.size for file in saved.files] == [0, 10000]
    assert env.files.reads == [("large", "alice", 5904, 4096)]


@pytest.mark.asyncio
async def test_caller_cannot_forge_open_data_verification_with_upload_metadata(env):
    env.files.infos["file-1"].metadata = {"source": "data_connector", "source_verified": True,
                                            "sha256": "a" * 64, "source_url": "https://ncbi.nlm.nih.gov/data"}
    ordinary = await env.service.create("alice", request())
    assert ordinary.source_type == "upload" and ordinary.source_verified is False and ordinary.source_url is None
    with pytest.raises(BadRequestError):
        await env.service.create("alice", request(source_type="open_data"))


@pytest.mark.asyncio
async def test_verified_downloads_keep_each_source_after_origin_chat_removed(env):
    digest = hashlib.sha256(env.files.contents["file-1"]).hexdigest()
    env.files.infos["file-1"].metadata = {"sha256": digest}
    env.repo.receipts[("alice", "file-1")] = {
        "file_id": "file-1", "size": env.files.infos["file-1"].size, "sha256": digest,
        "source_url": "https://example.org/data.csv?token=SECRET#section",
    }
    saved = await env.service.create("alice", request(source_type="open_data", session_id="first"))
    del env.sessions[("alice", "first")]
    env.repo.receipts.clear()
    recovered = await env.service.get("alice", saved.id)
    assert recovered.source_verified is True and recovered.source_url == "https://example.org/data.csv"
    assert recovered.files[0].source_url == recovered.source_url and recovered.files[0].sha256 == digest
    assert "SECRET" not in recovered.model_dump_json()
    default_label = await env.service.create("alice", request(source_type="upload"))
    assert default_label.source_type == "upload"  # Receipt was removed above.


@pytest.mark.asyncio
async def test_authoritative_open_data_source_overrides_generic_upload_label(env):
    digest = hashlib.sha256(env.files.contents["file-1"]).hexdigest()
    env.files.infos["file-1"].metadata = {"sha256": digest}
    env.repo.receipts[("alice", "file-1")] = {
        "file_id": "file-1", "size": env.files.infos["file-1"].size, "sha256": digest,
        "source_url": "https://example.org/data.csv",
    }
    saved = await env.service.create("alice", request(source_type="upload"))
    assert saved.source_type == "open_data" and saved.source_verified is True


@pytest.mark.asyncio
async def test_missing_selected_file_is_reported_instead_of_silently_reducing_scope(env):
    await env.service.set_context("alice", "first", SelectedDataInput(file_ids=["file-1"]))
    env.files.infos.clear()
    result = await env.service.get_context("alice", "first")
    assert result.files == [] and result.missing_file_ids == ["file-1"]


@pytest.mark.asyncio
async def test_analysis_save_requires_owned_session_membership(env):
    with pytest.raises(BadRequestError):
        await env.service.create("alice", request(source_type="analysis"))
    with pytest.raises(BadRequestError):
        await env.service.create("alice", request(source_type="analysis", session_id="second"))
    saved = await env.service.create("alice", request(source_type="analysis", session_id="first"))
    assert saved.source_type == "analysis" and saved.session_id == "first"


@pytest.mark.asyncio
async def test_historical_replaced_output_can_be_saved_after_current_file_changed(env):
    from app.domain.models.event import MessageEvent
    old = env.files.add("old", metadata={"source": "sandbox_artifact", "session_id": "first"})
    env.session_repo.get_events.return_value = [MessageEvent(role="assistant", message="旧结果", attachments=[old])]
    saved = await env.service.create("alice", request(file_ids=["old"], session_id="first", source_type="analysis"))
    assert saved.source_type == "analysis" and saved.files[0].file_id == "old"
    assert all(file.file_id != "old" for file in env.sessions[("alice", "first")].files)
    env.files.infos["old"].user_id = "bob"
    with pytest.raises(NotFoundError):
        await env.service.create("alice", request(file_ids=["old"], session_id="first", source_type="analysis"))


@pytest.mark.asyncio
async def test_generic_save_infers_analysis_only_from_owned_session_artifacts(env):
    env.files.infos["file-1"].metadata = {"source": "sandbox_artifact", "session_id": "first"}
    saved = await env.service.create("alice", request(session_id="first"))
    assert saved.source_type == "analysis"
    no_origin = await env.service.create("alice", request())
    assert no_origin.source_type == "upload"
    wrong_origin = await env.service.create("alice", request(session_id="second"))
    assert wrong_origin.source_type == "upload"
    env.files.infos["file-1"].metadata = {"user_metadata": {"source": "sandbox_artifact", "session_id": "first"}}
    upload = await env.service.create("alice", request(session_id="first"))
    assert upload.source_type == "upload"


@pytest.mark.asyncio
async def test_context_update_is_atomic_and_does_not_confuse_outputs_with_selected_inputs(env):
    await env.service.set_context("alice", "first", SelectedDataInput(file_ids=["file-1"]))
    with pytest.raises(NotFoundError):
        await env.service.set_context("alice", "first", SelectedDataInput(file_ids=["file-1", "unknown"]))
    assert [file.file_id for file in (await env.service.get_context("alice", "first")).files] == ["file-1"]
    cleared = await env.service.set_context("alice", "first", SelectedDataInput())
    assert cleared.files == [] and env.sessions[("alice", "first")].files


@pytest.mark.asyncio
async def test_library_search_paging_metadata_edits_and_missing_state(env):
    for index in range(23):
        await env.service.create("alice", request(name=f"气候 {index}", tags=["海洋"]))
    page = await env.service.list("alice", query="海洋", source_type="upload")
    rest = await env.service.list("alice", query="海洋", cursor=page.next_cursor)
    assert len(page.items) == 20 and len(rest.items) == 3 and rest.next_cursor is None
    assert len({asset.id for asset in page.items + rest.items}) == 23
    changed = await env.service.update("alice", page.items[0].id, UpdateLibraryAsset(name="温度", tags=[]))
    assert changed.name == "温度" and changed.tags == [] and changed.files == page.items[0].files
    env.files.infos.clear()
    assert (await env.service.get("alice", changed.id)).status == "missing"


@pytest.mark.parametrize("payload", [
    {"file_ids": []}, {"file_ids": ["/private/SECRET"]}, {"file_ids": ["dataset:source"]},
    {"file_ids": ["file-1", "gridfs:file-1"]},
    {"source_verified": True}, {"source_url": "https://example.org/data"}, {"name": " "},
])
def test_save_input_rejects_paths_duplicate_aliases_empty_selection_and_trust_claims(payload):
    with pytest.raises(ValidationError):
        request(**payload)


def test_saved_asset_has_no_file_count_cap_but_analysis_context_remains_bounded():
    files = [f"file-{i}" for i in range(43)]
    asset = CreateLibraryAsset(name="完整数据集", file_ids=files,
        file_paths={file_id: f"data/{file_id}.csv" for file_id in files})
    assert asset.file_ids == files and len(asset.file_paths) == 43
    with pytest.raises(ValidationError):
        SelectedDataInput(file_ids=files)


@pytest.mark.parametrize("payload", [{}, {"name": None}, {"file_ids": ["file-1"]}, {"version": 2}, {"source_type": "open_data"}])
def test_update_cannot_change_immutable_inputs_or_provenance(payload):
    with pytest.raises(ValidationError):
        UpdateLibraryAsset(**payload)


@pytest.mark.asyncio
async def test_http_routes_enforce_owner_and_publish_safe_contract(env):
    app = FastAPI()
    app.include_router(data_library_routes.router, prefix="/api/v1")
    register_exception_handlers(app)
    def current_user(x_test_user: str = Header("alice")):
        return SimpleNamespace(id=x_test_user)
    app.dependency_overrides[get_current_user] = current_user
    app.dependency_overrides[data_library_routes.get_data_library_service] = lambda: env.service
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/data-library", json={
            "name": "我的数据", "file_ids": ["file-1"], "file_paths": {"file-1": "原始数据/table.csv"},
        })
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["status"] == "ready" and "T" in data["created_at"] and data["version"] == 1
        assert data["files"][0]["relative_path"] == "原始数据/table.csv"
        asset_url = "/api/v1/data-library/" + data["id"]
        assert (await client.get(asset_url)).json()["data"]["files"][0]["relative_path"] == "原始数据/table.csv"
        bob = {"x-test-user": "bob"}
        assert (await client.get(asset_url, headers=bob)).status_code == 404
        assert (await client.patch(asset_url, headers=bob, json={"name": "stolen"})).status_code == 404
        assert (await client.delete(asset_url, headers=bob)).status_code == 404
        assert (await client.put("/api/v1/sessions/first/data-context", headers=bob, json={"file_ids": ["file-1"]})).status_code == 404
        assert (await client.get("/api/v1/data-library", headers=bob)).json()["data"]["items"] == []
        assert (await client.patch(asset_url, json={"name": "重命名"})).json()["data"]["name"] == "重命名"
        assert (await client.put("/api/v1/sessions/second/data-context", json={"file_ids": ["file-1"]})).status_code == 200
        assert (await client.get("/api/v1/sessions/second/data-context")).json()["data"]["files"][0]["file_id"] == "file-1"
        assert (await client.delete(asset_url)).status_code == 200
        assert await env.files.get_file_info("file-1", "alice") is not None
