from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Iterable, Sequence

from app.application.errors.exceptions import BadRequestError, NotFoundError
from app.core.config import get_settings
from app.domain.models.dataset import (
    DataCenterDataset,
    DatasetFile,
    DatasetMount,
    DatasetStorageType,
    MountedDataset,
)
from app.infrastructure.models.documents import (
    DataCenterDatasetDocument,
    TemporaryDatasetDocument,
)


SANDBOX_DATASET_ROOT = PurePosixPath("/home/ubuntu/datasets")
DATASET_CONTEXT_FILE_LIMIT = 48
DATASET_CONTEXT_GROUP_LIMIT = 16
DATASET_CONTEXT_METADATA_CHARS = 6_000
DATASET_CONTEXT_FIELD_CHARS = 2_000
DATASET_CONTEXT_PATH_CHARS = 512


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _unique_mount_names(source_paths: Sequence[str]) -> list[str]:
    """Return stable, filename-only mount names without exposing source directories."""
    used: set[str] = set()
    result: list[str] = []
    for source_path in source_paths:
        normalized = source_path.rstrip("/").replace("\\", "/")
        base_name = PurePosixPath(normalized).name or "source"
        candidate = base_name
        suffix = PurePosixPath(base_name).suffix
        stem = base_name[:-len(suffix)] if suffix else base_name
        index = 2
        while candidate in used:
            candidate = f"{stem}-{index}{suffix}"
            index += 1
        used.add(candidate)
        result.append(candidate)
    return result


class DataCenterDatasetService:
    """Read existing dataset bindings for historical analysis sessions only.

    DataLab no longer exposes an institution catalog or creates registrations.
    Keep the stored-document contract so existing read-only mounts and opaque
    preview references retain their owner and expiration checks.
    """

    def __init__(self):
        self._settings = get_settings()
    async def get_dataset(
        self,
        dataset_id: str,
        include_disabled: bool = False,
        user_id: str | None = None,
    ) -> DataCenterDataset:
        if dataset_id.startswith("tds_"):
            if user_id is None:
                raise NotFoundError(f"Dataset '{dataset_id}' is unavailable")
            now = _utc_now()
            temporary_doc = await TemporaryDatasetDocument.find_one({
                "dataset_id": dataset_id,
                "owner_id": user_id,
                "expires_at": {"$gt": now},
            })
            # Keep the local checks as defense in depth and for deterministic
            # expiration at the boundary while MongoDB's TTL monitor catches up.
            if (
                not temporary_doc
                or temporary_doc.owner_id != user_id
                or _as_utc(temporary_doc.expires_at) <= now
            ):
                raise NotFoundError(f"Dataset '{dataset_id}' is unavailable")
            return temporary_doc.to_domain()

        doc = await DataCenterDatasetDocument.find_one({"dataset_id": dataset_id})
        submission_unavailable = bool(
            doc
            and doc.is_submission
            and (
                user_id is None
                or doc.created_by != user_id
            )
        )
        if (
            not doc
            or (not include_disabled and not doc.enabled)
            or submission_unavailable
        ):
            raise NotFoundError(f"Dataset '{dataset_id}' is unavailable")
        return doc.to_domain()

    async def candidate_node_ids(self, dataset_ids: Iterable[str], user_id: str | None = None) -> set[str]:
        candidates: set[str] | None = None
        for dataset_id in dict.fromkeys(dataset_ids):
            dataset = await self.get_dataset(dataset_id, user_id=user_id)
            node_ids = {item.node_id for item in dataset.locations if item.verified}
            if not node_ids:
                raise BadRequestError(f"Dataset '{dataset.name}' has no verified storage location")
            candidates = node_ids if candidates is None else candidates & node_ids
        if not candidates:
            raise BadRequestError("Selected datasets are not available on a common execution node")
        return candidates

    async def resolve_mounts(
        self,
        dataset_ids: Iterable[str],
        node_id: str,
        user_id: str | None = None,
    ) -> list[DatasetMount]:
        mounts: list[DatasetMount] = []
        for dataset_id in dict.fromkeys(dataset_ids):
            dataset = await self.get_dataset(dataset_id, user_id=user_id)
            locations = [item for item in dataset.locations if item.node_id == node_id and item.verified]
            if not locations:
                raise BadRequestError(f"Dataset '{dataset.name}' is not available on execution node '{node_id}'")
            derived_names = _unique_mount_names([item.source_path for item in locations])
            for location, derived_name in zip(locations, derived_names):
                dataset_root = SANDBOX_DATASET_ROOT / dataset.dataset_id
                target = (
                    dataset_root
                    if location.storage_type == DatasetStorageType.MANAGED_UPLOAD
                    else dataset_root / "sources" / location.location_id / (location.mount_name or derived_name)
                )
                source = (
                    self._settings.dataset_managed_volume
                    if location.storage_type == DatasetStorageType.MANAGED_UPLOAD
                    else location.source_path
                )
                mounts.append(DatasetMount(
                    dataset_id=dataset.dataset_id,
                    source_id=location.location_id,
                    display_name=location.mount_name or derived_name,
                    node_id=node_id,
                    storage_type=location.storage_type,
                    source=source,
                    target=str(target),
                    read_only=True,
                    version=location.version,
                ))
        return mounts

    async def mounted_datasets(
        self,
        dataset_ids: Iterable[str],
        user_id: str | None = None,
    ) -> list[MountedDataset]:
        mounted: list[MountedDataset] = []
        for dataset_id in dict.fromkeys(dataset_ids):
            dataset = await self.get_dataset(dataset_id, user_id=user_id)
            payload = dataset.model_dump()
            # Host source paths are needed only while resolving Docker mounts.
            # Never carry them into the agent message/context object.
            payload["locations"] = []
            mounted.append(MountedDataset(
                **payload,
                sandbox_path=str(SANDBOX_DATASET_ROOT / dataset.dataset_id),
            ))
        return mounted


