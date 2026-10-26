"""Public, path-free contracts for durable discovery and acquisition."""
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.models.data_candidate import OpaqueId


# Shared request bound: selecting the complete registered source catalog must
# remain possible as it grows, without permitting an unbounded input payload.
MAX_SELECTED_DATA_SOURCES = 256


class OpenDataInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SearchInput(OpenDataInput):
    query: str = Field(min_length=1, max_length=1000)
    parent_id: OpaqueId | None = None
    source_ids: list[str] | None = Field(default=None, max_length=MAX_SELECTED_DATA_SOURCES)


class AcquisitionInput(OpenDataInput):
    candidate_id: OpaqueId
    file_ids: list[OpaqueId] = Field(min_length=1, description="选择已核验可获取的文件；所选文件会逐项处理。")

    @field_validator("file_ids")
    @classmethod
    def unique(cls, values):
        if len(values) != len(set(values)):
            raise ValueError("文件不能重复")
        return sorted(values)


class SearchSource(BaseModel):
    id: str
    title: str
    status: Literal["queued", "searching", "ready", "partial", "failed"] = "queued"
    count: int = 0
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)


class OpenDataCandidate(BaseModel):
    id: str
    source_id: str
    external_id: str
    title: str
    description: str = ""
    source_url: str
    access: Literal["downloadable", "needs_resolution", "restricted", "lead"] = "lead"
    formats: list[str] = Field(default_factory=list)
    size: int | None = None
    license: str | None = None
    coverage: dict = Field(default_factory=dict)
    match_notes: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    system_notes: list[str] = Field(default_factory=list)
    availability_verified_at: datetime | None = None
    available_file_count: int = 0
    relevance_status: Literal["related", "unrelated", "uncertain"] | None = None
    relevance_reason: str | None = None

    @field_validator("availability_verified_at")
    @classmethod
    def verification_time_is_utc(cls, value):
        # Mongo returns stored UTC dates without tzinfo by default. Preserve
        # their meaning when browsers render this timestamp in local time.
        return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


class OpenDataSearch(BaseModel):
    id: str
    session_id: str
    query: str
    effective_query: str | None = None
    notes: list[str] = Field(default_factory=list)
    parent_id: str | None = None
    status: Literal["queued", "searching", "ready", "partial", "failed"]
    sources: list[SearchSource]
    results: list[OpenDataCandidate] = Field(default_factory=list)
    has_more: bool = False
    relevance_pending_count: int = 0
    created_at: datetime
    updated_at: datetime


class OpenDataFileOption(BaseModel):
    id: str
    filename: str
    size: int | None = None
    content_type: str | None = None
    available: bool
    reason: str = ""
    kind: str | None = None
    group_id: str | None = None
    group_role: str | None = None


class ResolvedFiles(BaseModel):
    files: list[OpenDataFileOption]
    verification_pending_count: int = 0
    unavailable_file_count: int = 0
    limits: dict[str, int | None] = Field(default_factory=lambda: {"max_file_bytes": None,
        "max_total_bytes": None, "max_files": None})


class OpenDataFile(BaseModel):
    """An allowlist, deliberately excluding FileInfo paths, metadata and URLs."""
    file_id: str
    filename: str
    size: int
    content_type: str | None = None
    upload_date: datetime | None = None


class AcquisitionItem(BaseModel):
    id: str
    filename: str
    status: Literal["queued", "downloading", "ready", "failed"] = "queued"
    message: str | None = None
    bytes_received: int = 0
    size: int | None = None
    file: OpenDataFile | None = None


class AcquisitionJob(BaseModel):
    id: str
    search_id: str
    candidate_id: str
    session_id: str
    status: Literal["queued", "downloading", "ready", "partial", "failed", "cancelled"]
    items: list[AcquisitionItem]
    files: list[OpenDataFile] = Field(default_factory=list)
    asset_id: str | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime
