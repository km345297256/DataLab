"""One-copy, atomic ingestion for large analysis inputs."""
import asyncio
import errno
import os
import shutil
import tempfile
from collections.abc import AsyncIterator

from app.core.exceptions import AppException
from app.models.file import FileUploadResult


async def _joined_io(function, *args):
    """Cancellation must join the writer before its file is closed/unlinked."""
    worker = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not worker.cancelled():
            worker.exception()
        raise


def _failure(code, message, status_code):
    return AppException(message=message, status_code=status_code, data={"code": code})


def check_upload_capacity(path: str, expected_size: int | None):
    if not os.path.isabs(path) or "\0" in path:
        raise _failure("invalid_upload_path", "Invalid upload destination", 400)
    directory = os.path.dirname(path)
    while not os.path.exists(directory):
        directory = os.path.dirname(directory)
    if expected_size is not None and shutil.disk_usage(directory).free < expected_size:
        raise _failure("insufficient_storage", "Analysis storage has insufficient free space", 507)


def _write_chunk(target, chunk):
    while chunk:
        written = target.write(chunk)
        if not isinstance(written, int) or written <= 0:
            raise OSError(errno.EIO, "Incomplete upload write")
        chunk = chunk[written:]


async def upload_stream(path: str, chunks: AsyncIterator[bytes], expected_size: int | None):
    """Keep the old target intact until the whole request is durably received."""
    check_upload_capacity(path, expected_size)
    temporary = None
    target = None
    total_size = 0
    try:
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        target = tempfile.NamedTemporaryFile(mode="wb", buffering=0, prefix=".datalab-upload-", suffix=".part",
                                           dir=directory, delete=False)
        temporary = target.name
        async for chunk in chunks:
            if expected_size is not None and total_size + len(chunk) > expected_size:
                raise _failure("incomplete_upload", "Upload length does not match expected size", 400)
            # ASGI normally delivers bounded chunks. Slice as well so an unusual
            # producer cannot turn a single write into an unbounded worker job.
            for start in range(0, len(chunk), 1024 * 1024):
                await _joined_io(_write_chunk, target, memoryview(chunk)[start:start + 1024 * 1024])
            total_size += len(chunk)
        if expected_size is not None and total_size != expected_size:
            raise _failure("incomplete_upload", "Upload length does not match expected size", 400)
        await _joined_io(target.flush)
        await _joined_io(os.fsync, target.fileno())
        target.close()
        # No await between successful verification and atomic promotion.
        os.replace(temporary, path)
        temporary = None
        return FileUploadResult(file_path=path, file_size=total_size, success=True)
    except OSError as error:
        if error.errno in {errno.ENOSPC, errno.EDQUOT}:
            raise _failure("insufficient_storage", "Analysis storage has insufficient free space", 507) from error
        raise _failure("upload_failed", "Unable to write analysis input", 500) from error
    finally:
        try:
            if target is not None:
                target.close()
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
