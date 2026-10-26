"""Upload seekable large objects without materializing a multipart part in RAM.

MinIO's normal SDK uploader buffers each part. Here part size may grow to fit
the service's 10,000-part protocol limit, while both checksum and HTTP passes
read at most one MiB at a time. Signed URLs never escape this module.
"""

import base64
import hashlib
import errno
import re
import threading
from datetime import timedelta
from typing import Any, BinaryIO, Iterator

import httpx

UPLOAD_READ_BYTES = 1024 * 1024
MIN_PART_BYTES = 16 * 1024 * 1024
MAX_PART_BYTES = 5 * 1024**3
MAX_PARTS = 10_000
STREAMING_UPLOAD_THRESHOLD = MIN_PART_BYTES
_FULL_CODES = {"XMinioStorageFull", "XMinioAdminBucketQuotaExceeded", "InsufficientStorage", "QuotaExceeded"}


class MultipartUploadError(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _check_cancel(stop: threading.Event) -> None:
    if stop.is_set():
        raise MultipartUploadError("storage_upload_cancelled")


def multipart_part_size(length: int) -> int:
    """Select a protocol-compatible size; this is not an in-memory buffer size."""
    if length <= 0:
        raise ValueError("multipart_length_invalid")
    size = max(MIN_PART_BYTES, (length + MAX_PARTS - 1) // MAX_PARTS)
    if size > MAX_PART_BYTES:
        raise MultipartUploadError("storage_protocol_limit")
    return size


def _blocks(stream: BinaryIO, length: int, stop: threading.Event) -> Iterator[bytes]:
    remaining = length
    while remaining:
        _check_cancel(stop)
        block = stream.read(min(remaining, UPLOAD_READ_BYTES))
        if not block or len(block) > remaining or len(block) > UPLOAD_READ_BYTES:
            raise RuntimeError("multipart_source_length_changed")
        remaining -= len(block)
        yield block


def _part_checksum(stream: BinaryIO, length: int, stop: threading.Event) -> str:
    position = stream.tell()
    digest = hashlib.md5(usedforsecurity=False)
    try:
        for block in _blocks(stream, length, stop):
            digest.update(block)
    finally:
        stream.seek(position)
    return base64.b64encode(digest.digest()).decode("ascii")


def upload_seekable_object(
    client: Any,
    *,
    bucket_name: str,
    object_name: str,
    data: BinaryIO,
    length: int,
    content_type: str,
    metadata: dict[str, str],
    stop: threading.Event | None = None,
) -> Any:
    """Blocking operation: caller must join the worker before closing ``data``.

    Content-MD5 lets the server verify the second pass against the first. Each
    successful response supplies the ETag needed by CompleteMultipartUpload.
    Partial uploads are aborted on failure, and errors omit signed URL details.
    """
    from minio.datatypes import Part

    part_size = multipart_part_size(length)
    stop = stop or threading.Event()
    upload_id = None
    try:
        start = data.tell()
        data.seek(start)
        _check_cancel(stop)
        headers = {"Content-Type": content_type}
        headers.update({f"x-amz-meta-{key}": value for key, value in metadata.items()})
        upload_id = client._create_multipart_upload(bucket_name, object_name, headers)
        parts: list[Part] = []
        remaining = length
        # Direct transport has no redirects, env proxy, or Client INFO logging
        # (the latter would otherwise include presigned query credentials).
        with httpx.HTTPTransport(trust_env=False, retries=0) as http:
            while remaining:
                part_length = min(part_size, remaining)
                checksum = _part_checksum(data, part_length, stop)
                number = len(parts) + 1
                url = client.get_presigned_url(
                    "PUT", bucket_name, object_name,
                    expires=timedelta(hours=1),
                    extra_query_params={"uploadId": upload_id, "partNumber": str(number)},
                )
                part_start = data.tell()
                response = http.handle_request(httpx.Request(
                    "PUT", url,
                    headers={"Content-Length": str(part_length), "Content-MD5": checksum},
                    content=_blocks(data, part_length, stop),
                    extensions={"timeout": {"connect": 10.0, "read": 60.0, "write": 60.0, "pool": 60.0}},
                ))
                try:
                    if response.status_code != 200:
                        # Inspect only a bounded error code, never propagate its
                        # body (which may echo credentials or internal paths).
                        body = bytearray()
                        for block in response.iter_raw(chunk_size=1024):
                            _check_cancel(stop)
                            body.extend(block)
                            if len(body) >= 64 * 1024:
                                break
                        code = re.search(rb"<Code>([A-Za-z0-9]+)</Code>", body)
                        full = response.status_code == 507 or (code and code.group(1).decode() in _FULL_CODES)
                        raise MultipartUploadError("storage_write_insufficient" if full else "storage_upload_failed")
                    etag = response.headers.get("etag", "").strip('"')
                    if not etag or len(etag) > 256 or any(ord(char) < 32 for char in etag):
                        raise RuntimeError("multipart_etag_invalid")
                finally:
                    response.close()
                if data.tell() - part_start != part_length:
                    raise RuntimeError("multipart_source_length_changed")
                parts.append(Part(number, etag, size=part_length))
                remaining -= part_length
        if data.tell() - start != length:
            raise RuntimeError("multipart_source_length_changed")
        _check_cancel(stop)
        return client._complete_multipart_upload(bucket_name, object_name, upload_id, parts)
    except BaseException as error:
        if upload_id is not None:
            try:
                client._abort_multipart_upload(bucket_name, object_name, upload_id)
            except BaseException:
                pass
        # HTTP and SDK exceptions can embed signed URLs or provider response data.
        if not isinstance(error, Exception):
            raise
        reason = "storage_upload_failed"
        if isinstance(error, MultipartUploadError):
            reason = error.reason
        elif getattr(error, "code", "") in _FULL_CODES or getattr(error, "errno", None) in {errno.ENOSPC, errno.EDQUOT}:
            reason = "storage_write_insufficient"
        raise MultipartUploadError(reason) from None
