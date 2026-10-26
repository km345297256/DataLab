from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import quote

from fastapi import FastAPI
import httpx
import pytest

from app.application.services.object_mount_service import (
    GRANT_TTL_SECONDS, READ_CHUNK_BYTES, ObjectMountAccessError, ObjectMountService,
    get_object_mount_service,
)
from app.domain.models.analysis_input import assign_upload_namespace, upload_runtime_path, UPLOAD_INPUT_ROOT
from app.domain.models.file import FileInfo
from app.domain.models.object_file import ObjectFileMountError, ObjectFileReference
from app.infrastructure.external.file import miniofile
from app.interfaces.api.object_mount_routes import router


class MemoryRedis:
    def __init__(self):
        self.values = {}
        self.expirations = {}
    async def set(self, key, value, *, ex, nx):
        if nx and key in self.values:
            return False
        self.values[key] = value
        self.expirations[key] = ex
        return True
    async def get(self, key):
        return self.values.get(key)
    async def exists(self, key):
        return int(key in self.values)
    async def expire(self, key, seconds):
        if key not in self.values:
            return False
        self.expirations[key] = seconds
        return True
    async def delete(self, key):
        return self.values.pop(key, None) is not None


class Objects:
    def __init__(self, payloads):
        self.payloads = payloads
        self.references = {key: ObjectFileReference(key, "private-bucket", "private/" + key[6:], len(value),
            etag=hashlib.md5(value, usedforsecurity=False).hexdigest(), version_id="one") for key, value in payloads.items()}
        self.calls = []
        self.owner = "owner"
        self.fail_read = False
    async def get_object_reference(self, file_id, user_id):
        if user_id != self.owner or file_id not in self.references:
            raise ObjectFileMountError("Access denied")
        return self.references[file_id]
    async def read_object_range(self, reference, user_id, *, offset, length):
        assert 0 < length <= READ_CHUNK_BYTES
        if self.fail_read or await self.get_object_reference(reference.file_id, user_id) != reference:
            raise ObjectFileMountError("Changed private-bucket")
        self.calls.append((reference.file_id, offset, length))
        return self.payloads[reference.file_id][offset:offset + length]


async def setup(payload=b"abcdefghij", filename="数据 & <样本>.h5"):
    files = assign_upload_namespace([FileInfo(file_id="minio:selected", filename=filename, size=len(payload), user_id="owner")])
    objects = Objects({"minio:selected": payload, "minio:unselected": b"secret"})
    redis = MemoryRedis()
    service = ObjectMountService(objects, redis)
    grant = await service.issue("session", "owner", files)
    path = upload_runtime_path(files[0]).removeprefix(UPLOAD_INPUT_ROOT + "/")
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_object_mount_service] = lambda: service
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://backend")
    root = "/api/v1/internal/object-mounts/" + grant.grant_id + "/"
    return SimpleNamespace(files=files, objects=objects, redis=redis, service=service, grant=grant,
        path=path, app=app, client=client, root=root, headers={"Authorization": "Bearer " + grant.token})


@pytest.mark.asyncio
async def test_grant_persists_only_hashed_secret_with_scoped_metadata_and_ttl():
    context = await setup()
    async with context.client:
        raw = next(iter(context.redis.values.values()))
        assert context.grant.token not in raw and context.grant.token not in repr(context.grant)
        manifest = json.loads(raw)
        assert manifest["token_hash"] == hashlib.sha256(context.grant.token.encode()).hexdigest()
        assert manifest["owner_id"] == "owner" and manifest["session_id"] == "session"
        assert list(manifest["entries"]) == [context.path]
        assert list(context.redis.expirations.values()) == [GRANT_TTL_SECONDS]


@pytest.mark.asyncio
async def test_active_grant_check_distinguishes_expiry_without_recreating_authorization():
    context = await setup()
    async with context.client:
        assert await context.service.is_active(context.grant.grant_id)
        await context.service.revoke(context.grant.grant_id)
        assert not await context.service.is_active(context.grant.grant_id)
        assert context.redis.values == {}


