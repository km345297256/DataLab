"""Value-free progress from the host skill creator, never from tool output."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.services.skills.skill_creation_error import KNOWN_CODES, STAGE_LABELS


class SkillCreationProgressView(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    stage: str
    status: Literal["running", "repairing", "passed", "failed", "cancelled", "response_received", "accepted", "revision_required"]
    attempt: int = Field(ge=0)
    code: str | None = None

    @field_validator("stage")
    @classmethod
    def known_stage(cls, value):
        if value not in STAGE_LABELS:
            raise ValueError("Unknown skill creation stage")
        return value

    @field_validator("code")
    @classmethod
    def known_code(cls, value):
        if value is not None and value not in KNOWN_CODES:
            raise ValueError("Unknown skill creation diagnostic")
        return value
