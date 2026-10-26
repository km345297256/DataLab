"""Read-only compatibility for saved dataset bindings after catalog removal."""
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.application.services.data_center_dataset_service as dataset_service_module
from app.application.errors.exceptions import NotFoundError
from app.application.services.data_center_dataset_service import DataCenterDatasetService
from app.domain.models.dataset import DataCenterDataset, DatasetFile, DatasetLocation, DatasetStorageType
from app.infrastructure.models.documents import TemporaryDatasetDocument


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["intruder", None, "owner-a"])
@pytest.mark.parametrize("expired", [False, True])
async def test_legacy_temporary_lookup_enforces_owner_and_expiry(monkeypatch, actor, expired):
    now = datetime(2026, 10, 20, tzinfo=UTC)
    dataset = _submission_dataset()
    document = SimpleNamespace(
        owner_id="owner-a",
        expires_at=now if expired else now + timedelta(hours=1),
        to_domain=lambda: dataset,
    )
    # Deliberately return a document even for a mismatched owner or expiry:
    # the compatibility reader must also check the result itself.
    lookup = AsyncMock(return_value=document)
    monkeypatch.setattr(dataset_service_module, "_utc_now", lambda: now)
    monkeypatch.setattr(dataset_service_module, "TemporaryDatasetDocument", SimpleNamespace(find_one=lookup))
    service = DataCenterDatasetService()
    if actor == "owner-a" and not expired:
        assert await service.get_dataset(dataset.dataset_id, user_id=actor) == dataset
    else:
        with pytest.raises(NotFoundError):
            await service.get_dataset(dataset.dataset_id, user_id=actor)
    if actor is None:
        lookup.assert_not_awaited()
    else:
        lookup.assert_awaited_once_with({
            "dataset_id": dataset.dataset_id,
            "owner_id": actor,
            "expires_at": {"$gt": now},
        })


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["intruder", None, "owner-a"])
@pytest.mark.parametrize("enabled", [False, True])
async def test_legacy_persistent_lookup_enforces_owner_and_enabled(monkeypatch, actor, enabled):
    dataset = _submission_dataset(dataset_id="dsr_saved")
    document = SimpleNamespace(
        is_submission=True, created_by="owner-a", enabled=enabled,
        to_domain=lambda: dataset,
    )
    lookup = AsyncMock(return_value=document)
    monkeypatch.setattr(dataset_service_module, "DataCenterDatasetDocument", SimpleNamespace(find_one=lookup))
    service = DataCenterDatasetService()
    if actor == "owner-a" and enabled:
        assert await service.get_dataset(dataset.dataset_id, user_id=actor) == dataset
    else:
        with pytest.raises(NotFoundError):
            await service.get_dataset(dataset.dataset_id, user_id=actor)
    lookup.assert_awaited_once_with({"dataset_id": dataset.dataset_id})


@pytest.mark.asyncio
async def test_legacy_agent_context_discards_real_mount_locations():
    dataset = _submission_dataset(
        locations=[DatasetLocation(
            node_id="local-docker", storage_type=DatasetStorageType.HOST_PATH,
            source_path="/srv/private/owner-a", verified=True,
        )],
    )
    service = DataCenterDatasetService()
    service.get_dataset = AsyncMock(return_value=dataset)
    [mounted] = await service.mounted_datasets([dataset.dataset_id], user_id="owner-a")
    assert mounted.locations == []
    assert "/srv/private" not in mounted.model_dump_json()
    assert mounted.sandbox_path == f"/home/ubuntu/datasets/{dataset.dataset_id}"


def _submission_dataset(
    *,
    dataset_id: str = "tds_private",
    created_by: str = "owner-a",
    files: list[DatasetFile] | None = None,
    locations: list[DatasetLocation] | None = None,
) -> DataCenterDataset:
    return DataCenterDataset(
        dataset_id=dataset_id,
        external_id="external-1",
        data_center_id="dataset-chat-demo",
        data_center_name="Test datasets",
        name="Private dataset",
        description="Dataset submitted for an analysis session.",
        files=files or [],
        locations=locations or [],
        enabled=True,
        is_submission=True,
        created_by=created_by,
    )


