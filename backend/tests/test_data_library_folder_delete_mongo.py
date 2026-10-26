"""Recursive folder deletion against a disposable real Mongo database."""
import asyncio

import pytest

from app.application.errors.exceptions import ConflictError
from app.application.services.data_library_service import DataLibraryService
from app.domain.models.data_library import CreateLibraryFolder, library_folder_id
from app.infrastructure.external.file.retainedfile import RetainedFileStorage
from test_data_library import Files, request
from test_data_library_queries_mongo import asset, file, repo


def located(file_id, path):
    return {**file(file_id, path.rsplit("/", 1)[-1]), "relative_path": path}


@pytest.mark.asyncio
async def test_recursive_delete_covers_hidden_pages_but_preserves_other_paths_owners_and_references(repo):
    service = DataLibraryService(repo, None, None)
    for index in range(30):
        saved = asset(index, [located("shared", "a[1]/nested/data.csv"), located("shared", "a1/data.csv"),
                              located("shared", "a[1]-backup/data.csv")])
        await repo.insert("alice", saved)
        await repo.retain_file("alice", "shared", saved["id"])
    foreign = asset(200, [located("foreign", "a[1]/private.csv")])
    await repo.insert("bob", foreign)
    forged_id = repo.prefix("bob") + "forged"
    await repo.collection.insert_one({"_id": forged_id, "owner_id": "alice", "payload": foreign})
    await repo.set_context("alice", "session", ["shared"])
    root_id = library_folder_id("alice", "a[1]")
    await service.list_folders("alice")
    await service.create_folder("alice", CreateLibraryFolder(name="empty", parent_id=root_id))
    preview = await service.folder_deletion_preview("alice", root_id)
    assert preview.file_count == 30 and preview.subfolder_count == 2
    assert "generation" not in preview.model_dump_json()
    result = await service.delete_folder("alice", root_id, preview.deletion_token)
    assert result.completed and result.deleted_file_count == 30 and result.deleted_folder_count == 3
    assert result.remaining_file_count == result.remaining_folder_count == 0
    for index in range(30):
        kept = await repo.get("alice", f"{index:032x}")
        assert [entry["relative_path"] for entry in kept["files"]] == ["a1/data.csv", "a[1]-backup/data.csv"]
    assert await repo.get("bob", foreign["id"]) == foreign
    assert (await repo.collection.find_one({"_id": forged_id}))["payload"] == foreign
    assert await repo.get_context("alice", "session") == ["shared"]
    assert not await repo.begin_file_delete("alice", "shared")
    assert {folder.path for folder in (await service.list_folders("alice")).items} == {"a1", "a[1]-backup"}
    assert (await service.delete_folder("alice", root_id, preview.deletion_token)).deleted_folder_count == 0


@pytest.mark.asyncio
async def test_implicit_recreation_from_same_old_asset_gets_a_new_token(repo):
    saved = {**asset(500, [located("inside", "data/file.csv"), located("outside", "other/file.csv")]), "source_url": None}
    await repo.merge_acquisition("alice", saved)
    service = DataLibraryService(repo, None, None)
    root_id = library_folder_id("alice", "data")
    first = await service.folder_deletion_preview("alice", root_id)
    assert (await service.delete_folder("alice", root_id, first.deletion_token)).completed
    # The asset and its old created_at survive because it also contains an
    # outside reference. A later acquisition merge creates a new logical folder.
    await repo.merge_acquisition("alice", saved)
    second = await service.folder_deletion_preview("alice", root_id)
    assert first.folder.created_at == second.folder.created_at
    assert first.deletion_token != second.deletion_token
    old_retry = await service.delete_folder("alice", root_id, first.deletion_token)
    assert old_retry.completed and old_retry.deleted_file_count == 0
    assert (await repo.folder_subtree_counts("alice", "data"))["file_count"] == 1
    assert (await service.delete_folder("alice", root_id, second.deletion_token)).deleted_file_count == 1


@pytest.mark.asyncio
async def test_legacy_folder_gets_one_persistent_private_incarnation(repo):
    service = DataLibraryService(repo, None, None)
    saved = asset(501, [located("inside", "legacy/file.csv")])
    await repo.insert("alice", saved)
    await repo.list_folders("alice")
    root_id = library_folder_id("alice", "legacy")
    await repo.folders.update_one({"_id": repo.folder_prefix("alice") + root_id}, {"$unset": {"payload.generation": ""}})
    first = await service.folder_deletion_preview("alice", root_id)
    second = await DataLibraryService(type(repo)(repo.database), None, None).folder_deletion_preview("alice", root_id)
    assert first.deletion_token == second.deletion_token
    stored = await repo.get_folder("alice", root_id)
    assert len(stored["generation"]) == 32
    assert "generation" not in first.model_dump_json()


