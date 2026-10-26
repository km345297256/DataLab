"""Private generated packages are proposals until their owner installs them."""
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field


class SkillCreationValidation(BaseModel):
    package: Literal["passed"] = "passed"
    script_syntax: Literal["passed", "not_applicable"] = "not_applicable"
    workflow_execution: Literal["not_run"] = "not_run"
    synthetic_execution: Literal["passed", "not_run", "not_applicable", "unsupported"] = "not_run"
    synthetic_case_count: int = Field(default=0, ge=0)
    synthetic_passed_count: int = Field(default=0, ge=0)


class SkillCreationView(BaseModel):
    """The only draft representation allowed in browser events and responses."""
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    name: str
    description: str
    triggers: list[str] = Field(default_factory=list)
    applicability: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    resources: list[str] = Field(default_factory=list)
    validation: SkillCreationValidation = Field(default_factory=SkillCreationValidation)
    installed: bool = False
    installed_skill_id: str | None = None


class SkillDraft(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex, pattern=r"^[a-f0-9]{32}$")
    owner_id: str
    source_session_id: str
    name: str = Field(pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$", max_length=63)
    description: str
    triggers: list[str] = Field(default_factory=list)
    applicability: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    resources: list[str] = Field(default_factory=list)
    validation: SkillCreationValidation = Field(default_factory=SkillCreationValidation)
    package_file_id: str
    package_sha256: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    installed_skill_id: str | None = None

    def public_view(self) -> SkillCreationView:
        return SkillCreationView(**{
            field: getattr(self, field) for field in SkillCreationView.model_fields
            if field != "installed"
        }, installed=self.installed_skill_id is not None)
