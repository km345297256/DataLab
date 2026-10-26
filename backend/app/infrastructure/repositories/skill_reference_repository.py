"""Narrow, compensatable reference changes; never save a loaded user account."""
import re
from datetime import UTC, datetime

from app.infrastructure.models.documents import UserDocument
from app.infrastructure.repositories.mongo_skill_draft_repository import MongoSkillDraftRepository


class MongoSkillReferenceRepository:
    @property
    def users(self):
        return UserDocument.get_pymongo_collection()

    @property
    def drafts(self):
        return MongoSkillDraftRepository().collection

    async def snapshot(self, skill, owner_id):
        # Private skills must not change another user's same-named skill. A
        # shared personal skill can also have explicit subscribers.
        users = await self.users.find(
            {"$or": [{"user_id": owner_id}, {"installed_skill_ids": skill.id}]},
            {"user_id": 1, "installed_skill_ids": 1, "auto_enabled_skills": 1},
        ).to_list(length=None)
        drafts = await self.drafts.find(
            {"owner_id": owner_id, "installed_skill_id": skill.id}, {"_id": 1},
        ).to_list(length=None)
        return users, drafts

    async def detach(self, skill, owner_id, snapshot):
        users, _ = snapshot
        name_match = {"$regex": r"^\s*" + re.escape(skill.name.strip()) + r"\s*$", "$options": "i"}
        await self.users.update_many(
            {"user_id": {"$in": [row["user_id"] for row in users]}},
            {"$pull": {"installed_skill_ids": skill.id, "auto_enabled_skills": name_match},
             "$set": {"updated_at": datetime.now(UTC)}},
        )
        await self.drafts.update_many(
            {"owner_id": owner_id, "installed_skill_id": skill.id},
            {"$set": {"installed_skill_id": None}},
        )

    async def restore(self, skill, owner_id, snapshot):
        users, drafts = snapshot
        for row in users:
            additions = {}
            if skill.id in row.get("installed_skill_ids", []):
                additions["installed_skill_ids"] = skill.id
            names = [name for name in row.get("auto_enabled_skills", [])
                     if name.strip().casefold() == skill.name.strip().casefold()]
            if names:
                additions["auto_enabled_skills"] = {"$each": names}
            if additions:
                await self.users.update_one({"user_id": row["user_id"]}, {"$addToSet": additions})
        if drafts:
            await self.drafts.update_many(
                {"_id": {"$in": [row["_id"] for row in drafts]}, "owner_id": owner_id,
                 "installed_skill_id": None},
                {"$set": {"installed_skill_id": skill.id}},
            )
