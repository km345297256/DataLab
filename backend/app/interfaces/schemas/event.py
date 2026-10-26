from app.domain.services.connector_results import connector_results
from app.domain.models.skill_draft import SkillCreationView
from pydantic import BaseModel, Field, TypeAdapter
from typing import Any, Union, Literal, Dict, Optional, List, Self, Type
from datetime import datetime
from dataclasses import dataclass
from app.domain.models.plan import ExecutionStatus, Step
from app.interfaces.schemas.file import FileInfoResponse
from app.domain.models.event import ToolStatus, ToolContent, BrowserToolContent
from app.domain.models.event import (
    AgentEvent,
    RunEvent,
    ErrorEvent,
    PlanEvent,
    MessageEvent,
    TitleEvent,
    ToolEvent,
    StepEvent,
    MAX_EVENT_SEQUENCE,
)
from app.domain.models.spill import SpillArtifactNotice
from app.domain.models.tool_result import ToolResult
from app.domain.models.analysis_job import AnalysisJobView
from app.domain.models.tool_approval import ToolApprovalView
from app.domain.models.program_attempt import ProgramAttemptView
from app.domain.models.skill_creation_progress import SkillCreationProgressView
from app.domain.models.request_intent import ClarificationView
from app.domain.services.tools.spill_projection import (
    sanitize_spill_notice,
    spill_notice_from_result,
)
from app.domain.utils.public_error import public_error_message
from app.interfaces.schemas.tool_presentation import (
    ToolPresentation,
    normalize_tool_presentation,
    populate_tool_presentation_data,
    sanitize_tool_public_data,
)

class BaseEventData(BaseModel):
    event_id: Optional[str] = None
    # Additive envelope metadata.  ``seq`` remains optional for callers that
    # deserialize a pre-versioning event outside the session history mapper.
    seq: Optional[int] = Field(
        default=None,
        strict=True,
        ge=1,
        le=MAX_EVENT_SEQUENCE,
    )
    run_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$", exclude_if=lambda value: value is None)
    version: Literal[1] = 1
    timestamp: datetime = Field(default_factory=lambda: datetime.now())

    class Config:
        json_encoders = {
            datetime: lambda v: int(v.timestamp())
        }

    @classmethod
    def base_event_data(cls, event: AgentEvent) -> dict:
        return {
            **({"run_id": event.run_id} if event.run_id else {}),
            "event_id": event.id,
            "seq": event.seq,
            "version": event.version,
            "timestamp": int(event.timestamp.timestamp())
        }
    
    @classmethod
    def from_event(cls, event: AgentEvent) -> Self:
        return cls(
            **cls.base_event_data(event),
            **event.model_dump(exclude={"type", "id", "seq", "version", "timestamp", "run_id"})
        )

class CommonEventData(BaseEventData):
    class Config:
        json_encoders = {
            datetime: lambda v: int(v.timestamp())
        }
        extra = "allow"

class BaseSSEEvent(BaseModel):
    event: str
    data: BaseEventData

    @classmethod
    def from_event(cls, event: AgentEvent) -> Self:
        data_class: Type[BaseEventData] = cls.__annotations__.get('data', BaseEventData)
        return cls(
            event=event.type,
            data=data_class.from_event(event)
        )

class MessageEventData(BaseEventData):
    role: Literal["user", "assistant"]
    content: str
    attachments: Optional[List[FileInfoResponse]] = None
    metadata: Optional[Dict[str, Any]] = None

class MessageSSEEvent(BaseSSEEvent):
    event: Literal["message"] = "message"
    data: MessageEventData

    @classmethod
    async def from_event_async(cls, event: MessageEvent) -> Self:
        metadata = None
        content = event.message
        if event.metadata is not None:
            metadata = {key: value for key, value in event.metadata.items()
                        if key not in {"analysis_input_files", "requested_clarification_id"}
                        and (not key.startswith("clarification_") or key == "clarification_id")}
            if "clarification" in metadata:
                # Questions are public UI, while their execution context can
                # contain host details. Sanitize both the structured card and
                # its ordinary-message fallback on live SSE and history reads.
                if event.role == "assistant":
                    content = public_error_message(content)
                try:
                    view = ClarificationView.model_validate(metadata["clarification"])
                    questions = []
                    for question in view.questions:
                        options = [public_error_message(option) for option in question.options]
                        # A redacted option no longer identifies the original
                        # choice reliably. Fall back to a free-text answer.
                        if options != question.options or len(set(options)) != len(options):
                            options = []
                        questions.append(question.model_copy(update={
                            "question": public_error_message(question.question),
                            "reason": public_error_message(question.reason) if question.reason else "",
                            "options": options,
                        }))
                    metadata["clarification"] = view.model_copy(update={"questions": questions}).model_dump(
                        mode="json", exclude_none=True)
                except (ValueError, TypeError):
                    metadata.pop("clarification")
        return cls(
            data=MessageEventData(
                **BaseEventData.base_event_data(event),
                role=event.role,
                content=content,
                attachments=[await FileInfoResponse.from_file_info(attachment) for attachment in event.attachments] if event.attachments else None,
                metadata=metadata,
            )
        )

