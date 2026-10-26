"""A published result can become an input only through explicit user selection."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.domain.models.analysis_input import (
    assign_upload_namespace, build_analysis_inputs, render_upload_context, upload_runtime_path,
)
from app.domain.models.event import MessageEvent
from app.domain.models.file import FileInfo
from app.domain.services.agent_domain_service import AgentDomainService
from app.domain.services.analysis_input_selection import (
    input_snapshot, session_input_state, snapshot_files,
)


def artifact(file_id="derived", filename="grouped_values.csv", *, owner="owner", session="session"):
    return FileInfo(file_id=file_id, filename=filename, size=42, content_type="text/csv", user_id=owner,
        file_path=f"/home/ubuntu/output/{filename}", file_url="https://private.example/object",
        metadata={"source": "sandbox_artifact", "session_id": session,
                  "file_path": f"/home/ubuntu/output/{filename}",
                  "artifact_size": 42, "artifact_sha256": "a" * 64})


def publication(*files, status="succeeded"):
    return MessageEvent(role="assistant", message="已发布结果", attachments=list(files),
                        metadata={"analysis_outcome": {"status": status}})


def admission(files, *, attachments=None, message="继续分析"):
    return MessageEvent(role="user", message=message, attachments=attachments,
        metadata={"analysis_input_files": input_snapshot(files),
                  "analysis_input_file_ids": [item.file_id for item in files]})


def service_for(history, stored):
    service = AgentDomainService.__new__(AgentDomainService)
    service._session_repository = SimpleNamespace(get_events=AsyncMock(side_effect=lambda _: list(history)))
    service._file_storage = SimpleNamespace(
        get_file_info=AsyncMock(side_effect=lambda fid, user: stored.get(fid)),
        download_file=AsyncMock(side_effect=AssertionError("must not copy inputs")),
        upload_file=AsyncMock(side_effect=AssertionError("must not republish inputs")),
    )
    return service


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["succeeded", "partial", "failed"])
async def test_named_published_result_is_mounted_as_input_even_after_partial_or_failed_turn(status):
    source = FileInfo(file_id="source", filename="measurements.xlsx", size=64, user_id="owner")
    initial = assign_upload_namespace([source])
    result, unrelated = artifact(), artifact("chart", "other_chart.svg")
    history = [admission(initial, attachments=initial), publication(result, unrelated, status=status)]
    before = [event.model_dump() for event in history]
    service = service_for(history, {item.file_id: item for item in [source, result, unrelated]})
    selected, submitted = await service._prepare_input_selection(
        "session", "owner", None, None, message="请用grouped_values.csv按国家继续分组",
    )
    assert [item.file_id for item in selected] == ["source", "derived"]
    assert submitted == []
    assert upload_runtime_path(selected[0]) == upload_runtime_path(initial[0])
    assert upload_runtime_path(selected[1]).startswith("/home/ubuntu/inputs/")
    assert upload_runtime_path(selected[1]).endswith("/grouped_values.csv")
    assert selected[1].file_path is None and selected[1].file_url is None
    assert selected[1].metadata["analysis_input_origin"] == "published_artifact"
    assert [event.model_dump() for event in history] == before
    service._file_storage.download_file.assert_not_awaited()
    service._file_storage.upload_file.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["继续分析", "grouped_values.csv.bak", "prefix_grouped_values.csv",
                                          "grouped_values", "GROUPED_VALUES.CSV"])
async def test_outputs_never_auto_join_inputs_or_use_fuzzy_name_matching(message):
    result = artifact()
    service = service_for([publication(result)], {result.file_id: result})
    selected, submitted = await service._prepare_input_selection("session", "owner", None, None, message=message)
    assert selected == submitted == []
    service._file_storage.get_file_info.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["requested_id", "attachment"])
async def test_explicit_file_id_promotes_the_exact_published_object(kind):
    result = artifact()
    service = service_for([publication(result)], {result.file_id: result})
    selected, submitted = await service._prepare_input_selection("session", "owner",
        [{"file_id": result.file_id}] if kind == "attachment" else None,
        [result.file_id] if kind == "requested_id" else None, message="分析选中的文件")
    assert [item.file_id for item in selected] == [result.file_id]
    assert [item.file_id for item in submitted] == ([result.file_id] if kind == "attachment" else [])


@pytest.mark.asyncio
async def test_same_filename_requires_id_and_does_not_choose_latest_output():
    older, newer = artifact("older"), artifact("newer")
    service = service_for([publication(older), publication(newer)], {"older": older, "newer": newer})
    with pytest.raises(ValueError, match="多个.*选择具体文件"):
        await service._prepare_input_selection("session", "owner", None, None, message="分析 grouped_values.csv")
    service._file_storage.get_file_info.assert_not_awaited()
    selected, _ = await service._prepare_input_selection(
        "session", "owner", None, ["older"], message="分析 grouped_values.csv")
    assert [item.file_id for item in selected] == ["older"]


@pytest.mark.asyncio
async def test_output_and_original_input_with_same_name_are_also_ambiguous():
    source = FileInfo(file_id="source", filename="grouped_values.csv", size=20, user_id="owner")
    initial = assign_upload_namespace([source])
    result = artifact()
    service = service_for([admission(initial, attachments=initial), publication(result)],
                          {"source": source, "derived": result})
    with pytest.raises(ValueError, match="多个"):
        await service._prepare_input_selection("session", "owner", None, None, message="分析 grouped_values.csv")


@pytest.mark.asyncio
async def test_repeated_publication_of_same_id_is_not_a_name_collision():
    result = artifact()
    service = service_for([publication(result), publication(result)], {"derived": result})
    selected, _ = await service._prepare_input_selection(
        "session", "owner", None, None, message="分析 grouped_values.csv")
    assert [item.file_id for item in selected] == ["derived"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["foreign_owner", "missing_owner", "foreign_session", "not_artifact",
                                     "missing_hash", "changed_hash", "changed_size", "bad_proof_size",
                                     "changed_filename", "missing"])
async def test_promotion_revalidates_owned_stored_publication_provenance(mutation):
    delivered = artifact()
    current = delivered.model_copy(deep=True)
    if mutation == "foreign_owner":
        current.user_id = "other"
    elif mutation == "missing_owner":
        current.user_id = None
    elif mutation == "foreign_session":
        current.metadata["session_id"] = "other-session"
    elif mutation == "not_artifact":
        current.metadata["source"] = "user_upload"
    elif mutation == "missing_hash":
        current.metadata.pop("artifact_sha256")
    elif mutation == "changed_hash":
        current.metadata["artifact_sha256"] = "b" * 64
    elif mutation == "changed_size":
        current.size += 1
    elif mutation == "bad_proof_size":
        current.metadata["artifact_size"] = "42"
    elif mutation == "changed_filename":
        current.filename = "other.csv"
    elif mutation == "missing":
        current = None
    service = service_for([publication(delivered)], {"derived": current})
    with pytest.raises(ValueError, match="派生结果"):
        await service._prepare_input_selection("session", "owner", None, ["derived"])


@pytest.mark.asyncio
async def test_unpublished_stored_objects_and_model_written_paths_do_not_authorize_selection():
    result = artifact()
    history = [MessageEvent(role="assistant", message="结果在 /home/ubuntu/output/grouped_values.csv")]
    service = service_for(history, {"derived": result})
    selected, _ = await service._prepare_input_selection("session", "owner", None, None,
                                                       message="分析 /home/ubuntu/output/grouped_values.csv")
    assert selected == []
    with pytest.raises(ValueError, match="本会话"):
        await service._prepare_input_selection("session", "owner", None, ["derived"])
    with pytest.raises(ValueError, match="没有本会话的已发布记录"):
        await service._prepare_input_selection("session", "owner", [{"file_id": "derived"}], None)


@pytest.mark.asyncio
async def test_promoted_snapshot_survives_followups_and_clear_without_fabricating_uploads():
    result = artifact()
    history = [publication(result)]
    service = service_for(history, {"derived": result})
    selected, _ = await service._prepare_input_selection("session", "owner", None, None,
                                                       message="使用 grouped_values.csv")
    event = admission(selected)
    assert event.attachments is None
    history.append(event)
    restored, inherited = session_input_state(history)
    assert inherited == ["derived"]
    assert upload_runtime_path(restored[0]) == upload_runtime_path(selected[0])
    following, submitted = await service._prepare_input_selection("session", "owner", None, None, message="画直方图")
    assert [item.file_id for item in following] == ["derived"] and submitted == []
    assert upload_runtime_path(following[0]) == upload_runtime_path(selected[0])
    cleared, _ = await service._prepare_input_selection("session", "owner", None, [], message="清空分析资料")
    history.append(admission(cleared))
    assert (await service._prepare_input_selection("session", "owner", None, None))[0] == []
    payload = str(input_snapshot(selected))
    assert "private.example" not in payload and "/home/" not in payload and "file_path" not in payload


@pytest.mark.asyncio
async def test_frozen_snapshot_reauthorization_never_expands_and_rejects_changed_result():
    result = artifact()
    history = [publication(result)]
    service = service_for(history, {"derived": result})
    selected, _ = await service._prepare_input_selection("session", "owner", None, ["derived"])
    frozen = snapshot_files(admission(selected).metadata)
    # Even a later publication with the same name cannot change a queued scope.
    history.append(publication(artifact("later")))
    await service._reauthorize_input_files("session", "owner", frozen)
    assert [item.file_id for item in frozen] == ["derived"]
    result.metadata["artifact_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="归属校验"):
        await service._reauthorize_input_files("session", "owner", frozen)


@pytest.mark.asyncio
async def test_checkpoint_keeps_exact_manifest_and_cannot_promote_a_new_name():
    first, second = artifact("first", "first.csv"), artifact("second", "second.csv")
    history = [publication(first)]
    service = service_for(history, {"first": first, "second": second})
    selected, _ = await service._prepare_input_selection("session", "owner", None, ["first"])
    history.extend([admission(selected), publication(second)])
    context = build_analysis_inputs([], selected)
    resumed, submitted = await service._prepare_input_selection("session", "owner", None, None,
        checkpoint={"analysis_input_manifest": context.model_dump()}, message="使用 second.csv")
    assert [item.file_id for item in resumed] == ["first"] and submitted == []
    assert build_analysis_inputs([], resumed) == context
    assert resumed[0].metadata["analysis_input_origin"] == "published_artifact"


@pytest.mark.asyncio
async def test_legacy_dataset_checkpoint_does_not_inherit_newer_derived_inputs():
    result = artifact()
    history = [publication(result)]
    service = service_for(history, {"derived": result})
    selected, _ = await service._prepare_input_selection("session", "owner", None, ["derived"])
    history.append(admission(selected))
    resumed, submitted = await service._prepare_input_selection("session", "owner", None, None,
        checkpoint={"dataset_ids": ["legacy-dataset"]}, message="继续 grouped_values.csv")
    assert resumed == submitted == []


@pytest.mark.asyncio
async def test_named_output_path_is_rebound_to_the_current_readonly_manifest():
    result = artifact()
    service = service_for([publication(result)], {"derived": result})
    selected, _ = await service._prepare_input_selection("session", "owner", None, None,
                                                       message="分析 /home/ubuntu/output/grouped_values.csv")
    context = build_analysis_inputs([], selected)
    assert context.source_paths == [upload_runtime_path(selected[0])]
    prompt = render_upload_context(context)
    assert '"runtime_path": "/home/ubuntu/inputs/' in prompt
    assert "read it ONLY at its current manifest runtime_path" in prompt
    assert "previous /home/ubuntu/output path" in prompt
    assert "Update reused scripts" in prompt


@pytest.mark.asyncio
async def test_public_file_metadata_excludes_private_publication_identity(monkeypatch):
    from app.interfaces import dependencies
    from app.interfaces.schemas.event import EventMapper
    from app.interfaces.schemas.file import FileInfoResponse
    monkeypatch.setattr(dependencies, "get_file_service", lambda: SimpleNamespace(
        create_signed_url=AsyncMock(return_value="https://local.test/api/files/derived")))
    result = artifact()
    service = service_for([publication(result)], {"derived": result})
    selected, submitted = await service._prepare_input_selection(
        "session", "owner", [{"file_id": "derived"}], None)
    file_public = await FileInfoResponse.from_file_info(selected[0])
    event_public = await EventMapper.event_to_sse_event(admission(selected, attachments=submitted))
    for payload in [str(file_public.model_dump()), str(event_public.model_dump())]:
        assert "analysis_input_origin" not in payload and "analysis_input_sha256" not in payload
        assert "analysis_input_namespace" not in payload and "analysis_input_files" not in payload
        assert "private.example" not in payload and "/home/ubuntu/inputs" not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_after_admission", [False, True])
async def test_durable_recovery_reuses_exact_promoted_snapshot_before_model_or_mount(changed_after_admission):
    from app.application.services.dataset_request_resolver import FrontControllerResolution, RequestDecision, ExecutionDecision
    from app.domain.models.safety import SafetyReview
    from app.domain.services.execution_history import ExecutionHistory
    from test_input_delivery import FakeTask, MemoryInputs

    repository = MemoryInputs()
    result = artifact(owner="user")
    published = publication(result)
    published.seq = 1
    repository.events = [published]
    storage = SimpleNamespace(get_file_info=AsyncMock(side_effect=lambda fid, user: result if fid == "derived" else None))
    service = AgentDomainService(agent_repository=object(), session_repository=repository, sandbox_cls=object,
        task_cls=SimpleNamespace(get=lambda _: None), file_storage=storage, mcp_repository=object(),
        sandbox_runtime=object(), input_repository=repository)
    selected, _ = await service._prepare_input_selection("session", "user", None, None, message="分析 grouped_values.csv")
    record = await repository.accept("session", "user", admission(selected, message="分析 grouped_values.csv"))
    before = record.event.model_dump()
    projection = ExecutionHistory()
    projection.fold(published)
    repository.get_execution_history = AsyncMock(return_value=projection)
    # This later object has the same name, but recovery must not look it up or
    # reconsider the original explicit reference against changed history.
    later = publication(artifact("later", owner="user"))
    later.seq = record.event.seq + 1
    repository.events.append(later)
    resolution = FrontControllerResolution(decision=RequestDecision(
        safety=SafetyReview(decision="allow", risk_level="low"),
        execution=ExecutionDecision(mode="sandbox", required_evidence="file_content")), answer="", controller_metadata={})
    service._dataset_request_resolver = SimpleNamespace(resolve=AsyncMock(return_value=resolution))
    task = FakeTask()
    task.enqueue_input, task.run = AsyncMock(), AsyncMock()
    service._create_task = AsyncMock(return_value=task)
    storage.get_file_info.reset_mock()
    claimed = await service._input_delivery.claim(record)
    if changed_after_admission:
        result.metadata["artifact_sha256"] = "b" * 64
        with pytest.raises(ValueError, match="归属校验"):
            await service._dispatch_claimed_input(claimed)
        service._dataset_request_resolver.resolve.assert_not_awaited()
        service._create_task.assert_not_awaited()
        task.run.assert_not_awaited()
    else:
        assert await service._dispatch_claimed_input(claimed) is task
        restored = service._create_task.await_args.kwargs["input_files"]
        assert [item.file_id for item in restored] == ["derived"]
        assert upload_runtime_path(restored[0]) == upload_runtime_path(selected[0])
        routed = service._dataset_request_resolver.resolve.await_args.kwargs["datasets"]
        assert len(routed) == 1 and len(routed[0].files) == 1
        assert f"{routed[0].sandbox_path}/{routed[0].files[0].path}" == upload_runtime_path(selected[0])
        queued = MessageEvent.model_validate_json(task.enqueue_input.await_args.args[0])
        assert queued.metadata == record.event.metadata
        assert snapshot_files(queued.metadata)[0].file_id == "derived"
    storage.get_file_info.assert_awaited_once_with("derived", "user")
    assert record.event.model_dump() == before