@pytest.mark.asyncio
async def test_active_grant_check_does_not_treat_redis_outage_as_revocation(monkeypatch):
    context = await setup()
    async def unavailable(_key):
        raise ConnectionError("private Redis endpoint")
    monkeypatch.setattr(context.redis, "exists", unavailable)
    async with context.client:
        with pytest.raises(ObjectFileMountError) as error:
            await context.service.is_active(context.grant.grant_id)
        assert "private Redis" not in str(error.value)
        assert context.redis.values


@pytest.mark.asyncio
async def test_directory_names_are_escaped_and_head_exposes_no_storage_identifiers():
    context = await setup()
    async with context.client as client:
        root = await client.get(context.root, headers=context.headers)
        namespace, filename = context.path.split("/")
        assert root.status_code == 200 and f'href="{namespace}/"' in root.text
        listing = await client.get(context.root + namespace + "/", headers=context.headers)
        assert f'href="{quote(filename, safe="")}"' in listing.text
        assert "&amp;" in listing.text and "&lt;样本&gt;" in listing.text
        response = await client.head(context.root + quote(context.path), headers=context.headers)
        assert response.status_code == 200 and response.headers["content-length"] == "10"
        assert response.headers["accept-ranges"] == "bytes" and response.headers["cache-control"] == "no-store"
        assert response.headers["etag"] == '"' + context.objects.references["minio:selected"].etag + '"'
        assert response.content == b"" and not context.objects.calls
        for response in (root, listing, response):
            public = response.text + str(response.headers)
            assert "private-bucket" not in public and "minio:" not in public and "secret" not in public
        assert not context.app.openapi()["paths"]


@pytest.mark.asyncio
@pytest.mark.parametrize("headers,query,expected", [
    ({}, "", 403), ({"Authorization": "Bearer " + "x" * 43}, "", 403),
    ({"Authorization": "Basic secret"}, "", 403), ({}, "?token=secret", 404),
])
async def test_authentication_never_uses_browser_session_or_url_tokens(headers, query, expected):
    context = await setup()
    async with context.client as client:
        response = await client.get(context.root + query, headers=headers, cookies={"session": "user-cookie"})
        assert response.status_code == expected
        assert response.headers["cache-control"] == "no-store"
        assert not context.objects.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"])
async def test_gateway_rejects_mutating_and_other_methods(method):
    context = await setup()
    async with context.client as client:
        response = await client.request(method, context.root, headers=context.headers)
        assert response.status_code == 403 and not context.objects.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["private-bucket", "minio:unselected", "%2e%2e%2fsecret", "x%5cy", "x//y", "x%00y"])
async def test_unknown_and_traversal_paths_never_resolve_to_objects(path):
    context = await setup()
    async with context.client as client:
        response = await client.get(context.root + path, headers=context.headers)
        assert response.status_code == 404 and not context.objects.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("range_value,body,content_range", [
    ("bytes=2-5", b"cdef", "bytes 2-5/10"),
    ("bytes=8-", b"ij", "bytes 8-9/10"),
    ("bytes=-3", b"hij", "bytes 7-9/10"),
    ("bytes=-100", b"abcdefghij", "bytes 0-9/10"),
    ("bytes=8-100", b"ij", "bytes 8-9/10"),
])
async def test_exact_partial_reads_support_seek_and_suffix_ranges(range_value, body, content_range):
    context = await setup()
    async with context.client as client:
        response = await client.get(context.root + quote(context.path), headers={**context.headers, "Range": range_value})
        assert response.status_code == 206 and response.content == body
        assert response.headers["content-range"] == content_range
        assert response.headers["content-length"] == str(len(body))
        assert sum(call[2] for call in context.objects.calls) == len(body)


@pytest.mark.asyncio
@pytest.mark.parametrize("range_value", ["bytes=10-", "bytes=-0", "bytes=7-3", "bytes=-", "bytes=0-1,3-4", "items=0-1"])
async def test_invalid_ranges_fail_without_read(range_value):
    context = await setup()
    async with context.client as client:
        response = await client.get(context.root + quote(context.path), headers={**context.headers, "Range": range_value})
        assert response.status_code == 416 and response.headers["content-range"] == "bytes */10"
        assert not context.objects.calls