class ToolEventData(BaseEventData):
    tool_call_id: str
    name: str
    status: ToolStatus
    execution_status: Optional[Literal["succeeded", "failed", "not_started"]] = None
    write_action: Optional[Literal["reuse", "execute_saved", "finalize_saved", "inspect_saved"]] = None
    function: str
    args: Dict[str, Any]
    content: Optional[ToolContent] = None
    # Optional extension. Existing SSE event names and fields stay unchanged.
    presentation: Optional[ToolPresentation] = None
    # Typed additive projection. The reference bypasses the generic text
    # sanitizer because its locator is constrained by SpillArtifactRef's
    # regex; the untrusted preview is sanitized separately by the mapper.
    spill: Optional[SpillArtifactNotice] = None
    analysis_job: Optional[AnalysisJobView] = None
    tool_approval: Optional[ToolApprovalView] = None
    connector_status: Optional[str] = None
    connector_results: Optional[Dict[str, Any]] = None
    open_data_search_id: Optional[str] = None
    open_data_update_id: Optional[str] = None
    program_attempt: Optional[ProgramAttemptView] = None
    skill_creation: Optional[SkillCreationView] = None
    skill_creation_progress: Optional[SkillCreationProgressView] = None

class ToolSSEEvent(BaseSSEEvent):
    event: Literal["tool"] = "tool"
    data: ToolEventData

    @classmethod
    async def from_event_async(cls, event: ToolEvent) -> Self:
        result = event.function_result
        success = result.success if isinstance(result, ToolResult) else (
            result.get("success") if isinstance(result, dict) else None
        )
        execution_status = None
        if event.status == ToolStatus.CALLED and type(success) is bool:
            execution_status = "succeeded" if success else "failed"
            if (event.dispatch_status == "not_started"
                    and event.program_attempt is None):
                # Only the host-owned event projection can attest no dispatch.
                # Plugin result strings/flags must not erase a real failure.
                execution_status = "not_started"
        write_action = None
        if (execution_status == "not_started" and event.tool_name == "file"
                and event.function_name in {"file_write", "file_str_replace", "file_apply_patch"}):
            receipt = result.data if isinstance(result, ToolResult) else result
            if isinstance(receipt, dict) and isinstance(receipt.get("data"), dict):
                receipt = receipt["data"]
            action = receipt.get("write_action") if isinstance(receipt, dict) else None
            if isinstance(action, str) and action in {"reuse", "execute_saved", "finalize_saved", "inspect_saved"}:
                write_action = action
        content = event.tool_content
        if isinstance(content, BrowserToolContent):
            from app.interfaces.dependencies import get_file_service
            content = BrowserToolContent(screenshot=await get_file_service().create_signed_url(content.screenshot))
        safe_content = sanitize_tool_public_data(content)
        safe_args = sanitize_tool_public_data(event.function_args)
        spill_notice = (
            spill_notice_from_result(event.function_result)
            if event.status == ToolStatus.CALLED
            else None
        )
        # A spill has one universal, typed UI projection. Suppress a plugin's
        # declarative card for the same result so its legacy data traversal
        # cannot display a damaged locator or duplicate the preview.
        presentation = (
            None
            if spill_notice is not None
            else populate_tool_presentation_data(
                normalize_tool_presentation(getattr(event, "presentation", None)),
                event.function_result if event.status == ToolStatus.CALLED else None,
            )
        )
        spill = None
        if spill_notice is not None:
            spill = sanitize_spill_notice(spill_notice)
        return cls(
            data=ToolEventData(
                **BaseEventData.base_event_data(event),
                tool_call_id=event.tool_call_id,
                name=event.tool_name,
                status=event.status,
                execution_status=execution_status,
                write_action=write_action,
                function=event.function_name,
                args=safe_args if isinstance(safe_args, dict) else {},
                content=safe_content,
                presentation=presentation,
                spill=spill,
                analysis_job=event.analysis_job,
                tool_approval=event.tool_approval,
                connector_status=connector_status_text(event),
                connector_results=connector_results(event),
                open_data_search_id=open_data_search_id(event),
                open_data_update_id=open_data_update_id(event),
                program_attempt=event.program_attempt if event.status == ToolStatus.CALLED else None,
                skill_creation=skill_creation_view(event),
                skill_creation_progress=event.skill_creation_progress if (
                    event.tool_name == "skill" and event.function_name == "skill_create_from_session") else None,
            )
        )

