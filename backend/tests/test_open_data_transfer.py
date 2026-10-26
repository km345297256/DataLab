"""Disk streaming, durable restart, HTTP range evidence and writer fencing."""
import asyncio
import hashlib
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest

from app.domain.external.data_source import SourceConnectorError
from app.infrastructure.external.data_sources import open_data_transfer as module
from app.infrastructure.external.data_sources.open_data_sources import OpenDataSources
from app.infrastructure.external.data_sources.open_data_transfer import Transfer, cleanup_transfer, stream_download

KEY = "a" * 32
URL = "https://zenodo.org/api/records/123/files/data.bin/content"
DATA = bytes(range(256)) * 768


@pytest.fixture(autouse=True)
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("OPEN_DATA_DOWNLOAD_ROOT", str(tmp_path))
    return tmp_path


def option(data=DATA, *, checksum=True, size=True):
    return {"url": URL, "size": len(data) if size else None,
            "checksum": "md5:" + hashlib.md5(data).hexdigest() if checksum else None}


def download(handler, chosen=None, progress=None, key=KEY):
    source = OpenDataSources(transport=httpx.MockTransport(handler))
    return stream_download(source._client, source._status, chosen or option(), progress, transfer_key=key)


class BrokenStream(httpx.AsyncByteStream):
    def __init__(self, data=DATA, *, fail=True):
        self.data, self.fail = data, fail

    async def __aiter__(self):
        yield self.data[:module.CHUNK_BYTES]
        if self.fail:
            raise httpx.ReadError("connection lost")
        yield self.data[module.CHUNK_BYTES:]


async def interrupted(*, checksum=True, etag='"v1"'):
    chosen = option(checksum=checksum)
    with pytest.raises(SourceConnectorError, match="download_timeout_or_unavailable"):
        await download(lambda request: httpx.Response(200, headers={"etag": etag, "content-length": str(len(DATA))}, stream=BrokenStream()), chosen)
    return chosen


