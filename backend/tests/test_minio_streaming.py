import asyncio
import base64
import hashlib
import io
import logging
import threading
from types import SimpleNamespace

import httpx
import pytest

from app.infrastructure.external.file import minio_streaming as module
from app.infrastructure.external.file import miniofile


class Minio:
    def __init__(self, fail_complete=False):
        self.created = []
        self.parts = []
        self.aborted = []
        self.fail_complete = fail_complete

    def _create_multipart_upload(self, *args):
        self.created.append(args)
        return "upload-1"

    def get_presigned_url(self, method, bucket, key, **kwargs):
        number = kwargs["extra_query_params"]["partNumber"]
        return f"http://storage.internal/bucket/object?partNumber={number}&X-Amz-Signature=SECRET"

    def _complete_multipart_upload(self, bucket, key, upload_id, parts):
        if self.fail_complete:
            raise RuntimeError("upstream signed URL SECRET")
        self.parts = parts
        return "completed"

    def _abort_multipart_upload(self, *args):
        self.aborted.append(args)


class StreamingTransport(httpx.BaseTransport):
    def __init__(self, *, status=200, etag=True, body=b"", error=None, after_block=None):
        self.status, self.etag, self.body, self.error = status, etag, body, error
        self.after_block = after_block
        self.lengths = []
        self.chunk_sizes = []
        self.sha256 = hashlib.sha256()

    def handle_request(self, request):
        digest = hashlib.md5(usedforsecurity=False)
        total = 0
        for block in request.stream:
            assert len(block) <= module.UPLOAD_READ_BYTES
            self.chunk_sizes.append(len(block))
            self.sha256.update(block)
            digest.update(block)
            total += len(block)
            if self.after_block:
                self.after_block()
        self.lengths.append(total)
        assert total == int(request.headers["Content-Length"])
        assert request.headers["Content-MD5"] == base64.b64encode(digest.digest()).decode()
        assert request.extensions["timeout"]["write"] == 60
        if self.error:
            raise self.error
        return httpx.Response(self.status, headers={"etag": '"' + digest.hexdigest() + '"'} if self.etag else {},
                              stream=httpx.ByteStream(self.body), request=request)


def install_transport(monkeypatch, transport):
    def factory(**kwargs):
        assert kwargs == {"trust_env": False, "retries": 0}
        return transport
    monkeypatch.setattr(module.httpx, "HTTPTransport", factory)


def upload(client, data, length=None, **kwargs):
    return module.upload_seekable_object(client, bucket_name="bucket", object_name="object", data=data,
        length=length if length is not None else len(data.getvalue()) - data.tell(),
        content_type="application/octet-stream", metadata={"source": "open_data"}, **kwargs)


def test_real_65_mib_upload_reads_bounded_blocks_and_checksums(tmp_path, monkeypatch, caplog):
    path = tmp_path / "large.bin"
    expected = hashlib.sha256()
    chunk = bytes(range(256)) * 4096
    with path.open("wb") as file:
        file.write(b"skip")
        for _ in range(65):
            file.write(chunk)
            expected.update(chunk)
        file.write(b"x" * 17)
        expected.update(b"x" * 17)
    transport = StreamingTransport()
    install_transport(monkeypatch, transport)
    client = Minio()
    caplog.set_level(logging.DEBUG)
    class BoundedReader:
        def __init__(self, file):
            self.file = file
        def read(self, size=-1):
            assert 0 < size <= module.UPLOAD_READ_BYTES
            return self.file.read(size)
        def tell(self):
            return self.file.tell()
        def seek(self, *args):
            return self.file.seek(*args)
    with path.open("rb") as file:
        file.seek(4)
        assert upload(client, BoundedReader(file), 65 * 1024**2 + 17) == "completed"
    assert transport.lengths == [16 * 1024**2] * 4 + [1024**2 + 17]
    assert transport.sha256.hexdigest() == expected.hexdigest()
    assert len(client.parts) == 5
    assert [p.part_number for p in client.parts] == [1, 2, 3, 4, 5]
    assert client.created[0][2] == {"Content-Type": "application/octet-stream", "x-amz-meta-source": "open_data"}
    assert not client.aborted
    assert "SECRET" not in caplog.text and "X-Amz" not in caplog.text


def test_dynamic_part_size_does_not_impose_a_fixed_file_size_cap():
    length = 2 * 1024**4
    size = module.multipart_part_size(length)
    assert size > 200 * 1024**2
    assert (length + size - 1) // size <= 10_000
    assert module.multipart_part_size(10_000 * module.MAX_PART_BYTES) == module.MAX_PART_BYTES
    with pytest.raises(module.MultipartUploadError) as error:
        module.multipart_part_size(10_000 * module.MAX_PART_BYTES + 1)
    assert error.value.reason == "storage_protocol_limit"