@pytest.mark.asyncio
async def test_large_full_and_partial_requests_read_only_bounded_chunks():
    payload = b"a" * (READ_CHUNK_BYTES * 2 + 123)
    context = await setup(payload)
    async with context.client as client:
        response = await client.get(context.root + quote(context.path), headers=context.headers)
        assert response.status_code == 200 and response.content == payload
        assert [call[2] for call in context.objects.calls] == [READ_CHUNK_BYTES, READ_CHUNK_BYTES, 123]
        context.objects.calls.clear()
        response = await client.get(context.root + quote(context.path), headers={**context.headers, "Range": "bytes=7-"})
        assert response.status_code == 206 and response.content == payload[7:]
        assert max(call[2] for call in context.objects.calls) == READ_CHUNK_BYTES


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["owner", "etag", "size", "missing", "read_failure"])
async def test_changed_or_unavailable_object_fails_closed_before_success_headers(change):
    context = await setup()
    if change == "owner":
        context.objects.owner = "different-owner"
    elif change in {"etag", "size"}:
        ref = context.objects.references["minio:selected"]
        context.objects.references["minio:selected"] = replace(ref, **{change: "changed" if change == "etag" else 9})
    elif change == "missing":
        del context.objects.references["minio:selected"]
    else:
        context.objects.fail_read = True
    async with context.client as client:
        response = await client.get(context.root + quote(context.path), headers=context.headers)
        assert response.status_code == 404 and "private-bucket" not in response.text


@pytest.mark.asyncio
async def test_renew_is_owner_and_session_scoped_and_revoke_interrupts_later_chunks():
    context = await setup(b"x" * (READ_CHUNK_BYTES + 1))
    async with context.client:
        service, grant = context.service, context.grant
        assert not await service.renew(grant.grant_id, "other", "session")
        assert not await service.renew(grant.grant_id, "owner", "other")
        assert await service.renew(grant.grant_id, "owner", "session")
        view = await service.resolve(grant.grant_id, grant.token, context.path)
        chunks = service.chunks(grant.grant_id, grant.token, view, 0, view.reference.size)
        assert len(await anext(chunks)) == READ_CHUNK_BYTES
        await service.revoke(grant.grant_id)
        with pytest.raises(ObjectMountAccessError):
            await anext(chunks)
        assert not await service.renew(grant.grant_id, "owner", "session")
        assert not context.redis.values


@pytest.mark.asyncio
async def test_changed_grant_cannot_be_renewed_and_expired_grants_cannot_authorize():
    context = await setup()
    async with context.client as client:
        context.objects.references["minio:selected"] = replace(context.objects.references["minio:selected"], etag="new")
        assert not await context.service.renew(context.grant.grant_id, "owner", "session")
        context.redis.values.clear()
        response = await client.head(context.root + quote(context.path), headers=context.headers)
        assert response.status_code == 404


@pytest.mark.asyncio
async def test_storage_range_uses_version_and_if_match_without_entire_object_download():
    reference = ObjectFileReference("minio:one", "bucket", "files/data.h5", 100, etag="etag-one", version_id="version-one")
    body = SimpleNamespace(read=Mock(return_value=b"xyz"), close=Mock(), release_conn=Mock())
    client = SimpleNamespace(get_object=Mock(return_value=body))
    storage = miniofile.MinIOFileStorage.__new__(miniofile.MinIOFileStorage)
    storage._client = client
    async def current(file_id, user_id):
        assert file_id == reference.file_id and user_id == "owner"
        return reference
    storage.get_object_reference = current
    assert await storage.read_object_range(reference, "owner", offset=20, length=3) == b"xyz"
    client.get_object.assert_called_once_with("bucket", "files/data.h5", offset=20, length=3,
        version_id="version-one", request_headers={"If-Match": '"etag-one"'})
    body.close.assert_called_once()
    body.release_conn.assert_called_once()
