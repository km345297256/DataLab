"""Typed contracts shared by skill design, implementation and packaging."""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from app.domain.services.skills.method_contract import RequirementCoverage

class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

class SkillParameter(_StrictModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")
    description: str = Field(min_length=8, max_length=600)
    value_type: str = Field(min_length=2, max_length=100)
    required: bool
    default: str | None = Field(default=None, max_length=200)
    constraints: str = Field(min_length=5, max_length=600)

class WorkflowStep(_StrictModel):
    action: str = Field(min_length=12, max_length=1600)
    decision: str = Field(min_length=8, max_length=1000)
    verification: str = Field(min_length=8, max_length=1000)

class OutputContract(_StrictModel):
    name: str = Field(min_length=2, max_length=100)
    format: str = Field(min_length=2, max_length=100)
    content: str = Field(min_length=8, max_length=1000)
    acceptance: str = Field(min_length=8, max_length=1000)

class ValidationCase(_StrictModel):
    kind: Literal["normal", "boundary", "invalid"]
    input: str = Field(min_length=8, max_length=1200)
    expected: str = Field(min_length=8, max_length=1200)

class ScriptMetadata(_StrictModel):
    """The accepted implementation plan owns a script's identity and interface."""
    path: str = Field(pattern=r"^scripts/[a-z][a-z0-9_-]{0,47}\.py$")
    purpose: str = Field(min_length=8)
    invocation: str = Field(min_length=12)

class HelperScript(ScriptMetadata):
    content: Annotated[str, StringConstraints(strip_whitespace=False)] = Field(min_length=50)

class ScriptRequest(ScriptMetadata):
    requirement_ids: list[str] = Field(min_length=1)
    implementation_source_ids: list[str] = Field(default_factory=list)

class ImplementationPlan(_StrictModel):
    kind: Literal["parameterized_scripts", "tool_workflow", "method_only"]
    reason: str = Field(min_length=12)
    scripts: list[ScriptRequest] = Field(default_factory=list)

class SkillRecipe(_StrictModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$", max_length=63)
    description: str = Field(min_length=20, max_length=360)
    triggers: list[str] = Field(min_length=2)
    goal: str = Field(min_length=12, max_length=1000)
    applicability: list[str] = Field(min_length=1)
    parameters: list[SkillParameter] = Field(min_length=1)
    data_requirements: list[str] = Field(min_length=1)
    dependencies: list[str] = Field(min_length=1)
    steps: list[WorkflowStep] = Field(min_length=2)
    outputs: list[OutputContract] = Field(min_length=1)
    validation_cases: list[ValidationCase] = Field(min_length=3)
    limitations: list[str] = Field(min_length=1)
    scripts: list[HelperScript] = Field(default_factory=list)
    coverage: list[RequirementCoverage] = Field(default_factory=list)
    implementation: ImplementationPlan | None = None
