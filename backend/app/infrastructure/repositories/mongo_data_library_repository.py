"""Owner-scoped assets, selections, and atomic retain/delete claims.

The existing initialized Mongo connection owns these collections; no separate
database, credentials or TTL are used. Retention outlives the originating chat.
"""
import asyncio
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import wraps
from uuid import uuid4
from weakref import WeakValueDictionary

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.application.errors.exceptions import ConflictError
from app.domain.models.data_candidate import identity
from app.domain.models.data_library import canonical_file_id, library_folder_id, logical_file_path
from app.infrastructure.models.documents import SessionDocument
from app.infrastructure.repositories.mongo_data_candidate_repository import MongoDataCandidateRepository


class LibraryMutationState:
    """Shared metadata gate for the deployed single-process asyncio backend.

    All library writers, including acquisition tasks, currently run in the one
    uvicorn process. Multiple workers/replicas require a distributed replacement
    before deployment. No lock is held while validating or reading file bytes.
    """
    def __init__(self):
        self.lock = asyncio.Lock()
        self.task = None
        self.revision = 0
        self.removed_paths = {}

    @asynccontextmanager
    async def guard(self):
        task = asyncio.current_task()
        if self.task is task:
            yield
            return
        async with self.lock:
            self.task = task
            try:
                yield
            finally:
                self.task = None

    def removed(self, path: str):
        self.revision += 1
        self.removed_paths[path] = self.revision

    def validate_write(self, revision: int, paths: list[str]):
        if any(changed > revision and any(path.startswith(directory + "/") for path in paths)
               for directory, changed in self.removed_paths.items()):
            raise ConflictError("保存期间目标文件夹已被删除，请重新选择保存位置")


# Active requests retain their state, including uploads validating outside the
# gate. Idle owners are released, so deletion revisions never grow indefinitely.
_mutation_states = WeakValueDictionary()


def library_mutation_state(owner: str) -> LibraryMutationState:
    key = (id(asyncio.get_running_loop()), owner)
    state = _mutation_states.get(key)
    if state is None:
        state = LibraryMutationState()
        _mutation_states[key] = state
    return state


async def run_library_mutation(owner: str, operation):
    state = library_mutation_state(owner)
    if state.task is asyncio.current_task():
        return await operation()

    async def execute():
        async with state.guard():
            return await operation()

    # Cancellation cannot release the gate while an already-sent Mongo write
    # may still finish. The worker owns the gate, so nested metadata operations
    # stay in that same task instead of deadlocking on a shield-created child.
    pending = asyncio.create_task(execute())
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


def library_mutation(method):
    @wraps(method)
    async def guarded(self, owner, *args, **kwargs):
        return await run_library_mutation(owner, lambda: method(self, owner, *args, **kwargs))
    return guarded