def _bounded_context_text(value: object, max_chars: int) -> str:
    text = "" if value is None else str(value)
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}\n[truncated from {len(text)} characters]"


def _inventory_group(item: DatasetFile) -> tuple[str, str]:
    role = item.role or "unknown"
    suffix = PurePosixPath(item.path).suffix.lower() or "[no extension]"
    return role, suffix


def _render_inventory_context(files: list[DatasetFile]) -> str:
    if not files:
        return "  Inventory summary: 0 files\n  Inventory sample: (empty)"

    total_size = sum(max(0, item.size) for item in files)
    all_groups: Counter[tuple[str, str]] = Counter(_inventory_group(item) for item in files)

    # Pick at least one representative of as many role/type groups as possible,
    # then fill the remaining slots in stable catalog order.
    selected_indexes: list[int] = []
    represented_groups: set[tuple[str, str]] = set()
    for index, item in enumerate(files):
        group = _inventory_group(item)
        if group in represented_groups:
            continue
        represented_groups.add(group)
        selected_indexes.append(index)
        if len(selected_indexes) >= DATASET_CONTEXT_FILE_LIMIT:
            break
    if len(selected_indexes) < DATASET_CONTEXT_FILE_LIMIT:
        selected_set = set(selected_indexes)
        for index in range(len(files)):
            if index in selected_set:
                continue
            selected_indexes.append(index)
            if len(selected_indexes) >= DATASET_CONTEXT_FILE_LIMIT:
                break
    selected_indexes.sort()

    selected = [files[index] for index in selected_indexes]
    selected_index_set = set(selected_indexes)
    omitted = [item for index, item in enumerate(files) if index not in selected_index_set]
    omitted_groups: Counter[tuple[str, str]] = Counter(_inventory_group(item) for item in omitted)
    omitted_sizes: Counter[tuple[str, str]] = Counter()
    for item in omitted:
        omitted_sizes[_inventory_group(item)] += max(0, item.size)

    group_summary = ", ".join(
        f"{role}/{suffix}: {count}"
        for (role, suffix), count in all_groups.most_common(DATASET_CONTEXT_GROUP_LIMIT)
    )
    sample = "\n".join(
        "  - "
        + _bounded_context_text(item.path, DATASET_CONTEXT_PATH_CHARS)
        + f" ({item.role}, {item.size} bytes)"
        for item in selected
    )
    lines = [
        f"  Inventory summary: {len(files)} files, {total_size} bytes total",
        f"  Inventory groups: {group_summary or '(none)'}",
        f"  Inventory sample ({len(selected)} of {len(files)} files):",
        sample or "  - (empty)",
    ]
    if omitted:
        omitted_summary = ", ".join(
            f"{role}/{suffix}: {count} files, {omitted_sizes[(role, suffix)]} bytes"
            for (role, suffix), count in omitted_groups.most_common(DATASET_CONTEXT_GROUP_LIMIT)
        )
        lines.extend([
            f"  Omitted from prompt: {len(omitted)} files ({omitted_summary})",
            "  The sample is representative, not exhaustive. For an exact lookup, inspect the "
            "read-only mounted directory with one compact find/list command.",
        ])
    return "\n".join(lines)


def render_dataset_context(datasets: list[MountedDataset]) -> str:
    if not datasets:
        return ""
    blocks = []
    for dataset in datasets:
        inventory = _render_inventory_context(dataset.files)
        metadata = _bounded_context_text(
            json.dumps(dataset.metadata, ensure_ascii=False, indent=2, default=str),
            DATASET_CONTEXT_METADATA_CHARS,
        )
        blocks.append(
            f"- Dataset ID: {dataset.dataset_id}\n"
            f"  Data center: {dataset.data_center_name} ({dataset.data_center_id})\n"
            f"  Name: {_bounded_context_text(dataset.name, DATASET_CONTEXT_FIELD_CHARS)}\n"
            f"  Description: {_bounded_context_text(dataset.description, DATASET_CONTEXT_FIELD_CHARS)}\n"
            f"  Spatial coverage: {_bounded_context_text(dataset.spatial_coverage, DATASET_CONTEXT_FIELD_CHARS)}\n"
            f"  Temporal coverage: {_bounded_context_text(dataset.temporal_coverage, DATASET_CONTEXT_FIELD_CHARS)}\n"
            f"  Data type: {_bounded_context_text(dataset.data_type, DATASET_CONTEXT_FIELD_CHARS)}\n"
            f"  Read-only mounted directory: {dataset.sandbox_path}\n"
            f"  Write generated outputs to: /home/ubuntu/output\n"
            f"{inventory}\n"
            f"  Metadata: {metadata}"
        )
    return (
        "<data_center_datasets>\n"
        "These are coherent datasets published by scientific data centers, not user uploads. "
        "Source directories are read-only. Never modify them; write all generated results under /home/ubuntu/output. "
        "Use the mounted directory directly and preserve sidecar files with primary data.\n\n"
        + "\n\n".join(blocks)
        + "\n</data_center_datasets>"
    )
