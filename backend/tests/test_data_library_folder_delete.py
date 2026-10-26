"""Folder deletion is recursive, retryable, and serialized with library saves."""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Header
from httpx import ASGITransport, AsyncClient

from app.application.errors.exceptions import ConflictError, NotFoundError
from app.application.services.data_library_service import DataLibraryService
from app.domain.models.data_library import CreateLibraryFolder
from app.infrastructure.repositories.mongo_data_library_repository import library_mutation_state
from app.interfaces.api import data_library_routes
from app.interfaces.dependencies import get_current_user
from app.interfaces.errors.exception_handlers import register_exception_handlers
from test_data_library import request
from test_data_library_folders import env


@pytest.fixture
def folder_env(env):
    env.repo.deletions = {}

    async def counts(owner, path):
        return {
            "file_count": sum(file.get("relative_path", "").startswith(path + "/")
                              for (user, _), row in env.repo.rows.items() if user == owner
                              for file in row["files"] if file.get("relative_path")),
            "folder_count": sum(folder["path"] == path or folder["path"].startswith(path + "/")
                                for (user, _), folder in env.repo.folders.items() if user == owner),
        }

    async def get_deletion(owner, folder_id, token):
        return copy.deepcopy(env.repo.deletions.get((owner, folder_id, token)))

    async def record_deletion(owner, folder, token, *, completed):
        env.repo.deletions[(owner, folder["id"], token)] = {"folder": copy.deepcopy(folder), "completed": completed}

    async def remove_contents(owner, folder, progress):
        for key, row in list(env.repo.rows.items()):
            if key[0] != owner:
                continue
            retained = [file for file in row["files"] if not (file.get("relative_path") or "").startswith(folder["path"] + "/")]
            progress["deleted_file_count"] += len(row["files"]) - len(retained)
            row["files"] = retained
            if not retained:
                env.repo.rows.pop(key)
        for key, saved in list(env.repo.folders.items()):
            if key[0] == owner and (saved["path"] == folder["path"] or saved["path"].startswith(folder["path"] + "/")):
                env.repo.folders.pop(key)
                progress["deleted_folder_count"] += 1

    env.repo.folder_subtree_counts = AsyncMock(side_effect=counts)
    env.repo.get_folder_deletion = AsyncMock(side_effect=get_deletion)
    env.repo.record_folder_deletion = AsyncMock(side_effect=record_deletion)
    env.repo.remove_folder_contents = AsyncMock(side_effect=remove_contents)
    return env


@pytest.mark.asyncio
async def test_preview_and_delete_include_all_pages_descendants_and_empty_folders(folder_env):
    env = folder_env
    root = await env.service.create_folder("alice", CreateLibraryFolder(name="a[1]"))
    child = await env.service.create_folder("alice", CreateLibraryFolder(name="child", parent_id=root.id))
    await env.service.create_folder("alice", CreateLibraryFolder(name="empty", parent_id=root.id))
    neighbor = await env.service.create_folder("alice", CreateLibraryFolder(name="a1"))
    for index in range(25):
        await env.service.create("alice", request(name=f"entry {index}", file_paths={"file-1": "a[1]/child/table.csv"}))
    saved = await env.service.create("alice", request(file_paths={"file-1": "a1/table.csv"}))
    before = list(env.files.reads)
    preview = await env.service.folder_deletion_preview("alice", root.id)
    assert preview.file_count == 25 and preview.subfolder_count == 2
    assert "generation" not in preview.model_dump_json()
    result = await env.service.delete_folder("alice", root.id, preview.deletion_token)
    assert result.completed and result.deleted_file_count == 25 and result.deleted_folder_count == 3
    assert result.remaining_file_count == result.remaining_folder_count == 0
    assert (await env.service.get("alice", saved.id)).files[0].relative_path == "a1/table.csv"
    assert await env.repo.get_folder("alice", neighbor.id)
    assert await env.repo.get_folder("alice", child.id) is None
    assert len(env.repo.rows) == 1 and not env.files.deletions and env.files.reads == before
    assert env.repo.claim("alice", "file-1")["assets"]


@pytest.mark.asyncio
async def test_old_token_cannot_delete_recreated_same_name_folder_even_with_same_timestamp(folder_env):
    env = folder_env
    root = await env.service.create_folder("alice", CreateLibraryFolder(name="empty"))
    old = await env.service.folder_deletion_preview("alice", root.id)
    assert (await env.service.delete_folder("alice", root.id, old.deletion_token)).completed
    recreated = await env.service.create_folder("alice", CreateLibraryFolder(name="empty"))
    assert recreated.id == root.id
    # Mongo only stores milliseconds: incarnation must not depend on timestamp
    # alone, even under a fixed clock or an immediate delete/recreate sequence.
    env.repo.folders[("alice", root.id)]["created_at"] = root.created_at
    current = await env.service.folder_deletion_preview("alice", root.id)
    assert current.deletion_token != old.deletion_token
    retry = await env.service.delete_folder("alice", root.id, old.deletion_token)
    assert retry.completed and retry.deleted_folder_count == retry.deleted_file_count == 0
    assert await env.repo.get_folder("alice", root.id)
    assert (await env.service.delete_folder("alice", root.id, current.deletion_token)).completed


