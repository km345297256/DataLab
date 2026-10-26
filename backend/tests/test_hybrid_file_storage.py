import asyncio
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.models.file import FileInfo
from app.infrastructure.external.file.hybridfile import HybridFileStorage
from app.infrastructure.external.file.miniofile import (
    _complete_despite_cancellation,
    _minio_metadata,
)


class _FakeStorage:
    def __init__(self, name: str, *, object_count: int = 0, total_bytes: int = 0, available: bool = True):
        self.name = name
        self.object_count = object_count
        self.total_bytes = total_bytes
        self.available = available
        self.calls = []

    async def upload_file(self, file_data, filename, user_id, content_type=None, metadata=None):
        self.calls.append(("upload", filename, user_id, content_type, metadata))
        return FileInfo(file_id=f"{self.name}:new", filename=filename, user_id=user_id)

    async def download_file(self, file_id, user_id=None):
        self.calls.append(("download", file_id, user_id))
        return io.BytesIO(b"data"), FileInfo(file_id=file_id, filename="file.txt", user_id=user_id or "")

    async def download_file_range(self, file_id, user_id=None, *, offset, length):
        self.calls.append(("range", file_id, user_id, offset, length))
        return b"data"[offset:offset + length], FileInfo(
            file_id=file_id,
            filename="file.txt",
            user_id=user_id or "",
        )

    async def delete_file(self, file_id, user_id):
        self.calls.append(("delete", file_id, user_id))
        return True

    async def get_file_info(self, file_id, user_id=None):
        self.calls.append(("info", file_id, user_id))
        return FileInfo(file_id=file_id, filename="file.txt", user_id=user_id or "")

    async def storage_usage(self):
        return {
            "provider": self.name,
            "object_count": self.object_count,
            "total_bytes": self.total_bytes,
        }

    async def health_check(self):
        return {
            "provider": self.name,
            "available": self.available,
        }


@pytest.fixture
def configured_storage_factory(monkeypatch):
    from app.infrastructure.external.file import factory, datasetfile, retainedfile, miniofile
    from app.infrastructure.repositories import mongo_data_library_repository

    minio, gridfs = _FakeStorage("minio"), _FakeStorage("gridfs")
    minio.init_large_upload = AsyncMock(return_value="multipart-session")
    factory.get_file_storage.cache_clear()
    monkeypatch.setattr(factory, "get_mongodb", lambda: object())
    monkeypatch.setattr(factory, "GridFSFileStorage", lambda **kwargs: gridfs)
    monkeypatch.setattr(miniofile, "MinIOFileStorage", lambda: minio)
    monkeypatch.setattr(datasetfile, "DatasetPreviewFileStorage", lambda storage: storage)
    monkeypatch.setattr(retainedfile, "RetainedFileStorage", lambda storage, repository: storage)
    monkeypatch.setattr(mongo_data_library_repository, "MongoDataLibraryRepository", lambda: object())
    yield factory, minio, gridfs
    factory.get_file_storage.cache_clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", [None, "", "hybrid", "gridfs", "GRIDFS"])
async def test_factory_legacy_setting_keeps_reads_but_all_new_writes_are_objects(configured_storage_factory, monkeypatch, provider):
    factory, minio, gridfs = configured_storage_factory
    monkeypatch.setattr(factory, "get_settings", lambda: SimpleNamespace(file_storage_provider=provider))
    storage = factory.get_file_storage()
    assert isinstance(storage, HybridFileStorage)
    info = await storage.upload_file(io.BytesIO(b"data"), "input.csv", "owner")
    assert info.file_id == "minio:new"
    assert await storage.init_large_upload("large.csv", "owner", 8_000_000) == "multipart-session"
    minio.init_large_upload.assert_awaited_once_with("large.csv", "owner", 8_000_000)
    legacy_id = "507f1f77bcf86cd799439011"
    stream, _ = await storage.download_file("gridfs:" + legacy_id, "owner")
    stream.close()
    assert gridfs.calls == [("download", legacy_id, "owner")]
    assert minio.calls == [("upload", "input.csv", "owner", None, None)]


def test_factory_explicit_minio_has_no_legacy_write_path(configured_storage_factory, monkeypatch):
    factory, minio, _ = configured_storage_factory
    monkeypatch.setattr(factory, "get_settings", lambda: SimpleNamespace(file_storage_provider="minio"))
    assert factory.get_file_storage() is minio


def test_factory_unknown_provider_fails_closed(configured_storage_factory, monkeypatch):
    factory, _, _ = configured_storage_factory
    monkeypatch.setattr(factory, "get_settings", lambda: SimpleNamespace(file_storage_provider="disk-cache"))
    with pytest.raises(ValueError, match="Unsupported FILE_STORAGE_PROVIDER"):
        factory.get_file_storage()


