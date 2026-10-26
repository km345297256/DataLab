"""Drafts never enter the skills collection scanned by the runtime registry."""
from app.domain.models.skill_draft import SkillDraft
from app.infrastructure.models.documents import SessionDocument


class MongoSkillDraftRepository:
    def __init__(self, database=None):
        self._database = database

    @property
    def collection(self):
        database = self._database if self._database is not None else SessionDocument.get_pymongo_collection().database
        return database["skill_drafts"]

    async def create(self, draft: SkillDraft) -> SkillDraft:
        await self.collection.insert_one({"_id": draft.id, **draft.model_dump(mode="python")})
        return draft

    async def get(self, owner_id: str, draft_id: str) -> SkillDraft | None:
        row = await self.collection.find_one({"_id": draft_id, "owner_id": owner_id})
        return SkillDraft.model_validate(row) if row else None

    async def mark_installed(self, owner_id: str, draft_id: str, skill_id: str) -> None:
        result = await self.collection.update_one(
            {"_id": draft_id, "owner_id": owner_id}, {"$set": {"installed_skill_id": skill_id}},
        )
        if not result.matched_count:
            raise ValueError("Skill draft no longer exists")

    async def clear_installed(self, owner_id: str, draft_id: str, skill_id: str) -> None:
        await self.collection.update_one(
            {"_id": draft_id, "owner_id": owner_id, "installed_skill_id": skill_id},
            {"$set": {"installed_skill_id": None}},
        )
