"""Browser downloads remain bounded and resumable for large stored objects."""
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.application.services.file_preview import preview_version
from app.application.services.file_service import FileService
from app.domain.models.file import FileInfo
from app.interfaces.api import file_routes
from app.interfaces.dependencies import get_current_user, get_file_service, verify_signature


class RangedStorage:
    def __init__(self, size=12):
        self.info = FileInfo(file_id='object', filename='observations.bin', content_type='application/octet-stream',
            size=size, upload_date=datetime(2026, 10, 21, tzinfo=UTC), user_id='owner')
        self.reads = []
        self.short = False

    async def get_file_info(self, file_id, user_id=None):
        if user_id is not None and user_id != 'owner':
            raise PermissionError('Not owner')
        return self.info if file_id == 'object' else None

    async def download_file_range(self, file_id, user_id, *, offset, length):
        info = await self.get_file_info(file_id, user_id)
        self.reads.append((offset, length))
        assert length <= 8 * 1024 * 1024
        return b'x' * (length - 1 if self.short else length), info

    async def download_file(self, *args):
        raise AssertionError('Full object buffering is forbidden')


def app_for(storage, owner='owner'):
    app = FastAPI()
    app.include_router(file_routes.router)
    app.dependency_overrides[get_file_service] = lambda: FileService(storage)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=owner)
    app.dependency_overrides[verify_signature] = lambda: 'valid-test-signature'
    from app.application.errors.exceptions import NotFoundError
    from fastapi.responses import JSONResponse
    @app.exception_handler(NotFoundError)
    async def missing(request, error):
        return JSONResponse({'message': 'not found'}, status_code=404)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['/files/object', '/files/object/download'])
async def test_signed_and_owned_downloads_support_resume_without_materializing_large_file(path):
    storage = RangedStorage(11 * 1024**3)
    async with AsyncClient(transport=ASGITransport(app=app_for(storage)), base_url='http://test') as client:
        head = await client.head(path)
        assert head.status_code == 200 and head.content == b''
        assert int(head.headers['content-length']) == storage.info.size
        assert head.headers['accept-ranges'] == 'bytes' and storage.reads == []
        response = await client.get(path, headers={'Range': 'bytes=-1024', 'If-Range': head.headers['etag']})
        assert response.status_code == 206 and len(response.content) == 1024
        assert response.headers['content-range'] == f'bytes {storage.info.size - 1024}-{storage.info.size - 1}/{storage.info.size}'
        assert storage.reads == [(storage.info.size - 1024, 1024)]
        assert response.headers['x-accel-buffering'] == 'no'
        assert response.headers['x-content-type-options'] == 'nosniff'
        assert response.headers['content-security-policy'].startswith('sandbox;')
        assert response.headers['content-disposition'].startswith('attachment;')


@pytest.mark.asyncio
@pytest.mark.parametrize('header', ['bytes=50-', 'bytes=5-3', 'bytes=-0', 'bytes=0-1,4-5', 'bytes=-', 'other=0-1'])
async def test_invalid_ranges_fail_before_opening_storage(header):
    storage = RangedStorage()
    async with AsyncClient(transport=ASGITransport(app=app_for(storage)), base_url='http://test') as client:
        response = await client.get('/files/object/download', headers={'Range': header})
    assert response.status_code == 416 and response.headers['content-range'] == 'bytes */12'
    assert storage.reads == []


@pytest.mark.asyncio
async def test_changed_if_range_restarts_and_ordinary_download_is_complete():
    storage = RangedStorage()
    async with AsyncClient(transport=ASGITransport(app=app_for(storage)), base_url='http://test') as client:
        response = await client.get('/files/object/download', headers={'Range': 'bytes=4-', 'If-Range': '"obsolete"'})
        assert response.status_code == 200 and response.content == b'x' * 12
        assert response.headers['content-length'] == '12'
        assert 'observations.bin' in response.headers['content-disposition']


@pytest.mark.asyncio
async def test_large_transfer_memory_is_bounded_and_disconnect_stops_reads():
    storage = RangedStorage(81 * 1024**2 + 13)
    service = FileService(storage)
    stream = service.download_chunks('object', 'owner', offset=0, length=storage.info.size, version=preview_version(storage.info))
    received = 0
    async for chunk in stream:
        received += len(chunk)
    assert received == storage.info.size and len(storage.reads) == 11
    storage.reads.clear()
    stream = service.download_chunks('object', 'owner', offset=0, length=storage.info.size, version=preview_version(storage.info))
    await anext(stream)
    await stream.aclose()
    assert len(storage.reads) == 1


@pytest.mark.asyncio
async def test_short_storage_read_cannot_be_reported_as_complete():
    storage = RangedStorage()
    storage.short = True
    stream = FileService(storage).download_chunks('object', 'owner', offset=0, length=12, version=preview_version(storage.info))
    with pytest.raises(ValueError, match='Incomplete'):
        await anext(stream)


@pytest.mark.asyncio
async def test_owner_and_private_spill_boundaries_apply_to_ranges():
    storage = RangedStorage()
    async with AsyncClient(transport=ASGITransport(app=app_for(storage, owner='other')), base_url='http://test') as client:
        assert (await client.get('/files/object/download', headers={'Range': 'bytes=0-1'})).status_code == 404
    storage.info.metadata = {'source': 'tool_output_spill'}
    async with AsyncClient(transport=ASGITransport(app=app_for(storage)), base_url='http://test') as client:
        assert (await client.get('/files/object', headers={'Range': 'bytes=0-1'})).status_code == 404
    assert storage.reads == []


@pytest.mark.asyncio
async def test_analysis_upload_spools_large_nonseekable_input_with_bounded_reads():
    from app.domain.utils.file_stream import rewindable_file
    class Source:
        remaining = 67 * 1024**2 + 13
        def read(self, size):
            assert 0 < size <= 1024**2
            count = min(size, self.remaining)
            self.remaining -= count
            return b'x' * count
    source = Source()
    stream = await rewindable_file(source)
    try:
        assert stream._rolled
        stream.seek(0, 2)
        assert stream.tell() == 67 * 1024**2 + 13
        assert source.remaining == 0
    finally:
        stream.close()


@pytest.mark.asyncio
async def test_cancelled_analysis_preparation_joins_read_before_closing_spill(monkeypatch):
    import asyncio
    import threading
    from app.domain.utils import file_stream
    started, release = threading.Event(), threading.Event()
    spills = []
    factory = file_stream.tempfile.SpooledTemporaryFile
    def create(**kwargs):
        target = factory(**kwargs)
        spills.append(target)
        return target
    monkeypatch.setattr(file_stream.tempfile, 'SpooledTemporaryFile', create)
    class Source:
        def read(self, size):
            started.set()
            assert release.wait(3)
            assert not spills[0].closed
            return b'x'
    task = asyncio.create_task(file_stream.rewindable_file(Source()))
    try:
        while not started.is_set():
            await asyncio.sleep(.001)
        task.cancel()
        await asyncio.sleep(.01)
        assert not task.done() and not spills[0].closed
        task.cancel()
        await asyncio.sleep(.01)
        assert not task.done() and not spills[0].closed
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert spills[0].closed
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
