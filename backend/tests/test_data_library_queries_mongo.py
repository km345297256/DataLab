"""Real Mongo aggregation/query verification, confined to a disposable DB."""
import asyncio
from datetime import UTC, datetime, timedelta
import os
from uuid import uuid4

import pytest
import pytest_asyncio
from pymongo.asynchronous.mongo_client import AsyncMongoClient

from app.infrastructure.models.documents import SessionDocument
from app.infrastructure.repositories.mongo_data_library_repository import MongoDataLibraryRepository


@pytest_asyncio.fixture
async def repo(monkeypatch):
    uri = os.environ.get("DATA_LIBRARY_TEST_MONGODB_URI")
    if not uri:
        pytest.skip("Set DATA_LIBRARY_TEST_MONGODB_URI for isolated real-Mongo library verification")
    database_name = "datalab_library_query_test_" + uuid4().hex
    client = AsyncMongoClient(uri, serverSelectionTimeoutMS=3000, tz_aware=True)
    def reject_application_collection(*args, **kwargs):
        raise AssertionError("Library tests must not use the application database")
    monkeypatch.setattr(SessionDocument, "get_pymongo_collection", reject_application_collection)
    try:
        yield MongoDataLibraryRepository(client[database_name])
    finally:
        # The database name is newly generated here and never derived from a
        # deployed connection's database. No production collections are touched.
        await client.drop_database(database_name)
        await client.close()


BASE = datetime(2026, 10, 20, tzinfo=UTC)


def file(file_id, filename, size=10):
    return {"file_id": file_id, "filename": filename, "size": size}


def asset(index, files, *, updated_at=BASE, name="research", source_type="upload"):
    return {"id": f"{index:032x}", "name": name, "description": "ocean observations", "tags": ["climate"],
            "source_type": source_type, "files": files, "created_at": BASE, "updated_at": updated_at}


@pytest.mark.asyncio
async def test_stats_counts_full_owner_library_and_canonical_aliases_without_file_storage(repo):
    assert await repo.stats("alice") == {"file_count": 0, "total_bytes": 0, "updated_at": None, "formats": []}
    for index in range(36):
        await repo.insert("alice", asset(index, [file(f"file-{index}", f"data{index}.CSV", index)]))
    # Uppercase compound suffixes and hidden/extensionless names follow the
    # same format rule as the file filter.
    for index, name in enumerate(["brain.NII.GZ", "bundle.TAR.GZ", "readme", ".env", "packed.gz"], start=36):
        await repo.insert("alice", asset(index, [file(f"file-{index}", name, index)]))
    # Newest saved metadata wins for the same immutable file; aliases do not
    # inflate either the count or the size.
    await repo.insert("alice", asset(99, [file("gridfs:file-0", "data0.CSV", 999)], updated_at=BASE + timedelta(hours=1)))
    await repo.insert("alice", asset(100, [], updated_at=BASE + timedelta(hours=2)))
    await repo.insert("bob", asset(101, [file("foreign", "private.h5", 9999999)], updated_at=BASE + timedelta(days=20)))
    # Both owner and key namespace must match, even for inconsistent legacy rows.
    await repo.collection.insert_one({"_id": repo.prefix("alice") + "forged", "owner_id": "bob", "payload": asset(102, [file("secret", "private.h5")])})
    await repo.collection.insert_one({"_id": repo.prefix("bob") + "forged", "owner_id": "alice", "payload": asset(103, [file("secret2", "private.h5")])})
    result = await repo.stats("alice")
    assert result == {"file_count": 41, "total_bytes": sum(range(41)) + 999,
                      "updated_at": BASE + timedelta(hours=2),
                      "formats": ["csv", "gz", "nii.gz", "none", "tar.gz"]}
    assert (await repo.stats("bob"))["file_count"] == 1


@pytest.mark.asyncio
async def test_filters_apply_before_paging_and_to_the_same_file_with_full_asset_results(repo):
    for index in range(25):
        await repo.insert("alice", asset(index, [file(f"file-{index}", f"Needle{index}.CSV"), file(f"sidecar-{index}", "metadata.json")]))
    for index in range(30, 75):
        await repo.insert("alice", asset(index, [file(f"other-{index}", "unrelated.csv")]))
    await repo.insert("alice", asset(100, [file("name-only", "needle.tsv"), file("format-only", "unrelated.csv")]))
    await repo.insert("bob", asset(101, [file("foreign", "needle.csv")]))
    first = await repo.page("alice", filename="NEEDLE", file_format="csv", query="climate", source_type="upload")
    assert len(first) == 21
    second = await repo.page("alice", filename="NEEDLE", file_format="csv", cursor=first[19]["id"])
    all_rows = first[:20] + second
    assert len(all_rows) == 25 and len({row["id"] for row in all_rows}) == 25
    assert all(len(row["files"]) == 2 for row in all_rows)
    assert all(row["files"][1]["filename"] == "metadata.json" for row in all_rows)
    assert await repo.page("alice", filename="needle", file_format="csv", source_type="analysis") == []
    assert await repo.page("alice", filename="needle", file_format="csv", query="unknown") == []