def skill_creation_view(event: ToolEvent) -> SkillCreationView | None:
    """Project only the built-in creator's public draft receipt on live/history SSE."""
    if (event.tool_name != "skill" or event.function_name != "skill_create_from_session"
            or event.status != ToolStatus.CALLED):
        return None
    result = event.function_result
    if isinstance(result, ToolResult):
        result = result.model_dump(mode="json")
    if not isinstance(result, dict) or result.get("success") is not True:
        return None
    data = result.get("data")
    if not isinstance(data, dict):
        return None
    # Old events and malformed results must not manufacture an actionable card.
    # Whitelist the view rather than exposing owner/session/object metadata.
    try:
        safe = sanitize_tool_public_data({key: data[key] for key in SkillCreationView.model_fields if key in data})
        return SkillCreationView.model_validate(safe)
    except (TypeError, ValueError):
        return None


class DoneSSEEvent(BaseSSEEvent):
    event: Literal["done"] = "done"
    data: CommonEventData

class WaitSSEEvent(BaseSSEEvent):
    event: Literal["wait"] = "wait"

class ErrorEventData(BaseEventData):
    error: str

class ErrorSSEEvent(BaseSSEEvent):
    event: Literal["error"] = "error"
    data: ErrorEventData

    @classmethod
    def from_event(cls, event: ErrorEvent) -> Self:
        return cls(
            data=ErrorEventData(
                **BaseEventData.base_event_data(event),
                error=public_error_message(event.error),
            )
        )

class StepEventData(BaseEventData):
    status: ExecutionStatus
    id: str
    description: str

class StepSSEEvent(BaseSSEEvent):
    event: Literal["step"] = "step"
    data: StepEventData

    @classmethod
    def from_event(cls, event: StepEvent) -> Self:
        return cls(
            data=StepEventData(
                **BaseEventData.base_event_data(event),
                status=event.step.status,
                id=event.step.id,
                description=event.step.description
            )
        )

class TitleEventData(BaseEventData):
    title: str

class TitleSSEEvent(BaseSSEEvent):
    event: Literal["title"] = "title"
    data: TitleEventData

class PlanEventData(BaseEventData):
    steps: List[StepEventData]

class PlanSSEEvent(BaseSSEEvent):
    event: Literal["plan"] = "plan"
    data: PlanEventData

    @classmethod
    def from_event(cls, event: PlanEvent) -> Self:
        return cls(
            data=PlanEventData(
                **BaseEventData.base_event_data(event),
                steps=[StepEventData(
                    **BaseEventData.base_event_data(event),
                    status=step.status,
                    id=step.id, 
                    description=step.description
                ) for step in event.plan.steps]
            )
        )

class CommonSSEEvent(BaseSSEEvent):
    event: str
    data: CommonEventData

class RunSSEEvent(BaseSSEEvent):
    event: Literal["run"] = "run"
    data: CommonEventData


AgentSSEEvent = Union[
    RunSSEEvent,
    CommonEventData,
    PlanSSEEvent,
    MessageSSEEvent,
    TitleSSEEvent,
    ToolSSEEvent,
    StepSSEEvent,
    DoneSSEEvent,
    ErrorSSEEvent,
    WaitSSEEvent,
]

@dataclass
class EventMapping:
    """Data class to store event type mapping information"""
    sse_event_class: Type[BaseEventData]
    data_class: Type[BaseEventData]
    event_type: str

