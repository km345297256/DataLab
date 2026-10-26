"""Scoped read-only object views for execution helpers, with no disk cache."""
from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import asdict, dataclass, field
import hashlib
import hmac
import json
from pathlib import PurePosixPath
import re
import secrets

from app.domain.models.analysis_input import UPLOAD_INPUT_ROOT, upload_runtime_path
from app.domain.models.file import FileInfo
from app.domain.models.object_file import ObjectFileMountError, ObjectFileReference


GRANT_TTL_SECONDS = 7 * 24 * 60 * 60
READ_CHUNK_BYTES = 4 * 1024 * 1024
_KEY_PREFIX = "datalab:object-mount:"
_GRANT_ID = re.compile(r"^[a-f0-9]{32}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{43}$")


class ObjectMountAccessError(RuntimeError):
    def __init__(self, status_code: int = 404):
        self.status_code = status_code
        super().__init__("Forbidden" if status_code == 403 else "Not found")


@dataclass(frozen=True, slots=True)
class ObjectMountGrant:
    grant_id: str
    token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ObjectMountView:
    relative_path: str
    # Directory entries contain names, never source IDs or object keys.
    children: tuple[tuple[str, bool], ...] | None = None
    reference: ObjectFileReference | None = field(default=None, repr=False)
    user_id: str = field(default="", repr=False)


def _safe_relative_path(path: str) -> str:
    if (not isinstance(path, str) or "\\" in path
            or any(ord(character) < 32 or ord(character) == 127 for character in path)):
        raise ObjectMountAccessError()
    normalized = path.removesuffix("/")
    if normalized and any(part in {"", ".", ".."} for part in normalized.split("/")):
        raise ObjectMountAccessError()
    return normalized


