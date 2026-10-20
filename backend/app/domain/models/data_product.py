from datetime import UTC, datetime
from typing import Any, Dict, List
import uuid
from pydantic import BaseModel, Field


class DataProductFile(BaseModel):
    file_id: str
    filename: str
    relative_path: str
    role: str = "other"
    content_type: str | None = None
    size: int = 0
    source_artifact_id: str | None = None
    source_tool: str | None = None
    is_primary: bool = False
    created_at: datetime | None = None


class DataProduct(BaseModel):
    product_id: str = Field(default_factory=lambda: f"dp_{uuid.uuid4().hex[:16]}")
    dataset_id: str
    source_session_id: str
    name: str
    description: str = ""
    generation_method: str = "agent_tool"
    created_by: str
    owner_id: str | None = None
    version: int = 1
    files: List[DataProductFile] = Field(default_factory=list)
    directories: List[str] = Field(default_factory=list)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
