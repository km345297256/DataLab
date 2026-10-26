"""Public execution facts: no source, arguments, exception messages or paths."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


FailureCategory = Literal["dependency", "code", "data", "serialization", "publication", "runtime"]


class ExecutionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, revalidate_instances="always")

    sequence: int = Field(ge=1)
    state: Literal["succeeded", "failed", "not_started"]
    returncode: int | None = None
    category: FailureCategory | None = None
    exception_type: Literal[
        "ModuleNotFoundError", "ImportError", "SyntaxError", "IndentationError", "TabError",
        "NameError", "UnboundLocalError", "TypeError", "ValueError", "KeyError", "IndexError",
        "AttributeError", "ZeroDivisionError", "FileNotFoundError", "PermissionError",
        "OSError", "MemoryError", "RuntimeError", "AssertionError", "OtherError",
    ] | None = None

    @model_validator(mode="after")
    def consistent_state(self):
        if self.state == "not_started":
            if self.returncode is not None or self.category is None:
                raise ValueError("An unlaunched program has no exit code")
        elif self.returncode is None or (self.returncode == 0) != (self.state == "succeeded"):
            raise ValueError("Execution status must match the observed exit code")
        if self.state == "succeeded" and (self.category is not None or self.exception_type is not None):
            raise ValueError("Successful execution has no failure classification")
        return self
