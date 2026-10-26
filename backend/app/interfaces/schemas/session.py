from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Any, Optional, List
from app.interfaces.schemas.event import AgentSSEEvent
from app.domain.models.session import SessionStatus
from app.domain.models.event import MAX_EVENT_SEQUENCE
from app.domain.models.input_admission import InputState
from app.domain.models.open_data import MAX_SELECTED_DATA_SOURCES
from app.application.services.visualization_selection import VisualizationViewContext


class CreateSessionRequest(BaseModel):
    agent_profile_id: Optional[str] = None


class UpdateSessionTitleRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        title = value.strip()
        if not title:
            raise ValueError("Session title cannot be empty")
        return title


class ChatRequest(BaseModel):
    """Chat request schema"""
    timestamp: Optional[int] = None
    message: Optional[str] = None
    client_message_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    observe_client_message_id: Optional[str] = Field(default=None, min_length=1, max_length=128)
    resume_from: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    clarification_id: Optional[str] = Field(default=None, min_length=1, max_length=128,
                                           pattern=r"^[a-zA-Z0-9_-]+$")
    agent_profile_id: Optional[str] = None
    attachments: Optional[List[dict]] = None
    input_file_ids: Optional[List[str]] = None
    visualization_contexts: List[VisualizationViewContext] = Field(default_factory=list, max_length=4)
    skills: Optional[List[str]] = None
    mcp_servers: Optional[List[str]] = None
    data_connectors: List[str] = Field(default_factory=list, max_length=MAX_SELECTED_DATA_SOURCES)
    dataset_ids: Optional[List[str]] = None
    event_id: Optional[str] = None
    event_seq: Optional[int] = Field(
        default=None,
        strict=True,
        ge=0,
        le=MAX_EVENT_SEQUENCE,
    )

    @model_validator(mode="after")
    def validate_continuation(self):
        if self.observe_client_message_id is not None:
            if not self.observe_client_message_id.strip():
                raise ValueError("An observation requires an input identity")
            if (self.message or self.resume_from or self.clarification_id or self.client_message_id or self.agent_profile_id
                    or self.attachments or self.skills or self.mcp_servers or self.data_connectors or self.dataset_ids or self.input_file_ids is not None or self.visualization_contexts):
                raise ValueError("An input observation cannot submit or change execution inputs")
        if self.resume_from is not None:
            if not self.client_message_id or not self.client_message_id.strip():
                raise ValueError("A continuation requires a new client_message_id")
            if ((self.message or "").strip() or self.clarification_id or self.agent_profile_id or self.attachments
                    or self.skills or self.mcp_servers or self.data_connectors or self.dataset_ids or self.input_file_ids is not None or self.visualization_contexts):
                raise ValueError("A continuation cannot change the original task or execution scope")
        if self.visualization_contexts and not (self.message or "").strip():
            raise ValueError("A visualization selection requires a new analysis message")
        if self.clarification_id is not None and not (self.message or "").strip():
            raise ValueError("A clarification reply requires an answer")
        return self


class ShellViewRequest(BaseModel):
    """Shell view request schema"""
    session_id: str
    output_id: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{32}$", strict=True)
    cursor: Optional[int] = Field(default=None, ge=0, strict=True)
    max_bytes: int = Field(default=8192, ge=4, le=16384, strict=True)

    @model_validator(mode="after")
    def validate_output_cursor(self):
        if (self.output_id is None) != (self.cursor is None):
            raise ValueError("Output pagination requires output_id and cursor together")
        return self


class CreateSessionResponse(BaseModel):
    """Create session response schema"""
    session_id: str
    created_at: int


class GetSessionResponse(BaseModel):
    """Get session response schema"""
    session_id: str
    created_at: int
    title: Optional[str] = None
    title_manually_set: bool = False
    status: SessionStatus
    events: List[AgentSSEEvent] = []
    is_shared: bool = False
    is_owner: bool = False
    collaborators: List["SessionCollaboratorUser"] = []


class GetSessionHistoryResponse(GetSessionResponse):
    has_more: bool = False
    next_before_seq: Optional[int] = Field(default=None, ge=1, le=MAX_EVENT_SEQUENCE)


class InputReceiptResponse(BaseModel):
    """Owner-only admission observation, never the private input payload."""
    client_message_id: str = Field(min_length=1, max_length=128)
    accepted: bool
    event_seq: Optional[int] = Field(default=None, ge=1, le=MAX_EVENT_SEQUENCE)
    state: Optional[InputState] = None


class ListSessionItem(BaseModel):
    """List session item schema"""
    session_id: str
    title: Optional[str] = None
    latest_message: Optional[str] = None
    latest_message_at: Optional[int] = None
    status: SessionStatus
    unread_message_count: int
    is_shared: bool = False
    is_owner: bool = False


class ListSessionResponse(BaseModel):
    """List session response schema"""
    sessions: List[ListSessionItem]


class ConsoleRecord(BaseModel):
    """Console record schema"""
    ps1: str
    command: str
    output: str
    output_truncated: bool = False
    status: Optional[str] = None
    returncode: Optional[int] = Field(default=None, strict=True)


class ShellViewResponse(BaseModel):
    """Shell view response schema"""
    output: str
    session_id: str
    console: Optional[List[ConsoleRecord]] = None
    output_metadata: Optional[dict[str, Any]] = None
    output_page: Optional[dict[str, Any]] = None
    console_truncated: bool = False


class ShareSessionResponse(BaseModel):
    """Share session response schema"""
    session_id: str
    is_shared: bool


class TaskFeedbackRequest(BaseModel):
    preference: str = Field(pattern="^(like|dislike)$")
    dislike_reasons: List[str] = Field(default_factory=list, max_length=6)
    detail: str = Field(default="", max_length=2000)

    @field_validator("dislike_reasons")
    @classmethod
    def validate_dislike_reasons(cls, value: List[str]) -> List[str]:
        return [reason.strip() for reason in value if reason.strip()][:6]

    @field_validator("detail")
    @classmethod
    def validate_detail(cls, value: str) -> str:
        return value.strip()


class TaskFeedbackResponse(BaseModel):
    preference: Optional[str] = None
    dislike_reasons: List[str] = Field(default_factory=list)
    detail: str = ""


class OpenJupyterRequest(BaseModel):
    code: str = Field(min_length=1, max_length=200_000)
    language: str = Field(default="python", min_length=1, max_length=32)


class OpenJupyterResponse(BaseModel):
    notebook_path: str
    embed_url: str


class SessionCollaboratorUser(BaseModel):
    id: str
    fullname: str
    email: str


class SessionCollaboratorsResponse(BaseModel):
    collaborators: List[SessionCollaboratorUser] = []


class SessionCollaboratorsUpdateRequest(BaseModel):
    user_ids: List[str] = []


class UserSearchResponse(BaseModel):
    users: List[SessionCollaboratorUser] = []


class SharedSessionResponse(BaseModel):
    """Shared session response schema (for public access)"""
    session_id: str
    title: Optional[str] = None
    status: SessionStatus
    events: List[AgentSSEEvent] = []
    is_shared: bool
