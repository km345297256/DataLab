"""Real-Mongo checks for concurrent file-level removal and retained references."""
import asyncio

import pytest

from app.application.services.data_library_service import DataLibraryService
from app.domain.models.data_library import DeleteLibraryFiles
from test_data_library_queries_mongo import asset, file, repo


def deletion(*pairs):
    return DeleteLibraryFiles(files=[{"asset_id": asset_id, "file_id": file_id} for asset_id, file_id in pairs])


@pytest.mark.asyncio
async def test_file_removal_preserves_unselected_references_aliases_and_owner_boundary(repo):
    saved = asset(1, [file("same", "data.csv"), file("gridfs:same", "data.csv"), file("keep", "keep.csv")])
    other = asset(2, [file("same", "other.csv")])
    await repo.insert("alice", saved)
    await repo.insert("alice", other)
    await repo.insert("bob", saved)
    await repo.retain_file("alice", "same", saved["id"])
    await repo.retain_file("alice", "same", other["id"])
    await repo.set_context("alice", "session", ["same", "keep"])
    service = DataLibraryService(repo, None, None)

    result = await service.delete_files("alice", deletion((saved["id"], "gridfs:same")))
    assert result.deleted_count == 1
    assert (await repo.get("alice", saved["id"]))["files"] == [file("keep", "keep.csv")]
    assert await repo.get("alice", other["id"]) == other
    assert await repo.get("bob", saved["id"]) == saved
    assert await repo.get_context("alice", "session") == ["same", "keep"]
    assert not await repo.begin_file_delete("alice", "same")
    missing = await service.delete_files("charlie", deletion((saved["id"], "keep")))
    assert missing.results[0].status == "not_found"


@pytest.mark.asyncio
async def test_last_file_removal_keeps_legacy_folders_and_deduplicates_concurrent_requests(repo):
    saved = asset(3, [{**file("only", "data.csv"), "relative_path": "研究/原始/data.csv"}])
    await repo.insert("alice", saved)
    service = DataLibraryService(repo, None, None)
    results = await asyncio.gather(*(
        service.delete_files("alice", deletion((saved["id"], "only"))) for _ in range(8)
    ))
    assert sum(result.deleted_count for result in results) == 1
    assert await repo.get("alice", saved["id"]) is None
    assert {folder["path"] for folder in await repo.list_folders("alice")} == {"研究", "研究/原始"}


@pytest.mark.asyncio
async def test_concurrent_acquisition_append_and_deletion_never_lose_unselected_files(repo):
    for index in range(10, 25):
        saved = {**asset(index, [file("old", "old.csv")], source_type="open_data"), "source_url": None}
        await repo.merge_acquisition("alice", saved)
        await repo.retain_file("alice", "old", saved["id"])
        await repo.retain_file("alice", "new", saved["id"])
        incoming = {**saved, "files": [file("new", "new.csv")]}
        service = DataLibraryService(repo, None, None)
        await asyncio.gather(
            service.delete_files("alice", deletion((saved["id"], "old"))),
            repo.merge_acquisition("alice", incoming),
        )
        assert (await repo.get("alice", saved["id"]))["files"] == incoming["files"]
    assert not await repo.begin_file_delete("alice", "new")


@pytest.mark.asyncio
async def test_concurrent_resave_into_same_asset_keeps_retention_reservation(repo, monkeypatch):
    saved = {**asset(40, [file("same", "data.csv")], source_type="open_data"), "source_url": None}
    await repo.merge_acquisition("alice", saved)
    await repo.retain_file("alice", "same", saved["id"])
    original_remove = repo.remove_file

    async def remove_then_resave(owner, asset_id, file_id):
        result = await original_remove(owner, asset_id, file_id)
        assert await repo.retain_file(owner, file_id, asset_id)
        await repo.merge_acquisition(owner, saved)
        return result

    monkeypatch.setattr(repo, "remove_file", remove_then_resave)
    result = await DataLibraryService(repo, None, None).delete_files("alice", deletion((saved["id"], "same")))
    assert result.deleted_count == 1
    assert (await repo.get("alice", saved["id"]))["files"] == saved["files"]
    assert not await repo.begin_file_delete("alice", "same")
