"""Owned draft files are previewed as inert text from the existing object."""
import hashlib
import io
import zipfile

import pytest
from fastapi import FastAPI, Header
from httpx import ASGITransport, AsyncClient

from app.application.errors.exceptions import BadRequestError, NotFoundError
from app.application.services.skill_draft_service import MAX_DRAFT_BYTES, get_skill_draft_service
from app.domain.models.user import User
from app.interfaces.api import skill_routes
from app.interfaces.dependencies import get_current_user
from app.interfaces.errors.exception_handlers import register_exception_handlers
from test_skill_drafts import create_draft, make_service


@pytest.mark.asyncio
async def test_preview_http_returns_exact_text_without_installing_or_executing(monkeypatch):
    service = make_service()
    references = {"references/输入契约.json": '{"说明":"保持中文与原格式", "示例":"<script>alert(1)</script>"}\n'}
    scripts = {"scripts/analyse.py": 'raise RuntimeError("preview must never execute me")\n'}
    draft = await service.create(owner_id="alice", session_id="private-session", name="preview-skill",
        description="预览技能内容", triggers=[], applicability=[], capabilities=[],
        content="# 技能介绍\n\n保持原文。\n", references=references, scripts=scripts)
    before = draft.model_dump(mode="json")
    def cannot_install(*args, **kwargs):
        pytest.fail("preview must not open the installed skill registry")
    monkeypatch.setattr(skill_routes, "_registry", cannot_install)
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(skill_routes.router)
    async def current_user(x_user: str = Header("alice")):
        return User(id=x_user, fullname=x_user, email=x_user + "@example.test")
    app.dependency_overrides[get_current_user] = current_user
    app.dependency_overrides[get_skill_draft_service] = lambda: service
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for path, expected in {**references, **scripts}.items():
            response = await client.get(f"/skills/drafts/{draft.id}/resource", params={"path": path})
            assert response.status_code == 200
            assert response.json()["data"] == {"path": path, "content": expected, "binary": False}
            assert response.headers["content-type"] == "application/json"
            assert response.headers["cache-control"] == "private, no-store"
            assert response.headers["x-content-type-options"] == "nosniff"
        response = await client.get(f"/skills/drafts/{draft.id}/resource", params={"path": "SKILL.md"})
        assert response.status_code == 200 and "# 技能介绍\n\n保持原文。\n" in response.json()["data"]["content"]
        count = service.storage.download_count
        for path in ["SKILL.md", "references/输入契约.json"]:
            response = await client.get(f"/skills/drafts/{draft.id}/resource", params={"path": path}, headers={"x-user": "bob"})
            assert response.status_code == 404
        assert service.storage.download_count == count
        missing = await client.get(f"/skills/drafts/{draft.id}/resource", params={"path": "scripts/missing.py"})
        assert missing.status_code == 404
        assert service.storage.download_count == count
    assert (await service.get("alice", draft.id)).model_dump(mode="json") == before
    assert service.storage.upload_count == 1
    service.retention.retain_file.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "", "../SKILL.md", "/SKILL.md", "references/../../SKILL.md", "references\\validation.json",
    "./SKILL.md", "references//validation.json", "references/./validation.json", "skill.md",
    "references/%2e%2e/SKILL.md", "file:///etc/passwd", "references/validation.json\x00",
])
async def test_preview_requires_exact_declared_member_before_object_read(path):
    service = make_service()
    draft = await create_draft(service)
    with pytest.raises(NotFoundError):
        await service.read_resource("alice", draft.id, path)
    assert service.storage.download_count == 0


@pytest.mark.asyncio
async def test_preview_checks_package_digest_before_reading_member():
    service = make_service()
    draft = await create_draft(service)
    payload, info = service.storage.files[draft.package_file_id]
    service.storage.files[draft.package_file_id] = (payload + b"changed", info)
    with pytest.raises(BadRequestError, match="完整性"):
        await service.read_resource("alice", draft.id, "SKILL.md")


def replace_package(service, draft, members, resources=None):
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in members:
            archive.writestr(path, content)
    payload = package.getvalue()
    _, info = service.storage.files[draft.package_file_id]
    service.storage.files[draft.package_file_id] = (payload, info.model_copy(update={"size": len(payload)}))
    row = service.repository.rows[("alice", draft.id)]
    row.package_sha256 = hashlib.sha256(payload).hexdigest()
    if resources is not None:
        row.resources = resources


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["missing", "duplicate", "oversized", "invalid-utf8", "invalid-path"])
async def test_preview_rejects_invalid_archive_even_with_matching_digest(kind):
    service = make_service()
    draft = await create_draft(service)
    members = [("SKILL.md", b"# Original"), ("references/validation.json", b"{}")]
    resources = None
    if kind == "missing":
        members.pop()
    elif kind == "duplicate":
        members.append(("SKILL.md", b"# Replacement"))
    elif kind == "oversized":
        members[0] = ("SKILL.md", b"x" * (MAX_DRAFT_BYTES + 1))
    elif kind == "invalid-utf8":
        members[0] = ("SKILL.md", b"\xff\xfe")
    else:
        members.append(("../../secret", b"must never be extracted"))
        resources = [name for name, _ in members]
    if kind == "duplicate":
        with pytest.warns(UserWarning, match="Duplicate name"):
            replace_package(service, draft, members, resources)
    else:
        replace_package(service, draft, members, resources)
    with pytest.raises(BadRequestError):
        await service.read_resource("alice", draft.id, "SKILL.md")


@pytest.mark.asyncio
async def test_unknown_and_invalid_drafts_do_not_read_storage():
    service = make_service()
    for identifier in ["a" * 32, "../draft", "A" * 32]:
        with pytest.raises(NotFoundError):
            await service.read_resource("alice", identifier, "SKILL.md")
    assert service.storage.download_count == 0