def test_temporary_dataset_document_has_unique_id_and_absolute_ttl_indexes():
    unique_id_indexes = [
        item.document
        for item in TemporaryDatasetDocument.Settings.indexes
        if getattr(item, "document", {}).get("unique") is True
    ]
    ttl_indexes = [
        item.document
        for item in TemporaryDatasetDocument.Settings.indexes
        if getattr(item, "document", {}).get("expireAfterSeconds") == 0
    ]
    owner_slot_indexes = [
        item.document
        for item in TemporaryDatasetDocument.Settings.indexes
        if getattr(item, "document", {}).get("name")
        == "temporary_dataset_owner_slot_unique"
    ]
    global_slot_indexes = [
        item.document
        for item in TemporaryDatasetDocument.Settings.indexes
        if getattr(item, "document", {}).get("name")
        == "temporary_dataset_global_slot_unique"
    ]

    assert TemporaryDatasetDocument.Settings.name == "temporary_data_center_datasets"
    assert any(
        list(index["key"].items()) == [("dataset_id", 1)]
        for index in unique_id_indexes
    )
    assert any(
        list(index["key"].items()) == [("expires_at", 1)]
        and index.get("name") == "temporary_dataset_expiration_ttl"
        for index in ttl_indexes
    )
    assert any(
        list(index["key"].items()) == [("owner_id", 1), ("owner_slot", 1)]
        and index.get("unique") is True
        and index.get("partialFilterExpression")
        == {"owner_slot": {"$type": "int"}}
        for index in owner_slot_indexes
    )
    assert any(
        list(index["key"].items()) == [("global_slot", 1)]
        and index.get("unique") is True
        and index.get("partialFilterExpression")
        == {"global_slot": {"$type": "int"}}
        for index in global_slot_indexes
    )


@pytest.mark.asyncio
async def test_multiple_sources_resolve_to_unique_nested_read_only_targets():
    locations = [
        DatasetLocation(
            location_id="dsl_report_a",
            node_id="node-a",
            storage_type=DatasetStorageType.HOST_PATH,
            source_path="/srv/center-a/report.tif",
            verified=True,
        ),
        DatasetLocation(
            location_id="dsl_report_b",
            node_id="node-a",
            storage_type=DatasetStorageType.HOST_PATH,
            source_path="/srv/center-b/report.tif",
            verified=True,
        ),
        DatasetLocation(
            location_id="dsl_metadata",
            node_id="node-a",
            storage_type=DatasetStorageType.HOST_PATH,
            source_path="/srv/center-c/metadata.json",
            verified=True,
        ),
    ]
    dataset = _submission_dataset(locations=locations)
    service = object.__new__(DataCenterDatasetService)
    service._settings = SimpleNamespace(dataset_managed_volume="unused-volume")
    service.get_dataset = AsyncMock(return_value=dataset)

    mounts = await service.resolve_mounts(
        [dataset.dataset_id],
        "node-a",
        user_id="owner-a",
    )

    service.get_dataset.assert_awaited_once_with(dataset.dataset_id, user_id="owner-a")
    assert [mount.display_name for mount in mounts] == [
        "report.tif",
        "report-2.tif",
        "metadata.json",
    ]
    assert len({mount.target for mount in mounts}) == len(locations)
    assert all(mount.read_only is True for mount in mounts)

    dataset_root = PurePosixPath("/home/ubuntu/datasets") / dataset.dataset_id
    for mount in mounts:
        relative_target = PurePosixPath(mount.target).relative_to(dataset_root)
        assert relative_target.parts == ("sources", mount.source_id, mount.display_name)
        assert mount.source not in mount.target
