"""A task's inputs, downloads and historical outputs, without granting access.

Origin and next-turn selection are independent. Historical references only
discover IDs: every read revalidates ownership and materialized bytes, and no
storage paths, historical metadata or signed URLs are returned.
"""
import asyncio
import re
from datetime import UTC, datetime

from app.application.errors.exceptions import NotFoundError
from app.application.services.data_library_service import DataLibraryService
from app.domain.models.data_library import canonical_file_id
from app.domain.models.event import MessageEvent
from app.domain.services.analysis_input_selection import session_input_state, snapshot_files
from app.domain.services.data_runs import data_run_id
from app.interfaces.schemas.file import FileInfoResponse, public_filename
from app.interfaces.schemas.task_data import TaskDataItem, TaskDataResponse, TaskDataSavedLocation


def _utc(value):
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class TaskDataService:
    def __init__(self, sessions, files, library, provenance):
        self.sessions, self.library, self.provenance = sessions, library, provenance
        self._library_service = DataLibraryService(library, files, sessions)

    async def list(self, owner: str, session_id: str, *, file_ids: list[str] | None = None) -> TaskDataResponse:
        session = await self.sessions.find_owned_by_id_and_user_id(session_id, owner)
        if session is None:
            # Public sharing and collaboration never grant this private view.
            raise NotFoundError("分析任务不存在或不可访问")
        events, receipts, context_ids = await asyncio.gather(
            self.sessions.get_events(session_id),
            self.provenance.list_acquisitions(owner, session_id),
            self.library.get_context(owner, session_id),
        )
        known: dict[str, dict] = {}
        def remember(file_id, *, timestamp=None, round_number=None):
            if not isinstance(file_id, str) or not file_id:
                return
            key = canonical_file_id(file_id)
            item = known.setdefault(key, {"created_at": None, "round_number": None})
            # First actual event membership wins over later re-selection.
            if timestamp is not None and item["created_at"] is None:
                item.update(created_at=_utc(timestamp), round_number=round_number)

        # Repository history is ordered by its sequence, including legacy
        # events. Derive a stable run-to-round map to handle delayed deliveries.
        round_number = 0
        run_rounds, turns = {}, []
        for event in events:
            if isinstance(event, MessageEvent) and event.role == "user":
                round_number += 1
                turns.append((_utc(event.timestamp), round_number))
                if event.seq:
                    run_rounds[data_run_id(session_id, event.seq)] = round_number
        round_number = 0
        artifact_rounds = {}
        for event in events:
            if not isinstance(event, MessageEvent):
                continue
            if event.role == "user":
                round_number += 1
            current_round = run_rounds.get(event.run_id, round_number or None)
            attachments = list(event.attachments or [])
            if event.role == "user":
                attachments.extend(snapshot_files(event.metadata))
            for info in attachments:
                remember(info.file_id, timestamp=event.timestamp, round_number=current_round)
                if event.role == "assistant" and info.file_id:
                    artifact_rounds.setdefault(canonical_file_id(info.file_id), current_round)
        for info in session.files:
            remember(info.file_id)
        for file_id in [*context_ids, *(file_ids or [])]:
            remember(file_id)
        by_download = {}
        for receipt in receipts:
            key = receipt.get("file_id")
            if isinstance(key, str) and key:
                key = canonical_file_id(key)
                remember(key)
                by_download[key] = receipt
        _, selected = session_input_state(events)

        async def project(file_id, first_seen):
            try:
                info = await self._library_service._info(owner, file_id, validate_bytes=True)
            except NotFoundError:
                return None
            metadata = info.metadata or {}
            receipt = by_download.get(file_id)
            # Only service-written receipts for THIS task establish downloads.
            # A download/result imported from another task is a user input.
            downloaded = bool(receipt and receipt.get("size") == info.size
                              and re.fullmatch(r"[a-f0-9]{64}", str(receipt.get("sha256", "")))
                              and receipt["sha256"] == metadata.get("sha256"))
            generated = (metadata.get("source") == "sandbox_artifact"
                         and metadata.get("session_id") == session_id)
            category = "download" if downloaded else "analysis" if generated else "upload"
            created_at, round_index = first_seen["created_at"], first_seen["round_number"]
            if category != "upload" or created_at is None:
                created_at = _utc(info.upload_date) or created_at
            if category != "upload" and created_at is not None:
                round_index = next((number for date, number in reversed(turns) if date <= created_at), None)
            if category == "analysis" and file_id in artifact_rounds:
                round_index = artifact_rounds[file_id]
            # Explicit allowlist: public metadata sanitization alone is not a
            # promise that arbitrary legacy token/storage fields are harmless.
            public = FileInfoResponse(file_id=file_id, filename=public_filename(info.filename),
                                      content_type=info.content_type, size=info.size,
                                      upload_date=_utc(info.upload_date))
            return TaskDataItem(file=public, category=category, created_at=created_at, round_number=round_index)

        projected = await asyncio.gather(*(project(file_id, first_seen) for file_id, first_seen in known.items()))
        items = [item for item in projected if item is not None]
        locations = await self.library.file_locations(owner, [item.file.file_id for item in items])
        for item in items:
            item.saved_locations = [TaskDataSavedLocation.model_validate(location)
                                    for location in locations.get(item.file.file_id, [])]
        available = {item.file.file_id for item in items}
        items.sort(key=lambda item: (item.created_at or datetime.min.replace(tzinfo=UTC), item.file.file_id), reverse=True)
        return TaskDataResponse(items=items, unavailable_count=len(known) - len(items),
                                selected_file_ids=list(dict.fromkeys(canonical_file_id(key) for key in selected
                                                                     if canonical_file_id(key) in available)))
