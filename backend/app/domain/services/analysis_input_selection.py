"""Explicit input selection and server-owned publication references.

Published attachments are only candidates. Admission must reauthorize their
stored provenance before promoting them into the immutable input snapshot.
"""
import re

from app.domain.models.event import MessageEvent
from app.domain.models.file import FileInfo
from app.domain.models.analysis_input import assign_upload_namespace, safe_upload_filename

PUBLISHED_INPUT_ORIGIN = "published_artifact"
_INPUT_METADATA_KEYS = ("analysis_input_namespace", "analysis_input_filename",
                        "analysis_input_origin", "analysis_input_sha256")


def input_snapshot(files: list[FileInfo]) -> list[dict]:
    return [{"file_id": info.file_id, "filename": info.filename, "size": info.size,
             "content_type": info.content_type,
             "metadata": {key: (info.metadata or {}).get(key) for key in _INPUT_METADATA_KEYS
                          if key in (info.metadata or {})}}
            for info in files]


def snapshot_files(metadata: dict | None) -> list[FileInfo]:
    return [FileInfo.model_validate(item) for item in (metadata or {}).get("analysis_input_files", [])]


def session_input_state(events) -> tuple[list[FileInfo], list[str]]:
    """Recover admitted inputs, including explicitly promoted published files."""
    known: dict[str, FileInfo] = {}
    selected: list[str] = []
    for event in events:
        if not isinstance(event, MessageEvent) or event.role != "user":
            continue
        incoming = [item for item in event.attachments or [] if item.file_id]
        normalized = assign_upload_namespace(incoming)
        authoritative = {item.file_id: item for item in snapshot_files(event.metadata)}
        for original, assigned in zip(incoming, normalized):
            # New admission metadata is server-owned. Older input events are only
            # read as user provenance; no old task or event is rewritten.
            # Upload metadata is user-controlled. Only the server-owned event
            # input snapshot can establish a persisted namespace, not metadata
            # copied from an uploaded object or an old attachment event.
            info = authoritative.get(original.file_id, assigned)
            known[info.file_id] = info.model_copy(deep=True)
        # A user can explicitly name a previously published result without
        # uploading it again. Its server-owned admission snapshot is the scope
        # authority; assistant attachments alone never enter the input state.
        known.update({file_id: info.model_copy(deep=True) for file_id, info in authoritative.items()})
        metadata = event.metadata or {}
        if "analysis_input_file_ids" in metadata:
            selected = list(metadata["analysis_input_file_ids"])
        elif incoming:
            selected = list(dict.fromkeys([*selected, *(item.file_id for item in incoming)]))
    return list(known.values()), [item for item in selected if item in known]


def select_input_files(events, incoming: list[FileInfo], requested: list[str] | None, *,
                       promoted: list[FileInfo] | None = None) -> tuple[list[FileInfo], list[FileInfo]]:
    known_files, inherited = session_input_state(events)
    known = {item.file_id: item for item in known_files}
    # Only the admission service can attach a publication identity. Ignore
    # similarly named metadata on ordinary user-uploaded objects.
    additions = {item.file_id: item.model_copy(deep=True, update={"metadata": {
        key: value for key, value in (item.metadata or {}).items()
        if key not in {"analysis_input_origin", "analysis_input_sha256"}
    }}) for item in incoming}
    additions.update({item.file_id: item for item in promoted or []})
    fresh = assign_upload_namespace([item for item in additions.values() if item.file_id not in known])
    known.update({item.file_id: item for item in fresh})
    submitted = [known[item.file_id] for item in incoming]
    chosen = list(dict.fromkeys([*(inherited if requested is None else requested),
                                *(item.file_id for item in submitted), *(item.file_id for item in promoted or [])]))
    if any(item not in known for item in chosen):
        raise ValueError("所选文件不是本会话中由用户提交的分析资料。")
    return [known[item].model_copy(deep=True) for item in chosen], submitted


def published_output_candidates(events) -> dict[str, FileInfo]:
    """Index actual delivered attachments, irrespective of the turn's outcome.

    Tool text, model-written paths and un-published session files are not proof
    of publication. Fresh storage provenance is checked separately.
    """
    return {item.file_id: item for event in events
            if isinstance(event, MessageEvent) and event.role == "assistant"
            for item in event.attachments or [] if item.file_id}


def explicitly_referenced_outputs(events, message: str | None, requested: list[str] | None,
                                  incoming: list[FileInfo]) -> list[FileInfo]:
    candidates = published_output_candidates(events)
    explicit_ids = set(requested or []) | {item.file_id for item in incoming}
    chosen = {file_id: item for file_id, item in candidates.items() if file_id in explicit_ids}
    known, _ = session_input_state(events)
    by_name: dict[str, dict[str, FileInfo]] = {}
    for item in [*known, *incoming, *candidates.values()]:
        if item.file_id and item.filename and item.filename == safe_upload_filename(item.filename):
            by_name.setdefault(item.filename, {})[item.file_id] = item
    for name, matches in by_name.items():
        # Exact, case-sensitive file names only. Chinese prose can touch a
        # Latin filename; longer filenames such as result.csv.bak must not match.
        if not message or not re.search(r"(?<![A-Za-z0-9_.-])" + re.escape(name) + r"(?![A-Za-z0-9_.-])", message):
            continue
        output_ids = set(matches) & candidates.keys()
        if not output_ids:
            continue
        identified = set(matches) & explicit_ids
        if identified:
            output_ids &= identified
        elif len(matches) > 1:
            raise ValueError(f"存在多个名为“{name}”的文件，请在任务数据中选择具体文件后再分析。")
        chosen.update({file_id: candidates[file_id] for file_id in output_ids})
    return list(chosen.values())


def published_input_reference(publication: FileInfo, current: FileInfo, *,
                              session_id: str, user_id: str) -> FileInfo:
    """Verify a delivered object's immutable identity without reading bytes."""
    published = publication.metadata or {}
    stored = current.metadata or {}
    expected_hash = (published.get("analysis_input_sha256")
                     if published.get("analysis_input_origin") == PUBLISHED_INPUT_ORIGIN
                     else published.get("artifact_sha256"))
    already_admitted = published.get("analysis_input_origin") == PUBLISHED_INPUT_ORIGIN
    if (not publication.file_id or publication.file_id != current.file_id
            or current.user_id != user_id or stored.get("source") != "sandbox_artifact"
            or stored.get("session_id") != session_id
            or not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_hash)
            or stored.get("artifact_sha256") != expected_hash
            or type(current.size) is not int or current.size < 0
            or type(stored.get("artifact_size")) is not int or stored["artifact_size"] != current.size
            or publication.size != current.size or publication.filename != current.filename
            or not already_admitted and (published.get("source") != "sandbox_artifact"
                or published.get("session_id") != session_id
                or type(published.get("artifact_size")) is not int
                or published["artifact_size"] != publication.size)):
        raise ValueError("所选派生结果未通过发布记录和归属校验，或已变化，请重新选择已发布文件。")
    # Only private admission identity survives. Never reuse a former writable
    # /output path, storage URL, object key, or user-supplied mount namespace.
    return current.model_copy(deep=True, update={"file_path": None, "file_url": None, "metadata": {
        "analysis_input_origin": PUBLISHED_INPUT_ORIGIN,
        "analysis_input_sha256": expected_hash,
    }})