@pytest.mark.parametrize("status,etag,body,complete,error,reason", [
    (403, True, b"SECRET", False, None, "storage_upload_failed"),
    (307, True, b"", False, None, "storage_upload_failed"),
    (200, False, b"", False, None, "storage_upload_failed"),
    (200, True, b"", True, None, "storage_upload_failed"),
    (200, True, b"", False, httpx.WriteTimeout("SECRET signed URL"), "storage_upload_failed"),
    (507, True, b"", False, None, "storage_write_insufficient"),
    (503, True, b"<Error><Code>XMinioStorageFull</Code><Message>SECRET</Message></Error>", False, None, "storage_write_insufficient"),
])
def test_failures_abort_and_never_expose_signed_urls(monkeypatch, caplog, status, etag, body, complete, error, reason):
    client = Minio(fail_complete=complete)
    install_transport(monkeypatch, StreamingTransport(status=status, etag=etag, body=body, error=error))
    caplog.set_level(logging.DEBUG)
    with pytest.raises(module.MultipartUploadError) as raised:
        upload(client, io.BytesIO(b"dataset"))
    assert raised.value.reason == reason
    assert "SECRET" not in str(raised.value) + caplog.text
    assert raised.value.__suppress_context__
    assert len(client.aborted) == 1 and not client.parts


def test_short_source_aborts_before_sending(monkeypatch):
    client, transport = Minio(), StreamingTransport()
    install_transport(monkeypatch, transport)
    with pytest.raises(module.MultipartUploadError):
        upload(client, io.BytesIO(b"abc"), length=4)
    assert client.aborted and not transport.lengths


def test_sdk_quota_failure_preserves_only_safe_reason(monkeypatch):
    class QuotaError(Exception):
        code = "XMinioAdminBucketQuotaExceeded"
    client = Minio()
    def fail(*args):
        raise QuotaError("SECRET")
    client._create_multipart_upload = fail
    with pytest.raises(module.MultipartUploadError) as raised:
        upload(client, io.BytesIO(b"abc"))
    assert raised.value.reason == "storage_write_insufficient"
    assert not client.aborted


@pytest.mark.asyncio
async def test_repeated_cancel_stops_multipart_before_completion_and_joins(monkeypatch):
    started, release, aborted = threading.Event(), threading.Event(), threading.Event()
    def after_block():
        started.set()
        assert release.wait(3)
    transport = StreamingTransport(after_block=after_block)
    install_transport(monkeypatch, transport)
    client = Minio()
    def abort(*args):
        aborted.set()
        client.aborted.append(args)
    client._abort_multipart_upload = abort
    data = io.BytesIO(b"x" * (3 * 1024**2))
    task = asyncio.create_task(miniofile._upload_large_with_cancellation(client, bucket_name="bucket",
        object_name="object", data=data, length=3 * 1024**2, content_type="text/plain", metadata={}))
    assert await asyncio.to_thread(started.wait, 3)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and not data.closed
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert aborted.is_set() and not client.parts
    assert len(transport.chunk_sizes) == 1
    assert not data.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("length,mode", [(5, "small"), (-1, "unknown"), (20 * 1024**2, "large")])
async def test_storage_selects_streaming_upload_and_keeps_small_path(monkeypatch, length, mode):
    calls = []
    class Client:
        def put_object(self, **kwargs):
            calls.append(("sdk", kwargs))
        def stat_object(self, *args):
            return SimpleNamespace(size=length if length > 0 else 5)
    class Doc:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
        async def insert(self):
            return self
    async def large(client, **kwargs):
        calls.append(("large", kwargs))
        return None, None
    monkeypatch.setattr(miniofile, "StoredFileDocument", Doc)
    monkeypatch.setattr(miniofile, "_stream_length", lambda _: length)
    monkeypatch.setattr(miniofile, "_upload_large_with_cancellation", large)
    storage = miniofile.MinIOFileStorage.__new__(miniofile.MinIOFileStorage)
    storage._client, storage.bucket_name, storage._bucket_ready = Client(), "bucket", True
    storage.settings = SimpleNamespace(minio_object_prefix="data")
    result = await storage.upload_file(io.BytesIO(b"hello"), "data.txt", "owner", "text/plain")
    assert result.size == (length if length > 0 else 5)
    assert calls[0][0] == ("large" if mode == "large" else "sdk")
    if mode == "unknown":
        assert calls[0][1]["num_parallel_uploads"] == 1
        assert calls[0][1]["part_size"] == 10 * 1024**2
    if mode == "small":
        assert "part_size" not in calls[0][1]
