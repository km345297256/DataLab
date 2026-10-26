"""Adversarial HTTP range and real filesystem-error boundaries for transfers."""
import asyncio
import errno
import hashlib
import json
import os

import httpx
import pytest

from app.domain.external.data_source import SourceConnectorError
from app.infrastructure.external.data_sources import open_data_transfer as module
from app.infrastructure.external.data_sources.open_data_sources import OpenDataSources


KEY = "e" * 32
URL = "https://zenodo.org/api/records/123/files/observations.bin/content"
DATA = bytes(range(256)) * 768
OFFSET = module.CHUNK_BYTES


@pytest.fixture(autouse=True)
def staging(tmp_path, monkeypatch):
    monkeypatch.setenv("OPEN_DATA_DOWNLOAD_ROOT", str(tmp_path))
    return tmp_path


def option():
    # A strong ETag, without a provider checksum, exercises the HTTP proof path
    # rather than the already-complete provider-checksum fast path.
    return {"url": URL, "size": len(DATA), "checksum": None}


async def download(handler):
    source = OpenDataSources(transport=httpx.MockTransport(handler))
    return await module.stream_download(source._client, source._status, option(), transfer_key=KEY)


def manifest(root):
    return json.loads((root / (KEY + ".json")).read_text())


async def seed(*, complete=False):
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield DATA[:OFFSET]
            if not complete:
                raise httpx.ReadError("interrupted before the next block")
            yield DATA[OFFSET:]
    call = download(lambda request: httpx.Response(200, headers={"etag": '"v1"', "content-length": str(len(DATA))}, stream=Body()))
    if complete:
        stream, digest = await call
        stream.close()
        assert digest == hashlib.sha256(DATA).hexdigest()
    else:
        with pytest.raises(SourceConnectorError, match="download_timeout_or_unavailable"):
            await call


class UnreadableBody(httpx.AsyncByteStream):
    async def __aiter__(self):
        pytest.fail("a rejected range response must not append any body bytes")
        yield b"unreachable"


@pytest.mark.asyncio
@pytest.mark.parametrize("content_range", [
    f"bytes {OFFSET + 1}-{len(DATA) - 1}/{len(DATA)}",  # wrong start
    f"bytes {OFFSET}-{len(DATA) - 2}/{len(DATA)}",      # wrong end
    f"bytes {OFFSET}-{len(DATA)}/{len(DATA) + 1}",      # changed total
    f"bytes {OFFSET}-{OFFSET - 1}/{OFFSET}",            # no remaining bytes
    f"bytes {OFFSET}-{len(DATA) - 1}/*",                # unproven total
])
async def test_invalid_206_range_never_appends_or_delivers_and_retry_restarts(staging, content_range):
    await seed()
    prefix = (staging / (KEY + ".part")).read_bytes()
    def invalid(request):
        assert request.headers["range"] == f"bytes={OFFSET}-"
        return httpx.Response(206, headers={"etag": '"v1"', "content-range": content_range}, stream=UnreadableBody())
    with pytest.raises(SourceConnectorError, match="download_integrity_failed"):
        await download(invalid)
    assert (staging / (KEY + ".part")).read_bytes() == prefix == DATA[:OFFSET]
    assert manifest(staging)["invalid"] is True
    def retry(request):
        assert "range" not in request.headers
        return httpx.Response(200, headers={"etag": '"v1"'}, content=DATA)
    stream, digest = await download(retry)
    try:
        assert stream.read() == DATA and digest == hashlib.sha256(DATA).hexdigest()
    finally:
        stream.close()


