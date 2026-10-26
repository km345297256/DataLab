"""Server-only identities for read-only object storage mounts.

These references are deliberately separate from FileInfo and API schemas: the
object key is an internal storage location, never a browser-visible file path.
"""
from dataclasses import dataclass, field
import re


class ObjectFileMountError(RuntimeError):
    """An actionable mount failure whose message contains no storage location."""


UNSUPPORTED_OBJECT_MOUNT = (
    "该文件尚未存储在对象存储中，暂不能只读挂载；请联系管理员迁移后再分析。"
)
INVALID_OBJECT_REFERENCE = "文件的对象存储信息无效，暂不能只读挂载；请联系管理员检查。"


@dataclass(frozen=True, slots=True)
class ObjectFileReference:
    file_id: str
    bucket: str = field(repr=False)
    object_key: str = field(repr=False)
    size: int
    etag: str | None = None
    version_id: str | None = None

    def __post_init__(self) -> None:
        # The object namespace will be projected into a FUSE mount. Reject
        # names whose filesystem interpretation differs from their S3 key.
        if (
            not isinstance(self.file_id, str)
            or not re.fullmatch(r"minio:[A-Za-z0-9_-]+", self.file_id)
            or not isinstance(self.bucket, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", self.bucket)
            or ".." in self.bucket
            or not isinstance(self.object_key, str)
            or not self.object_key
            or len(self.object_key.encode("utf-8")) > 1024
            or "\\" in self.object_key
            or any(ord(character) < 32 or ord(character) == 127 for character in self.object_key)
            or any(component in {"", ".", ".."} for component in self.object_key.split("/"))
            or isinstance(self.size, bool)
            or not isinstance(self.size, int)
            or self.size < 0
            or (self.etag is not None and (not isinstance(self.etag, str)
                or not re.fullmatch(r"[A-Za-z0-9._-]{1,256}", self.etag)))
            or (self.version_id is not None and (not isinstance(self.version_id, str)
                or len(self.version_id) > 1024
                or any(ord(character) < 32 or ord(character) == 127 for character in self.version_id)))
        ):
            raise ObjectFileMountError(INVALID_OBJECT_REFERENCE)
