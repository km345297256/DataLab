"""Storage limits fail explicitly without losing validated package contents."""
import copy
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import zipfile

import pytest

from app.application.errors.exceptions import BadRequestError
from app.application.services.skill_draft_service import MAX_DRAFT_BYTES, SkillDraftCapacityError
from app.domain.services.skills.registry import SkillRegistry
from app.domain.services.skills.skill_creation import service as creation
from app.domain.services.skills.skill_creation_error import SkillCreationError
from test_skill_drafts import make_service


def package_at_limit(kind, *, excess=False):
    fields = dict(owner_id="alice", session_id="private-session", name="capacity-fixture",
        description="人工容量边界验证", triggers=["容量验证"], applicability=["人工验证"],
        capabilities=["保留完整资料"], content="# 完整方法\n保持原始内容。",
        references={}, scripts={"scripts/run.py": "print('preserve this source unchanged')\n"},
        synthetic_execution={"status": "not_run", "cases": []})
    if kind == "files":
        fields["references"] = {f"references/item-{i}.txt": f"完整资料 {i}\n"
                                for i in range(62 + int(excess))}
    else:
        markdown = ("---\nname: " + fields["name"] + "\ndescription: "
            + json.dumps(fields["description"], ensure_ascii=False)
            + "\ntriggers: " + json.dumps(fields["triggers"], ensure_ascii=False)
            + "\n---\n\n" + fields["content"].strip() + "\n")
        fixed_bytes = sum(len(value.encode("utf-8"))
                          for value in [markdown, *fields["scripts"].values()])
        # UTF-8 bytes, including the real SKILL.md frontmatter, define capacity.
        prefix = "完整证据："
        padding = MAX_DRAFT_BYTES - fixed_bytes - len(prefix.encode("utf-8")) + int(excess)
        fields["references"] = {"references/evidence.txt": prefix + "x" * padding}
    return fields


def wire_application_boundary(monkeypatch, draft_service, fields):
    """Replace synthesis only; exercise the actual application/storage path."""
    recipe = SimpleNamespace(name=fields["name"], description=fields["description"],
        triggers=fields["triggers"], applicability=fields["applicability"],
        steps=[SimpleNamespace(action=value) for value in fields["capabilities"]])
    result = SimpleNamespace(recipe=recipe, execution=fields["synthetic_execution"])
    pipeline = SimpleNamespace(run=AsyncMock(return_value=result))
    monkeypatch.setattr(creation, "prepare_analysis_context",
                        AsyncMock(return_value=SimpleNamespace(privacy=None)))
    monkeypatch.setattr(creation, "resolve_skill_model_policy", lambda *args: SimpleNamespace(
        settings=SimpleNamespace(), capacity=None))
    monkeypatch.setattr(creation, "design_instructions", lambda: "Artificial storage boundary")
    monkeypatch.setattr(creation, "SynthesisGateway", lambda **kwargs: object())
    monkeypatch.setattr(creation, "SkillCreationPipeline", lambda *args, **kwargs: pipeline)
    monkeypatch.setattr(creation, "render_result", lambda value:
                        (fields["content"], fields["references"], fields["scripts"]))
    dependencies = creation.CreationDependencies(lambda: SimpleNamespace(),
        lambda *args, **kwargs: object(), lambda: draft_service,
        lambda: None, None, None)

    async def run():
        return await creation.create_skill_from_session(fields["session_id"], fields["owner_id"],
            None, None, dependencies=dependencies)
    return run, pipeline


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["bytes", "files"])
async def test_real_capacity_rejection_has_nonretryable_creator_diagnostic(monkeypatch, kind):
    fields = package_at_limit(kind, excess=True)
    frozen = copy.deepcopy(fields)
    draft_service = make_service()
    run, pipeline = wire_application_boundary(monkeypatch, draft_service, fields)

    with pytest.raises(SkillCreationError) as caught:
        await run()

    assert type(caught.value.__cause__) is SkillDraftCapacityError
    assert (caught.value.stage, caught.value.code, caught.value.retryable) == (
        "package_storage", "package_too_large", False)
    assert "缩小技能范围" in caught.value.public_message
    assert all(private not in caught.value.public_message for private in
               (fields["owner_id"], fields["session_id"], "scripts/run.py"))
    pipeline.run.assert_awaited_once()
    assert fields == frozen  # Neither evidence nor source is trimmed to fit.
    assert draft_service.storage.upload_count == 0
    assert draft_service.storage.files == {} and draft_service.repository.rows == {}
    draft_service.retention.retain_file.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["bytes", "files"])
async def test_exact_capacity_remains_downloadable_and_installable(tmp_path, kind):
    fields = package_at_limit(kind)
    frozen = copy.deepcopy(fields)
    draft_service = make_service()
    draft = await draft_service.create(**fields)
    payload, downloaded = await draft_service.download(fields["owner_id"], draft.id)
    assert downloaded.id == draft.id
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        entries = archive.infolist()
        if kind == "bytes":
            assert sum(item.file_size for item in entries) == MAX_DRAFT_BYTES
        else:
            assert len(entries) == 64
        for path, content in {**fields["references"], **fields["scripts"]}.items():
            assert archive.read(path).decode("utf-8") == content
    installed = await draft_service.install(fields["owner_id"], draft.id,
        SkillRegistry(str(tmp_path / "skills"), user_id=fields["owner_id"]))
    assert installed.installed_skill_id
    assert fields == frozen
    assert draft_service.storage.upload_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["resource_path", "verification"])
async def test_other_bad_requests_remain_distinct_failures(monkeypatch, failure):
    fields = package_at_limit("files")
    if failure == "resource_path":
        fields["references"] = {"../private.txt": "must not be packaged"}
    else:
        fields["synthetic_execution"] = {"status": "failed", "cases": []}
    draft_service = make_service()
    run, _ = wire_application_boundary(monkeypatch, draft_service, fields)

    with pytest.raises(BadRequestError) as caught:
        await run()

    assert type(caught.value) is BadRequestError
    assert draft_service.storage.upload_count == 0
    assert draft_service.repository.rows == {}
