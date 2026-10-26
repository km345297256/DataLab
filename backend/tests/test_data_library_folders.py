"""Personal folders are logical metadata, with owner-scoped create/save APIs."""
import copy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Header
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from pymongo.errors import DuplicateKeyError

from app.application.errors.exceptions import BadRequestError, ConflictError, NotFoundError
from app.application.services.data_library_service import DataLibraryService
from app.domain.models.data_library import (
    CreateLibraryFolder, DataLibraryFolder, library_folder_id,
)
from app.infrastructure.external.file.retainedfile import RetainedFileStorage
from app.interfaces.api import data_library_routes
from app.interfaces.dependencies import get_current_user
from app.interfaces.errors.exception_handlers import register_exception_handlers
from test_data_library import Files, Repository, request


class FolderRepository(Repository):
    def __init__(self):
        super().__init__()
        self.folders = {}

    async def list_folders(self, owner):
        return copy.deepcopy([folder for (user, _), folder in self.folders.items() if user == owner])

    async def get_folder(self, owner, key):
        return copy.deepcopy(self.folders.get((owner, key)))

    async def insert_folder(self, owner, payload):
        key = (owner, payload["id"])
        if key in self.folders:
            raise DuplicateKeyError("Duplicate folder")
        self.folders[key] = copy.deepcopy(payload)
        return payload

    async def file_path_exists(self, owner, path):
        return any(file.get("relative_path") == path or (
            not file.get("relative_path") and file["filename"] == path
        ) for (user, _), asset in self.rows.items() if user == owner for file in asset["files"])


@pytest.fixture
def env():
    repo, files = FolderRepository(), Files()
    files.add()
    retained = RetainedFileStorage(files, repo)
    return SimpleNamespace(repo=repo, files=files, service=DataLibraryService(repo, retained, None))


@pytest.mark.parametrize("name", [
    "", " ", ".", "..", "../data", "data/child", "/private", "//host/share",
    "data/", "data//child", " data", "data ", "data\n", "a\x00b", "a\x7fb", "a\x85b",
    r"C:\private", r"\\host\data", "C:data", "https://example.com", "a" * 256, "雪" * 86, None,
])
def test_folder_names_reject_paths_controls_whitespace_and_oversized_utf8(name):
    with pytest.raises(ValidationError):
        CreateLibraryFolder(name=name)


@pytest.mark.parametrize("folder_id", ["", "root", "../x", "a" * 31, "A" * 32, "g" * 32])
def test_folder_targets_are_opaque_ids_not_paths(folder_id):
    with pytest.raises(ValidationError):
        CreateLibraryFolder(name="data", parent_id=folder_id)
    with pytest.raises(ValidationError):
        request(target_folder_id=folder_id)


def test_omitted_target_and_explicit_root_remain_distinguishable():
    assert "target_folder_id" not in request().model_fields_set
    assert "target_folder_id" in request(target_folder_id=None).model_fields_set
    assert request().target_folder_id is request(target_folder_id=None).target_folder_id is None


@pytest.mark.asyncio
async def test_empty_nested_folders_survive_new_service_and_are_private(env):
    parent = await env.service.create_folder("alice", CreateLibraryFolder(name="研究数据"))
    child = await env.service.create_folder("alice", CreateLibraryFolder(name="原始数据", parent_id=parent.id))
    deep = await env.service.create_folder("alice", CreateLibraryFolder(name="2026", parent_id=child.id))
    assert parent.path == "研究数据" and parent.parent_id is None
    assert (child.path, child.parent_id) == ("研究数据/原始数据", parent.id)
    assert (deep.path, deep.parent_id) == ("研究数据/原始数据/2026", child.id)
    restarted = DataLibraryService(env.repo, None, None)
    assert (await restarted.list_folders("alice")).items == [parent, child, deep]
    assert (await restarted.list_folders("bob")).items == []
    assert not env.repo.rows and not env.files.reads
    bob = await restarted.create_folder("bob", CreateLibraryFolder(name="研究数据"))
    assert bob.id != parent.id
    with pytest.raises(NotFoundError):
        await restarted.create_folder("bob", CreateLibraryFolder(name="stolen", parent_id=parent.id))
    with pytest.raises(NotFoundError):
        await restarted.owned_folder("bob", child.id)
    with pytest.raises(NotFoundError):
        await restarted.create_folder("alice", CreateLibraryFolder(name="missing", parent_id="f" * 32))


@pytest.mark.asyncio
async def test_duplicate_folder_names_conflict_only_with_same_parent(env):
    first = await env.service.create_folder("alice", CreateLibraryFolder(name="data"))
    with pytest.raises(ConflictError) as error:
        await env.service.create_folder("alice", CreateLibraryFolder(name="data"))
    assert error.value.status_code == 409
    nested = await env.service.create_folder("alice", CreateLibraryFolder(name="data", parent_id=first.id))
    assert nested.path == "data/data"
    with pytest.raises(ConflictError):
        await env.service.create_folder("alice", CreateLibraryFolder(name="data", parent_id=first.id))


