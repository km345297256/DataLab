from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.config import Settings
from app.domain.models.object_file import ObjectFileMountError, ObjectFileReference
from app.infrastructure.external.file import miniofile
from app.infrastructure.external.file.datasetfile import DatasetPreviewFileStorage
from app.infrastructure.external.file.gridfsfile import GridFSFileStorage
from app.infrastructure.external.file.hybridfile import HybridFileStorage


def make_storage(monkeypatch, **overrides):
    doc = SimpleNamespace(
        file_id="minio:abc123", user_id="owner", provider="minio",
        bucket="private-bucket", object_key="files/users/owner/session/data.h5",
        size=4_800_000_000, filename="data.h5", content_type="application/octet-stream",
        metadata={"source": "data_connector"}, upload_date=datetime.now(UTC),
    )
    doc.__dict__.update(overrides)
    document = SimpleNamespace(file_id="file_id", find_one=AsyncMock(return_value=doc))
    monkeypatch.setattr(miniofile, "StoredFileDocument", document)
    client = SimpleNamespace(
        stat_object=Mock(return_value=SimpleNamespace(size=doc.size, etag="content-etag", version_id="version-1")),
        get_object=Mock(side_effect=AssertionError("mount resolution must not read file data")),
    )
    storage = miniofile.MinIOFileStorage.__new__(miniofile.MinIOFileStorage)
    storage._client, storage.bucket_name = client, "fallback-bucket"
    return storage, doc, document, client


@pytest.mark.asyncio
async def test_mount_reference_verifies_head_only_and_does_not_change_public_metadata(monkeypatch):
    storage, doc, _, client = make_storage(monkeypatch)
    before = storage._file_info_from_doc(doc).model_dump()
    reference = await storage.get_object_reference(doc.file_id, "owner")
    assert reference == ObjectFileReference(
        file_id=doc.file_id, bucket=doc.bucket, object_key=doc.object_key,
        size=doc.size, etag="content-etag", version_id="version-1",
    )
    client.stat_object.assert_called_once_with(doc.bucket, doc.object_key)
    client.get_object.assert_not_called()
    assert storage._file_info_from_doc(doc).model_dump() == before
    assert doc.bucket not in repr(reference) and doc.object_key not in repr(reference)
    assert "bucket" not in before and "object_key" not in before
    with pytest.raises(FrozenInstanceError):
        reference.size = 1


@pytest.mark.asyncio
@pytest.mark.parametrize("user_id", [None, "", " "])
async def test_mount_reference_requires_explicit_owner(monkeypatch, user_id):
    storage, doc, document, client = make_storage(monkeypatch)
    with pytest.raises(ObjectFileMountError, match="登录"):
        await storage.get_object_reference(doc.file_id, user_id)
    document.find_one.assert_not_awaited()
    client.stat_object.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [False, True])
async def test_unauthorized_and_missing_objects_fail_before_storage_head(monkeypatch, missing):
    storage, doc, document, client = make_storage(monkeypatch)
    if missing:
        document.find_one.return_value = None
    with pytest.raises(ObjectFileMountError, match="文件不存在或无权访问"):
        await storage.get_object_reference(doc.file_id, "other-owner")
    client.stat_object.assert_not_called()
    client.get_object.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("object_key", "../private"), ("object_key", "/absolute"),
    ("object_key", "a//b"), ("object_key", "a/./b"),
    ("object_key", "a/../b"), ("object_key", "a\\b"),
    ("object_key", "a\nb"), ("object_key", "a\x00b"),
    ("object_key", ""), ("object_key", None),
    ("bucket", "../other"), ("bucket", "a..b"),
    ("size", -1), ("size", True),
])
async def test_unsafe_mount_names_fail_before_head_without_disclosing_key(monkeypatch, field, value):
    storage, doc, _, client = make_storage(monkeypatch, **{field: value})
    with pytest.raises(ObjectFileMountError, match="存储信息无效") as error:
        await storage.get_object_reference(doc.file_id, "owner")
    assert "private-bucket" not in str(error.value)
    client.stat_object.assert_not_called()


@pytest.mark.asyncio
async def test_object_size_mismatch_blocks_mount(monkeypatch):
    storage, doc, _, client = make_storage(monkeypatch)
    client.stat_object.return_value.size -= 1
    with pytest.raises(ObjectFileMountError, match="大小与存储记录不一致"):
        await storage.get_object_reference(doc.file_id, "owner")
    client.get_object.assert_not_called()


@pytest.mark.asyncio
async def test_head_failure_does_not_expose_sdk_location(monkeypatch):
    storage, doc, _, client = make_storage(monkeypatch)
    client.stat_object.side_effect = RuntimeError("SECRET private-bucket/files/users/owner/session/data.h5")
    with pytest.raises(ObjectFileMountError, match="文件暂不可用") as error:
        await storage.get_object_reference(doc.file_id, "owner")
    assert "SECRET" not in str(error.value) and "private-bucket" not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.asyncio
async def test_incompatible_stored_provider_does_not_start_materialization(monkeypatch):
    storage, doc, _, client = make_storage(monkeypatch, provider="gridfs")
    with pytest.raises(ObjectFileMountError, match="尚未存储在对象存储"):
        await storage.get_object_reference(doc.file_id, "owner")
    client.stat_object.assert_not_called()
    client.get_object.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("file_id", ["gridfs:507f1f77bcf86cd799439011", "507f1f77bcf86cd799439011", "future:new"])
async def test_hybrid_rejects_legacy_mount_but_keeps_download_routing(file_id):
    minio, gridfs = SimpleNamespace(get_object_reference=AsyncMock()), SimpleNamespace(download_file=AsyncMock())
    storage = HybridFileStorage(minio, gridfs)
    with pytest.raises(ObjectFileMountError, match="联系管理员迁移"):
        await storage.get_object_reference(file_id, "owner")
    minio.get_object_reference.assert_not_awaited()
    gridfs.download_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_gridfs_mount_has_no_full_download_fallback():
    storage = GridFSFileStorage.__new__(GridFSFileStorage)
    with pytest.raises(ObjectFileMountError, match="尚未存储在对象存储"):
        await storage.get_object_reference("507f1f77bcf86cd799439011", "owner")


@pytest.mark.asyncio
async def test_wrappers_resolve_only_objects_and_do_not_accept_dataset_references():
    reference = ObjectFileReference("minio:abc123", "bucket", "files/data.h5", 42)
    minio = SimpleNamespace(get_object_reference=AsyncMock(return_value=reference))
    storage = DatasetPreviewFileStorage(HybridFileStorage(minio, object()), previews=object())
    assert await storage.get_object_reference("minio:abc123", "owner") is reference
    minio.get_object_reference.assert_awaited_once_with("minio:abc123", "owner")
    with pytest.raises(ObjectFileMountError, match="尚未存储在对象存储"):
        await storage.get_object_reference("dataset-preview:private", "owner")
    assert minio.get_object_reference.await_count == 1


def test_new_file_writes_default_to_object_storage():
    assert Settings.model_fields["file_storage_provider"].default == "hybrid"