@pytest.mark.asyncio
async def test_real_over_64mib_stream_uses_disk_and_all_bytes_are_checked(root):
    chunk = b"x" * module.CHUNK_BYTES
    size = 65 * 1024 * 1024
    expected = hashlib.sha256()
    expected_md5 = hashlib.md5()
    for _ in range(size // len(chunk)):
        expected.update(chunk)
        expected_md5.update(chunk)

    class LargeStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(size // len(chunk)):
                yield chunk

    progress = []
    async def update(received, total): progress.append((received, total))
    stream, digest = await download(lambda request: httpx.Response(200, headers={"content-length": str(size)}, stream=LargeStream()),
                                    {"url": URL, "size": size, "checksum": "md5:" + expected_md5.hexdigest()}, update)
    try:
        assert isinstance(stream, module.LockedReader)
        assert os.fstat(stream.fileno()).st_size == size
        assert digest == expected.hexdigest() and progress[-1] == (size, size)
        assert len(stream.read(module.CHUNK_BYTES)) == module.CHUNK_BYTES
    finally:
        stream.close()
    assert (root / (KEY + ".part")).stat().st_size == size
    await cleanup_transfer(KEY)
    assert not (root / (KEY + ".part")).exists() and not (root / (KEY + ".json")).exists()


@pytest.mark.asyncio
async def test_new_instance_resumes_persisted_bytes_with_strong_etag(root):
    chosen = await interrupted(checksum=False)
    manifest = json.loads((root / (KEY + ".json")).read_text())
    assert manifest["bytes_saved"] == module.CHUNK_BYTES
    requests, progress = [], []

    def handler(request):
        requests.append(request)
        assert request.headers["range"] == f"bytes={module.CHUNK_BYTES}-"
        assert request.headers["if-range"] == '"v1"'
        return httpx.Response(206, headers={"etag": '"v1"', "content-range": f"bytes {module.CHUNK_BYTES}-{len(DATA)-1}/{len(DATA)}"}, content=DATA[module.CHUNK_BYTES:])

    async def update(received, total): progress.append((received, total))
    stream, digest = await download(handler, chosen, update)
    try:
        assert stream.read() == DATA and digest == hashlib.sha256(DATA).hexdigest()
        assert progress[-1] == (len(DATA), len(DATA)) and len(requests) == 1
    finally:
        stream.close()


@pytest.mark.asyncio
async def test_checksum_without_etag_is_safe_resume_evidence():
    chosen = await interrupted(etag="")
    def handler(request):
        assert request.headers["range"] == f"bytes={module.CHUNK_BYTES}-"
        assert "if-range" not in request.headers
        return httpx.Response(206, headers={"content-range": f"bytes {module.CHUNK_BYTES}-{len(DATA)-1}/{len(DATA)}"}, content=DATA[module.CHUNK_BYTES:])
    stream, _ = await download(handler, chosen)
    try: assert stream.read() == DATA
    finally: stream.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('algorithm', ['md5', 'sha1', 'sha256', 'sha512', 'adler32'])
async def test_provider_digest_survives_resume_and_verified_cache(algorithm):
    import zlib
    expected = f'{zlib.adler32(DATA):08x}' if algorithm == 'adler32' else hashlib.new(algorithm, DATA).hexdigest()
    chosen = {**option(), 'checksum': algorithm + ':' + expected}
    with pytest.raises(SourceConnectorError, match='download_timeout_or_unavailable'):
        await download(lambda _: httpx.Response(200, headers={'content-length': str(len(DATA))},
                                               stream=BrokenStream()), chosen)
    def resumed(request):
        assert request.headers['range'] == f'bytes={module.CHUNK_BYTES}-'
        return httpx.Response(206, headers={'content-range': f'bytes {module.CHUNK_BYTES}-{len(DATA)-1}/{len(DATA)}'},
                              content=DATA[module.CHUNK_BYTES:])
    stream, digest = await download(resumed, chosen)
    try:
        assert stream.read() == DATA and digest == hashlib.sha256(DATA).hexdigest()
    finally:
        stream.close()
    stream, _ = await download(lambda _: pytest.fail('Verified cache should not re-download'), chosen)
    try:
        assert stream.read() == DATA
    finally:
        stream.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('algorithm,length', [('sha1', 40), ('sha512', 128), ('adler32', 8)])
async def test_new_provider_digest_algorithms_reject_corrupt_bytes(algorithm, length):
    chosen = {**option(), 'checksum': algorithm + ':' + '0' * length}
    with pytest.raises(SourceConnectorError, match='download_integrity_failed'):
        await download(lambda _: httpx.Response(200, content=DATA), chosen)


@pytest.mark.parametrize('checksum', ['crc32:12345678', 'sha1:abc', 'sha256:' + 'g' * 64, {}, 'sha512'])
def test_invalid_provider_checksum_is_not_used_as_resume_evidence(checksum):
    with pytest.raises(SourceConnectorError, match='invalid_response'):
        module.checksum_hasher(checksum)


@pytest.mark.asyncio
@pytest.mark.parametrize("etag", ["", 'W/"v1"'])
async def test_missing_or_weak_version_evidence_restarts_instead_of_combining_responses(etag):
    chosen = await interrupted(checksum=False, etag=etag)
    def handler(request):
        assert "range" not in request.headers
        return httpx.Response(200, content=DATA)
    stream, _ = await download(handler, chosen)
    try: assert stream.read() == DATA
    finally: stream.close()


@pytest.mark.asyncio
async def test_ignored_range_uses_full_200_body_without_appending():
    await interrupted()
    def handler(request):
        assert "range" in request.headers
        return httpx.Response(200, content=DATA, headers={"etag": '"v1"'})
    stream, _ = await download(handler)
    try: assert stream.read() == DATA
    finally: stream.close()


@pytest.mark.asyncio
async def test_changed_response_etag_restarts_and_never_mixes_old_new_bytes():
    chosen = await interrupted(checksum=False)
    replacement = b"z" * len(DATA)
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(206, headers={"etag": '"v2"', "content-range": f"bytes {module.CHUNK_BYTES}-{len(DATA)-1}/{len(DATA)}"}, content=replacement[module.CHUNK_BYTES:])
        assert "range" not in request.headers
        return httpx.Response(200, content=replacement, headers={"etag": '"v2"'})
    stream, digest = await download(handler, chosen)
    try:
        assert stream.read() == replacement and digest == hashlib.sha256(replacement).hexdigest()
        assert len(calls) == 2
    finally: stream.close()


@pytest.mark.asyncio
async def test_changed_authoritative_manifest_resets_old_part():
    await interrupted()
    replacement = b"z" * len(DATA)
    def handler(request):
        assert "range" not in request.headers
        return httpx.Response(200, content=replacement)
    stream, _ = await download(handler, option(replacement))
    try: assert stream.read() == replacement
    finally: stream.close()


@pytest.mark.asyncio
async def test_bad_checksum_is_not_delivered_or_reused_as_complete(root):
    with pytest.raises(SourceConnectorError, match="download_integrity_failed"):
        await download(lambda request: httpx.Response(200, content=b"z" * len(DATA)))
    assert (root / (KEY + ".part")).stat().st_size == len(DATA)
    assert json.loads((root / (KEY + ".json")).read_text())["invalid"] is True
    def handler(request):
        assert "range" not in request.headers
        return httpx.Response(200, content=DATA)
    stream, _ = await download(handler)
    try: assert stream.read() == DATA
    finally: stream.close()


@pytest.mark.asyncio
async def test_unknown_size_stream_is_delivered_at_eof():
    stream, digest = await download(lambda request: httpx.Response(200, stream=BrokenStream(fail=False)), option(size=False))
    try: assert stream.read() == DATA and digest == hashlib.sha256(DATA).hexdigest()
    finally: stream.close()


@pytest.mark.asyncio
async def test_actual_disk_capacity_failure_preserves_partial_and_reports_storage(root, monkeypatch):
    original = module.shutil.disk_usage
    monkeypatch.setattr(module.shutil, "disk_usage", lambda path: SimpleNamespace(free=1))
    with pytest.raises(SourceConnectorError, match="storage_insufficient"):
        await download(lambda request: httpx.Response(200, stream=BrokenStream(fail=False)), option(size=False))
    monkeypatch.setattr(module.shutil, "disk_usage", original)
    # Storage capacity failure releases the same key's exclusive lock.
    transfer = await asyncio.wait_for(Transfer(KEY).acquire(), 1)
    transfer.close()


@pytest.mark.asyncio
async def test_cancellation_preserves_prefix_and_releases_lock(root):
    async def cancel(received, total): raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await download(lambda request: httpx.Response(200, stream=BrokenStream(fail=False), headers={"etag": '"v1"'}), progress=cancel)
    assert json.loads((root / (KEY + ".json")).read_text())["bytes_saved"] == module.CHUNK_BYTES
    transfer = await asyncio.wait_for(Transfer(KEY).acquire(), 1)
    transfer.close()


@pytest.mark.asyncio
async def test_reader_holds_writer_lock_until_upload_finishes_and_waiter_cancel_is_safe():
    stream, _ = await download(lambda request: httpx.Response(200, content=DATA))
    waiting = asyncio.create_task(Transfer(KEY).acquire())
    await asyncio.sleep(0.02)
    assert not waiting.done()
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError): await waiting
    second = asyncio.create_task(Transfer(KEY).acquire())
    await asyncio.sleep(0.02)
    assert not second.done()
    stream.close()
    transfer = await asyncio.wait_for(second, 1)
    transfer.close()


@pytest.mark.asyncio
async def test_complete_verified_file_is_reused_after_upload_failure():
    stream, _ = await download(lambda request: httpx.Response(200, content=DATA))
    stream.close()
    def handler(request): pytest.fail("An already verified source checksum needs no second body transfer")
    stream, _ = await download(handler)
    try: assert stream.read() == DATA
    finally: stream.close()


@pytest.mark.asyncio
async def test_corrupt_disk_prefix_is_not_combined_with_future_ranges(root):
    await interrupted()
    with (root / (KEY + ".part")).open("r+b") as file: file.write(b"tampered")
    def handler(request):
        assert "range" not in request.headers
        return httpx.Response(200, content=DATA)
    stream, _ = await download(handler)
    try: assert stream.read() == DATA
    finally: stream.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["../secret", "", "a" * 31, "A" * 32])
