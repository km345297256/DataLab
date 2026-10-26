"""Disk-backed, fenced HTTP transfers. Paths never cross the service boundary."""
from __future__ import annotations

import asyncio
import errno
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import zlib
from uuid import uuid4

import httpx

from app.domain.external.data_source import SourceConnectorError

CHUNK_BYTES = 64 * 1024
CHECKPOINT_BYTES = 4 * 1024 * 1024


def transfer_root():
    root = Path(os.environ.get("OPEN_DATA_DOWNLOAD_ROOT") or Path(tempfile.gettempdir()) / "datalab-open-data-downloads")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def storage_error(error):
    return SourceConnectorError("storage_insufficient" if getattr(error, "errno", None) in {errno.ENOSPC, errno.EDQUOT}
                                else "transfer_storage_unavailable")


class Transfer:
    """One flock is retained through storage upload, not merely HTTP download."""
    def __init__(self, key=None):
        if key is not None and not re.fullmatch(r"[a-f0-9]{32}", key):
            raise SourceConnectorError("invalid_transfer_key")
        self.key, self.anonymous = key or uuid4().hex, key is None
        try:
            self.root = transfer_root()
        except OSError as error:
            raise storage_error(error) from None
        self.part = self.root / (self.key + ".part")
        self.manifest = self.root / (self.key + ".json")
        self.lock = None
        self.file = None

    async def acquire(self):
        try:
            fd = os.open(self.root / (self.key + ".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            self.lock = os.fdopen(fd, "r+b", buffering=0)
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.05)
            fd = os.open(self.part, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            self.file = os.fdopen(fd, "r+b", buffering=0)
            return self
        except BaseException as error:
            self.close()
            if isinstance(error, OSError):
                raise storage_error(error) from None
            raise

    def metadata(self):
        try:
            with self.manifest.open("r") as file:
                value = json.load(file)
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def space(self, additional):
        if additional > shutil.disk_usage(self.root).free:
            raise SourceConnectorError("storage_insufficient")

    def reset(self):
        self.file.seek(0)
        self.file.truncate(0)

    def write(self, data):
        try:
            self.space(len(data))
            remaining = memoryview(data)
            while remaining:
                written = self.file.write(remaining)
                if not written:
                    raise SourceConnectorError("transfer_storage_unavailable")
                remaining = remaining[written:]
        except OSError as error:
            raise storage_error(error) from None

    def save(self, metadata):
        temporary = self.root / (self.key + "." + uuid4().hex + ".tmp")
        try:
            self.file.flush()
            os.fsync(self.file.fileno())
            with temporary.open("x", encoding="utf-8") as file:
                json.dump(metadata, file, ensure_ascii=True)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.manifest)
        except OSError as error:
            raise storage_error(error) from None
        finally:
            temporary.unlink(missing_ok=True)

    async def hashes(self, size, checksum=None):
        sha, provider_hash = hashlib.sha256(), checksum_hasher(checksum)
        self.file.seek(0)
        remaining = size
        while remaining:
            chunk = self.file.read(min(1024 * 1024, remaining))
            if not chunk:
                raise SourceConnectorError("download_integrity_failed")
            sha.update(chunk)
            provider_hash.update(chunk)
            remaining -= len(chunk)
            await asyncio.sleep(0)
        return sha, provider_hash

    def reader(self):
        self.file.seek(0)
        return LockedReader(self)

    def discard(self):
        self.part.unlink(missing_ok=True)
        self.manifest.unlink(missing_ok=True)
        for temporary in self.root.glob(self.key + ".*.tmp"):
            temporary.unlink(missing_ok=True)

    def close(self):
        if self.file is not None:
            self.file.close()
            self.file = None
        if self.lock is not None:
            try:
                if self.anonymous:
                    self.discard()
            finally:
                fcntl.flock(self.lock.fileno(), fcntl.LOCK_UN)
                self.lock.close()
                self.lock = None
        # Lock files intentionally remain: unlinking a flock inode while a
        # waiting process holds its fd would create two independent locks.


class LockedReader(io.BufferedReader):
    def __init__(self, transfer):
        self.transfer = transfer
        super().__init__(transfer.file)

    def close(self):
        try:
            super().close()
        finally:
            self.transfer.close()


async def cleanup_transfer(key):
    transfer = await Transfer(key).acquire()
    try:
        transfer.discard()
    finally:
        transfer.close()


def strong_etag(value):
    return value if value and re.fullmatch(r'"[^"\r\n]*"', value) else None


class Adler32Hasher:
    """Incremental checksum used by CERN's published file manifests."""

    def __init__(self):
        self.value = 1

    def update(self, data):
        self.value = zlib.adler32(data, self.value)

    def hexdigest(self):
        return f"{self.value & 0xffffffff:08x}"


def checksum_hasher(checksum):
    """Validate and preserve the algorithm supplied by a trusted manifest."""
    if checksum is None:
        return hashlib.md5()
    if not isinstance(checksum, str):
        raise SourceConnectorError("invalid_response")
    algorithm, separator, digest = checksum.partition(":")
    lengths = {"md5": 32, "sha1": 40, "sha256": 64, "sha512": 128, "adler32": 8}
    if not separator or algorithm not in lengths or not re.fullmatch(r"[a-f0-9]{%d}" % lengths[algorithm], digest):
        raise SourceConnectorError("invalid_response")
    return Adler32Hasher() if algorithm == "adler32" else hashlib.new(algorithm)


async def stream_download(client_factory, status, option, progress=None, *, transfer_key=None, response_opener=None):
    provider_hash = checksum_hasher(option.get("checksum"))
    transfer = await Transfer(transfer_key).acquire()
    fingerprint = {key: option.get(key) for key in ("url", "size", "checksum")}
    metadata = transfer.metadata()
    sha = hashlib.sha256()
    received, total = 0, option.get("size")
    handed_off = False
    checkpoint_ready = False
    try:
        if metadata.get("source") == fingerprint and not metadata.get("invalid"):
            offset = metadata.get("bytes_saved", 0)
            actual = os.fstat(transfer.file.fileno()).st_size
            if type(offset) is int and 0 < offset <= actual:
                transfer.file.truncate(offset)
                sha, provider_hash = await transfer.hashes(offset, option.get("checksum"))
                if sha.hexdigest() == metadata.get("prefix_sha256"):
                    received = offset
                    total = metadata.get("total", total)
                    checkpoint_ready = True
        if received and metadata.get("complete") and option.get("checksum"):
            expected_hash = option["checksum"].split(":", 1)[1]
            if provider_hash.hexdigest() == expected_hash:
                if progress:
                    await progress(received, total)
                handed_off = True
                return transfer.reader(), sha.hexdigest()
        # A strong ETag or a source checksum is required to combine responses.
        resumable = bool(received and (strong_etag(metadata.get("etag")) or option.get("checksum")))
        if not resumable:
            transfer.reset()
            received, sha, provider_hash = 0, hashlib.sha256(), checksum_hasher(option.get("checksum"))
            metadata = {}
        async with client_factory() as client:
            for attempt in range(2):
                headers = {"Accept-Encoding": "identity"}
                if received:
                    headers["Range"] = f"bytes={received}-"
                    if strong_etag(metadata.get("etag")):
                        headers["If-Range"] = metadata["etag"]
                response_context = (response_opener(client, option, headers=headers) if response_opener
                                    else client.stream("GET", option["url"], headers=headers))
                async with response_context as response:
                    etag = strong_etag(response.headers.get("etag"))
                    if received and response.status_code == 416:
                        if (metadata.get("complete") and etag and etag == metadata.get("etag")
                                and response.headers.get("content-range") == f"bytes */{received}"):
                            handed_off = True
                            return transfer.reader(), sha.hexdigest()
                        transfer.reset()
                        received, sha, provider_hash, metadata = 0, hashlib.sha256(), checksum_hasher(option.get("checksum")), {}
                        continue
                    if received and response.status_code == 206:
                        match = re.fullmatch(r"bytes ([0-9]+)-([0-9]+)/([0-9]+)", response.headers.get("content-range", ""))
                        if not match:
                            raise SourceConnectorError("download_integrity_failed")
                        start, end, declared_total = map(int, match.groups())
                        changed = (metadata.get("etag") and etag != metadata["etag"])
                        if changed:
                            transfer.reset()
                            received, sha, provider_hash, metadata = 0, hashlib.sha256(), checksum_hasher(option.get("checksum")), {}
                            continue
                        if (start != received or end != declared_total - 1 or declared_total <= received
                                or total is not None and total != declared_total):
                            raise SourceConnectorError("download_integrity_failed")
                        declared_body = declared_total - received
                        total = declared_total
                    else:
                        status(response)
                        if response.headers.get("content-range"):
                            raise SourceConnectorError("download_integrity_failed")
                        # The source may ignore Range or fail If-Range. This
                        # 200 body is a fresh full response, never appended.
                        transfer.reset()
                        received, sha, provider_hash = 0, hashlib.sha256(), checksum_hasher(option.get("checksum"))
                        declared_body = None
                        total = option.get("size")
                    length = response.headers.get("content-length")
                    if length is not None:
                        if not length.isdigit():
                            raise SourceConnectorError("download_integrity_failed")
                        length = int(length)
                        if declared_body is not None and length != declared_body:
                            raise SourceConnectorError("download_integrity_failed")
                        if not received:
                            if total is not None and length != total:
                                raise SourceConnectorError("download_integrity_failed")
                            total = length
                    if response.headers.get("content-encoding", "identity").lower() not in ("", "identity"):
                        raise SourceConnectorError("download_integrity_failed")
                    if total is not None:
                        transfer.space(max(0, total - received))
                    metadata = {"source": fingerprint, "etag": etag, "last_modified": response.headers.get("last-modified"),
                                "total": total, "bytes_saved": received, "prefix_sha256": sha.hexdigest(), "complete": False}
                    checkpoint_ready = True
                    transfer.file.seek(received)
                    transfer.save(metadata)
                    checkpoint = received
                    async for chunk in response.aiter_bytes(CHUNK_BYTES):
                        if total is not None and received + len(chunk) > total:
                            raise SourceConnectorError("download_integrity_failed")
                        transfer.write(chunk)
                        sha.update(chunk)
                        provider_hash.update(chunk)
                        received += len(chunk)
                        if received - checkpoint >= CHECKPOINT_BYTES:
                            metadata.update(bytes_saved=received, prefix_sha256=sha.hexdigest())
                            transfer.save(metadata)
                            checkpoint = received
                        if progress:
                            await progress(received, total)
                    if not received or total is not None and received != total:
                        raise SourceConnectorError("download_integrity_failed")
                    checksum = option.get("checksum")
                    if checksum:
                        expected_hash = checksum.split(":", 1)[1]
                        if provider_hash.hexdigest() != expected_hash:
                            raise SourceConnectorError("download_integrity_failed")
                    metadata.update(bytes_saved=received, total=received, prefix_sha256=sha.hexdigest(), complete=True)
                    transfer.save(metadata)
                    handed_off = True
                    return transfer.reader(), sha.hexdigest()
            raise SourceConnectorError("download_integrity_failed")
    except BaseException as error:
        # Durable checkpoints retain only bytes whose digest was persisted.
        if checkpoint_ready and metadata.get("source") == fingerprint:
            metadata.update(bytes_saved=received, prefix_sha256=sha.hexdigest())
            if isinstance(error, SourceConnectorError) and error.reason == "download_integrity_failed":
                metadata["invalid"] = True
            try:
                transfer.save(metadata)
            except (OSError, SourceConnectorError):
                pass
        if isinstance(error, (httpx.HTTPError, TimeoutError)):
            raise SourceConnectorError("download_timeout_or_unavailable") from None
        if isinstance(error, OSError):
            raise storage_error(error) from None
        raise
    finally:
        if not handed_off:
            transfer.close()
