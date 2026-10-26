"""Delete only an owned managed installation, keeping its portable draft."""
import logging
import shutil
import uuid
from pathlib import Path

from app.application.errors.exceptions import BadRequestError, ForbiddenError, NotFoundError
from app.application.services.skill_draft_service import _complete_commit
from app.domain.services.skills.catalog_lock import skill_catalog_lock

logger = logging.getLogger(__name__)


def managed_owned_package(skill, registry, owner_id):
    path = Path(skill.path)
    if not path.is_absolute() or path.name != "SKILL.md" or ".." in path.parts:
        raise BadRequestError("技能文件路径无效，无法删除")
    package = path.parent
    # Exactly one package under this owner's directory, or an owned published
    # package under a known global root. Never accept an arbitrary descendant,
    # the storage root itself, another owner's directory, or a symlink escape.
    roots = {registry._user_dir(owner_id).resolve(), registry.skills_dir.resolve(),
             (registry.skills_dir / "global").resolve()}
    if (package.parent.resolve() not in roots or package.resolve().parent not in roots
            or package.name.startswith(".") or package.is_symlink() or path.is_symlink()
            or package.resolve() in roots or package.resolve() == registry.user_skills_dir.resolve()):
        raise BadRequestError("技能文件不在可删除目录内")
    if package.exists() and (not package.is_dir() or not path.is_file()):
        raise BadRequestError("技能文件路径无效，无法删除")
    return package if package.exists() else None


class SkillDeletionService:
    def __init__(self, repository, references):
        self.repository = repository
        self.references = references

    async def delete(self, owner_id, skill_id, registry):
        async def commit():
            async with skill_catalog_lock():
                skill = await self.repository.get_by_id(skill_id)
                if skill is None:
                    raise NotFoundError("技能不存在或无权访问")
                actual_owner = skill.owner_user_id or skill.user_id
                if not actual_owner:
                    raise ForbiddenError("官方技能不能删除，可从我的技能中移除")
                if actual_owner != owner_id:
                    raise NotFoundError("技能不存在或无权访问")
                package = managed_owned_package(skill, registry, owner_id)
                snapshot = await self.references.snapshot(skill, owner_id)
                staged = None
                if package is not None:
                    trash = package.parent / ".skill-trash"
                    if trash.is_symlink():
                        raise BadRequestError("技能暂存目录无效，无法删除")
                    trash.mkdir(exist_ok=True)
                    staged = trash / uuid.uuid4().hex
                    package.rename(staged)
                try:
                    # The durable deletion is the commit point. Reference
                    # changes can be compensated without replacing account data.
                    await self.references.detach(skill, owner_id, snapshot)
                    await self.repository.delete_owned(skill, owner_id)
                except Exception:
                    # Resolve lost deletion acknowledgements before restoring
                    # files; a committed delete must never be resurrected.
                    remaining = await self.repository.get_by_id(skill_id)
                    if remaining is not None:
                        try:
                            await self.references.restore(skill, owner_id, snapshot)
                        finally:
                            if staged is not None and staged.exists():
                                staged.rename(package)
                        raise
                if staged is not None:
                    try:
                        shutil.rmtree(staged)
                        staged.parent.rmdir()
                    except OSError:
                        # The quarantined directory is outside registry scans;
                        # cleanup failure cannot bring a deleted skill back.
                        logger.warning("Deleted skill package cleanup deferred: %s", skill.id)
                return skill
        # Once a delete begins, a disconnected HTTP client cannot leave the
        # package staged and its references only partly updated.
        return await _complete_commit(commit())


def get_skill_deletion_service():
    from app.infrastructure.repositories.skill_repository import MongoSkillRepository
    from app.infrastructure.repositories.skill_reference_repository import MongoSkillReferenceRepository
    return SkillDeletionService(MongoSkillRepository(), MongoSkillReferenceRepository())
