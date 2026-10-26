"""Folder persistence and directory queries in a disposable real Mongo DB."""
import asyncio
import copy

import pytest

from app.application.errors.exceptions import ConflictError, NotFoundError
from app.application.services.data_library_service import DataLibraryService
from app.domain.models.data_library import CreateLibraryFolder, library_folder_id
from app.infrastructure.external.file.retainedfile import RetainedFileStorage
from test_data_library import Files, request
from test_data_library_queries_mongo import BASE, asset, file, repo


def saved_file(file_id, name, path=None):
    return {**file(file_id, name), **({"relative_path": path} if path is not None else {})}


@pytest.mark.asyncio
async def test_legacy_folders_backfill_all_pages_without_mutating_assets_or_exposing_storage(repo):
    original = asset(1, [
        saved_file("root", "README.md"),
        saved_file("windows", r"C:\private\root.csv"),
        saved_file("normal", "table.csv", "实验/原始/2026/table.csv"),
        saved_file("neighbor", "notes.md", "实验/notes.md"),
        saved_file("bad1", "bad.csv", "/private/hidden/bad.csv"),
        saved_file("bad2", "bad.csv", "../secret/bad.csv"),
    ])
    await repo.insert("alice", original)
    for index in range(2, 55):
        await repo.insert("alice", asset(index, [saved_file(f"file-{index}", "data.csv", f"批次{index}/data.csv")]))
    await repo.insert("bob", asset(100, [saved_file("secret", "data.csv", "bob-only/data.csv")]))
    await repo.collection.insert_one({"_id": repo.prefix("alice") + "forged", "owner_id": "bob",
                                      "payload": asset(101, [saved_file("forged", "data.csv", "forged/data.csv")])})
    first = await repo.page("alice")
    assert len(first) == 21 and original["id"] not in {row["id"] for row in first}
    folders = await repo.list_folders("alice")
    paths = {row["path"] for row in folders}
    assert paths == {"实验", "实验/原始", "实验/原始/2026"} | {f"批次{index}" for index in range(2, 55)}
    deepest = next(row for row in folders if row["path"] == "实验/原始/2026")
    assert deepest["parent_id"] == library_folder_id("alice", "实验/原始")
    assert deepest["id"] == library_folder_id("alice", "实验/原始/2026")
    assert deepest["created_at"] == deepest["updated_at"] == BASE
    assert await repo.get("alice", original["id"]) == original
    assert await repo.list_folders("alice") == folders
    assert [row["path"] for row in await repo.list_folders("bob")] == ["bob-only"]
    serialized = str(folders)
    for forbidden in ("/private", "secret", "forged", "C:", "owner_id", "storage"):
        assert forbidden not in serialized


@pytest.mark.asyncio
async def test_empty_directory_creation_is_atomic_owner_scoped_and_restart_safe(repo):
    service = DataLibraryService(repo, None, None)
    results = await asyncio.gather(*(
        service.create_folder("alice", CreateLibraryFolder(name="研究")) for _ in range(8)
    ), return_exceptions=True)
    successes = [result for result in results if not isinstance(result, Exception)]
    failures = [result for result in results if isinstance(result, Exception)]
    assert len(successes) == 1 and len(failures) == 7
    assert all(isinstance(error, ConflictError) for error in failures)
    root = successes[0]
    child = await service.create_folder("alice", CreateLibraryFolder(name="empty", parent_id=root.id))
    restarted = DataLibraryService(type(repo)(repo.database), None, None)
    assert {folder.path for folder in (await restarted.list_folders("alice")).items} == {"研究", "研究/empty"}
    assert await repo.collection.count_documents({}) == 0
    assert (await restarted.list("alice", folder_id=child.id)).items == []
    bob = await restarted.create_folder("bob", CreateLibraryFolder(name="研究"))
    assert bob.id != root.id
    with pytest.raises(NotFoundError):
        await restarted.create_folder("bob", CreateLibraryFolder(name="x", parent_id=root.id))
    with pytest.raises(NotFoundError):
        await restarted.list("bob", folder_id=root.id)
    # An inconsistent document must fail both the owner and namespace boundary.
    forged = copy.deepcopy(root.model_dump())
    forged["id"] = "a" * 32
    forged["path"] = forged["name"] = "forged"
    await repo.folders.insert_one({"_id": repo.folder_prefix("bob") + forged["id"], "owner_id": "alice", "payload": forged})
    await repo.folders.insert_one({"_id": repo.folder_prefix("alice") + "b" * 32, "owner_id": "bob", "payload": forged})
    assert all(folder.path != "forged" for folder in (await restarted.list_folders("alice")).items)
    assert all(folder.path != "forged" for folder in (await restarted.list_folders("bob")).items)