@pytest.mark.asyncio
async def test_folder_combined_depth_and_utf8_path_limit(env):
    parent = None
    for index in range(15):
        parent = await env.service.create_folder("alice", CreateLibraryFolder(name=f"d{index}", parent_id=parent.id if parent else None))
    with pytest.raises(BadRequestError, match="15"):
        await env.service.create_folder("alice", CreateLibraryFolder(name="too-deep", parent_id=parent.id))
    # A file fits in a 15-directory hierarchy; adding a relative subfolder does not.
    saved = await env.service.create("alice", request(target_folder_id=parent.id))
    assert saved.files[0].relative_path == parent.path + "/table.csv"
    with pytest.raises(BadRequestError):
        await env.service.create("alice", request(target_folder_id=parent.id, file_paths={"file-1": "extra/table.csv"}))
    long_parent = None
    for index in range(4):
        long_parent = await env.service.create_folder("alice", CreateLibraryFolder(name="雪" * 85, parent_id=long_parent.id if long_parent else None))
    with pytest.raises(BadRequestError):
        await env.service.create_folder("alice", CreateLibraryFolder(name="abc", parent_id=long_parent.id))
    with pytest.raises(BadRequestError):
        await env.service.create("alice", request(target_folder_id=long_parent.id))


@pytest.mark.asyncio
async def test_save_target_combines_only_public_paths_and_preserves_storage_and_retention(env):
    parent = await env.service.create_folder("alice", CreateLibraryFolder(name="研究"))
    before = copy.deepcopy(env.files.infos["file-1"])
    nested = await env.service.create("alice", request(
        target_folder_id=parent.id, file_paths={"file-1": "实验/原始/table.csv"},
    ))
    root = await env.service.create("alice", request(target_folder_id=None))
    legacy = await env.service.create("alice", request())
    legacy_path = await env.service.create("alice", request(file_paths={"file-1": "existing/table.csv"}))
    assert nested.files[0].relative_path == "研究/实验/原始/table.csv"
    assert root.files[0].relative_path == "table.csv"
    assert legacy.files[0].relative_path is None
    assert legacy_path.files[0].relative_path == "existing/table.csv"
    assert env.files.infos["file-1"] == before
    assert env.repo.claim("alice", "file-1")["assets"] == {nested.id, root.id, legacy.id, legacy_path.id}
    assert not env.files.deletions
    for saved in (nested, root, legacy, legacy_path):
        assert "/private" not in saved.model_dump_json() and "/srv" not in saved.model_dump_json()


@pytest.mark.asyncio
async def test_foreign_or_missing_save_target_fails_before_retaining_or_reading_files(env):
    folder = await env.service.create_folder("bob", CreateLibraryFolder(name="private"))
    for target in (folder.id, "f" * 32):
        with pytest.raises(NotFoundError):
            await env.service.create("alice", request(target_folder_id=target))
    assert not env.repo.claims and not env.files.reads and not env.repo.rows


@pytest.mark.asyncio
async def test_directory_file_conflicts_and_failed_save_release_claims(env):
    folder = await env.service.create_folder("alice", CreateLibraryFolder(name="table.csv"))
    with pytest.raises(ConflictError):
        await env.service.create("alice", request(target_folder_id=None))
    assert not env.repo.claim("alice", "file-1")["assets"] and not env.repo.rows
    child = await env.service.create("alice", request(target_folder_id=folder.id))
    with pytest.raises(ConflictError):
        await env.service.create_folder("alice", CreateLibraryFolder(name="table.csv", parent_id=folder.id))
    with pytest.raises(ConflictError):
        await env.service.create("alice", request(
            target_folder_id=folder.id, file_paths={"file-1": "table.csv/table.csv"},
        ))
    assert env.repo.claim("alice", "file-1")["assets"] == {child.id}
    env.files.add("file-2")
    with pytest.raises(ConflictError, match="同名文件"):
        await env.service.create("alice", request(target_folder_id=folder.id, file_ids=["file-1", "file-2"]))
    assert env.repo.claim("alice", "file-1")["assets"] == {child.id}
    assert not env.repo.claim("alice", "file-2")["assets"]


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True])
async def test_explicit_save_rejects_existing_final_path_and_releases_only_new_claims(env, nested):
    target = await env.service.create_folder("alice", CreateLibraryFolder(name="data")) if nested else None
    target_id = target.id if target else None
    first = await env.service.create("alice", request(target_folder_id=target_id))
    env.files.add("file-2")
    for file_id in ("file-1", "file-2"):
        with pytest.raises(ConflictError, match="已有同名文件") as error:
            await env.service.create("alice", request(target_folder_id=target_id, file_ids=[file_id]))
        assert error.value.status_code == 409
        assert env.repo.claim("alice", "file-1")["assets"] == {first.id}
        assert not env.repo.claim("alice", "file-2")["assets"]
        assert len(env.repo.rows) == 1
    other = await env.service.create_folder("alice", CreateLibraryFolder(name="other"))
    different_directory = await env.service.create("alice", request(target_folder_id=other.id, file_ids=["file-2"]))
    assert different_directory.files[0].relative_path == "other/table.csv"
    env.files.add("bob-file", owner="bob")
    bob_target = await env.service.create_folder("bob", CreateLibraryFolder(name="data")) if nested else None
    bob_saved = await env.service.create("bob", request(target_folder_id=bob_target.id if bob_target else None, file_ids=["bob-file"]))
    assert bob_saved.files[0].relative_path == first.files[0].relative_path
    assert env.repo.claim("alice", "file-1")["assets"] == {first.id}
    assert not env.files.deletions


