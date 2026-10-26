from types import SimpleNamespace

import pytest

from app.domain.models.event import ToolEvent, ToolStatus
from app.domain.models.session import Session
from app.domain.models.tool_result import ToolResult
from app.interfaces.schemas.event import EventMapper, ToolSSEEvent


def draft_receipt():
    return {
        "id": "a" * 32, "name": "monthly-analysis",
        "description": "按月汇总并核对数据质量。", "triggers": ["月度汇总"],
        "applicability": ["包含日期和金额字段的表格"], "capabilities": ["检查字段并汇总"],
        "resources": ["SKILL.md", "references/validation.json"],
        "validation": {"package": "passed", "script_syntax": "not_applicable", "workflow_execution": "not_run",
                       "synthetic_execution": "not_run", "synthetic_case_count": 0, "synthetic_passed_count": 0},
        "installed": False, "installed_skill_id": None,
    }


def creation_event(**updates):
    return ToolEvent(tool_call_id="creator", tool_name="skill", function_name="skill_create_from_session", function_args={},
                     status=ToolStatus.CALLED, function_result=ToolResult(success=True, data=draft_receipt())).model_copy(update=updates)


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_validation", [False, True])
async def test_skill_creation_receipt_survives_live_and_persisted_history_projection(legacy_validation):
    event = creation_event()
    if legacy_validation:
        for field in ("synthetic_execution", "synthetic_case_count", "synthetic_passed_count"):
            event.function_result.data["validation"].pop(field)
    event.function_result.data.update(owner_id="private-owner", package_file_id="private-object", path="/srv/private")
    live = await ToolSSEEvent.from_event_async(event)
    assert live.data.skill_creation.model_dump(mode="json") == draft_receipt()
    persisted = ToolEvent.model_validate(event.model_dump(mode="json"))
    history = await EventMapper.events_to_sse_events([persisted])
    assert history[0].data.skill_creation == live.data.skill_creation
    assert "private-owner" not in live.model_dump_json()
    assert "private-object" not in live.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("updates", [
    {"status": ToolStatus.CALLING},
    {"tool_name": "plugin"},
    {"function_name": "skill_read"},
    {"function_result": ToolResult(success=False, data=draft_receipt())},
    {"function_result": ToolResult(success=True, data={"name": "old-installed-skill", "resources": {"scripts": []}})},
    {"function_result": ToolResult(success=True, data={**draft_receipt(), "id": "../../private"})},
])
async def test_only_valid_successful_creator_results_get_an_actionable_card(updates):
    assert (await ToolSSEEvent.from_event_async(creation_event(**updates))).data.skill_creation is None


@pytest.mark.asyncio
async def test_public_share_does_not_publish_private_skill_draft_or_mutate_history():
    from app.interfaces.api.session_routes import get_shared_session

    event = creation_event()
    session = Session(id="shared-session", user_id="owner", agent_id="agent", is_shared=True)
    async def get_shared(_id):
        return session
    async def get_events(_id):
        return [event]
    response = await get_shared_session("shared-session", SimpleNamespace(get_shared_session=get_shared, get_session_events=get_events))
    assert response.data.events[0].data.skill_creation is None
    assert "a" * 32 not in response.model_dump_json()
    assert event.function_result.data == draft_receipt()


@pytest.mark.asyncio
async def test_public_share_does_not_publish_owner_authoring_progress():
    from app.domain.models.skill_creation_progress import SkillCreationProgressView
    from app.interfaces.api.session_routes import get_shared_session

    progress = SkillCreationProgressView(stage="workflow", status="repairing", attempt=2, code="invalid_schema")
    event = creation_event(skill_creation_progress=progress)
    session = Session(id="shared-session", user_id="owner", agent_id="agent", is_shared=True)
    async def get_shared(_id):
        return session
    async def get_events(_id):
        return [event]
    response = await get_shared_session("shared-session", SimpleNamespace(get_shared_session=get_shared, get_session_events=get_events))
    assert response.data.events[0].data.skill_creation_progress is None
    assert event.skill_creation_progress == progress
