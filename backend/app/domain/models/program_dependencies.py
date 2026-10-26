"""Bounded runtime capabilities reported by the first-party sandbox probe."""
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


def _module_name(value: str) -> str:
    if not all(part.isidentifier() for part in value.split(".")):
        raise ValueError("A dependency must be a Python module name")
    return value


ModuleName = Annotated[str, Field(min_length=1, max_length=256), AfterValidator(_module_name)]


class RuntimePackage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    module: ModuleName
    distribution: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,128}$")
    version: str | None = Field(default=None, max_length=80, pattern=r"^[A-Za-z0-9_.+!-]+$")
    available: bool


class ProgramRuntime(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    python_version: str = Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    implementation: Literal["CPython", "PyPy", "GraalVM", "Jython", "IronPython"]
    packages: list[RuntimePackage] = Field(max_length=32)
    json_writer: Literal["datalab_json.write_json"]


class ProgramDependencies(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1]
    check: Literal["static_import_resolution"]
    state: Literal["ready", "missing", "not_checked", "unavailable"]
    required_modules: list[ModuleName] = Field(max_length=256)
    missing_modules: list[ModuleName] = Field(max_length=256)
    optional_modules: list[ModuleName] = Field(max_length=256)
    optional_missing_modules: list[ModuleName] = Field(max_length=256)
    dynamic_imports: bool
    runtime: ProgramRuntime

    @model_validator(mode="after")
    def consistent_modules(self):
        for names in (self.required_modules, self.missing_modules, self.optional_modules, self.optional_missing_modules):
            if len(names) != len(set(names)):
                raise ValueError("Dependency entries must be unique")
        if (not set(self.missing_modules) <= set(self.required_modules)
                or not set(self.optional_missing_modules) <= set(self.optional_modules)
                or self.state == "ready" and self.missing_modules
                or self.state == "missing" and not self.missing_modules):
            raise ValueError("Dependency status does not match observed imports")
        return self
