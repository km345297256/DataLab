"""Private, current-authority projection of a task's materialized data."""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.interfaces.schemas.file import FileInfoResponse


class TaskDataSavedLocation(BaseModel):
    asset_id: str
    asset_name: str
    folder_id: str | None = None
    folder_path: str = ""
    file_path: str


class TaskDataItem(BaseModel):
    file: FileInfoResponse
    category: Literal["upload", "analysis", "download"]
    created_at: datetime | None = None
    round_number: int | None = None
    available: bool = True
    saved_locations: list[TaskDataSavedLocation] = Field(default_factory=list)


class TaskDataResponse(BaseModel):
    items: list[TaskDataItem] = Field(default_factory=list)
    selected_file_ids: list[str] = Field(default_factory=list)
    unavailable_count: int = 0
