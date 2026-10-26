"""Source extraction failures stop authoring before any model/storage effects."""
import ast
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.domain.models.event import DoneEvent, ErrorEvent, MessageEvent, ToolEvent, ToolStatus
from app.domain.models.tool_result import ToolResult
from app.domain.services.skills.skill_creation.evidence import prepare_analysis_context, select_analysis_turn
from app.domain.services.skills.skill_creation.service import CreationDependencies, create_skill_from_session
from app.domain.services.skills.skill_creation_error import SkillCreationError
from test_session_skill_creator import Repository
from test_skill_creation_failures import creation_flow


def repository_with_program():
    repository = Repository()
    repository.events.insert(2, ToolEvent(
        seq=20, tool_call_id="source-program", tool_name="shell", function_name="program_run",
        function_args={"script_path": "/private/analysis/source.py"}, status=ToolStatus.CALLED,
        function_result=ToolResult(success=True),
    ))
    repository.get_successful_program_sources = AsyncMock(return_value=[{"private_source": "private-source-content"}])
    return repository


def dependencies_without_effects():
    factories = [Mock() for _ in range(6)]
    return CreationDependencies(*factories), factories


FORMATTED_SOURCE = '''from pathlib import Path
input_path = "/private/analysis/private-cohort.csv"
def describe(value, width, precision):
    fixed = f"private-label-17:{value:.3f}"
    dynamic = f"private-prefix:{value:{width}.{precision}f}"
    return f"private-tail:{fixed!r};{dynamic}"
score = 17.25
description = describe(score, 8, 3)
'''


def formatted_source_record():
    content = FORMATTED_SOURCE.encode()
    digest = hashlib.sha256(content).hexdigest()
    return {"schema": "program-method-evidence/v1", "event_seq": 20,
            "operation_id": "private-operation", "script_path": "/private/analysis/source.py",
            "source_digest": digest, "source_snapshot": {"version": 1, "encoding": "utf-8",
                "size_bytes": len(content), "sha256": digest, "content": FORMATTED_SOURCE}}


@pytest.mark.asyncio
async def test_preparing_verified_fstring_source_keeps_method_reference_and_original_analysis():
    repository = repository_with_program()
    record = formatted_source_record()
    repository.get_successful_program_sources.return_value = [record]
    before = [event.model_dump() for event in repository.events]
    context = await prepare_analysis_context("session-owned", "owner", repository, SimpleNamespace(user_id="owner"))
    assert len(context.programs) == 1
    reference = context.programs[0]
    assert reference["id"] == "P001" and reference["reference_only"] is True
    assert reference["provenance"] == "host_verified_successful_source"
    projected = ast.parse(reference["source"])
    compile(projected, "<synthetic-method-reference>", "exec")
    assert any(isinstance(node, ast.JoinedStr) for node in ast.walk(projected))
    assert "CURRENT_TASK_PARAMETER" in reference["source"]
    assert "CURRENT_TASK_NUMBER" in reference["source"]
    for private in ("/private/analysis", "private-cohort.csv", "private-label-17", "private-prefix", "private-tail", "17.25", ".3f"):
        assert private not in reference["source"]
    assert not context.privacy.scan_document({"content": reference["source"]})
    assert [event.model_dump() for event in repository.events] == before
    assert record["source_snapshot"]["content"] == FORMATTED_SOURCE


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["invalid_record", "integrity_unavailable", "unsupported_syntax", "invalid_projection", "private_identifiers"])
async def test_known_source_extraction_failure_has_safe_stage_before_model_or_draft_creation(monkeypatch, caplog, reason):
    from app.domain.services.skills import program_method_evidence

    failure = program_method_evidence.ProgramMethodEvidenceError(reason)
    failure.__cause__ = ValueError("private-source-content /private/analysis/source.py")
    extraction = Mock(side_effect=failure)
    monkeypatch.setattr(program_method_evidence, "method_program_references", extraction)
    repository = repository_with_program()
    before = [event.model_dump() for event in repository.events]
    dependencies, effects = dependencies_without_effects()
    with pytest.raises(SkillCreationError) as caught:
        await create_skill_from_session("session-owned", "owner", repository, SimpleNamespace(user_id="owner"),
                                       dependencies=dependencies)
    assert (caught.value.stage, caught.value.code) == ("source_analysis", "source_evidence_invalid")
    assert "原分析结果保持不变" in caught.value.public_message
    assert "reason=" + reason in caplog.text
    for private in ("private-source-content", "/private/analysis/source.py", "session-owned"):
        assert private not in str(caught.value) and private not in caplog.text
    extraction.assert_called_once()
    repository.get_successful_program_sources.assert_awaited_once_with("session-owned", "owner", [20])
    for effect in effects:
        effect.assert_not_called()
    assert [event.model_dump() for event in repository.events] == before