class EventMapper:
    """Map AgentEvent to SSEEvent"""
    
    _cached_mapping: Optional[Dict[str, EventMapping]] = None
    
    @staticmethod
    def _get_event_type_mapping() -> Dict[str, EventMapping]:
        """Dynamically get mapping from event type to SSE event class with caching"""
        if EventMapper._cached_mapping is not None:
            return EventMapper._cached_mapping
            
        from typing import get_args
        
        # Get all subclasses of AgentSSEEvent Union
        sse_event_classes = get_args(AgentSSEEvent)
        mapping = {}
        
        for sse_event_class in sse_event_classes:
            # Skip base class
            if sse_event_class == BaseSSEEvent:
                continue
                
            # Get event type
            if hasattr(sse_event_class, '__annotations__') and 'event' in sse_event_class.__annotations__:
                event_field = sse_event_class.__annotations__['event']
                if hasattr(event_field, '__args__') and len(event_field.__args__) > 0:
                    event_type = event_field.__args__[0]  # Get Literal value
                    
                    # Get data class from sse_event_class
                    data_class = None
                    if hasattr(sse_event_class, '__annotations__') and 'data' in sse_event_class.__annotations__:
                        data_class = sse_event_class.__annotations__['data']
                    
                    mapping[event_type] = EventMapping(
                        sse_event_class=sse_event_class,
                        data_class=data_class,
                        event_type=event_type
                    )
        
        # Cache the mapping
        EventMapper._cached_mapping = mapping
        return mapping
    
    @staticmethod
    async def event_to_sse_event(event: AgentEvent) -> AgentSSEEvent:
        # Get mapping dynamically
        event_type_mapping = EventMapper._get_event_type_mapping()
        
        # Find matching SSE event class
        event_mapping = event_type_mapping.get(event.type)
        
        if event_mapping:
            # Prioritize from_event_async class method if exists, otherwise use from_event
            sse_event_class = event_mapping.sse_event_class
            if hasattr(sse_event_class, 'from_event_async'):
                sse_event = await sse_event_class.from_event_async(event)
            else:
                sse_event = sse_event_class.from_event(event)
            return sse_event
        # If no matching type found, return base event
        return CommonEventData.from_event(event)
    
    @staticmethod
    async def events_to_sse_events(events: List[AgentEvent]) -> List[AgentSSEEvent]:
        """Create SSE event list from event list"""
        return list(filter(lambda x: x is not None, [
            await EventMapper.event_to_sse_event(event) for event in events if event
        ]))


def connector_status_text(event):
    """Expose a bounded explanation, never upstream bodies, paths or credentials."""
    metadata_read = event.function_name in {'ncbi_search_genomes', 'ncbi_describe_genome'} or (
        event.function_name == 'data_connector_call' and (event.function_args or {}).get('operation') in {'search', 'describe'})
    if metadata_read:
        if event.status != ToolStatus.CALLED:
            return '正在查询公开数据元信息…'
        result = event.function_result
        result = result if isinstance(result, dict) else result.model_dump() if result else {}
        data = result.get('data') or {}
        if result.get('success') is True:
            entries = data.get('candidates', data.get('resources'))
            return f'元数据检索完成，本页返回 {len(entries)} 条结果。' if isinstance(entries, list) else '元数据读取完成。'
        from app.domain.services.tools.data_discovery import metadata_error_message
        return metadata_error_message(data.get('reason', 'unknown'))
    if event.function_name == 'ncbi_plan_acquisition':
        return '这一步仅查看下载计划，尚未开始下载。下载需要调用数据连接器的 acquire 操作。'
    if event.function_name != 'data_connector_call' or (event.function_args or {}).get('operation') != 'acquire':
        return None
    if event.tool_approval and event.tool_approval.status in {'rejected', 'expired', 'cancelled'}:
        return '本次下载未执行：批准已拒绝、过期或任务已取消。可重新发起下载。'
    if event.status != ToolStatus.CALLED:
        if event.tool_approval and event.tool_approval.status == 'pending':
            return '下载等待你的单次批准；批准后将下载并校验数据包。'
        return '正在下载并校验数据包（最多 32 MiB）；完成后将交付 ZIP 文件，当前请勿重复发送。'
    result = event.function_result
    result = result if isinstance(result, dict) else result.model_dump() if result else {}
    data = result.get('data') or {}
    if result.get('success') is True and data.get('files_downloaded') is True and data.get('files'):
        return '数据包已下载并通过校验。任务结束后将统一提供结果和下载文件。'
    from app.domain.services.tools.data_discovery import connector_error_message
    return connector_error_message(data.get('reason', 'unknown'))


def open_data_search_id(event: ToolEvent) -> str | None:
    """Project a durable reference only, never provider URLs or file paths."""
    import re
    if event.status != ToolStatus.CALLED or event.function_name != "data_search":
        return None
    result = event.function_result
    result = result if isinstance(result, dict) else result.model_dump() if isinstance(result, BaseModel) else {}
    if result.get("success") is not True or not isinstance(result.get("data"), dict):
        return None
    value = result["data"].get("search_id")
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) else None


def open_data_update_id(event: ToolEvent) -> str | None:
    """Invalidate an existing card after a conversational action without duplicating it."""
    import re
    if event.status != ToolStatus.CALLED or event.function_name not in {
        'data_acquire', 'data_search_continue', 'data_search_resolve', 'data_search_status',
    }:
        return None
    result = event.function_result
    result = result if isinstance(result, dict) else result.model_dump() if isinstance(result, BaseModel) else {}
    if result.get('success') is not True:
        return None
    value = (event.function_args or {}).get('search_id')
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value) else None
