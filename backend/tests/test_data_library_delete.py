"""Removing selected library references must not remove other files or bytes."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Header
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.domain.models.data_library import DeleteLibraryFiles, SelectedDataInput, canonical_file_id
from app.infrastructure.external.file.retainedfile import RetainedFileError
from app.infrastructure.repositories.mongo_data_library_repository import MongoDataLibraryRepository
from app.interfaces.api import data_library_routes
from app.interfaces.dependencies import get_current_user
from app.interfaces.errors.exception_handlers import register_exception_handlers
from test_data_library import env, request


def deletion(*pairs):
    return DeleteLibraryFiles(files=[{"asset_id": asset_id, "file_id": file_id} for asset_id, file_id in pairs])


@pytest.fixture
def delete_env(env):
    env.repo.list_folders = AsyncMock(return_value=[])

    async def remove_file(owner, asset_id, file_id):
        row = env.repo.rows.get((owner, asset_id))
        if row is None:
            return False
        original = row["files"]
        row["files"] = [file for file in original if canonical_file_id(file["file_id"]) != file_id]
        if not row["files"]:
            env.repo.rows.pop((owner, asset_id))
        return len(original) != len(row["files"])

    env.repo.remove_file = AsyncMock(side_effect=remove_file)
    return env


@pytest.mark.asyncio
async def test_selected_file_delete_preserves_other_files_assets_sessions_and_objects(delete_env):
    env = delete_env
    env.files.add("file-2")
    first = await env.service.create("alice", request(file_ids=["file-1", "file-2"]))
    second = await env.service.create("alice", request())
    await env.service.set_context("alice", "second", SelectedDataInput(file_ids=["file-1"]))
    reads_before = list(env.files.reads)

    result = await env.service.delete_files("alice", deletion((first.id, "file-1")))

    assert result.deleted_count == 1 and result.failed_count == 0
    assert result.results[0].status == "deleted"
    assert [file.file_id for file in (await env.service.get("alice", first.id)).files] == ["file-2"]
    assert [file.file_id for file in (await env.service.get("alice", second.id)).files] == ["file-1"]
    assert [file.file_id for file in (await env.service.get_context("alice", "second")).files] == ["file-1"]
    assert env.sessions[("alice", "first")].files[0].file_id == "file-1"
    assert set(env.files.contents) == {"file-1", "file-2"}
    assert not env.files.deletions and env.files.reads == reads_before
    assert env.repo.claim("alice", "file-1")["assets"] == {first.id, second.id}
    with pytest.raises(RetainedFileError):
        await env.retained.delete_file("file-1", "alice")


@pytest.mark.asyncio
async def test_delete_last_file_removes_empty_asset_even_when_underlying_file_is_missing(delete_env):
    env = delete_env
    saved = await env.service.create("alice", request())
    env.files.contents.clear()
    env.files.infos.clear()
    result = await env.service.delete_files("alice", deletion((saved.id, "file-1")))
    assert result.deleted_count == 1
    assert (await env.service.list("alice")).items == []
    assert not env.files.deletions
    env.repo.list_folders.assert_awaited_once_with("alice")


@pytest.mark.asyncio
async def test_batch_deduplicates_aliases_and_completes_after_missing_or_failed_items(delete_env):
    env = delete_env
    env.files.add("file-2")
    first = await env.service.create("alice", request())
    second = await env.service.create("alice", request(file_ids=["file-2"]))
    remove_file = env.repo.remove_file.side_effect

    async def fail_one(owner, asset_id, file_id):
        if file_id == "fail":
            raise RuntimeError("mongodb://SECRET/private/path")
        return await remove_file(owner, asset_id, file_id)

    env.repo.remove_file.side_effect = fail_one
    result = await env.service.delete_files("alice", deletion(
        (first.id, "file-1"), (first.id, "gridfs:file-1"),
        ("a" * 32, "fail"), (first.id, "missing"), (second.id, "file-2"),
    ))
    assert [item.status for item in result.results] == ["deleted", "failed", "not_found", "deleted"]
    assert result.deleted_count == 2 and result.failed_count == 2
    assert result.results[0].file_id == "file-1"
    assert env.repo.remove_file.await_count == 4
    assert not env.repo.rows and not env.files.deletions
    assert "SECRET" not in result.model_dump_json() and "private" not in result.model_dump_json()
    retry = await env.service.delete_files("alice", deletion((first.id, "file-1")))
    assert retry.results[0].status == "not_found"


@pytest.mark.asyncio
async def test_foreign_and_missing_library_references_are_indistinguishable(delete_env):
    env = delete_env
    saved = await env.service.create("alice", request())
    result = await env.service.delete_files("bob", deletion((saved.id, "file-1"), ("f" * 32, "file-1")))
    assert result.deleted_count == 0 and result.failed_count == 2
    assert {item.status for item in result.results} == {"not_found"}
    assert len({item.message for item in result.results}) == 1
    assert (await env.service.get("alice", saved.id)).files == saved.files
    assert not env.files.deletions
    env.repo.list_folders.assert_awaited_once_with("bob")


@pytest.mark.asyncio
async def test_folder_preparation_failure_is_reported_without_removing_any_selection(delete_env):
    env = delete_env
    saved = await env.service.create("alice", request())
    env.repo.list_folders.side_effect = RuntimeError("secret")
    result = await env.service.delete_files("alice", deletion((saved.id, "file-1")))
    assert result.deleted_count == 0 and result.failed_count == 1
    assert result.results[0].status == "failed"
    env.repo.remove_file.assert_not_awaited()
    assert (await env.service.get("alice", saved.id)).files == saved.files


@pytest.mark.asyncio
async def test_overlapping_delete_requests_only_count_actual_removal_once(delete_env):
    env = delete_env
    saved = await env.service.create("alice", request())
    results = await asyncio.gather(*(
        env.service.delete_files("alice", deletion((saved.id, "file-1"))) for _ in range(4)
    ))
    assert sum(result.deleted_count for result in results) == 1
    assert sorted(result.results[0].status for result in results) == ["deleted", "not_found", "not_found", "not_found"]
    assert not env.files.deletions


@pytest.mark.parametrize("payload", [
    {"files": []},
    {"files": [{"asset_id": "a" * 32, "file_id": "file"}] * 501},
    {"files": [{"asset_id": "bad", "file_id": "file"}]},
    {"files": [{"asset_id": "a" * 32, "file_id": "../private/file"}]},
    {"files": [{"asset_id": "a" * 32, "file_id": "file", "owner_id": "bob"}]},
    {"files": [{"asset_id": "a" * 32, "file_id": "file"}], "delete_bytes": True},
])
def test_delete_contract_rejects_invalid_or_unbounded_input(payload):
    with pytest.raises(ValidationError):
        DeleteLibraryFiles.model_validate(payload)


@pytest.mark.asyncio
async def test_delete_route_uses_authenticated_owner_and_returns_each_result(delete_env):
    env = delete_env
    saved = await env.service.create("alice", request())
    app = FastAPI()
    app.include_router(data_library_routes.router, prefix="/api/v1")
    register_exception_handlers(app)

    async def current_user(x_test_user: str = Header("alice")):
        return SimpleNamespace(id=x_test_user)

    app.dependency_overrides[get_current_user] = current_user
    app.dependency_overrides[data_library_routes.get_data_library_service] = lambda: env.service
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = {"files": [{"asset_id": saved.id, "file_id": "gridfs:file-1"}]}
        foreign = await client.post("/api/v1/data-library/files/delete", json=body, headers={"x-test-user": "bob"})
        assert foreign.status_code == 200 and foreign.json()["data"]["results"][0]["status"] == "not_found"
        response = await client.post("/api/v1/data-library/files/delete", json=body)
        assert response.status_code == 200
        assert response.json()["data"] == {
            "results": [{"asset_id": saved.id, "file_id": "file-1", "status": "deleted", "message": None}],
            "deleted_count": 1, "failed_count": 0,
        }
        assert (await client.post("/api/v1/data-library/files/delete", json={"files": []})).status_code == 422


@pytest.mark.asyncio
async def test_repository_remove_uses_owner_scoped_atomic_pull_and_conditional_cleanup():
    collection = SimpleNamespace(find_one_and_update=AsyncMock(return_value={"payload": {}}), delete_one=AsyncMock())
    repo = MongoDataLibraryRepository({"data_library": collection})
    assert await repo.remove_file("alice", "a" * 32, "gridfs:file-1")
    match, update = collection.find_one_and_update.call_args.args
    assert match == {"_id": repo.prefix("alice") + "a" * 32, "owner_id": "alice",
                     "payload.files.file_id": {"$in": ["file-1", "gridfs:file-1"]}}
    assert update["$pull"] == {"payload.files": {"file_id": {"$in": ["file-1", "gridfs:file-1"]}}}
    collection.delete_one.assert_awaited_once_with({
        "_id": repo.prefix("alice") + "a" * 32, "owner_id": "alice", "payload.files": {"$size": 0},
    })
