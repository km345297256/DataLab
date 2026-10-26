"""Mongo is the durable task authority; lease tokens fence obsolete workers."""
import copy
import re
import time
from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.domain.models.data_candidate import identity
from app.infrastructure.models.documents import SessionDocument


class OpenDataRepository:
    def __init__(self, database=None):
        self._database = database

    @property
    def database(self):
        return self._database if self._database is not None else SessionDocument.get_pymongo_collection().database

    @property
    def collection(self):
        return self.database["open_data_tasks"]

    async def ensure_indexes(self):
        await self.collection.create_index([("kind", 1), ("status", 1), ("lease_until", 1), ("created_at", 1)])
        await self.collection.create_index([("owner_id", 1), ("session_id", 1), ("kind", 1), ("created_at", -1)])

    async def insert(self, kind, owner, session, payload):
        row = {"_id": kind + ":" + payload["id"], "kind": kind, "owner_id": owner,
               "session_id": session, "status": payload["status"], "version": 0,
               "lease_until": 0, "created_at": payload["created_at"], "payload": copy.deepcopy(payload)}
        try:
            await self.collection.insert_one(row)
            return row
        except DuplicateKeyError:
            return await self.get(kind, owner, session, payload["id"])

    async def get(self, kind, owner, session, task_id):
        return await self.collection.find_one({"_id": kind + ":" + task_id, "owner_id": owner, "session_id": session})

    async def list(self, kind, owner, session, search_id=None):
        match = {"kind": kind, "owner_id": owner, "session_id": session}
        if search_id:
            match["payload.search_id"] = search_id
        return await self.collection.find(match).sort("created_at", -1).limit(100).to_list(length=100)

    async def mutate(self, row, payload, allowed):
        """User mutations invalidate the current lease/version in one operation."""
        return await self.collection.find_one_and_update(
            {"_id": row["_id"], "owner_id": row["owner_id"], "session_id": row["session_id"],
             "version": row["version"], "status": {"$in": list(allowed)}},
            {"$set": {"payload": payload, "status": payload["status"], "lease_until": 0},
             "$inc": {"version": 1}, "$unset": {"lease_token": ""}}, return_document=ReturnDocument.AFTER)

    async def claim(self, lease_seconds=90):
        now = time.time()
        row = await self.collection.find_one_and_update(
            {"kind": {"$in": ["search", "acquisition"]},
             "status": {"$in": ["queued", "searching", "downloading"]}, "lease_until": {"$lte": now}},
            {"$set": {"lease_token": uuid4().hex, "lease_until": now + lease_seconds}, "$inc": {"version": 1, "lease_generation": 1}},
            sort=[("created_at", 1)], return_document=ReturnDocument.AFTER)
        if row is None:
            return None
        # Claim changes status under the same fence before doing external work.
        payload = copy.deepcopy(row["payload"])
        payload["status"] = "searching" if row["kind"] == "search" else "downloading"
        if not await self.checkpoint(row, payload):
            return None
        return row

    @staticmethod
    def fence(row):
        return {"_id": row["_id"], "owner_id": row["owner_id"], "session_id": row["session_id"],
                "version": row["version"], "lease_token": row["lease_token"],
                "status": {"$in": ["queued", "searching", "downloading"]}}

    async def checkpoint(self, row, payload):
        result = await self.collection.update_one(self.fence(row), {"$set": {"payload": copy.deepcopy(payload), "status": payload["status"]}, "$inc": {"version": 1}})
        if result.matched_count:
            row["payload"], row["status"] = copy.deepcopy(payload), payload["status"]
            row["version"] += 1
        return bool(result.matched_count)

    async def renew(self, row, lease_seconds=90):
        fence = self.fence(row)
        fence.pop("version")
        result = await self.collection.update_one(fence, {"$set": {"lease_until": time.time() + lease_seconds}})
        return bool(result.matched_count)

    async def put_options(self, owner, session, search_id, candidate_id, options, *, verified_at=None, generation=0, proof_token=None):
        key = identity([owner, session, search_id, candidate_id])
        try:
            # A delayed writer from an expired task generation cannot replace
            # a newer proof. The upsert's unique _id turns a failed generation
            # match into DuplicateKeyError, including when no row existed at
            # the time the stale request began.
            await self.database["open_data_options"].replace_one(
                {"_id": key, "$or": [{"generation": {"$lte": generation}}, {"generation": {"$exists": False}}]},
                {"_id": key, "owner_id": owner, "session_id": session, "generation": generation,
                 "search_id": search_id, "candidate_id": candidate_id,
                 "proof_token": proof_token,
                 "verification_version": 1 if verified_at is not None else None,
                 "verified_at": verified_at, "options": options}, upsert=True)
            return True
        except DuplicateKeyError:
            return False

    async def get_verified_options(self, owner, session, search_id, candidate_ids):
        if not candidate_ids:
            return {}
        keys = [identity([owner, session, search_id, candidate_id]) for candidate_id in candidate_ids]
        rows = await self.database["open_data_options"].find(
            {"_id": {"$in": keys}, "owner_id": owner, "session_id": session,
             "search_id": search_id, "verification_version": 1}).to_list(length=len(keys))
        return {row["candidate_id"]: row for row in rows}

    async def get_options(self, owner, session, search_id, candidate_id):
        row = await self.database["open_data_options"].find_one(
            {"_id": identity([owner, session, search_id, candidate_id]), "owner_id": owner, "session_id": session})
        return row["options"] if row else []

    async def find_materialized_file(self, owner, item_key):
        """Recover a completed upload if a process died before its task checkpoint."""
        row = await self.database["stored_files"].find_one(
            {"user_id": owner, "metadata.open_data_item_key": item_key})
        if row and re.fullmatch(r"minio:[a-f0-9]{32}", str(row.get("file_id", ""))):
            return row["file_id"]
        row = await self.database["fs.files"].find_one(
            {"metadata.user_id": owner, "metadata.open_data_item_key": item_key})
        # Pure GridFS accepts bare ObjectId strings; hybrid selects GridFS for
        # the same shape. Dataset-reference IDs are never recovery candidates.
        file_id = str(row["_id"]) if row else ""
        return file_id if re.fullmatch(r"[a-f0-9]{24}", file_id) else None