@pytest.mark.asyncio
async def test_explicit_root_detects_legacy_file_without_relative_path_but_omission_stays_compatible(env):
    legacy = await env.service.create("alice", request())
    assert legacy.files[0].relative_path is None
    with pytest.raises(ConflictError, match="已有同名文件"):
        await env.service.create("alice", request(target_folder_id=None))
    second_legacy = await env.service.create("alice", request())
    assert second_legacy.files[0].relative_path is None
    assert env.repo.claim("alice", "file-1")["assets"] == {legacy.id, second_legacy.id}


@pytest.mark.asyncio
async def test_list_resolves_owned_folder_to_direct_path_and_preserves_other_filters(env):
    folder = await env.service.create_folder("alice", CreateLibraryFolder(name="a[1]"))
    env.repo.page = AsyncMock(return_value=[])
    await env.service.list("alice", folder_id=folder.id, filename="needle", file_format="CSV", cursor="f" * 32)
    env.repo.page.assert_awaited_with("alice", cursor="f" * 32, query="", source_type=None,
                                    filename="needle", file_format="csv", folder_path="a[1]")
    await env.service.list("alice", folder_id="root")
    env.repo.page.assert_awaited_with("alice", cursor=None, query="", source_type=None, folder_path="")
    env.repo.page.reset_mock()
    with pytest.raises(NotFoundError):
        await env.service.list("bob", folder_id=folder.id)
    env.repo.page.assert_not_awaited()


def test_folder_metadata_uses_stable_private_ids_and_explicit_utc():
    folder = DataLibraryFolder(id=library_folder_id("alice", "研究"), name="研究", path="研究",
                               created_at=datetime(2026, 10, 21), updated_at=datetime(2026, 10, 21))
    assert len(folder.id) == 32 and folder.id != library_folder_id("bob", "研究")
    assert folder.model_dump(mode="json")["created_at"] == "2026-10-21T00:00:00Z"
    assert folder.updated_at.tzinfo == UTC


@pytest.mark.asyncio
async def test_folder_http_routes_precede_asset_lookup_and_validate_authenticated_requests(env):
    app = FastAPI()
    app.include_router(data_library_routes.router, prefix="/api/v1")
    register_exception_handlers(app)
    def current_user(x_test_user: str = Header("alice")):
        return SimpleNamespace(id=x_test_user)
    app.dependency_overrides[get_current_user] = current_user
    app.dependency_overrides[data_library_routes.get_data_library_service] = lambda: env.service
    env.repo.page = AsyncMock(return_value=[])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        created = await client.post("/api/v1/data-library/folders", json={"name": "研究", "parent_id": None})
        assert created.status_code == 200
        folder = created.json()["data"]
        assert set(folder) == {"id", "parent_id", "name", "path", "created_at", "updated_at"}
        assert (await client.get("/api/v1/data-library/folders")).json()["data"] == {"items": [folder]}
        assert (await client.get("/api/v1/data-library/folders", headers={"x-test-user": "bob"})).json()["data"] == {"items": []}
        duplicate = await client.post("/api/v1/data-library/folders", json={"name": "研究"})
        assert duplicate.status_code == 409
        foreign = await client.post("/api/v1/data-library/folders", headers={"x-test-user": "bob"}, json={"name": "x", "parent_id": folder["id"]})
        assert foreign.status_code == 404
        for payload in ({"name": "../x"}, {"name": "valid", "owner_id": "bob"}, {"name": "x", "parent_id": "root"}):
            assert (await client.post("/api/v1/data-library/folders", json=payload)).status_code == 422
        assert (await client.get("/api/v1/data-library", params={"folder_id": folder["id"]})).status_code == 200
        env.repo.page.assert_awaited_with("alice", cursor=None, query="", source_type=None, folder_path="研究")
        assert (await client.get("/api/v1/data-library", params={"folder_id": "root"})).status_code == 200
        env.repo.page.assert_awaited_with("alice", cursor=None, query="", source_type=None, folder_path="")
        assert (await client.get("/api/v1/data-library", params={"folder_id": "../研究"})).status_code == 422
        assert (await client.get("/api/v1/data-library", params={"folder_id": "f" * 32})).status_code == 404
        saved = await client.post("/api/v1/data-library", json={"name": "data", "file_ids": ["file-1"], "target_folder_id": folder["id"]})
        assert saved.status_code == 200
        assert saved.json()["data"]["files"][0]["relative_path"] == "研究/table.csv"
