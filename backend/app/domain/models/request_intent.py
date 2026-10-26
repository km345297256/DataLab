"""Bounded semantic intent and public clarification payloads.

These describe the user's request. They never authorize a storage object,
contain a host path, or replace the host's ownership and mount checks.
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ClarificationQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=48, pattern=r"^[a-zA-Z0-9_-]+$")
    question: str = Field(min_length=1, max_length=1000)
    reason: str = Field(default="", max_length=1000)
    options: list[str] = Field(default_factory=list, max_length=4)

    @field_validator("question", "reason")
    @classmethod
    def trim_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("question")
    @classmethod
    def substantive_question(cls, value: str) -> str:
        if not value:
            raise ValueError("question is required")
        return value

    @field_validator("options")
    @classmethod
    def distinct_options(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value or len(value) > 500 for value in cleaned):
            raise ValueError("options must be nonempty bounded text")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("options must be distinct")
        return cleaned


class ClarificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    questions: list[ClarificationQuestion] = Field(min_length=1, max_length=3)

    @field_validator("questions")
    @classmethod
    def unique_questions(cls, questions: list[ClarificationQuestion]) -> list[ClarificationQuestion]:
        if len({question.id for question in questions}) != len(questions):
            raise ValueError("question ids must be unique")
        return questions


class ClarificationView(ClarificationRequest):
    id: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    stage: Literal["request", "execution"]
    source_event_seq: int | None = Field(default=None, ge=1)


class RequestIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    summary: str = Field(default="", max_length=2000)
    current_actions: list[str] = Field(default_factory=list, max_length=24)
    deferred_actions: list[str] = Field(default_factory=list, max_length=12)
    constraints: list[str] = Field(default_factory=list, max_length=32)
    skill_creation: Literal["none", "now", "after_analysis", "deferred"] = "none"

    @field_validator("current_actions", "deferred_actions", "constraints")
    @classmethod
    def bounded_items(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or len(value) > 2000 for value in values):
            raise ValueError("intent items must be nonempty bounded text")
        return [value.strip() for value in values]