async def test_transfer_keys_are_server_opaque_identifiers(key):
    with pytest.raises(SourceConnectorError, match="invalid_transfer_key"):
        await Transfer(key).acquire()


@pytest.mark.asyncio
async def test_file_lock_excludes_a_separate_process_until_reader_close(root):
    stream, _ = await download(lambda request: httpx.Response(200, content=DATA))
    script = """import fcntl,sys
with open(sys.argv[1], 'r+b') as f:
    try: fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError: sys.exit(3)
"""
    args = [sys.executable, "-c", script, str(root / (KEY + ".lock"))]
    try:
        assert subprocess.run(args, capture_output=True, timeout=3).returncode == 3
    finally:
        stream.close()
    assert subprocess.run(args, capture_output=True, timeout=3).returncode == 0


@pytest.mark.asyncio
async def test_process_crash_recovers_last_fsynced_checkpoint(root):
    script = """import asyncio,hashlib,httpx,os
from app.infrastructure.external.data_sources.open_data_sources import OpenDataSources
from app.infrastructure.external.data_sources.open_data_transfer import stream_download,CHUNK_BYTES,CHECKPOINT_BYTES
class Body(httpx.AsyncByteStream):
    async def __aiter__(self):
        for _ in range(96): yield b'x'*CHUNK_BYTES
async def main():
    source=OpenDataSources(transport=httpx.MockTransport(lambda r:httpx.Response(200,headers={'etag':'"v1"','content-length':str(96*CHUNK_BYTES)},stream=Body())))
    async def progress(received,total):
        if received>=CHECKPOINT_BYTES: os._exit(9)
    await stream_download(source._client,source._status,{'url':'https://zenodo.org/api/records/123/files/data.bin/content','size':96*CHUNK_BYTES,'checksum':None},progress,transfer_key='a'*32)
asyncio.run(main())
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, timeout=10)
    assert result.returncode == 9, result.stderr.decode()
    metadata = json.loads((root / (KEY + ".json")).read_text())
    offset = metadata["bytes_saved"]
    assert offset == module.CHECKPOINT_BYTES
    size = 96 * module.CHUNK_BYTES
    def handler(request):
        assert request.headers["range"] == f"bytes={offset}-"
        return httpx.Response(206, headers={"etag": '"v1"', "content-range": f"bytes {offset}-{size-1}/{size}"}, content=b"x" * (size-offset))
    stream, digest = await download(handler, {"url": URL, "size": size, "checksum": None})
    try:
        assert os.fstat(stream.fileno()).st_size == size
        assert digest == hashlib.sha256(b"x" * size).hexdigest()
    finally: stream.close()


@pytest.mark.asyncio
async def test_cancellation_while_rehashing_preserves_existing_checkpoint(root, monkeypatch):
    await interrupted()
    before = json.loads((root / (KEY + ".json")).read_text())
    started = asyncio.Event()
    async def blocked_hashes(self, size, checksum=None):
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(Transfer, "hashes", blocked_hashes)
    def handler(request): pytest.fail("No network request should precede prefix revalidation")
    task = asyncio.create_task(download(handler))
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    after = json.loads((root / (KEY + ".json")).read_text())
    assert after["bytes_saved"] == before["bytes_saved"] == module.CHUNK_BYTES
    assert after["prefix_sha256"] == before["prefix_sha256"]
