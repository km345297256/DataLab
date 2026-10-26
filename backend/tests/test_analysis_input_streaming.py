"""Read-only analysis inputs never use the legacy generic file transfer adapter."""
import asyncio
import errno
import io
import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.domain.models.tool_result import ToolResult
from app.domain.models.file import FileInfo
from app.domain.services.agent_task_runner import AgentTaskRunner
from app.domain.utils.file_stream import file_chunks
from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox


def file():
    return FileInfo(file_id="first", filename="data.bin", size=3, content_type="application/octet-stream",
                    upload_date=datetime.now(UTC), user_id="owner")


class Nonseekable:
    def __init__(self, size=3 * 1024**2 + 17):
        self.size = self.remaining = size
        self.headers = {"Content-Length": str(size)}
        self.closed = False

    def seek(self, position):
        raise io.UnsupportedOperation("nonseekable")

    def read(self, size):
        assert 0 < size <= 1024**2
        count = min(self.remaining, size)
        self.remaining -= count
        return b"x" * count

    def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_adapter_streams_nonseekable_source_without_spooling_and_retains_source_ownership(monkeypatch):
    source = Nonseekable()
    probes = []
    sizes = []

    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            if request.method == "HEAD":
                assert source.remaining == source.size
                probes.append(request.url.path)
                return httpx.Response(204)
            assert request.url.path == "/api/v1/file/upload-stream"
            assert request.url.params["path"] == "/home/ubuntu/inputs/data.bin"
            assert request.headers["Content-Length"] == str(source.size)
            async for chunk in request.stream:
                assert 0 < len(chunk) <= 1024**2
                sizes.append(len(chunk))
            return httpx.Response(200, json={"success": True})

    async def forbidden(*args, **kwargs):
        raise AssertionError("Full object spill is forbidden for streaming sandboxes")
    monkeypatch.setattr("app.domain.utils.file_stream.rewindable_file", forbidden)
    sandbox = DockerSandbox.__new__(DockerSandbox)
    sandbox.base_url = "http://sandbox.test"
    async with httpx.AsyncClient(transport=Transport()) as client:
        sandbox.client = client
        result = await sandbox.file_upload(source, "/home/ubuntu/inputs/data.bin")
    assert result.success and sum(sizes) == source.size
    assert len(probes) == 2 and not source.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 405])
async def test_old_sandbox_is_probed_before_nonseekable_stream_is_consumed(status):
    source = Nonseekable(13)
    calls = []

    async def respond(request):
        calls.append(request.url.path)
        if request.method == "HEAD":
            assert source.remaining == 13
            return httpx.Response(status)
        assert request.url.path == "/api/v1/file/upload"
        assert b"x" * 13 in await request.aread()
        return httpx.Response(200, json={"success": True})

    sandbox = DockerSandbox.__new__(DockerSandbox)
    sandbox.base_url = "http://sandbox.test"
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        sandbox.client = client
        assert (await sandbox.file_upload(source, "/tmp/data.bin")).success
    assert calls == ["/api/v1/file/upload-stream", "/api/v1/file/upload"]
    assert not source.closed


@pytest.mark.asyncio
async def test_streaming_upload_rewinds_seekable_stream_and_reuses_capability():
    bodies = []
    methods = []
    async def respond(request):
        methods.append(request.method)
        if request.method == "HEAD":
            return httpx.Response(204)
        bodies.append(await request.aread())
        return httpx.Response(200, json={"success": True})
    sandbox = DockerSandbox.__new__(DockerSandbox)
    sandbox.base_url = "http://sandbox.test"
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        sandbox.client = client
        source = io.BytesIO(b"complete")
        for _ in range(2):
            source.read()
            assert (await sandbox.file_upload(source, "/tmp/data")).success
    assert bodies == [b"complete", b"complete"] and methods == ["HEAD", "HEAD", "POST", "HEAD", "POST"]
    assert not source.closed


@pytest.mark.asyncio
async def test_capability_error_does_not_trigger_legacy_upload_or_consume_input():
    source = Nonseekable()
    def respond(request):
        assert request.method == "HEAD"
        return httpx.Response(503)
    sandbox = DockerSandbox.__new__(DockerSandbox)
    sandbox.base_url = "http://sandbox.test"
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        sandbox.client = client
        with pytest.raises(httpx.HTTPStatusError):
            await sandbox.file_upload(source, "/tmp/data")
    assert source.remaining == source.size


@pytest.mark.asyncio
async def test_storage_preflight_rejection_does_not_send_or_read_the_file():
    source = Nonseekable()
    def respond(request):
        assert request.method == "HEAD"
        return httpx.Response(507 if "size" in request.url.params else 204)
    sandbox = DockerSandbox.__new__(DockerSandbox)
    sandbox.base_url = "http://sandbox.test"
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        sandbox.client = client
        result = await sandbox.file_upload(source, "/tmp/data")
    assert result.success is False and result.data == {"code": "insufficient_storage"}
    assert source.remaining == source.size


@pytest.mark.asyncio
async def test_cancelled_read_is_joined_before_source_can_be_closed():
    started, release = threading.Event(), threading.Event()
    class Source:
        def read(self, size):
            started.set()
            assert release.wait(3)
            return b"x"
    chunks = file_chunks(Source())
    task = asyncio.create_task(anext(chunks))
    try:
        while not started.is_set():
            await asyncio.sleep(.001)
        task.cancel()
        await asyncio.sleep(.01)
        task.cancel()
        await asyncio.sleep(.01)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await chunks.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unavailable", "disk_error", "false_receipt"])
async def test_mount_verification_failure_is_actionable_and_never_falls_back_to_copy(failure):
    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._agent_id = "agent"
    runner._user_id = "owner"
    runner._file_storage = SimpleNamespace(get_file_info=AsyncMock(return_value=file()),
        download_file=AsyncMock(side_effect=AssertionError("no downloads")))
    verify = AsyncMock(return_value=None)
    runner._sandbox = SimpleNamespace(verify_readonly_inputs=verify,
        file_upload=AsyncMock(side_effect=AssertionError("no uploads")))
    if failure == "disk_error":
        verify.side_effect = OSError(errno.ENOSPC, "/private/secret")
    elif failure == "false_receipt":
        verify.return_value = False
    else:
        del runner._sandbox.verify_readonly_inputs
    with pytest.raises(ValueError, match="只读挂载不可用") as raised:
        await runner._sync_file_to_sandbox("first")
    assert "/private" not in str(raised.value)
    runner._file_storage.download_file.assert_not_awaited()
    runner._sandbox.file_upload.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_input_metadata_fails_closed_without_transferring_data():
    runner = AgentTaskRunner.__new__(AgentTaskRunner)
    runner._agent_id = "agent"
    runner._user_id = "owner"
    runner._file_storage = SimpleNamespace(get_file_info=AsyncMock(return_value=None), download_file=AsyncMock())
    runner._sandbox = SimpleNamespace(file_upload=AsyncMock(), verify_readonly_inputs=AsyncMock(return_value=None))
    with pytest.raises(ValueError, match="不存在或无权访问"):
        await runner._sync_file_to_sandbox("first")
    runner._file_storage.download_file.assert_not_awaited()
    runner._sandbox.file_upload.assert_not_awaited()
    runner._sandbox.verify_readonly_inputs.assert_not_awaited()