class ObjectMountService:
    def __init__(self, file_storage=None, redis_client=None):
        self._file_storage = file_storage
        self._redis_client = redis_client

    @property
    def files(self):
        if self._file_storage is None:
            from app.infrastructure.external.file.factory import get_file_storage
            self._file_storage = get_file_storage()
        return self._file_storage

    async def _redis(self):
        if self._redis_client is not None:
            return self._redis_client
        from app.infrastructure.storage.redis import get_redis
        redis = get_redis()
        await redis.initialize()
        return redis.client

    @staticmethod
    def _key(grant_id: str) -> str:
        if not isinstance(grant_id, str) or not _GRANT_ID.fullmatch(grant_id):
            raise ObjectMountAccessError()
        return _KEY_PREFIX + grant_id

    async def issue(self, session_id: str, user_id: str, files: Sequence[FileInfo]) -> ObjectMountGrant:
        if (not isinstance(session_id, str) or not session_id.strip()
                or not isinstance(user_id, str) or not user_id.strip() or not files):
            raise ObjectFileMountError("缺少分析会话或已授权的分析文件。")
        entries = {}
        for info in files:
            relative = PurePosixPath(upload_runtime_path(info)).relative_to(UPLOAD_INPUT_ROOT).as_posix()
            if _safe_relative_path(relative) != relative or len(PurePosixPath(relative).parts) != 2:
                raise ObjectFileMountError("分析文件的挂载位置无效。")
            if relative in entries:
                raise ObjectFileMountError("分析文件的挂载位置重复，请重新选择文件。")
            reference = await self.files.get_object_reference(info.file_id, user_id)
            if not reference.etag:
                raise ObjectFileMountError("对象存储未提供文件版本标识，暂不能只读挂载。")
            if info.user_id is not None and info.user_id != user_id:
                raise ObjectFileMountError("文件不存在或无权访问，请重新选择分析文件。")
            if info.size is not None and info.size != reference.size:
                raise ObjectFileMountError("分析文件大小已发生变化，请重新选择文件。")
            entries[relative] = asdict(reference)
        grant = ObjectMountGrant(secrets.token_hex(16), secrets.token_urlsafe(32))
        payload = json.dumps({"version": 1, "owner_id": user_id, "session_id": session_id,
            "token_hash": hashlib.sha256(grant.token.encode()).hexdigest(), "entries": entries},
            ensure_ascii=True, separators=(",", ":"))
        if not await (await self._redis()).set(self._key(grant.grant_id), payload, ex=GRANT_TTL_SECONDS, nx=True):
            raise ObjectFileMountError("暂时无法创建文件挂载，请稍后重试。")
        return grant

    async def _load(self, grant_id: str) -> dict:
        try:
            raw = await (await self._redis()).get(self._key(grant_id))
            grant = json.loads(raw) if raw else None
            if (not isinstance(grant, dict) or grant.get("version") != 1
                    or not isinstance(grant.get("owner_id"), str) or not grant["owner_id"]
                    or not isinstance(grant.get("session_id"), str) or not grant["session_id"]
                    or not isinstance(grant.get("entries"), dict) or not grant["entries"]):
                raise ObjectMountAccessError()
            return grant
        except ObjectMountAccessError:
            raise
        except Exception:
            raise ObjectMountAccessError() from None

    async def _authorize(self, grant_id: str, token: str) -> dict:
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise ObjectMountAccessError(403)
        grant = await self._load(grant_id)
        digest = hashlib.sha256(token.encode()).hexdigest()
        if not hmac.compare_digest(digest, str(grant.get("token_hash", ""))):
            raise ObjectMountAccessError(403)
        return grant

    async def renew(self, grant_id: str, user_id: str, session_id: str) -> bool:
        try:
            grant = await self._load(grant_id)
            if grant["owner_id"] != user_id or grant["session_id"] != session_id:
                return False
            await self._validate_entries(grant)
            # EXPIRE never recreates a grant that was revoked during validation.
            return bool(await (await self._redis()).expire(self._key(grant_id), GRANT_TTL_SECONDS))
        except Exception:
            return False

    async def revoke(self, grant_id: str) -> None:
        await (await self._redis()).delete(self._key(grant_id))

    async def is_active(self, grant_id: str) -> bool:
        """Distinguish confirmed expiry/revocation from a transient Redis outage.

        This private lifecycle check never grants access or recreates a token.
        Missing grants require a fresh scoped mount on the next user request;
        connection errors must not cause writable analysis work to be deleted.
        """
        try:
            key = self._key(grant_id)
        except ObjectMountAccessError:
            return False
        try:
            return bool(await (await self._redis()).exists(key))
        except Exception:
            raise ObjectFileMountError("暂时无法验证分析文件的挂载授权，请稍后重试。") from None

    async def _validate_entries(self, grant: dict, paths: Sequence[str] | None = None) -> dict[str, ObjectFileReference]:
        result = {}
        try:
            for path in paths if paths is not None else grant["entries"]:
                if not path or _safe_relative_path(path) != path:
                    raise ObjectMountAccessError()
                expected = ObjectFileReference(**grant["entries"][path])
                current = await self.files.get_object_reference(expected.file_id, grant["owner_id"])
                if current != expected:
                    raise ObjectMountAccessError()
                result[path] = current
        except ObjectMountAccessError:
            raise
        except Exception:
            raise ObjectMountAccessError() from None
        return result

    async def resolve(self, grant_id: str, token: str, relative_path: str) -> ObjectMountView:
        path = _safe_relative_path(relative_path)
        grant = await self._authorize(grant_id, token)
        if path in grant["entries"]:
            if relative_path.endswith("/"):
                raise ObjectMountAccessError()
            references = await self._validate_entries(grant, [path])
            return ObjectMountView(path, reference=references[path], user_id=grant["owner_id"])
        prefix = path + "/" if path else ""
        descendants = [entry for entry in grant["entries"] if entry.startswith(prefix)]
        if not descendants:
            raise ObjectMountAccessError()
        await self._validate_entries(grant, descendants)
        children = set()
        for descendant in descendants:
            remaining = descendant.removeprefix(prefix)
            name, separator, _ = remaining.partition("/")
            children.add((name, bool(separator)))
        return ObjectMountView(path, tuple(sorted(children)))

    async def read_chunk(self, grant_id: str, token: str, view: ObjectMountView, offset: int, length: int) -> bytes:
        if (view.reference is None or isinstance(offset, bool) or not isinstance(offset, int) or offset < 0
                or isinstance(length, bool) or not isinstance(length, int)
                or not 0 < length <= READ_CHUNK_BYTES or offset + length > view.reference.size):
            raise ObjectMountAccessError()
        grant = await self._authorize(grant_id, token)
        if (grant["owner_id"] != view.user_id or grant["entries"].get(view.relative_path) != asdict(view.reference)):
            raise ObjectMountAccessError()
        try:
            data = await self.files.read_object_range(view.reference, view.user_id, offset=offset, length=length)
            if not isinstance(data, bytes) or len(data) != length:
                raise ObjectMountAccessError()
            return data
        except ObjectMountAccessError:
            raise
        except Exception:
            raise ObjectMountAccessError() from None

    async def chunks(self, grant_id: str, token: str, view: ObjectMountView, start: int, length: int) -> AsyncIterator[bytes]:
        remaining = length
        while remaining:
            count = min(READ_CHUNK_BYTES, remaining)
            yield await self.read_chunk(grant_id, token, view, start, count)
            start += count
            remaining -= count


_service = ObjectMountService()


def get_object_mount_service() -> ObjectMountService:
    return _service