@pytest.mark.asyncio
async def test_literal_filename_compound_formats_and_half_open_date_window(repo):
    cases = [
        (1, "a[1].CSV", BASE), (2, "a1.CSV", BASE),
        (3, "brain.nii.gz", BASE), (4, "archive.tar.gz", BASE), (5, "archive.gz", BASE),
        (6, "README", BASE), (7, ".hidden", BASE), (8, "trailing.", BASE),
        (9, "earlier.csv", BASE - timedelta(milliseconds=1)),
        (10, "later.csv", BASE + timedelta(days=1)),
        (11, "last.csv", BASE + timedelta(days=1) - timedelta(milliseconds=1)),
    ]
    for index, name, updated in cases:
        await repo.insert("alice", asset(index, [file(str(index), name)], updated_at=updated))
    assert [row["id"] for row in await repo.page("alice", filename="[1]")] == [f"{1:032x}"]
    assert await repo.page("alice", filename=".*") == []
    for fmt, expected in (("nii.gz", {3}), ("tar.gz", {4}), ("gz", {5}), ("none", {6, 7, 8})):
        found = await repo.page("alice", file_format=fmt)
        assert {int(row["id"], 16) for row in found} == expected
    within = await repo.page("alice", file_format="csv", updated_from=BASE, updated_to=BASE + timedelta(days=1))
    assert {int(row["id"], 16) for row in within} == {1, 2, 11}
    assert await repo.page("alice", file_format="$csv") == []


@pytest.mark.asyncio
async def test_acquisition_merge_is_atomic_additive_owner_scoped_and_preserves_user_edits(repo):
    first = asset(500, [file("minio:first", "data.csv")], source_type="open_data")
    first["source_url"] = "https://zenodo.org/records/123"
    await repo.merge_acquisition("alice", first)
    await repo.update("alice", first["id"], {"name": "My chosen name", "tags": ["saved"]})
    second = {**first, "files": first["files"] + [file("minio:second", "metadata.json")],
              "updated_at": BASE + timedelta(hours=1)}
    await asyncio.gather(repo.merge_acquisition("alice", second), repo.merge_acquisition("alice", second))
    merged = await repo.get("alice", first["id"])
    assert merged["name"] == "My chosen name" and merged["tags"] == ["saved"]
    assert {item["file_id"] for item in merged["files"]} == {"minio:first", "minio:second"}
    assert len(merged["files"]) == 2 and merged["created_at"] == BASE
    await repo.merge_acquisition("bob", first)
    assert len((await repo.get("bob", first["id"]))["files"]) == 1


@pytest.mark.asyncio
async def test_task_data_locations_cover_all_pages_aliases_and_only_safe_owner_paths(repo):
    from app.domain.models.data_library import library_folder_id
    for index in range(25):
        await repo.insert("alice", asset(index, [{**file("shared", "table.csv"),
                                                "relative_path": f"项目{index}/table.csv"}]))
    await repo.insert("alice", asset(30, [file("gridfs:shared", "/private/source/table.csv")]))
    await repo.insert("alice", asset(31, [{**file("shared", "table.csv"), "relative_path": "/private/secret/table.csv"}]))
    await repo.insert("bob", asset(40, [{**file("shared", "table.csv"), "relative_path": "秘密/table.csv"}]))
    # A mismatched owner/prefix must not leak even when the immutable ID matches.
    await repo.collection.insert_one({"_id": repo.prefix("bob") + "bad", "owner_id": "alice",
                                      "payload": asset(41, [file("shared", "foreign.csv")])})
    result = await repo.file_locations("alice", ["gridfs:shared"])
    assert len(result["shared"]) == 26
    assert {entry["folder_path"] for entry in result["shared"]} == {"", *(f"项目{x}" for x in range(25))}
    nested = next(entry for entry in result["shared"] if entry["folder_path"] == "项目0")
    assert nested["folder_id"] == library_folder_id("alice", "项目0")
    assert nested["file_path"] == "项目0/table.csv"
    root = next(entry for entry in result["shared"] if not entry["folder_path"])
    assert root["folder_id"] is None and root["file_path"] == "table.csv"
    assert "private" not in str(result) and "秘密" not in str(result)
    assert await repo.file_locations("alice", []) == {}
    assert await repo.folders.count_documents({}) == 0  # read-only; no lazy folder writes


@pytest.mark.asyncio
async def test_task_data_receipts_require_exact_owner_session_and_service_namespace(repo):
    from app.infrastructure.repositories.mongo_data_candidate_repository import MongoDataCandidateRepository
    provenance = MongoDataCandidateRepository(repo.database["data_provenance"])
    for index in range(25):
        await provenance.save_acquisition("alice", "task", str(index), {"file_id": f"file-{index}"})
    await provenance.save_acquisition("bob", "task", "foreign", {"file_id": "foreign"})
    await provenance.save_acquisition("alice", "other", "previous", {"file_id": "previous"})
    await provenance.put("alice", "task", "candidate", "manual", {"file_id": "forged"})
    await provenance.collection.insert_one({
        "_id": provenance.prefix("alice", "task", "acquired_package") + "wrong-owner",
        "owner_id": "bob", "session_id": "task", "payload": {"file_id": "foreign2"}})
    await provenance.collection.insert_one({
        "_id": provenance.prefix("alice", "task", "acquired_package") + "wrong-task",
        "owner_id": "alice", "session_id": "other", "payload": {"file_id": "foreign3"}})
    receipts = await provenance.list_acquisitions("alice", "task")
    assert {row["file_id"] for row in receipts} == {f"file-{index}" for index in range(25)}
