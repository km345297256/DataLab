"""Bounded, version-checked text transactions; never execute candidate code.

All replacement matches refer to the original bytes decoded as UTF-8. A match
count failure, overlapping edit, stale version, or syntax failure changes no
destination bytes. Directory locks serialize cooperating file-service writers;
identity checks also detect external changes observed before the atomic rename.
"""
from __future__ import annotations

import ast
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid
import warnings

from app.core.exceptions import AppException
from app.models.file import FilePatchResult
from app.schemas.file import FilePatchRequest


MAX_PATCH_BYTES = 8 * 1024 * 1024
MAX_PATCH_MATCHES = 100000
READONLY_ROOTS = ("/home/ubuntu/inputs", "/home/ubuntu/datasets", "/datasets")


class FilePatchError(AppException):
    def __init__(self, reason: str, *, details: dict | None = None):
        messages = {
            "version_conflict": "File version changed; read the current version before applying a new patch",
            "match_count_mismatch": "Patch match count differs from expected_count; no replacements were saved",
            "overlapping_replacements": "Patch replacements overlap in the original file; no replacements were saved",
            "syntax_invalid": "Patched content failed the requested syntax check; no replacements were saved",
            "unsafe_path": "Patch requires an absolute regular-file path without symbolic links",
            "readonly_target": "Patch target is read-only or is an analysis input",
            "file_missing": "Patch target does not exist",
            "file_too_large": "Patch source or result exceeds the supported text size",
            "invalid_encoding": "Patch requires UTF-8 text",
            "io_error": "Patch could not be saved; destination content was not changed",
        }
        super().__init__(messages[reason], status_code=409 if reason == "version_conflict" else 400,
                         data={"error_code": reason, "changed": False,
                               "side_effect_state": "not_started", **(details or {})})