@pytest.mark.asyncio
async def test_folder_and_file_filters_match_same_direct_file_before_paging(repo):
    for index in range(1, 26):
        await repo.insert("alice", asset(index, [
            saved_file(f"target-{index}", f"Needle{index}.CSV", f"a[1]/Needle{index}.CSV"),
            saved_file(f"sidecar-{index}", "metadata.json", "another/metadata.json"),
        ]))
    for index in range(30, 65):
        await repo.insert("alice", asset(index, [saved_file(f"elsewhere-{index}", "needle.csv", "elsewhere/needle.csv")]))
    # No single file matches all three conditions in any of these records.
    await repo.insert("alice", asset(70, [saved_file("name", "needle.tsv", "a[1]/needle.tsv"), saved_file("fmt", "other.csv", "a[1]/other.csv")]))
    await repo.insert("alice", asset(71, [saved_file("name-format", "needle.csv", "a1/needle.csv"), saved_file("folder", "other.tsv", "a[1]/other.tsv")]))
    await repo.insert("alice", asset(72, [saved_file("deep", "needle.csv", "a[1]/nested/needle.csv")]))
    await repo.insert("bob", asset(73, [saved_file("foreign", "needle.csv", "a[1]/needle.csv")]))
    first = await repo.page("alice", folder_path="a[1]", filename="NEEDLE", file_format="csv")
    assert len(first) == 21
    second = await repo.page("alice", folder_path="a[1]", filename="NEEDLE", file_format="csv", cursor=first[19]["id"])
    found = first[:20] + second
    assert {int(row["id"], 16) for row in found} == set(range(1, 26))
    assert len(found) == 25 and all(len(row["files"]) == 2 for row in found)
    assert all(row["files"][1]["relative_path"] == "another/metadata.json" for row in found)
    assert [int(row["id"], 16) for row in await repo.page("alice", folder_path="a[1]/nested")] == [72]


@pytest.mark.asyncio
async def test_virtual_root_and_legacy_no_path_files_are_compatible(repo):
    await repo.insert("alice", asset(1, [saved_file("old", "plain.csv")]))
    await repo.insert("alice", asset(2, [saved_file("root", "root.csv", "root.csv")]))
    await repo.insert("alice", asset(3, [saved_file("child", "child.csv", "dir/child.csv")]))
    await repo.insert("alice", asset(4, [saved_file("both-root", "other.tsv"), saved_file("both-child", "needle.csv", "dir/needle.csv")]))
    await repo.insert("bob", asset(5, [saved_file("secret", "secret.csv")]))
    assert {int(row["id"], 16) for row in await repo.page("alice", folder_path="")} == {1, 2, 4}
    assert {int(row["id"], 16) for row in await repo.page("alice", folder_path="", file_format="csv")} == {1, 2}
    assert await repo.page("alice", folder_path="", filename="needle") == []
    assert {int(row["id"], 16) for row in await repo.page("alice")} == {1, 2, 3, 4}


@pytest.mark.asyncio
async def test_legacy_folder_conflict_and_target_save_materialize_relative_descendants(repo):
    files = Files()
    info = files.add()
    retained = RetainedFileStorage(files, repo)
    service = DataLibraryService(repo, retained, None)
    legacy = await service.create("alice", request(file_paths={"file-1": "existing/deep/table.csv"}))
    with pytest.raises(ConflictError):
        await service.create_folder("alice", CreateLibraryFolder(name="existing"))
    target = await service.owned_folder("alice", library_folder_id("alice", "existing/deep"))
    nested = await service.create("alice", request(target_folder_id=target.id, file_paths={"file-1": "more/data/table.csv"}))
    assert nested.files[0].relative_path == "existing/deep/more/data/table.csv"
    folders = (await service.list_folders("alice")).items
    assert {folder.path for folder in folders} == {"existing", "existing/deep", "existing/deep/more", "existing/deep/more/data"}
    deepest = next(folder for folder in folders if folder.path.endswith("more/data"))
    listed = await service.list("alice", folder_id=deepest.id)
    assert [entry.id for entry in listed.items] == [nested.id]
    assert (await service.list("alice", folder_id=target.id)).items[0].id == legacy.id
    assert files.infos["file-1"] == info and not files.deletions
    # Empty folders are durable metadata even when their last entry is removed.
    await service.delete("alice", nested.id)
    assert deepest.id in {folder.id for folder in (await service.list_folders("alice")).items}
    assert (await service.list("alice", folder_id=deepest.id)).items == []


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True])
async def test_existing_saved_file_paths_conflict_only_within_same_owner_and_directory(repo, nested):
    files = Files()
    files.add()
    files.add("file-2")
    files.add("bob-file", owner="bob")
    service = DataLibraryService(repo, RetainedFileStorage(files, repo), None)
    target = await service.create_folder("alice", CreateLibraryFolder(name="data")) if nested else None
    first = await service.create("alice", request(target_folder_id=target.id if target else None))
    for file_id in ("file-1", "file-2"):
        with pytest.raises(ConflictError, match="已有同名文件"):
            await service.create("alice", request(target_folder_id=target.id if target else None, file_ids=[file_id]))
    assert await repo.collection.count_documents({"owner_id": "alice"}) == 1
    # The existing asset stays protected; the failed new file claim is released.
    assert await repo.begin_file_delete("alice", "file-1") is False
    assert await repo.begin_file_delete("alice", "file-2") is True
    await repo.finish_file_delete("alice", "file-2", deleted=False)
    other = await service.create_folder("alice", CreateLibraryFolder(name="other"))
    second = await service.create("alice", request(target_folder_id=other.id, file_ids=["file-2"]))
    bob_target = await service.create_folder("bob", CreateLibraryFolder(name="data")) if nested else None
    bob_saved = await service.create("bob", request(target_folder_id=bob_target.id if bob_target else None, file_ids=["bob-file"]))
    assert second.files[0].relative_path == "other/table.csv"
    assert bob_saved.files[0].relative_path == first.files[0].relative_path
    assert not files.deletions
