"""Public failures distinguish the failing gate without leaking its input."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.domain.models.event import MessageEvent
from app.domain.services.skills.skill_creation.script_policy import (
    SkillScriptValidationError,
    validate_script,
)
from app.domain.services.skills.skill_creation_error import SCRIPT_CODES, SkillCreationError
from app.domain.services.tools.skill import SkillToolkit
from app.interfaces.schemas.event import EventMapper
from test_skill_creation_failures import creation_flow


@pytest.mark.parametrize("code,reason", [
    ("script_syntax_invalid", "语法错误"),
    ("script_dependency_unsupported", "依赖导入"),
    ("script_unsafe_operation", "安全检查"),
    ("script_arguments_missing", "命令行参数"),
    ("script_distinct_path_guard_missing", "输入与输出路径不同"),
    ("script_output_destination_unknown", "文件写入位置"),
    ("script_open_mode_unknown", "文件打开方式"),
    ("script_output_not_isolated", "写入都限制在输出位置"),
    ("script_invocation_invalid", "运行示例"),
])
def test_public_tool_failure_identifies_the_actual_gate(code, reason):
    error = SkillCreationError("implementation", code)
    result = SkillToolkit._creation_failure_result(error)
    failure = result.data["skill_creation_failure"]
    assert result.success is False
    assert failure == {"stage": "implementation", "code": code,
                       "message": error.public_message, "retryable": True}
    assert reason in result.message
    assert "参数化或输入只读要求" not in result.message
    assert "未创建技能，也未添加到技能库" in result.message
    if code in {"script_syntax_invalid", "script_dependency_unsupported", "script_unsafe_operation"}:
        assert "只读" not in result.message
    assert "允许名单" not in result.message and "白名单" not in result.message


def test_distinct_script_failures_do_not_collapse_into_one_public_reason():
    assert len({SkillCreationError("implementation", code).public_message
                for code in SCRIPT_CODES}) == len(SCRIPT_CODES)


@pytest.mark.parametrize("body,invocation,code,reason", [
    ("def broken(:\n", "python scripts/convert.py --input INPUT --output OUTPUT",
     "script_syntax_invalid", "语法错误"),
    ("print('PRIVATE-SOURCE-TEXT')\n", "python scripts/convert.py --input 'PRIVATE-SOURCE-TEXT",
     "script_invocation_invalid", "运行示例"),
])
def test_real_static_diagnostics_keep_private_source_out_of_public_results(body, invocation, code, reason):
    source = SimpleNamespace(path="scripts/convert.py",
        invocation=invocation,
        content="""import argparse
from pathlib import Path
parser = argparse.ArgumentParser()
parser.add_argument('--input', required=True)
parser.add_argument('--output', required=True)
args = parser.parse_args()
if Path(args.input).resolve() == Path(args.output).resolve():
    raise ValueError('PRIVATE-SOURCE-TEXT')
""" + body)
    with pytest.raises(SkillScriptValidationError) as caught:
        validate_script(source)
    assert caught.value.code == code
    try:
        raise SkillCreationError("implementation", caught.value.code) from caught.value
    except SkillCreationError as error:
        result = SkillToolkit._creation_failure_result(error)
    assert reason in result.message
    assert "PRIVATE-SOURCE-TEXT" not in result.model_dump_json()
    assert "scripts/convert.py" not in result.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("code,reason", [
    ("script_dependency_unsupported", "依赖导入"),
    ("script_syntax_invalid", "语法错误"),
    ("script_output_not_isolated", "写入都限制在输出位置"),
])
async def test_chat_and_rest_preserve_the_same_specific_safe_reason(tmp_path, monkeypatch, code, reason):
    from app.interfaces.api import skill_routes
    from app.interfaces.schemas.skill import CreateSkillRequest

    error = SkillCreationError("implementation", code)
    creator = AsyncMock(side_effect=error)
    monkeypatch.setattr("app.domain.services.tools.skill.create_skill_from_session", creator)
    events = [event async for event in creation_flow(tmp_path)._run_skill_create_command()]
    message = next(event for event in events if isinstance(event, MessageEvent))
    creator.assert_awaited_once()
    assert reason in message.message and error.public_message in message.message
    assert "已有分析结果保持不变" in message.message
    assert message.metadata["skill_creation_failure"]["code"] == code
    restored = MessageEvent.model_validate_json(message.model_dump_json())
    assert (await EventMapper.event_to_sse_event(restored)).data.content == message.message

    monkeypatch.setattr(skill_routes, "create_session_skill", AsyncMock(side_effect=error))
    monkeypatch.setattr(skill_routes, "MongoSessionRepository", lambda: SimpleNamespace())
    monkeypatch.setattr(skill_routes, "_registry", lambda user_id: SimpleNamespace())
    with pytest.raises(HTTPException) as caught:
        await skill_routes.create_skill_from_session(
            CreateSkillRequest(session_id="private-session"), SimpleNamespace(id="owner"),
            SimpleNamespace(default_workspace_id=AsyncMock(return_value="workspace")),
            SimpleNamespace(record=AsyncMock()),
        )
    assert caught.value.status_code == 400
    assert caught.value.detail == error.public_message
    assert "private-session" not in caught.value.detail
