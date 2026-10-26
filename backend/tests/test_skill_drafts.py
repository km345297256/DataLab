"""Generation is private; only an explicit owner action installs a package."""
import asyncio
import hashlib
import io
import json
import threading
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Header
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.application.errors.exceptions import BadRequestError, NotFoundError
from app.application.services.skill_draft_service import SkillDraftService, get_skill_draft_service
from app.domain.models.file import FileInfo
from app.domain.models.skill_draft import SkillCreationView, SkillDraft
from app.domain.models.user import User
from app.domain.services.skills.registry import SkillRegistry
from app.infrastructure.repositories.mongo_skill_draft_repository import MongoSkillDraftRepository
from app.interfaces.api import skill_routes
from app.interfaces.dependencies import get_current_user
from app.interfaces.errors.exception_handlers import register_exception_handlers


class DraftRepository:
    def __init__(self):
        self.rows = {}
        self.fail_install = False

    async def create(self, draft):
        self.rows[(draft.owner_id, draft.id)] = draft.model_copy(deep=True)
        return draft

    async def get(self, owner, draft_id):
        value = self.rows.get((owner, draft_id))
        return value.model_copy(deep=True) if value else None

    async def mark_installed(self, owner, draft_id, skill_id):
        if self.fail_install:
            raise RuntimeError("status persistence failed")
        self.rows[(owner, draft_id)].installed_skill_id = skill_id


class Storage:
    def __init__(self):
        self.files = {}
        self.upload_count = 0
        self.download_count = 0

    async def upload_file(self, data, filename, user_id, content_type=None, metadata=None):
        self.upload_count += 1
        payload = data.read()
        info = FileInfo(file_id=f"object-{self.upload_count}", filename=filename, user_id=user_id,
                        size=len(payload), content_type=content_type, metadata=metadata)
        self.files[info.file_id] = (payload, info)
        return info

    async def download_file(self, file_id, user_id):
        self.download_count += 1
        payload, info = self.files[file_id]
        if info.user_id != user_id:
            raise NotFoundError()
        return io.BytesIO(payload), info

    async def delete_file(self, file_id, user_id):
        self.files.pop(file_id, None)
        return True


def make_service():
    retention = SimpleNamespace(retain_file=AsyncMock(return_value=True), release_file=AsyncMock())
    return SkillDraftService(DraftRepository(), Storage(), retention)


async def create_draft(service, name="monthly-rainfall"):
    return await service.create(owner_id="alice", session_id="private-session", name=name,
        description="比较多个年份的逐月降水变化，检查数据完整性与时间分组。",
        triggers=["月度降水"], applicability=["具有日期和降水量字段的数据"],
        capabilities=["检查日期与单位", "按月汇总并核对总量"], content="# 分析方法\n保持输入只读。",
        references={"references/validation.json": '{"status":"not_run"}'}, scripts={})


@pytest.mark.asyncio
async def test_draft_stays_outside_catalog_and_zip_has_no_private_install_metadata(tmp_path):
    service = make_service()
    draft = await create_draft(service)
    registry = SkillRegistry(str(tmp_path), user_id="alice")
    assert registry.list_skills() == [] and not list(tmp_path.rglob("SKILL.md"))
    payload, downloaded = await service.download("alice", draft.id)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert sorted(archive.namelist()) == ["SKILL.md", "references/validation.json"]
        contents = "\n".join(archive.read(name).decode() for name in archive.namelist())
        assert "monthly-rainfall" in contents
        assert all(value not in contents for value in ["alice", "private-session", "owner_user_id", "metadata.json", draft.id])
    view = downloaded.public_view().model_dump(mode="json")
    assert not view["installed"] and view["capabilities"]
    assert view["validation"] == {"package": "passed", "script_syntax": "not_applicable", "workflow_execution": "not_run", "synthetic_execution": "not_run", "synthetic_case_count": 0, "synthetic_passed_count": 0}
    assert not {"package_file_id", "owner_id", "source_session_id", "package_sha256"} & view.keys()
    assert service.storage.upload_count == 1
    service.retention.retain_file.assert_awaited_once_with("alice", draft.package_file_id, "skill-draft:" + draft.id)