def file_identity(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@contextmanager
def directory_write_lock(directory: str):
    """A cooperating-writer lock, without creating a lock file in the workspace."""
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


@contextmanager
def _safe_parent(path: str):
    p = Path(path)
    if not p.is_absolute() or ".." in p.parts or "\x00" in path or p == Path("/"):
        raise FilePatchError("unsafe_path")
    if any(p == Path(root) or Path(root) in p.parents for root in READONLY_ROOTS):
        raise FilePatchError("readonly_target")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    parents = []
    try:
        parent = os.open("/", flags)
        parents.append(parent)
        for part in p.parts[1:-1]:
            parent = os.open(part, flags, dir_fd=parent)
            parents.append(parent)
        fcntl.flock(parent, fcntl.LOCK_EX)
        def verify_parents():
            for ancestor, part, child in zip(parents, p.parts[1:-1], parents[1:]):
                entry = os.stat(part, dir_fd=ancestor, follow_symlinks=False)
                current = os.fstat(child)
                if not stat.S_ISDIR(entry.st_mode) or (entry.st_dev, entry.st_ino) != (current.st_dev, current.st_ino):
                    raise FilePatchError("version_conflict")
        yield parent, p.name, verify_parents
    except FileNotFoundError:
        raise FilePatchError("file_missing") from None
    except (NotADirectoryError, PermissionError):
        raise FilePatchError("unsafe_path") from None
    finally:
        for descriptor in reversed(parents):
            os.close(descriptor)


def _read_snapshot(parent: int, name: str) -> tuple[bytes, os.stat_result, dict[str, bytes]]:
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise FilePatchError("unsafe_path")
    if not info.st_mode & 0o222 or not os.access(name, os.W_OK, dir_fd=parent, effective_ids=True):
        raise FilePatchError("readonly_target")
    if not os.fstat(parent).st_mode & 0o222 or not os.access(".", os.W_OK, dir_fd=parent, effective_ids=True):
        raise FilePatchError("readonly_target")
    if os.fstatvfs(parent).f_flag & os.ST_RDONLY:
        raise FilePatchError("readonly_target")
    if info.st_size > MAX_PATCH_BYTES:
        raise FilePatchError("file_too_large")
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
    with os.fdopen(fd, "rb") as stream:
        if file_identity(os.fstat(stream.fileno())) != file_identity(info):
            raise FilePatchError("version_conflict")
        source = stream.read(MAX_PATCH_BYTES + 1)
        # Linux stores POSIX ACLs and security labels as extended attributes.
        # Keep them on the replacement inode rather than broadening access.
        attributes = {}
        if hasattr(os, "listxattr"):
            for key in os.listxattr(stream.fileno()):
                value = os.getxattr(stream.fileno(), key)
                if len(attributes) >= 128 or sum(map(len, attributes.values())) + len(value) > 2 * 1024 * 1024:
                    raise FilePatchError("file_too_large")
                attributes[key] = value
        if (len(source) != info.st_size or
                file_identity(os.fstat(stream.fileno())) != file_identity(info)):
            raise FilePatchError("version_conflict")
    return source, info, attributes


def _syntax_check(candidate: bytes, kind: str) -> None:
    try:
        if kind == "python":
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                tree = ast.parse(candidate, filename="<patched-file>")
                compile(tree, "<patched-file>", "exec")
        elif kind == "json":
            def reject_constant(_value):
                raise ValueError("Non-finite JSON number")
            def unique_keys(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("Duplicate JSON key")
                    result[key] = value
                return result
            json.loads(candidate, parse_constant=reject_constant, object_pairs_hook=unique_keys)
    except (SyntaxError, ValueError, RecursionError) as error:
        details = {"syntax_check": kind}
        line = getattr(error, "lineno", None)
        if type(line) is int:
            details["line"] = line
        raise FilePatchError("syntax_invalid", details=details) from None


def apply_patch(request: FilePatchRequest) -> FilePatchResult:
    temporary = None
    try:
        with _safe_parent(request.file) as (parent, name, verify_parents):
            try:
                source, info, attributes = _read_snapshot(parent, name)
                before = hashlib.sha256(source).hexdigest()
                if before != request.base_sha256:
                    raise FilePatchError("version_conflict", details={"current_sha256": before})
                try:
                    content = source.decode("utf-8")
                except UnicodeDecodeError:
                    raise FilePatchError("invalid_encoding") from None
                edits = []
                counts = []
                candidate_size = len(source)
                for index, replacement in enumerate(request.replacements):
                    count = content.count(replacement.old_str)
                    if count != replacement.expected_count:
                        raise FilePatchError("match_count_mismatch", details={
                            "replacement_index": index, "expected_count": replacement.expected_count,
                            "actual_count": count})
                    counts.append(count)
                    if sum(counts) > MAX_PATCH_MATCHES:
                        raise FilePatchError("file_too_large")
                    candidate_size += count * (len(replacement.new_str.encode("utf-8"))
                                               - len(replacement.old_str.encode("utf-8")))
                    start = 0
                    for _ in range(count):
                        start = content.find(replacement.old_str, start)
                        end = start + len(replacement.old_str)
                        edits.append((start, end, replacement.new_str))
                        start = end
                if candidate_size > MAX_PATCH_BYTES:
                    raise FilePatchError("file_too_large")
                edits.sort(key=lambda item: (item[0], item[1]))
                cursor = 0
                pieces = []
                for start, end, replacement in edits:
                    if start < cursor:
                        raise FilePatchError("overlapping_replacements")
                    pieces.extend((content[cursor:start], replacement))
                    cursor = end
                pieces.append(content[cursor:])
                candidate = "".join(pieces).encode("utf-8")
                if len(candidate) > MAX_PATCH_BYTES:
                    raise FilePatchError("file_too_large")
                _syntax_check(candidate, request.syntax_check)
                after = hashlib.sha256(candidate).hexdigest()
                changed = candidate != source
                if changed:
                    temporary = ".datalab-patch-" + uuid.uuid4().hex
                    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                                 0o600, dir_fd=parent)
                    with os.fdopen(fd, "wb") as stream:
                        # Atomic replacement must not change the file's ownership or mode.
                        temp_info = os.fstat(stream.fileno())
                        if (temp_info.st_uid, temp_info.st_gid) != (info.st_uid, info.st_gid):
                            os.fchown(stream.fileno(), info.st_uid, info.st_gid)
                        os.fchmod(stream.fileno(), stat.S_IMODE(info.st_mode))
                        stream.write(candidate)
                        stream.flush()
                        for key, value in attributes.items():
                            os.setxattr(stream.fileno(), key, value)
                        os.fsync(stream.fileno())
                    # Recheck both path identity and complete bytes after staging.
                    current, current_info, current_attributes = _read_snapshot(parent, name)
                    if file_identity(current_info) != file_identity(info) or current != source:
                        raise FilePatchError("version_conflict")
                    if current_attributes != attributes:
                        raise FilePatchError("version_conflict")
                    verify_parents()
                    os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
                    temporary = None
                return FilePatchResult(file=request.file, before_sha256=before, after_sha256=after,
                    replaced_count=sum(counts), replacement_counts=counts, changed=changed,
                    write_action="patch" if changed else "reuse",
                    side_effect_state="completed" if changed else "not_started", syntax_check=request.syntax_check)
            finally:
                if temporary is not None:
                    try:
                        os.unlink(temporary, dir_fd=parent)
                    except FileNotFoundError:
                        pass
    except FilePatchError:
        raise
    except OSError:
        raise FilePatchError("io_error") from None