@pytest.mark.asyncio
async def test_tokens_and_folder_ids_do_not_cross_owner_boundary(folder_env):
    env = folder_env
    root = await env.service.create_folder("alice", CreateLibraryFolder(name="private"))
    preview = await env.service.folder_deletion_preview("alice", root.id)
    with pytest.raises(NotFoundError):
        await env.service.folder_deletion_preview("bob", root.id)
    with pytest.raises(NotFoundError):
        await env.service.delete_folder("bob", root.id, preview.deletion_token)
    with pytest.raises(ConflictError):
        await env.service.delete_folder("alice", root.id, "0" * 64)
    with pytest.raises(NotFoundError):
        await env.service.delete_folder("alice", "root", preview.deletion_token)
    assert await env.repo.get_folder("alice", root.id)
    assert not env.repo.deletions


@pytest.mark.asyncio
async def test_partial_failure_preserves_retry_scope_and_does_not_report_completion(folder_env):
    env = folder_env
    root = await env.service.create_folder("alice", CreateLibraryFolder(name="data"))
    saved = await env.service.create("alice", request(file_paths={"file-1": "data/table.csv"}))
    preview = await env.service.folder_deletion_preview("alice", root.id)
    original = env.repo.remove_folder_contents.side_effect

    async def partial(owner, folder, progress):
        env.repo.rows.pop((owner, saved.id))
        progress["deleted_file_count"] += 1
        raise RuntimeError("SECRET/private/database")

    env.repo.remove_folder_contents.side_effect = partial
    result = await env.service.delete_folder("alice", root.id, preview.deletion_token)
    assert not result.completed and result.deleted_file_count == 1
    assert result.remaining_file_count == 0 and result.remaining_folder_count == 1
    assert "SECRET" not in result.model_dump_json()
    assert await env.repo.get_folder("alice", root.id)
    env.repo.remove_folder_contents.side_effect = original
    retry = await env.service.delete_folder("alice", root.id, preview.deletion_token)
    assert retry.completed and retry.deleted_folder_count == 1


@pytest.mark.asyncio
async def test_uncertain_completion_receipt_can_retry_after_root_was_removed(folder_env):
    env = folder_env
    root = await env.service.create_folder("alice", CreateLibraryFolder(name="data"))
    preview = await env.service.folder_deletion_preview("alice", root.id)
    original = env.repo.record_folder_deletion.side_effect

    async def fail_final(owner, folder, token, *, completed):
        if completed:
            raise RuntimeError("connection closed")
        await original(owner, folder, token, completed=completed)

    env.repo.record_folder_deletion.side_effect = fail_final
    result = await env.service.delete_folder("alice", root.id, preview.deletion_token)
    assert not result.completed and result.remaining_folder_count == 0
    env.repo.record_folder_deletion.side_effect = original
    assert (await env.service.delete_folder("alice", root.id, preview.deletion_token)).completed


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
async def test_save_validating_before_folder_deletion_cannot_commit_afterwards(folder_env, explicit):
    env = folder_env
    root = await env.service.create_folder("alice", CreateLibraryFolder(name="data"))
    preview = await env.service.folder_deletion_preview("alice", root.id)
    reading, resume = asyncio.Event(), asyncio.Event()
    original_read = env.files.download_file_range

    async def delayed_read(*args, **kwargs):
        reading.set()
        await resume.wait()
        return await original_read(*args, **kwargs)

    env.files.download_file_range = delayed_read
    save = request(target_folder_id=root.id) if explicit else request(file_paths={"file-1": "data/table.csv"})
    pending = asyncio.create_task(env.service.create("alice", save))
    await reading.wait()
    other_service = DataLibraryService(env.repo, env.service.files, None)
    assert (await other_service.delete_folder("alice", root.id, preview.deletion_token)).completed
    resume.set()
    with pytest.raises(ConflictError, match="保存期间"):
        await pending
    assert not env.repo.rows
    assert not env.repo.claim("alice", "file-1")["assets"]