@pytest.mark.asyncio
async def test_owner_only_get_download_and_install(tmp_path):
    service = make_service()
    draft = await create_draft(service)
    for operation in (service.get("bob", draft.id), service.download("bob", draft.id),
                      service.install("bob", draft.id, SkillRegistry(str(tmp_path), user_id="bob"))):
        with pytest.raises(NotFoundError):
            await operation
    assert service.storage.download_count == 0 and not list(tmp_path.rglob("SKILL.md"))


@pytest.mark.asyncio
async def test_explicit_install_and_concurrent_retry_are_idempotent(tmp_path):
    service = make_service()
    draft = await create_draft(service)
    results = await asyncio.gather(*[
        service.install("alice", draft.id, SkillRegistry(str(tmp_path), user_id="alice")) for _ in range(4)
    ])
    assert len({value.installed_skill_id for value in results}) == 1
    assert all(value.public_view().installed for value in results)
    assert len(list(tmp_path.rglob("SKILL.md"))) == 1
    assert SkillRegistry(str(tmp_path), user_id="alice").get_skill(draft.name) is not None
    assert SkillRegistry(str(tmp_path), user_id="bob").get_skill(draft.name) is None
    payload, _ = await service.download("alice", draft.id)
    assert hashlib.sha256(payload).hexdigest() == draft.package_sha256
    assert service.storage.upload_count == 1


@pytest.mark.asyncio
async def test_failed_status_write_recovers_existing_install_without_second_package(tmp_path):
    service = make_service()
    draft = await create_draft(service)
    service.repository.fail_install = True
    with pytest.raises(RuntimeError):
        await service.install("alice", draft.id, SkillRegistry(str(tmp_path), user_id="alice"))
    assert len(list(tmp_path.rglob("SKILL.md"))) == 1
    service.repository.fail_install = False
    installed = await service.install("alice", draft.id, SkillRegistry(str(tmp_path), user_id="alice"))
    assert installed.installed_skill_id and len(list(tmp_path.rglob("SKILL.md"))) == 1


@pytest.mark.asyncio
async def test_separate_drafts_with_same_name_do_not_overwrite_owned_skill(tmp_path):
    service = make_service()
    first = await create_draft(service)
    second = await create_draft(service)
    first_installed = await service.install("alice", first.id, SkillRegistry(str(tmp_path), user_id="alice"))
    second_installed = await service.install("alice", second.id, SkillRegistry(str(tmp_path), user_id="alice"))
    assert first_installed.installed_skill_id != second_installed.installed_skill_id
    registry = SkillRegistry(str(tmp_path), user_id="alice")
    assert {skill.name for skill in registry.list_skills()} == {"monthly-rainfall", "monthly-rainfall-2"}
    assert service.storage.upload_count == 2


@pytest.mark.asyncio
async def test_changed_package_is_rejected_before_install(tmp_path):
    service = make_service()
    draft = await create_draft(service)
    payload, info = service.storage.files[draft.package_file_id]
    service.storage.files[draft.package_file_id] = (payload + b"corrupt", info)
    with pytest.raises(BadRequestError, match="完整性"):
        await service.install("alice", draft.id, SkillRegistry(str(tmp_path), user_id="alice"))
    assert not list(tmp_path.rglob("SKILL.md"))


@pytest.mark.asyncio
async def test_failed_draft_save_releases_only_its_object_reference():
    service = make_service()
    service.repository.create = AsyncMock(side_effect=RuntimeError("unavailable"))
    with pytest.raises(RuntimeError):
        await create_draft(service)
    assert not service.storage.files
    service.retention.release_file.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["upload", "insert"])
