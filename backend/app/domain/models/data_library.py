"""Durable personal data assets and the selected inputs of an exploration."""
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.models.data_candidate import OpaqueId, identity

SourceType = Literal["upload", "open_data", "analysis"]
FolderId = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]


def canonical_file_id(value: str) -> str:
    # Hybrid storage accepts either spelling for a legacy GridFS object. All
    # retention and selection records must use the same identity.
    return value.removeprefix("gridfs:")


def logical_file_path(value: str) -> str:
    """Validate display-only paths; these are never storage or mount paths."""
    if (not isinstance(value, str) or not value or len(value.encode("utf-8")) > 1024
            or "\\" in value or ":" in value
            or any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in value)):
        raise ValueError("文件目录必须是不含系统路径的相对目录")
    parts = value.split("/")
    if len(parts) > 16 or any(
        part in {"", ".", ".."} or part != part.strip() or len(part.encode("utf-8")) > 255
        for part in parts
    ):
        raise ValueError("文件目录格式不正确或目录层级过多")
    return value


def logical_directory_path(value: str) -> str:
    value = logical_file_path(value)
    if len(value.split("/")) > 15:
        raise ValueError("文件夹最多支持 15 层目录")
    return value


def library_folder_id(owner: str, path: str) -> str:
    """Stable owner-scoped identities also let legacy implicit directories merge."""
    return identity(["data_library_folder", owner, logical_directory_path(path)])[:32]


class LibraryInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SelectedDataInput(LibraryInput):
    file_ids: list[OpaqueId] = Field(default_factory=list, max_length=32)

    @field_validator("file_ids")
    @classmethod
    def unique_files(cls, values: list[str]) -> list[str]:
        values = [canonical_file_id(value) for value in values]
        if len(values) != len(set(values)):
            raise ValueError("文件不能重复")
        return values


class CreateLibraryAsset(SelectedDataInput):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    tags: list[str] = Field(default_factory=list, max_length=10)
    source_type: SourceType | None = None
    session_id: OpaqueId | None = None
    file_ids: list[OpaqueId] = Field(min_length=1)
    # Explicit logical paths supplied by the public file UI. Do not derive these
    # from FileInfo.file_path or storage metadata, which may contain host paths.
    file_paths: dict[OpaqueId, str] = Field(default_factory=dict)
    # Omitted keeps legacy behavior. Explicit null selects the virtual root.
    # Any file_paths supplied alongside a target are relative to that target.
    target_folder_id: FolderId | None = None

    @field_validator("file_paths", mode="before")
    @classmethod
    def clean_file_paths(cls, values):
        if not isinstance(values, dict):
            raise ValueError("文件目录映射格式不正确")
        paths = {}
        for file_id, path in values.items():
            if not isinstance(file_id, str):
                raise ValueError("文件标识格式不正确")
            file_id = canonical_file_id(file_id)
            if file_id in paths:
                raise ValueError("文件目录映射不能重复")
            paths[file_id] = logical_file_path(path)
        return paths

    @model_validator(mode="after")
    def validate_file_paths(self):
        if not self.file_paths.keys() <= set(self.file_ids):
            raise ValueError("只能为已选择的文件设置目录")
        paths = set(self.file_paths.values())
        if len(paths) != len(self.file_paths):
            raise ValueError("文件目录不能重复")
        for path in paths:
            parts = path.split("/")
            if any("/".join(parts[:index]) in paths for index in range(1, len(parts))):
                raise ValueError("文件与目录不能使用相同路径")
        return self

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, values: list[str]) -> list[str]:
        result = []
        for value in values:
            value = value.strip()
            if not value or len(value) > 40:
                raise ValueError("标签长度应为 1 至 40 个字符")
            if value not in result:
                result.append(value)
        return result


class CreateLibraryFolder(LibraryInput):
    name: str = Field(min_length=1, max_length=255)
    parent_id: FolderId | None = None

    @field_validator("name", mode="before")
    @classmethod
    def safe_name(cls, value):
        value = logical_directory_path(value)
        if "/" in value:
            raise ValueError("文件夹名称不能包含目录分隔符")
        return value