class MongoDataLibraryRepository:
    def __init__(self, database=None):
        self._database = database

    @property
    def database(self):
        return self._database if self._database is not None else SessionDocument.get_pymongo_collection().database

    @property
    def collection(self):
        return self.database["data_library"]

    @property
    def folders(self):
        return self.database["data_library_folders"]

    @staticmethod
    def prefix(owner: str) -> str:
        return identity(["data_library", owner]) + ":"

    @staticmethod
    def folder_prefix(owner: str) -> str:
        return identity(["data_library_folders", owner]) + ":"

    @staticmethod
    def _owner_query(owner: str, prefix: str) -> dict:
        return {"owner_id": owner, "_id": {"$gte": prefix, "$lt": prefix[:-1] + ";"}}

    async def _saved_paths(self, owner: str):
        """Read the whole owner's saved metadata, independently of UI paging."""
        cursor = self.collection.find(self._owner_query(owner, self.prefix(owner)),
                                      {"payload.files": 1, "payload.created_at": 1, "payload.updated_at": 1})
        async for row in cursor:
            payload = row.get("payload", {})
            for file in payload.get("files", []):
                if not isinstance(file, dict) or not file.get("file_id"):
                    continue
                # Legacy filenames never create directories. Only the explicit,
                # validated relative_path may define a logical folder hierarchy.
                filename = str(file.get("filename", "")).replace("\\", "/").rsplit("/", 1)[-1]
                raw_path = file.get("relative_path")
                try:
                    path = logical_file_path(raw_path) if raw_path is not None else logical_file_path(filename)
                except ValueError:
                    continue
                yield path, payload

    @library_mutation
    async def list_folders(self, owner: str) -> list[dict]:
        """Lazily persist legacy logical directories, including ancestors."""
        discovered = {}
        now = datetime.now(UTC)
        async for path, asset in self._saved_paths(owner):
            parts = path.split("/")
            for depth in range(1, len(parts)):
                directory = "/".join(parts[:depth])
                if directory in discovered:
                    continue
                parent = "/".join(parts[:depth - 1])
                discovered[directory] = {
                    "id": library_folder_id(owner, directory),
                    "parent_id": library_folder_id(owner, parent) if parent else None,
                    "name": parts[depth - 1], "path": directory,
                    "generation": uuid4().hex,
                    "created_at": asset.get("created_at") if isinstance(asset.get("created_at"), datetime) else now,
                    "updated_at": asset.get("updated_at") if isinstance(asset.get("updated_at"), datetime) else now,
                }
        for directory, payload in sorted(discovered.items(), key=lambda item: (item[0].count("/"), item[0])):
            try:
                await self.folders.update_one(
                    {"_id": self.folder_prefix(owner) + payload["id"], "owner_id": owner},
                    {"$setOnInsert": {"owner_id": owner, "payload": payload}}, upsert=True,
                )
            except DuplicateKeyError:
                # A concurrent list/create may have persisted the same path.
                # Ownership is still checked on the subsequent read.
                pass
        rows = await self.folders.find(self._owner_query(owner, self.folder_prefix(owner))).sort("payload.path", 1).to_list(length=None)
        for row in rows:
            await self._ensure_folder_generation(owner, row)
        return [row["payload"] for row in rows]

    async def _ensure_folder_generation(self, owner: str, row: dict):
        if not row["payload"].get("generation"):
            generation = uuid4().hex
            await self.folders.update_one(
                {"_id": row["_id"], "owner_id": owner}, {"$set": {"payload.generation": generation}},
            )
            row["payload"]["generation"] = generation

    @library_mutation
    async def get_folder(self, owner: str, folder_id: str) -> dict | None:
        row = await self.folders.find_one({"_id": self.folder_prefix(owner) + folder_id, "owner_id": owner})
        if row:
            await self._ensure_folder_generation(owner, row)
        return row["payload"] if row else None

    @library_mutation
    async def insert_folder(self, owner: str, payload: dict) -> dict:
        # Deterministic owner/path IDs make Mongo's _id unique index the atomic
        # same-parent/name constraint, without races in a read-then-insert check.
        await self.folders.insert_one({"_id": self.folder_prefix(owner) + payload["id"], "owner_id": owner, "payload": payload})
        return payload

    async def file_path_exists(self, owner: str, path: str) -> bool:
        async for existing, _asset in self._saved_paths(owner):
            if existing == path:
                return True
        return False

    @library_mutation
    async def insert(self, owner: str, payload: dict) -> dict:
        await self.collection.insert_one({"_id": self.prefix(owner) + payload["id"], "owner_id": owner, "payload": payload})
        return payload

    @library_mutation
    async def merge_acquisition(self, owner: str, payload: dict) -> dict:
        """Idempotent additive save: retries preserve names/tags and ready files."""
        row = await self.collection.find_one_and_update(
            {"_id": self.prefix(owner) + payload["id"], "owner_id": owner},
            [{"$set": {"owner_id": {"$literal": owner},
                       "payload": {"$ifNull": ["$payload", {"$literal": payload}]}}},
             {"$set": {"payload.files": {"$setUnion": ["$payload.files", {"$literal": payload["files"]}]},
                       "payload.source_url": {"$cond": [
                           {"$eq": ["$payload.source_url", {"$literal": payload["source_url"]}]},
                           "$payload.source_url", None]},
                       "payload.updated_at": payload["updated_at"]}}],
            upsert=True, return_document=ReturnDocument.AFTER)
        return row["payload"]

    async def get(self, owner: str, asset_id: str) -> dict | None:
        row = await self.collection.find_one({"_id": self.prefix(owner) + asset_id, "owner_id": owner})
        return row["payload"] if row else None

    @staticmethod
    def _format_expression(filename):
        # Keep compound scientific/archive extensions together. A lone leading
        # dot (e.g. .env) or a trailing dot does not count as an extension.
        return {"$let": {
            "vars": {"suffix": {"$regexFind": {
                "input": {"$toLower": {"$ifNull": [filename, ""]}},
                "regex": r"^.+?\.(nii\.gz|tar\.gz|[^.]+)$",
            }}},
            "in": {"$arrayElemAt": [{"$ifNull": ["$$suffix.captures", ["none"]]}, 0]},
        }}

    async def page(
        self, owner: str, *, cursor=None, query="", source_type=None,
        filename=None, file_format=None, updated_from=None, updated_to=None, folder_path=None,
    ) -> list[dict]:
        prefix = self.prefix(owner)
        match = {"_id": {"$gte": prefix, "$lt": prefix + cursor if cursor else prefix[:-1] + ";"}, "owner_id": owner}
        if source_type:
            match["payload.source_type"] = source_type
        if query:
            pattern = {"$regex": re.escape(query), "$options": "i"}
            match["$or"] = [{"payload." + field: pattern} for field in ("name", "description", "tags")]
        if updated_from is not None or updated_to is not None:
            match["payload.updated_at"] = {
                **({"$gte": updated_from} if updated_from is not None else {}),
                **({"$lt": updated_to} if updated_to is not None else {}),
            }
        file_conditions = []
        if filename:
            file_conditions.append({"$regexMatch": {
                "input": {"$ifNull": ["$$file.filename", ""]},
                "regex": re.escape(filename), "options": "i",
            }})
        if file_format:
            file_conditions.append({"$eq": [
                self._format_expression("$$file.filename"), {"$literal": file_format},
            ]})
        if folder_path is not None:
            relative = {"$ifNull": ["$$file.relative_path", ""]}
            if folder_path:
                file_conditions.append({"$regexMatch": {"input": relative, "regex": "^" + re.escape(folder_path) + r"/[^/]+$"}})
            else:
                file_conditions.append({"$not": [{"$regexMatch": {"input": relative, "regex": "/"}}]})
        if file_conditions:
            # All file filters must match one file. Apply before the cursor
            # limit so matches after an unfiltered page are still reachable.
            match["$expr"] = {"$anyElementTrue": [{"$map": {
                "input": {"$ifNull": ["$payload.files", []]}, "as": "file",
                "in": {"$and": file_conditions},
            }}]}
        rows = await self.collection.find(match).sort("_id", -1).limit(21).to_list(length=21)
        return [row["payload"] for row in rows]

    async def stats(self, owner: str) -> dict:
        prefix = self.prefix(owner)
        canonical_id = {"$let": {
            "vars": {"id": {"$ifNull": ["$payload.files.file_id", ""]}},
            "in": {"$cond": [
                {"$eq": [{"$substrCP": ["$$id", 0, 7]}, "gridfs:"]},
                {"$substrCP": ["$$id", 7, {"$subtract": [{"$strLenCP": "$$id"}, 7]}]},
                "$$id",
            ]},
        }}
        cursor = await self.collection.aggregate([
            {"$match": {"owner_id": owner, "_id": {"$gte": prefix, "$lt": prefix[:-1] + ";"}}},
            # Deterministically use the newest saved projection if legacy
            # duplicate records disagree. Only safe metadata enters the groups.
            {"$sort": {"payload.updated_at": -1, "_id": -1}},
            {"$unwind": {"path": "$payload.files", "preserveNullAndEmptyArrays": True}},
            {"$group": {
                "_id": canonical_id,
                "size": {"$first": {"$ifNull": ["$payload.files.size", 0]}},
                "format": {"$first": self._format_expression("$payload.files.filename")},
                "updated_at": {"$max": "$payload.updated_at"},
            }},
            {"$group": {
                "_id": None,
                "file_count": {"$sum": {"$cond": [{"$ne": ["$_id", ""]}, 1, 0]}},
                "total_bytes": {"$sum": {"$cond": [{"$ne": ["$_id", ""]}, "$size", 0]}},
                "formats": {"$addToSet": {"$cond": [{"$ne": ["$_id", ""]}, "$format", None]}},
                "updated_at": {"$max": "$updated_at"},
            }},
            {"$project": {"_id": 0, "file_count": 1, "total_bytes": 1, "updated_at": 1, "formats": 1}},
        ], allowDiskUse=True)
        rows = await cursor.to_list(length=1)
        if not rows:
            return {"file_count": 0, "total_bytes": 0, "updated_at": None, "formats": []}
        row = rows[0]
        row["formats"] = sorted(value for value in row["formats"] if value is not None)
        return row

    @library_mutation
    async def update(self, owner: str, asset_id: str, changes: dict) -> dict | None:
        from pymongo import ReturnDocument
        row = await self.collection.find_one_and_update(
            {"_id": self.prefix(owner) + asset_id, "owner_id": owner},
            {"$set": {"payload." + key: value for key, value in changes.items()}},
            return_document=ReturnDocument.AFTER,
        )
        return row["payload"] if row else None

    @library_mutation
    async def delete(self, owner: str, asset_id: str) -> dict | None:
        row = await self.collection.find_one_and_delete({"_id": self.prefix(owner) + asset_id, "owner_id": owner})
        return row["payload"] if row else None

    @library_mutation
    async def remove_file(self, owner: str, asset_id: str, file_id: str) -> bool:
        """Atomically unlink exactly one owned file, preserving concurrent saves."""
        file_id = canonical_file_id(file_id)
        aliases = [file_id, "gridfs:" + file_id]
        owned = {"_id": self.prefix(owner) + asset_id, "owner_id": owner}
        previous = await self.collection.find_one_and_update(
            {**owned, "payload.files.file_id": {"$in": aliases}},
            {"$pull": {"payload.files": {"file_id": {"$in": aliases}}},
             "$set": {"payload.updated_at": datetime.now(UTC)}},
            projection={"_id": 1},
            return_document=ReturnDocument.BEFORE,
        )
        # Do not delete from a stale snapshot: an acquisition may have appended
        # another file after the pull. Retrying also cleans an empty row left by
        # an interrupted previous request without touching any existing file.
        await self.collection.delete_one({**owned, "payload.files": {"$size": 0}})
        return previous is not None

    def _folder_subtree_query(self, owner: str, path: str) -> dict:
        return {**self._owner_query(owner, self.folder_prefix(owner)),
                "payload.path": {"$regex": "^" + re.escape(path) + r"(?:/|$)"}}

    async def _folder_files(self, owner: str, path: str):
        query = {**self._owner_query(owner, self.prefix(owner)),
                 "payload.files.relative_path": {"$regex": "^" + re.escape(path) + "/"}}
        async for row in self.collection.find(query, {"payload.files": 1}):
            files = []
            for file in row.get("payload", {}).get("files", []):
                try:
                    relative_path = logical_file_path(file.get("relative_path"))
                except (AttributeError, ValueError):
                    continue
                if relative_path.startswith(path + "/"):
                    files.append(file)
            if files:
                yield row["_id"], files

    async def folder_subtree_counts(self, owner: str, path: str) -> dict:
        files = 0
        async for _asset_id, entries in self._folder_files(owner, path):
            files += len(entries)
        folders = await self.folders.count_documents(self._folder_subtree_query(owner, path))
        return {"file_count": files, "folder_count": folders}

    async def get_folder_deletion(self, owner: str, folder_id: str, token: str) -> dict | None:
        return await self.database["data_library_folder_deletions"].find_one({
            "_id": identity(["library_folder_delete", owner, folder_id, token]), "owner_id": owner,
        })

    async def record_folder_deletion(self, owner: str, folder: dict, token: str, *, completed: bool):
        await self.database["data_library_folder_deletions"].update_one(
            {"_id": identity(["library_folder_delete", owner, folder["id"], token]), "owner_id": owner},
            {"$set": {"folder": folder, "completed": completed}}, upsert=True,
        )

    async def remove_folder_contents(self, owner: str, folder: dict, progress: dict):
        """Called under the owner metadata gate; retain every object claim."""
        path = folder["path"]
        async for document_id, files in self._folder_files(owner, path):
            paths = list({file["relative_path"] for file in files})
            result = await self.collection.update_one(
                {"_id": document_id, "owner_id": owner},
                {"$pull": {"payload.files": {"relative_path": {"$in": paths}}},
                 "$set": {"payload.updated_at": datetime.now(UTC)}},
            )
            if result.modified_count:
                progress["deleted_file_count"] += len(files)
            await self.collection.delete_one({"_id": document_id, "owner_id": owner, "payload.files": {"$size": 0}})
        # Keep the root until all references and descendants are gone. If any
        # write fails, the root and its receipt still identify the retry scope.
        counts = await self.folder_subtree_counts(owner, path)
        if counts["file_count"]:
            raise ConflictError("目录内容仍在变化，请重试删除")
        descendants = {**self._folder_subtree_query(owner, path), "payload.id": {"$ne": folder["id"]}}
        result = await self.folders.delete_many(descendants)
        progress["deleted_folder_count"] += result.deleted_count
        result = await self.folders.delete_one({
            "_id": self.folder_prefix(owner) + folder["id"], "owner_id": owner,
            "payload.created_at": folder["created_at"],
        })
        progress["deleted_folder_count"] += result.deleted_count

    @staticmethod
    def retention_key(owner: str, file_id: str) -> str:
        return identity(["library_file", owner, canonical_file_id(file_id)])

    async def retain_file(self, owner: str, file_id: str, asset_id: str) -> bool:
        """Reserve before validating bytes. A concurrent deletion cannot win too."""
        try:
            await self.database["data_library_retention"].update_one(
                {"_id": self.retention_key(owner, file_id), "deleting": {"$ne": True}},
                {"$addToSet": {"asset_ids": asset_id}}, upsert=True,
            )
            return True
        except DuplicateKeyError:
            return False

    async def release_file(self, owner: str, file_id: str, asset_id: str) -> None:
        await self.database["data_library_retention"].update_one(
            {"_id": self.retention_key(owner, file_id)}, {"$pull": {"asset_ids": asset_id}},
        )

    async def begin_file_delete(self, owner: str, file_id: str) -> bool:
        try:
            await self.database["data_library_retention"].update_one(
                {"_id": self.retention_key(owner, file_id), "deleting": {"$ne": True},
                 "$or": [{"asset_ids": {"$exists": False}}, {"asset_ids": {"$size": 0}}]},
                {"$set": {"deleting": True}}, upsert=True,
            )
            return True
        except DuplicateKeyError:
            return False

    async def finish_file_delete(self, owner: str, file_id: str, *, deleted: bool) -> None:
        if not deleted:
            await self.database["data_library_retention"].update_one(
                {"_id": self.retention_key(owner, file_id)}, {"$unset": {"deleting": ""}},
            )
        # A successful deletion keeps a tombstone. Object IDs are immutable;
        # subsequent saves must never retain an object that has disappeared.

    async def acquisitions(self, owner: str, file_ids: list[str]) -> dict[str, dict]:
        result = {}
        rows = self.database["data_provenance"].find({"owner_id": owner, "payload.file_id": {"$in": file_ids}})
        async for row in rows:
            prefix = MongoDataCandidateRepository.prefix(owner, row.get("session_id", ""), "acquired_package")
            # User-created candidate/version records are not trusted receipts.
            if str(row.get("_id", "")).startswith(prefix):
                result[row["payload"]["file_id"]] = row["payload"]
        return result

    async def get_context(self, owner: str, session_id: str) -> list[str]:
        row = await self.database["data_library_contexts"].find_one({"_id": identity([owner, session_id]), "owner_id": owner})
        return row["file_ids"] if row else []

    async def file_locations(self, owner: str, file_ids: list[str]) -> dict[str, list[dict]]:
        """All saved logical locations, independent of the library UI's pages."""
        wanted = {canonical_file_id(file_id) for file_id in file_ids}
        if not wanted:
            return {}
        query = self._owner_query(owner, self.prefix(owner))
        query["payload.files.file_id"] = {"$in": [alias for key in wanted for alias in (key, "gridfs:" + key)]}
        rows = self.collection.find(query, {"payload.id": 1, "payload.name": 1, "payload.files": 1})
        result, seen = {}, set()
        async for row in rows:
            payload = row.get("payload", {})
            asset_id = payload.get("id", "")
            if not re.fullmatch(r"[a-f0-9]{32}", asset_id):
                continue
            for file in payload.get("files", []):
                key = canonical_file_id(str(file.get("file_id", "")))
                if key not in wanted:
                    continue
                filename = str(file.get("filename", "")).replace("\\", "/").rsplit("/", 1)[-1]
                try:
                    path = logical_file_path(file.get("relative_path") or filename)
                except ValueError:
                    continue
                if (key, asset_id, path) in seen:
                    continue
                seen.add((key, asset_id, path))
                parent = path.rsplit("/", 1)[0] if "/" in path else ""
                # Asset names are editable titles, never storage locations.
                from app.interfaces.schemas.file import _redact_absolute_paths
                title = _redact_absolute_paths(str(payload.get("name", "")))
                result.setdefault(key, []).append({"asset_id": asset_id, "asset_name": title,
                    "folder_id": library_folder_id(owner, parent) if parent else None,
                    "folder_path": parent, "file_path": path})
        return result

    async def set_context(self, owner: str, session_id: str, file_ids: list[str]) -> None:
        await self.database["data_library_contexts"].replace_one(
            {"_id": identity([owner, session_id]), "owner_id": owner},
            {"_id": identity([owner, session_id]), "owner_id": owner, "session_id": session_id, "file_ids": file_ids},
            upsert=True,
        )