async def test_cancelled_caller_waits_for_commit_and_preserves_draft_object(stage):
    service = make_service()
    started, finish = asyncio.Event(), asyncio.Event()
    if stage == "upload":
        upload = service.storage.upload_file
        async def blocked_upload(*args, **kwargs):
            result = await upload(*args, **kwargs)
            started.set()
            await finish.wait()
            return result
        service.storage.upload_file = blocked_upload
    else:
        insert = service.repository.create
        async def blocked_insert(draft):
            result = await insert(draft)
            started.set()
            await finish.wait()
            return result
        service.repository.create = blocked_insert
    pending = asyncio.create_task(create_draft(service))
    await started.wait()
    pending.cancel()
    await asyncio.sleep(0)
    pending.cancel()
    await asyncio.sleep(0)
    assert not pending.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert len(service.repository.rows) == 1 and len(service.storage.files) == 1
    draft = next(iter(service.repository.rows.values()))
    payload, _ = await service.download("alice", draft.id)
    assert hashlib.sha256(payload).hexdigest() == draft.package_sha256
    service.retention.release_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_lost_insert_acknowledgement_preserves_confirmed_committed_package():
    service = make_service()
    insert = service.repository.create
    async def lost_ack(draft):
        await insert(draft)
        raise RuntimeError("write acknowledgement lost")
    service.repository.create = lost_ack
    draft = await create_draft(service)
    assert ("alice", draft.id) in service.repository.rows
    assert draft.package_file_id in service.storage.files
    service.retention.release_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_commit_outcome_does_not_reclaim_possibly_referenced_object():
    service = make_service()
    service.repository.create = AsyncMock(side_effect=RuntimeError("write acknowledgement lost"))
    service.repository.get = AsyncMock(side_effect=RuntimeError("database unavailable"))
    with pytest.raises(RuntimeError):
        await create_draft(service)
    assert len(service.storage.files) == 1
    service.retention.release_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_download_reads_closes_and_releases_connection_off_event_loop():
    service = make_service()
    draft = await create_draft(service)
    payload, info = service.storage.files[draft.package_file_id]
    calls = []
    class Response(io.BytesIO):
        def read(self, size=-1):
            calls.append(("read", threading.get_ident()))
            return super().read(size)
        def close(self):
            calls.append(("close", threading.get_ident()))
            super().close()
        def release_conn(self):
            calls.append(("release", threading.get_ident()))
    service.storage.download_file = AsyncMock(return_value=(Response(payload), info))
    downloaded, _ = await service.download("alice", draft.id)
    assert downloaded == payload
    assert [call[0] for call in calls] == ["read", "close", "release"]
    assert len({call[1] for call in calls}) == 1 and calls[0][1] != threading.get_ident()


@pytest.mark.parametrize("invalid_id", ["../private", "a" * 31, "A" * 32, "a" * 33])
def test_draft_public_ids_only_accept_opaque_lowercase_hex(invalid_id):
    for model, values in ((SkillCreationView, {}), (SkillDraft, {
        "owner_id": "alice", "source_session_id": "private", "package_file_id": "object", "package_sha256": "a" * 64,
    })):
        with pytest.raises(ValidationError):
            model(id=invalid_id, name="rainfall", description="A generated analysis skill", **values)


@pytest.mark.asyncio
async def test_repository_queries_include_owner_on_read_and_write():
    collection = SimpleNamespace(find_one=AsyncMock(return_value=None), update_one=AsyncMock(return_value=SimpleNamespace(matched_count=1)))
    repository = MongoSkillDraftRepository({"skill_drafts": collection})
    assert await repository.get("alice", "a" * 32) is None
    await repository.mark_installed("alice", "a" * 32, "skill-id")
    assert collection.find_one.call_args.args[0] == {"_id": "a" * 32, "owner_id": "alice"}
    assert collection.update_one.call_args.args[0] == {"_id": "a" * 32, "owner_id": "alice"}


@pytest.mark.asyncio
async def test_http_owner_views_and_download_before_install(tmp_path, monkeypatch):
    service = make_service()
    draft = await create_draft(service)
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(skill_routes.router)
    async def current_user(x_user: str = Header("alice")):
        return User(id=x_user, fullname=x_user, email=x_user + "@example.test")
    app.dependency_overrides[get_current_user] = current_user
    app.dependency_overrides[get_skill_draft_service] = lambda: service
    monkeypatch.setattr(skill_routes, "_registry", lambda owner: SkillRegistry(str(tmp_path), user_id=owner))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        preview = await client.get("/skills/drafts/" + draft.id)
        assert preview.status_code == 200 and preview.json()["data"]["installed"] is False
        response = await client.get(f"/skills/drafts/{draft.id}/download")
        assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
        assert "private, no-store" == response.headers["cache-control"]
        for method, suffix in (("get", ""), ("get", "/download"), ("post", "/install")):
            response = await getattr(client, method)(f"/skills/drafts/{draft.id}{suffix}", headers={"x-user": "bob"})
            assert response.status_code == 404
        installed = await client.post(f"/skills/drafts/{draft.id}/install")
        assert installed.status_code == 200 and installed.json()["data"]["installed"] is True
        refreshed = await client.get(f"/skills/drafts/{draft.id}")
        assert refreshed.json()["data"] == installed.json()["data"]