class UpdateLibraryAsset(LibraryInput):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=4000)
    tags: list[str] | None = Field(default=None, max_length=10)

    @field_validator("tags")
    @classmethod
    def clean_tags(cls, values: list[str] | None) -> list[str] | None:
        return CreateLibraryAsset.clean_tags(values) if values is not None else None

    @model_validator(mode="after")
    def require_changes(self):
        if not self.model_fields_set or any(getattr(self, name) is None for name in self.model_fields_set):
            raise ValueError("请提供需要修改的名称、描述或标签")
        return self


class DeleteLibraryFile(LibraryInput):
    asset_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
    file_id: OpaqueId

    @field_validator("file_id")
    @classmethod
    def normalize_file_id(cls, value: str) -> str:
        return canonical_file_id(value)


class DeleteLibraryFiles(LibraryInput):
    files: list[DeleteLibraryFile] = Field(min_length=1, max_length=500)


class LibraryFileDeleteResult(BaseModel):
    asset_id: str
    file_id: str
    status: Literal["deleted", "not_found", "failed"]
    message: str | None = None


class LibraryFilesDeleteResponse(BaseModel):
    results: list[LibraryFileDeleteResult]
    deleted_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)


class LibraryFile(BaseModel):
    """An allowlisted file projection: no storage paths, URLs or raw metadata."""
    file_id: str
    filename: str
    relative_path: str | None = None
    content_type: str | None = None
    size: int = Field(ge=0)
    upload_date: datetime | None = None
    source_url: str | None = None
    sha256: str | None = None

    @field_validator("relative_path")
    @classmethod
    def safe_relative_path(cls, value: str | None) -> str | None:
        return logical_file_path(value) if value is not None else None


class DataLibraryAsset(BaseModel):
    id: str
    name: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    source_type: SourceType
    source_url: str | None = None
    source_verified: bool = False
    session_id: str | None = None
    files: list[LibraryFile]
    version: Literal[1] = 1
    status: Literal["ready", "missing"] = "ready"
    created_at: datetime
    updated_at: datetime

    @field_validator("created_at", "updated_at")
    @classmethod
    def utc_record_dates(cls, value: datetime) -> datetime:
        # PyMongo's default decoder returns UTC datetimes without tzinfo. Keep
        # browser date rendering aligned with the UTC query boundaries.
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class DataLibraryPage(BaseModel):
    items: list[DataLibraryAsset]
    next_cursor: str | None = None


class DataLibraryFolder(BaseModel):
    id: FolderId
    parent_id: FolderId | None = None
    name: str
    path: str
    created_at: datetime
    updated_at: datetime
    # Internal incarnation distinguishes same-name folders even when recreated
    # within Mongo's millisecond timestamp precision. Never sent to browsers.
    generation: str | None = Field(default=None, exclude=True, repr=False)

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        return logical_directory_path(value)

    @field_validator("created_at", "updated_at")
    @classmethod
    def utc_dates(cls, value: datetime) -> datetime:
        return DataLibraryAsset.utc_record_dates(value)


class DataLibraryFolders(BaseModel):
    items: list[DataLibraryFolder] = Field(default_factory=list)


class LibraryFolderDeletionPreview(BaseModel):
    folder: DataLibraryFolder
    deletion_token: str
    subfolder_count: int = Field(ge=0)
    file_count: int = Field(ge=0)


class LibraryFolderDeleteResponse(BaseModel):
    folder_id: FolderId
    parent_id: FolderId | None = None
    completed: bool
    deleted_file_count: int = Field(default=0, ge=0)
    deleted_folder_count: int = Field(default=0, ge=0)
    remaining_file_count: int | None = Field(default=None, ge=0)
    remaining_folder_count: int | None = Field(default=None, ge=0)
    message: str | None = None


class DataLibraryStats(BaseModel):
    """Whole-library saved metadata, independent of the current list page."""
    file_count: int = Field(default=0, ge=0)
    total_bytes: int = Field(default=0, ge=0)
    updated_at: datetime | None = None
    formats: list[str] = Field(default_factory=list)

    @field_validator("updated_at")
    @classmethod
    def utc_update_date(cls, value: datetime | None) -> datetime | None:
        return DataLibraryAsset.utc_record_dates(value) if value is not None else None


class SelectedDataResponse(BaseModel):
    files: list[LibraryFile]
    missing_file_ids: list[str] = Field(default_factory=list)
