"""Large uploads are atomic, bounded, cancellable and return safe disk errors."""
import asyncio
import errno
import importlib
import threading
from types import SimpleNamespace

import httpx
import pytest
from starlette.requests import ClientDisconnect

from app.core.exceptions import AppException
from app.services.upload_stream import upload_stream
from conftest import app
from conftest import BASE_URL

upload_module = importlib.import_module("app.services.upload_stream")


async def chunks(*parts):
    for part in parts:
        yield part


@pytest.mark.asyncio
async def test_stream_route_bypasses_multipart_and_installs_exact_bytes(tmp_path, monkeypatch):
    from starlette.requests import Request
    async def forbidden(*args, **kwargs):
        raise AssertionError("Multipart parsing would create an unnecessary full-size spool")
    monkeypatch.setattr(Request, "form", forbidden)
    destination = tmp_path / "inputs" / "data.bin"
    content = b"sample\0" * 200000
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        probe = await client.head("/api/v1/file/upload-stream")
        assert probe.status_code == 204
        response = await client.post("/api/v1/file/upload-stream", params={"path": str(destination)},
                                     content=chunks(content[:1024], content[1024:]),
                                     headers={"Content-Length": str(len(content))})
    assert response.status_code == 200 and response.json()["data"]["file_size"] == len(content)
    assert destination.read_bytes() == content
    assert list(destination.parent.iterdir()) == [destination]


@pytest.mark.asyncio
@pytest.mark.parametrize("expected", [2, 8])
async def test_short_or_overlong_body_never_replaces_existing_input(tmp_path, expected):
    destination = tmp_path / "data.bin"
    destination.write_bytes(b"original")
    with pytest.raises(AppException) as raised:
        await upload_stream(str(destination), chunks(b"1234"), expected)
    assert raised.value.data == {"code": "incomplete_upload"}
    assert destination.read_bytes() == b"original" and list(tmp_path.iterdir()) == [destination]


@pytest.mark.asyncio
async def test_capacity_preflight_rejects_before_reading_or_creating_partial_file(tmp_path, monkeypatch):
    monkeypatch.setattr(upload_module.shutil, "disk_usage", lambda _: SimpleNamespace(free=2))
    async def unread():
        raise AssertionError("The body must not be consumed when space is insufficient")
        yield b""
    destination = tmp_path / "data.bin"
    with pytest.raises(AppException) as raised:
        await upload_stream(str(destination), unread(), 3)
    assert raised.value.status_code == 507
    assert raised.value.data == {"code": "insufficient_storage"} and not list(tmp_path.iterdir())
    assert str(tmp_path) not in raised.value.message


def test_real_http_capacity_probe_returns_507_without_request_body(client, tmp_path, monkeypatch):
    monkeypatch.setattr(upload_module.shutil, "disk_usage", lambda _: SimpleNamespace(free=2))
    response = client.head(f"{BASE_URL}/api/v1/file/upload-stream",
                           params={"path": str(tmp_path / "inputs" / "large.bin"), "size": 3})
    assert response.status_code == 507 and response.content == b""
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_enospc_during_write_cleans_partial_file_and_preserves_previous_input(tmp_path, monkeypatch):
    destination = tmp_path / "data.bin"
    destination.write_bytes(b"original")
    real_io = upload_module._joined_io
    async def fail_write(function, *args):
        if function.__name__ == "_write_chunk":
            await real_io(function, *args)
            raise OSError(errno.ENOSPC, "private mount details")
        return await real_io(function, *args)
    monkeypatch.setattr(upload_module, "_joined_io", fail_write)
    with pytest.raises(AppException) as raised:
        await upload_stream(str(destination), chunks(b"new bytes"), None)
    assert raised.value.status_code == 507 and "private" not in raised.value.message
    assert destination.read_bytes() == b"original" and list(tmp_path.iterdir()) == [destination]


@pytest.mark.asyncio
async def test_disconnect_cleans_partial_file(tmp_path):
    destination = tmp_path / "data.bin"
    async def disconnect():
        yield b"partial"
        raise ClientDisconnect()
    with pytest.raises(ClientDisconnect):
        await upload_stream(str(destination), disconnect(), None)
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_repeated_cancellation_joins_pending_write_before_cleanup(tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()
    destination = tmp_path / "data.bin"
    destination.write_bytes(b"original")
    real_io = upload_module._joined_io
    async def blocked_io(function, *args):
        if function.__name__ == "_write_chunk":
            def blocked():
                started.set()
                assert release.wait(3)
                return function(*args)
            return await real_io(blocked)
        return await real_io(function, *args)
    monkeypatch.setattr(upload_module, "_joined_io", blocked_io)
    task = asyncio.create_task(upload_stream(str(destination), chunks(b"new bytes"), None))
    try:
        while not started.is_set():
            await asyncio.sleep(.001)
        task.cancel()
        await asyncio.sleep(.01)
        task.cancel()
        await asyncio.sleep(.01)
        assert not task.done() and len(list(tmp_path.iterdir())) == 2
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert destination.read_bytes() == b"original" and list(tmp_path.iterdir()) == [destination]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_write_jobs_remain_bounded_for_unusually_large_asgi_chunks(tmp_path, monkeypatch):
    sizes = []
    real_io = upload_module._joined_io
    async def observed(function, *args):
        if function.__name__ == "_write_chunk":
            sizes.append(len(args[1]))
        return await real_io(function, *args)
    monkeypatch.setattr(upload_module, "_joined_io", observed)
    await upload_stream(str(tmp_path / "data"), chunks(b"x" * (3 * 1024**2 + 17)), None)
    assert sizes == [1024**2, 1024**2, 1024**2, 17]


@pytest.mark.asyncio
async def test_empty_and_unknown_length_uploads_are_supported(tmp_path):
    empty = tmp_path / "empty"
    assert (await upload_stream(str(empty), chunks(), 0)).file_size == 0
    unknown = tmp_path / "unknown"
    assert (await upload_stream(str(unknown), chunks(b"one", b"two"), None)).file_size == 6
    assert empty.read_bytes() == b"" and unknown.read_bytes() == b"onetwo"


def test_short_writes_are_completed_and_zero_write_fails_closed():
    class ShortWriter:
        content = b""
        def write(self, content):
            self.content += bytes(content[:2])
            return min(2, len(content))
    target = ShortWriter()
    upload_module._write_chunk(target, memoryview(b"complete"))
    assert target.content == b"complete"
    target.write = lambda _: 0
    with pytest.raises(OSError):
        upload_module._write_chunk(target, memoryview(b"incomplete"))