@pytest.mark.asyncio
async def test_partial_mongo_failure_keeps_root_and_retry_removes_remaining_files(repo, monkeypatch):
    await repo.insert("alice", asset(1, [located("one", "data/one.csv")]))
    await repo.insert("alice", asset(2, [located("two", "data/sub/two.csv")]))
    service = DataLibraryService(repo, None, None)
    preview = await service.folder_deletion_preview("alice", library_folder_id("alice", "data"))
    original = repo.remove_folder_contents

    async def fail_after_first(owner, folder, progress):
        await repo.collection.delete_one({"_id": repo.prefix(owner) + f"{1:032x}", "owner_id": owner})
        progress["deleted_file_count"] = 1
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(repo, "remove_folder_contents", fail_after_first)
    result = await service.delete_folder("alice", preview.folder.id, preview.deletion_token)
    assert not result.completed and result.deleted_file_count == 1 and result.remaining_file_count == 1
    assert await repo.get_folder("alice", preview.folder.id)
    monkeypatch.setattr(repo, "remove_folder_contents", original)
    retry = await service.delete_folder("alice", preview.folder.id, preview.deletion_token)
    assert retry.completed and retry.deleted_file_count == 1 and retry.deleted_folder_count == 2
    assert await repo.collection.count_documents({"owner_id": "alice"}) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
async def test_delayed_validation_cannot_resurrect_deleted_folder(repo, explicit):
    files = Files()
    files.add()
    service = DataLibraryService(repo, RetainedFileStorage(files, repo), None)
    folder = await service.create_folder("alice", CreateLibraryFolder(name="target"))
    preview = await service.folder_deletion_preview("alice", folder.id)
    reading, resume = asyncio.Event(), asyncio.Event()
    original_read = files.download_file_range

    async def delayed(*args, **kwargs):
        reading.set()
        await resume.wait()
        return await original_read(*args, **kwargs)

    files.download_file_range = delayed
    save = request(target_folder_id=folder.id) if explicit else request(file_paths={"file-1": "target/table.csv"})
    pending = asyncio.create_task(service.create("alice", save))
    await reading.wait()
    assert (await DataLibraryService(type(repo)(repo.database), None, None).delete_folder(
        "alice", folder.id, preview.deletion_token,
    )).completed
    resume.set()
    with pytest.raises(ConflictError):
        await pending
    assert await repo.collection.count_documents({"owner_id": "alice"}) == 0
    assert (await service.list_folders("alice")).items == []


@pytest.mark.asyncio
async def test_cancelled_commit_settles_before_delete_can_acquire_gate(repo, monkeypatch):
    files = Files()
    files.add()
    service = DataLibraryService(repo, RetainedFileStorage(files, repo), None)
    folder = await service.create_folder("alice", CreateLibraryFolder(name="target"))
    preview = await service.folder_deletion_preview("alice", folder.id)
    entered, resume = asyncio.Event(), asyncio.Event()
    original_insert = repo.insert

    async def delayed_insert(*args, **kwargs):
        entered.set()
        await resume.wait()
        return await original_insert(*args, **kwargs)

    monkeypatch.setattr(repo, "insert", delayed_insert)
    creating = asyncio.create_task(service.create("alice", request(target_folder_id=folder.id)))
    await entered.wait()
    creating.cancel()
    deleting = asyncio.create_task(service.delete_folder("alice", folder.id, preview.deletion_token))
    await asyncio.sleep(0)
    assert not deleting.done() and not creating.done()
    resume.set()
    with pytest.raises(asyncio.CancelledError):
        await creating
    result = await deleting
    assert result.completed and result.deleted_file_count == 1
    assert await repo.collection.count_documents({"owner_id": "alice"}) == 0
    assert (await service.list_folders("alice")).items == []


@pytest.mark.asyncio
async def test_lazy_backfill_cannot_resurrect_directory_after_deletion(repo, monkeypatch):
    await repo.insert("alice", asset(1, [located("one", "data/sub/one.csv")]))
    service = DataLibraryService(repo, None, None)
    preview = await service.folder_deletion_preview("alice", library_folder_id("alice", "data"))
    entered, resume = asyncio.Event(), asyncio.Event()
    original = repo._saved_paths
    first = True

    async def delayed_paths(owner):
        nonlocal first
        async for item in original(owner):
            if first:
                first = False
                entered.set()
                await resume.wait()
            yield item

    monkeypatch.setattr(repo, "_saved_paths", delayed_paths)
    listing = asyncio.create_task(repo.list_folders("alice"))
    await entered.wait()
    deleting = asyncio.create_task(service.delete_folder("alice", preview.folder.id, preview.deletion_token))
    await asyncio.sleep(0)
    assert not deleting.done()
    resume.set()
    await listing
    assert (await deleting).completed
    assert (await service.list_folders("alice")).items == []