@pytest.mark.asyncio
async def test_complete_matching_416_reuses_validated_file_without_reading_response(staging):
    await seed(complete=True)
    before = manifest(staging)
    calls = []
    def complete(request):
        calls.append(request)
        assert request.headers["range"] == f"bytes={len(DATA)}-"
        assert request.headers["if-range"] == '"v1"'
        return httpx.Response(416, headers={"etag": '"v1"', "content-range": f"bytes */{len(DATA)}"}, stream=UnreadableBody())
    stream, digest = await download(complete)
    try:
        assert stream.read() == DATA and digest == hashlib.sha256(DATA).hexdigest()
        assert len(calls) == 1 and manifest(staging) == before
    finally:
        stream.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("etag,total,complete", [
    ('"v2"', len(DATA), True),       # same length is not same source version
    (None, len(DATA), True),          # an absent validator is not proof
    ('"v1"', len(DATA) + 1, True),   # claimed complete length disagrees
    ('"v1"', OFFSET, False),        # 416 does not make a partial file complete
])
async def test_unproven_416_restarts_and_never_returns_old_or_mixed_bytes(etag, total, complete):
    await seed(complete=complete)
    replacement, calls = b"z" * len(DATA), []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            assert request.headers["range"] == f"bytes={len(DATA) if complete else OFFSET}-"
            headers = {"content-range": f"bytes */{total}"}
            if etag is not None:
                headers["etag"] = etag
            return httpx.Response(416, headers=headers, stream=UnreadableBody())
        assert "range" not in request.headers and "if-range" not in request.headers
        return httpx.Response(200, headers={"etag": '"v2"'}, content=replacement)
    stream, digest = await download(handler)
    try:
        assert stream.read() == replacement and digest == hashlib.sha256(replacement).hexdigest()
        assert len(calls) == 2
    finally:
        stream.close()


async def resume_verified_prefix():
    def handler(request):
        assert request.headers["range"] == f"bytes={OFFSET}-"
        return httpx.Response(206, headers={"etag": '"v1"', "content-range": f"bytes {OFFSET}-{len(DATA) - 1}/{len(DATA)}"}, content=DATA[OFFSET:])
    stream, digest = await download(handler)
    try:
        assert stream.read() == DATA and digest == hashlib.sha256(DATA).hexdigest()
    finally:
        stream.close()


@pytest.mark.asyncio
async def test_write_enospc_after_partial_os_write_keeps_only_hashed_prefix(staging, monkeypatch):
    acquire = module.Transfer.acquire
    class DiskFullFile:
        def __init__(self, file): self.file, self.writes = file, 0
        def __getattr__(self, name): return getattr(self.file, name)
        def write(self, data):
            self.writes += 1
            if self.writes == 2:
                self.file.write(data[:13])
                raise OSError(errno.ENOSPC, "filesystem full after partial write")
            return self.file.write(data)
    async def faulty_acquire(self):
        transfer = await acquire(self)
        transfer.file = DiskFullFile(transfer.file)
        return transfer
    with monkeypatch.context() as patch:
        patch.setattr(module.Transfer, "acquire", faulty_acquire)
        with pytest.raises(SourceConnectorError, match="storage_insufficient"):
            await download(lambda request: httpx.Response(200, headers={"etag": '"v1"'}, content=DATA))
    saved = manifest(staging)
    assert saved["bytes_saved"] == OFFSET and saved["complete"] is False
    assert saved["prefix_sha256"] == hashlib.sha256(DATA[:OFFSET]).hexdigest()
    assert (staging / (KEY + ".part")).stat().st_size == OFFSET + 13
    # A new instance truncates uncheckpointed bytes and validates the retained
    # prefix before Range; the partial second write must never be appended twice.
    await resume_verified_prefix()
    assert manifest(staging)["complete"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["part", "manifest"])
async def test_fsync_enospc_cannot_publish_complete_and_retry_uses_last_durable_prefix(staging, monkeypatch, target):
    await seed()
    before = manifest(staging)
    part = staging / (KEY + ".part")
    fsync, failures = os.fsync, []
    def failing_fsync(fd):
        is_part = os.fstat(fd).st_ino == part.stat().st_ino
        if part.stat().st_size > OFFSET and is_part == (target == "part"):
            failures.append(fd)
            raise OSError(errno.ENOSPC, "filesystem full during durable checkpoint")
        return fsync(fd)
    with monkeypatch.context() as patch:
        patch.setattr(module.os, "fsync", failing_fsync)
        with pytest.raises(SourceConnectorError, match="storage_insufficient"):
            await resume_verified_prefix()
    assert failures and manifest(staging) == before
    assert before["bytes_saved"] == OFFSET and before["complete"] is False
    assert part.stat().st_size == len(DATA) and not list(staging.glob("*.tmp"))
    # Both fsync sites must release the same writer lock after failure.
    transfer = await asyncio.wait_for(module.Transfer(KEY).acquire(), 1)
    transfer.close()
    await resume_verified_prefix()
    assert manifest(staging)["complete"] is True