def test_minio_metadata_encodes_non_ascii_values_for_headers():
    metadata = _minio_metadata({
        "filename": "【DDL下载】UMT-OAuth2-SDK-JAVA版使用说明-2015-01-20.docx文件等.zip",
        "source": "user_upload",
    })

    assert metadata["source"] == "user_upload"
    metadata["filename"].encode("ascii")
    assert "\\u3010DDL\\u4e0b\\u8f7d\\u3011" in metadata["filename"]


def test_minio_metadata_limits_total_header_size():
    metadata = _minio_metadata({
        "filename": "large.txt",
        **{f"large-{index}": "x" * 2000 for index in range(20)},
    })

    total_size = sum(len(key.encode("ascii")) + len(value.encode("ascii")) for key, value in metadata.items())

    assert metadata["filename"] == "large.txt"
    assert total_size <= 1800


@pytest.mark.asyncio
async def test_hybrid_download_routes_minio_ids_to_minio_storage():
    minio = _FakeStorage("minio")
    gridfs = _FakeStorage("gridfs")
    storage = HybridFileStorage(minio, gridfs)

    await storage.download_file("minio:abc123", "user-1")

    assert minio.calls == [("download", "minio:abc123", "user-1")]
    assert gridfs.calls == []


@pytest.mark.asyncio
async def test_hybrid_download_routes_prefixed_gridfs_ids_to_gridfs_storage():
    minio = _FakeStorage("minio")
    gridfs = _FakeStorage("gridfs")
    storage = HybridFileStorage(minio, gridfs)

    await storage.download_file("gridfs:507f1f77bcf86cd799439011", "user-1")

    assert gridfs.calls == [("download", "507f1f77bcf86cd799439011", "user-1")]
    assert minio.calls == []


@pytest.mark.asyncio
async def test_hybrid_download_routes_legacy_object_ids_to_gridfs_storage():
    minio = _FakeStorage("minio")
    gridfs = _FakeStorage("gridfs")
    storage = HybridFileStorage(minio, gridfs)

    await storage.download_file("507f1f77bcf86cd799439011", "user-1")

    assert gridfs.calls == [("download", "507f1f77bcf86cd799439011", "user-1")]
    assert minio.calls == []


@pytest.mark.asyncio
async def test_hybrid_download_routes_unknown_new_ids_to_minio_storage():
    minio = _FakeStorage("minio")
    gridfs = _FakeStorage("gridfs")
    storage = HybridFileStorage(minio, gridfs)

    await storage.download_file("future-provider:abc123", "user-1")

    assert minio.calls == [("download", "future-provider:abc123", "user-1")]
    assert gridfs.calls == []


@pytest.mark.asyncio
async def test_hybrid_range_read_routes_without_full_download():
    minio = _FakeStorage("minio")
    gridfs = _FakeStorage("gridfs")
    storage = HybridFileStorage(minio, gridfs)

    data, _ = await storage.download_file_range(
        "minio:abc123",
        "user-1",
        offset=1,
        length=2,
    )

    assert data == b"at"
    assert minio.calls == [("range", "minio:abc123", "user-1", 1, 2)]
    assert gridfs.calls == []


@pytest.mark.asyncio
async def test_external_write_finishes_before_cancellation_is_propagated():
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def operation():
        started.set()
        await release.wait()
        finished.set()
        return "done"

    waiter = asyncio.create_task(_complete_despite_cancellation(operation()))
    await started.wait()
    waiter.cancel()
    await asyncio.sleep(0)

    assert waiter.done() is False
    release.set()
    result, cancellation = await waiter
    assert result == "done"
    assert isinstance(cancellation, asyncio.CancelledError)
    assert finished.is_set()


@pytest.mark.asyncio
async def test_hybrid_storage_usage_combines_minio_and_gridfs_totals():
    minio = _FakeStorage("minio", object_count=2, total_bytes=200)
    gridfs = _FakeStorage("gridfs", object_count=3, total_bytes=300)
    storage = HybridFileStorage(minio, gridfs)

    usage = await storage.storage_usage()

    assert usage["provider"] == "hybrid"
    assert usage["object_count"] == 5
    assert usage["total_bytes"] == 500
    assert usage["backends"]["minio"]["object_count"] == 2
    assert usage["backends"]["gridfs"]["object_count"] == 3


@pytest.mark.asyncio
async def test_hybrid_health_requires_both_backends_available():
    minio = _FakeStorage("minio", available=True)
    gridfs = _FakeStorage("gridfs", available=False)
    storage = HybridFileStorage(minio, gridfs)

    health = await storage.health_check()

    assert health["provider"] == "hybrid"
    assert health["available"] is False
    assert health["backends"]["minio"]["available"] is True
    assert health["backends"]["gridfs"]["available"] is False
