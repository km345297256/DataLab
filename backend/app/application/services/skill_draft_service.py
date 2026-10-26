"""Persist a bounded portable package once, then install only on owner action."""
import asyncio
import hashlib
import io
import json
import re
import zipfile
from pathlib import PurePosixPath
from weakref import WeakValueDictionary

from app.application.errors.exceptions import BadRequestError, NotFoundError
from app.domain.models.skill_draft import SkillCreationValidation, SkillDraft
from app.domain.models.workspace import personal_workspace_id
from app.domain.services.skills.catalog_lock import serialized_skill_catalog

# Portable package resource protection, independent of model generation length.
MAX_DRAFT_BYTES = 1024 * 1024
_installation_locks = WeakValueDictionary()


class SkillDraftCapacityError(BadRequestError):
    """The complete validated package exceeds the portable storage policy."""
    def __init__(self):
        super().__init__("生成的技能包超过容量限制，请缩小技能范围后重新创建")


async def _complete_commit(operation):
    """A cancelled HTTP/tool caller cannot undo a sent durable write."""
    pending = asyncio.create_task(operation)
    try:
        return await asyncio.shield(pending)
    except asyncio.CancelledError:
        while not pending.done():
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not pending.cancelled():
            pending.exception()
        raise


class SkillDraftService:
    def __init__(self, repository, storage, retention, skill_repository=None):
        self.repository = repository
        self.storage = storage
        self.retention = retention
        self.skill_repository = skill_repository

    async def create(self, *, owner_id, session_id, name, description, triggers,
                     applicability, capabilities, content, references, scripts, synthetic_execution=None) -> SkillDraft:
        execution = synthetic_execution or {"status": "not_run", "cases": []}
        if execution.get("status") not in {"passed", "not_run", "not_applicable", "unsupported"}:
            raise BadRequestError("技能的合成用例验证未通过，未保存技能")
        cases = execution.get("cases", [])
        if any(case.get("status") not in {"passed", "unsupported"} for case in cases):
            raise BadRequestError("技能的合成用例验证未通过，未保存技能")
        passed_count = sum(case.get("status") == "passed" for case in cases)
        if execution.get("status") == "passed" and (not cases or passed_count != len(cases)):
            raise BadRequestError("技能的合成用例验证记录不完整，未保存技能")
        markdown = ("---\nname: " + name + "\ndescription: " + json.dumps(description, ensure_ascii=False)
                    + "\ntriggers: " + json.dumps(triggers, ensure_ascii=False)
                    + "\n---\n\n" + content.strip() + "\n")
        files = {"SKILL.md": markdown, **references, **scripts}
        for path in files:
            self._validate_resource(path)
        if len(files) > 64 or sum(len(value.encode("utf-8")) for value in files.values()) > MAX_DRAFT_BYTES:
            raise SkillDraftCapacityError()
        package = io.BytesIO()
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path, value in sorted(files.items()):
                archive.writestr(path, value)
        payload = package.getvalue()
        package.seek(0)
        draft = SkillDraft(owner_id=owner_id, source_session_id=session_id, name=name,
            description=description, triggers=triggers, applicability=applicability,
            capabilities=capabilities, resources=sorted(files), package_file_id="",
            package_sha256=hashlib.sha256(payload).hexdigest(),
            validation=SkillCreationValidation(script_syntax="passed" if scripts else "not_applicable",
                synthetic_execution=execution["status"],
                synthetic_case_count=sum(case.get("status") != "unsupported" for case in cases),
                synthetic_passed_count=passed_count))
        reference = "skill-draft:" + draft.id

        async def commit():
            info = None
            retained = False
            try:
                info = await self.storage.upload_file(package, name + ".zip", owner_id,
                    content_type="application/zip", metadata={"source": "generated_skill_package"})
                draft.package_file_id = info.file_id
                retained = await self.retention.retain_file(owner_id, info.file_id, reference)
                if not retained:
                    raise BadRequestError("技能包暂时无法保存，请重新生成")
                return await self.repository.create(draft)
            except Exception:
                if info is not None:
                    if retained:
                        # A lost write acknowledgement can follow a committed
                        # insert. Resolve it before reclaiming the object; if
                        # the database cannot answer, preserve its reference.
                        existing = await self.repository.get(owner_id, draft.id)
                        if existing is not None:
                            return existing
                        await self.retention.release_file(owner_id, info.file_id, reference)
                    await self.storage.delete_file(info.file_id, owner_id)
                raise
            finally:
                package.close()

        return await _complete_commit(commit())

    @serialized_skill_catalog
    async def get(self, owner_id: str, draft_id: str) -> SkillDraft:
        if not re.fullmatch(r"[a-f0-9]{32}", draft_id):
            raise NotFoundError("技能草稿不存在或无权访问")
        draft = await self.repository.get(owner_id, draft_id)
        if draft is None or draft.owner_id != owner_id:
            raise NotFoundError("技能草稿不存在或无权访问")
        if draft.installed_skill_id and self.skill_repository is not None:
            installed = await self.skill_repository.get_by_id(draft.installed_skill_id)
            if installed is None or (installed.owner_user_id or installed.user_id) != owner_id:
                await self.repository.clear_installed(owner_id, draft.id, draft.installed_skill_id)
                draft.installed_skill_id = None
        return draft

    async def download(self, owner_id: str, draft_id: str) -> tuple[bytes, SkillDraft]:
        draft = await self.get(owner_id, draft_id)
        try:
            stream, info = await self.storage.download_file(draft.package_file_id, owner_id)
        except (FileNotFoundError, PermissionError) as exc:
            raise NotFoundError("技能包不可用") from exc
        def read_and_close():
            try:
                if info.user_id != owner_id or not info.size or info.size > MAX_DRAFT_BYTES:
                    raise NotFoundError("技能包不可用")
                return stream.read(MAX_DRAFT_BYTES + 1)
            finally:
                try:
                    stream.close()
                finally:
                    release = getattr(stream, "release_conn", None)
                    if callable(release):
                        release()

        # Keep read and both cleanup operations in the same worker even if the
        # awaiting caller is cancelled while the bounded network read runs.
        payload = await asyncio.to_thread(read_and_close)
        if len(payload) > MAX_DRAFT_BYTES or hashlib.sha256(payload).hexdigest() != draft.package_sha256:
            raise BadRequestError("技能包完整性校验失败，请重新生成")
        return payload, draft

    async def read_resource(self, owner_id: str, draft_id: str, path: str) -> str:
        """Read one owned package member as text, without extracting or running it."""
        draft = await self.get(owner_id, draft_id)
        try:
            self._validate_resource(path)
        except BadRequestError as exc:
            raise NotFoundError("技能文件不存在或无权访问") from exc
        if path not in draft.resources:
            raise NotFoundError("技能文件不存在或无权访问")
        payload, draft = await self.download(owner_id, draft_id)
        if path not in draft.resources:
            raise NotFoundError("技能文件不存在或无权访问")

        def read_member():
            try:
                with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                    entries = archive.infolist()
                    names = [entry.filename for entry in entries]
                    if (len(entries) != len(draft.resources) or len(set(names)) != len(names)
                            or set(names) != set(draft.resources)
                            or sum(entry.file_size for entry in entries) > MAX_DRAFT_BYTES):
                        raise BadRequestError("技能包文件清单校验失败")
                    for entry in entries:
                        self._validate_resource(entry.filename)
                    # Only ZIP member bytes are read. No filesystem path is
                    # constructed, including for script or Markdown content.
                    with archive.open(path) as member:
                        content = member.read(MAX_DRAFT_BYTES + 1)
                    if len(content) > MAX_DRAFT_BYTES:
                        raise BadRequestError("技能文件超过预览容量限制")
                    return content.decode("utf-8")
            except (zipfile.BadZipFile, UnicodeDecodeError, KeyError, RuntimeError, NotImplementedError) as exc:
                raise BadRequestError("技能包格式无效，无法预览文件") from exc

        return await asyncio.to_thread(read_member)

    @staticmethod
    def _validate_resource(path):
        parts = PurePosixPath(path).parts
        if (not parts or path.startswith("/") or "\\" in path or ":" in path or ".." in parts
                or any(part.startswith(".") for part in parts) or PurePosixPath(path).as_posix() != path
                or path != "SKILL.md" and (len(parts) < 2 or parts[0] not in {"references", "scripts"})):
            raise BadRequestError("技能包包含无效文件路径")

    @serialized_skill_catalog
    async def install(self, owner_id: str, draft_id: str, registry) -> SkillDraft:
        if registry.user_id != owner_id:
            raise NotFoundError("技能草稿不存在或无权访问")
        # The deployed backend uses a single asyncio worker (as do library
        # mutations). Keep a strong reference while pending. On process restart,
        # registry installation metadata recovers an already committed package.
        key = (id(asyncio.get_running_loop()), owner_id, draft_id)
        lock = _installation_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _installation_locks[key] = lock
        async with lock:
            draft = await self.get(owner_id, draft_id)
            if draft.installed_skill_id:
                return draft
            payload, draft = await self.download(owner_id, draft_id)
            try:
                with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                    entries = archive.infolist()
                    if (len(entries) != len(draft.resources) or {entry.filename for entry in entries} != set(draft.resources)
                            or sum(entry.file_size for entry in entries) > MAX_DRAFT_BYTES):
                        raise BadRequestError("技能包文件清单校验失败")
                    for entry in entries:
                        self._validate_resource(entry.filename)
                    files = {entry.filename: archive.read(entry).decode("utf-8") for entry in entries}
            except (zipfile.BadZipFile, UnicodeDecodeError, KeyError) as exc:
                raise BadRequestError("技能包格式无效，请重新生成") from exc
            _, body = registry._split_frontmatter(files.pop("SKILL.md"))
            skill = await registry.save_generated_skill(
                name=draft.name, description=draft.description, triggers=draft.triggers, content=body,
                user_id=owner_id, created_from_session_id=draft.source_session_id,
                workspace_id=personal_workspace_id(owner_id), generated_draft_id=draft.id,
                references={path: value for path, value in files.items() if path.startswith("references/")},
                scripts={path: value for path, value in files.items() if path.startswith("scripts/")},
            )
            await self.repository.mark_installed(owner_id, draft.id, skill.id)
            draft.installed_skill_id = skill.id
            return draft


def get_skill_draft_service() -> SkillDraftService:
    from app.infrastructure.external.file.factory import get_file_storage
    from app.infrastructure.repositories.mongo_data_library_repository import MongoDataLibraryRepository
    from app.infrastructure.repositories.mongo_skill_draft_repository import MongoSkillDraftRepository
    from app.infrastructure.repositories.skill_repository import MongoSkillRepository
    return SkillDraftService(MongoSkillDraftRepository(), get_file_storage(), MongoDataLibraryRepository(), MongoSkillRepository())