@pytest.mark.asyncio
async def test_unrecognized_source_bug_is_not_relabelled_or_silently_dropped(monkeypatch):
    from app.domain.services.skills import program_method_evidence

    monkeypatch.setattr(program_method_evidence, "method_program_references", Mock(side_effect=ValueError("unknown invariant")))
    dependencies, effects = dependencies_without_effects()
    with pytest.raises(ValueError, match="unknown invariant") as caught:
        await create_skill_from_session("session-owned", "owner", repository_with_program(),
                                       SimpleNamespace(user_id="owner"), dependencies=dependencies)
    assert not isinstance(caught.value, SkillCreationError)
    for effect in effects:
        effect.assert_not_called()


@pytest.mark.asyncio
async def test_real_creation_command_isolates_source_failure_and_next_retry_keeps_original_analysis(tmp_path, monkeypatch):
    from app.domain.services.skills import program_method_evidence
    from app.domain.services.skills.skill_creation import service

    failure = program_method_evidence.ProgramMethodEvidenceError("invalid_projection")
    monkeypatch.setattr(program_method_evidence, "method_program_references", Mock(side_effect=failure))
    settings, model, draft = Mock(), Mock(), Mock()
    monkeypatch.setattr(service, "get_settings", settings)
    monkeypatch.setattr(service, "create_chat_model", model)
    monkeypatch.setattr(service, "get_skill_draft_service", draft)
    repository = repository_with_program()
    original = list(repository.events)
    original_before = [event.model_dump() for event in original]
    flow = creation_flow(tmp_path)
    flow._session_id = flow.skill_toolkit.session_id = "session-owned"
    flow.skill_toolkit.session_repository = repository
    creation_request = MessageEvent(role="user", message="/skill-creator 将这个流程保存为可复用的技能")
    repository.events.append(creation_request)

    events = [event async for event in flow._run_skill_create_command()]

    called = [event for event in events if isinstance(event, ToolEvent) and event.status == ToolStatus.CALLED]
    assert len(called) == 1 and called[0].function_result.success is False
    assert called[0].function_result.data["skill_creation_failure"]["code"] == "source_evidence_invalid"
    assert sum(isinstance(event, DoneEvent) for event in events) == 1
    assert not any(isinstance(event, ErrorEvent) for event in events)
    messages = [event for event in events if isinstance(event, MessageEvent)]
    assert len(messages) == 1 and "analysis_outcome" not in messages[0].metadata
    assert messages[0].metadata["skill_creation_failure"]["code"] == "source_evidence_invalid"
    assert "Task error" not in messages[0].message and "execution_failed" not in messages[0].message
    assert "已有分析结果保持不变" in messages[0].message
    for effect in (settings, model, draft):
        effect.assert_not_called()
    assert flow.skill_toolkit.registry.list_skills() == []
    assert [event.model_dump() for event in original] == original_before
    repository.events.extend(events)
    repository.events.append(MessageEvent(role="user", message="/skill-creator"))
    assert select_analysis_turn(repository.events) == original