@pytest.mark.asyncio
async def test_gate_serializes_folder_creation_but_does_not_block_other_owners(folder_env):
    env = folder_env
    root = await env.service.create_folder("alice", CreateLibraryFolder(name="data"))
    preview = await env.service.folder_deletion_preview("alice", root.id)
    entered, resume = asyncio.Event(), asyncio.Event()
    original = env.repo.remove_folder_contents.side_effect

    async def delayed_delete(*args):
        entered.set()
        await resume.wait()
        await original(*args)

    env.repo.remove_folder_contents.side_effect = delayed_delete
    deleting = asyncio.create_task(env.service.delete_folder("alice", root.id, preview.deletion_token))
    await entered.wait()
    child = asyncio.create_task(env.service.create_folder("alice", CreateLibraryFolder(name="child", parent_id=root.id)))
    other = await asyncio.wait_for(env.service.create_folder("bob", CreateLibraryFolder(name="data")), timeout=1)
    assert other.name == "data" and not child.done()
    resume.set()
    assert (await deleting).completed
    with pytest.raises(NotFoundError):
        await child


@pytest.mark.asyncio
async def test_gate_cancellation_releases_owner_and_reentrant_calls_do_not_deadlock():
    state = library_mutation_state("cancellation-test")
    entered = asyncio.Event()

    async def holding():
        async with state.guard():
            async with state.guard():
                entered.set()
                await asyncio.Event().wait()

    task = asyncio.create_task(holding())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with state.guard():
        assert state.task is asyncio.current_task()


@pytest.mark.asyncio
async def test_cancelled_final_save_waits_for_commit_before_allowing_recursive_delete(folder_env):
    env = folder_env
    folder = await env.service.create_folder("alice", CreateLibraryFolder(name="data"))
    preview = await env.service.folder_deletion_preview("alice", folder.id)
    entered, resume = asyncio.Event(), asyncio.Event()
    original = env.repo.insert

    async def delayed_insert(*args, **kwargs):
        entered.set()
        await resume.wait()
        return await original(*args, **kwargs)

    env.repo.insert = delayed_insert
    creating = asyncio.create_task(env.service.create("alice", request(target_folder_id=folder.id)))
    await entered.wait()
    creating.cancel()
    deleting = asyncio.create_task(env.service.delete_folder("alice", folder.id, preview.deletion_token))
    await asyncio.sleep(0)
    assert not creating.done() and not deleting.done()
    # Repeated cancellation must also leave the worker and its gate intact.
    creating.cancel()
    resume.set()
    with pytest.raises(asyncio.CancelledError):
        await creating
    result = await deleting
    assert result.completed and result.deleted_file_count == 1
    assert not env.repo.rows and not env.files.deletions


@pytest.mark.asyncio
async def test_failed_delete_with_unavailable_counts_does_not_report_zero_remaining(folder_env):
    env = folder_env
    folder = await env.service.create_folder("alice", CreateLibraryFolder(name="data"))
    preview = await env.service.folder_deletion_preview("alice", folder.id)
    env.repo.remove_folder_contents.side_effect = RuntimeError("unavailable")
    env.repo.folder_subtree_counts.side_effect = RuntimeError("unavailable")
    result = await env.service.delete_folder("alice", folder.id, preview.deletion_token)
    assert not result.completed
    assert result.remaining_file_count is None and result.remaining_folder_count is None


@pytest.mark.asyncio
async def test_folder_delete_routes_require_preview_token_and_keep_root_private(folder_env):
    env = folder_env
    root = await env.service.create_folder("alice", CreateLibraryFolder(name="data"))
    app = FastAPI()
    app.include_router(data_library_routes.router, prefix="/api/v1")
    register_exception_handlers(app)

    async def current_user(x_test_user: str = Header("alice")):
        return SimpleNamespace(id=x_test_user)

    app.dependency_overrides[get_current_user] = current_user
    app.dependency_overrides[data_library_routes.get_data_library_service] = lambda: env.service
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        url = "/api/v1/data-library/folders/" + root.id
        preview = await client.get(url + "/deletion-preview")
        assert preview.status_code == 200 and "generation" not in preview.text
        token = preview.json()["data"]["deletion_token"]
        assert (await client.delete(url)).status_code == 422
        assert (await client.delete(url, params={"deletion_token": "0" * 64})).status_code == 409
        assert (await client.delete(url, params={"deletion_token": token}, headers={"x-test-user": "bob"})).status_code == 404
        assert (await client.delete("/api/v1/data-library/folders/root", params={"deletion_token": token})).status_code == 422
        response = await client.delete(url, params={"deletion_token": token})
        assert response.status_code == 200 and response.json()["data"]["completed"]
        assert (await client.delete(url, params={"deletion_token": token})).json()["data"]["deleted_folder_count"] == 0
