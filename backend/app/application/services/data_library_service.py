"""Save owned, materialized files independently of the lifetime of a chat."""
import asyncio
import logging
import re
import time
from datetime import UTC, datetime
from uuid import uuid4
from pymongo.errors import DuplicateKeyError

from app.application.errors.exceptions import BadRequestError, ConflictError, NotFoundError
from app.domain.models.data_candidate import CandidateInput, identity
from app.domain.models.data_library import (
    CreateLibraryAsset, CreateLibraryFolder, DataLibraryAsset, DataLibraryFolder, DataLibraryFolders,
    DataLibraryPage, DataLibraryStats, DeleteLibraryFiles, LibraryFile, LibraryFileDeleteResult,
    LibraryFilesDeleteResponse, LibraryFolderDeleteResponse, LibraryFolderDeletionPreview,
    SelectedDataInput, SelectedDataResponse,
    UpdateLibraryAsset, canonical_file_id, library_folder_id, logical_directory_path, logical_file_path,
)
from app.infrastructure.repositories.mongo_data_library_repository import library_mutation, library_mutation_state, run_library_mutation
from app.interfaces.schemas.file import public_filename

logger = logging.getLogger(__name__)


class DataLibraryService:
    def __init__(self, repository, files, sessions):
        self.repository, self.files, self.sessions = repository, files, sessions
        self._reads = asyncio.Semaphore(16)

    async def owned_session(self, owner: str, session_id: str):
        session = await self.sessions.find_owned_by_id_and_user_id(session_id, owner)
        if session is None:
            raise NotFoundError("探索不存在或不可访问")
        return session

    async def _info(self, owner: str, file_id: str, *, validate_bytes=False):
        async with self._reads:
            try:
                info = await self.files.get_file_info(file_id, owner)
                if (info is None or info.user_id != owner or not info.file_id
                        or canonical_file_id(info.file_id) != canonical_file_id(file_id)
                        or info.size is None or info.size < 0
                        or (info.metadata or {}).get("source") == "tool_output_spill"):
                    raise NotFoundError("文件不存在或不可访问")
                if validate_bytes:
                    # Completed uploads have immutable object IDs. An owned tail
                    # read verifies the object exists and matches its recorded
                    # size without reading a multi-gigabyte file into memory.
                    length = min(info.size, 4096)
                    if info.size == 0:
                        # Ranged providers can answer a zero-length request from
                        # metadata alone; open the object to verify empty files.
                        stream, current = await self.files.download_file(file_id, owner)
                        try:
                            data = await asyncio.to_thread(stream.read, 1)
                        finally:
                            try:
                                stream.close()
                            finally:
                                release = getattr(stream, "release_conn", None)
                                if callable(release):
                                    release()
                    else:
                        data, current = await self.files.download_file_range(
                            file_id, owner, offset=info.size - length, length=length,
                        )
                    if (current.user_id != owner or current.size != info.size
                            or canonical_file_id(current.file_id or "") != canonical_file_id(file_id)
                            or len(data) != length
                            or (current.metadata or {}).get("source") == "tool_output_spill"):
                        raise NotFoundError("文件尚未完整保存，请重新上传或获取")
                return info
            except (FileNotFoundError, PermissionError, ValueError):
                raise NotFoundError("文件不存在或不可访问") from None

    @staticmethod
    def _public_file(info) -> LibraryFile:
        return LibraryFile(
            file_id=canonical_file_id(info.file_id), filename=public_filename(info.filename),
            content_type=info.content_type, size=info.size, upload_date=info.upload_date,
        )

    async def _asset(self, owner: str, payload: dict) -> DataLibraryAsset:
        asset = DataLibraryAsset.model_validate(payload)
        # Metadata is safe and immutable even if an external storage operator
        # removed the underlying file record.
        async def exists(file):
            try:
                info = await self._info(owner, file.file_id)
                return info.size == file.size
            except NotFoundError:
                return False
        if not all(await asyncio.gather(*(exists(file) for file in asset.files))):
            asset.status = "missing"
        return asset

    async def list(
        self, owner: str, *, cursor=None, query="", source_type=None,
        filename=None, file_format=None, updated_from=None, updated_to=None, folder_id=None,
    ) -> DataLibraryPage:
        filters = {}
        if filename:
            filters["filename"] = filename
        if file_format:
            filters["file_format"] = file_format.strip().lower()
        for name, value in (("updated_from", updated_from), ("updated_to", updated_to)):
            if value is not None:
                if value.tzinfo is None or value.utcoffset() is None:
                    raise BadRequestError("更新时间必须包含时区")
                filters[name] = value.astimezone(UTC)
        if updated_from is not None and updated_to is not None and updated_from >= updated_to:
            raise BadRequestError("开始时间必须早于结束时间")
        if folder_id is not None:
            if folder_id == "root":
                filters["folder_path"] = ""
            else:
                filters["folder_path"] = (await self.owned_folder(owner, folder_id)).path
        rows = await self.repository.page(
            owner, cursor=cursor, query=query.strip(), source_type=source_type, **filters,
        )
        return DataLibraryPage(
            items=await asyncio.gather(*(self._asset(owner, row) for row in rows[:20])),
            next_cursor=rows[19]["id"] if len(rows) > 20 else None,
        )

    async def stats(self, owner: str) -> DataLibraryStats:
        # Saved metadata defines this overview. Do not re-read storage for every
        # file: the list separately reports any missing underlying objects.
        return DataLibraryStats.model_validate(await self.repository.stats(owner))

    @library_mutation
    async def list_folders(self, owner: str) -> DataLibraryFolders:
        return DataLibraryFolders(items=await self.repository.list_folders(owner))

    @library_mutation
    async def owned_folder(self, owner: str, folder_id: str) -> DataLibraryFolder:
        if not re.fullmatch(r"[a-f0-9]{32}", folder_id):
            raise NotFoundError("文件夹不存在或不可访问")
        folder = await self.repository.get_folder(owner, folder_id)
        if folder is None:
            # Legacy directory IDs may have been obtained before a process
            # restart; ensure their safe metadata projection exists first.
            await self.repository.list_folders(owner)
            folder = await self.repository.get_folder(owner, folder_id)
        if folder is None:
            raise NotFoundError("文件夹不存在或不可访问")
        return DataLibraryFolder.model_validate(folder)

    @library_mutation
    async def create_folder(self, owner: str, request: CreateLibraryFolder) -> DataLibraryFolder:
        # Backfill all legacy paths so existing implicit folders are conflicts
        # too, rather than silently turning creation into a duplicate success.
        await self.repository.list_folders(owner)
        parent = await self.owned_folder(owner, request.parent_id) if request.parent_id else None
        try:
            path = logical_directory_path(f"{parent.path}/{request.name}" if parent else request.name)
        except ValueError as error:
            raise BadRequestError(str(error)) from None
        if await self.repository.file_path_exists(owner, path):
            raise ConflictError("此位置已有同名文件，请使用其他文件夹名称")
        now = datetime.now(UTC)
        folder = DataLibraryFolder(
            id=library_folder_id(owner, path), parent_id=parent.id if parent else None,
            name=request.name, path=path, created_at=now, updated_at=now, generation=uuid4().hex,
        )
        try:
            await self.repository.insert_folder(owner, {**folder.model_dump(), "generation": folder.generation})
        except DuplicateKeyError:
            raise ConflictError("此目录下已有同名文件夹") from None
        return folder

    @staticmethod
    def _folder_deletion_token(owner: str, folder: DataLibraryFolder) -> str:
        # Mongo persists milliseconds; use the persisted precision even when a
        # just-created model still holds Python's finer microsecond timestamp.
        return identity(["library_folder_delete", owner, folder.id,
                         int(folder.created_at.timestamp() * 1000), folder.generation])

    @library_mutation
    async def folder_deletion_preview(self, owner: str, folder_id: str) -> LibraryFolderDeletionPreview:
        await self.repository.list_folders(owner)
        folder = await self.owned_folder(owner, folder_id)
        counts = await self.repository.folder_subtree_counts(owner, folder.path)
        return LibraryFolderDeletionPreview(
            folder=folder, deletion_token=self._folder_deletion_token(owner, folder),
            file_count=counts["file_count"], subfolder_count=max(0, counts["folder_count"] - 1),
        )

    @library_mutation
    async def delete_folder(self, owner: str, folder_id: str, deletion_token: str) -> LibraryFolderDeleteResponse:
        if not re.fullmatch(r"[a-f0-9]{32}", folder_id):
            raise NotFoundError("文件夹不存在或不可访问")
        receipt = await self.repository.get_folder_deletion(owner, folder_id, deletion_token)
        if receipt and receipt.get("completed"):
            # An old request must not delete a newly created same-name folder,
            # whose deterministic folder ID is identical but token is different.
            return LibraryFolderDeleteResponse(
                folder_id=folder_id, parent_id=receipt["folder"].get("parent_id"), completed=True,
                remaining_file_count=0, remaining_folder_count=0,
            )
        await self.repository.list_folders(owner)
        row = await self.repository.get_folder(owner, folder_id)
        if row is None and not receipt:
            raise NotFoundError("文件夹不存在或不可访问")
        folder = DataLibraryFolder.model_validate(row or receipt["folder"])
        if self._folder_deletion_token(owner, folder) != deletion_token:
            raise ConflictError("文件夹已变更，请重新确认删除")
        folder_payload = {**folder.model_dump(), "generation": folder.generation}
        progress = {"deleted_file_count": 0, "deleted_folder_count": 0}
        await self.repository.record_folder_deletion(owner, folder_payload, deletion_token, completed=False)
        library_mutation_state(owner).removed(folder.path)
        try:
            await self.repository.remove_folder_contents(owner, folder_payload, progress)
            remaining = await self.repository.folder_subtree_counts(owner, folder.path)
            completed = not remaining["file_count"] and not remaining["folder_count"]
            if completed:
                await self.repository.record_folder_deletion(owner, folder_payload, deletion_token, completed=True)
            return LibraryFolderDeleteResponse(
                folder_id=folder.id, parent_id=folder.parent_id, completed=completed, **progress,
                remaining_file_count=remaining["file_count"], remaining_folder_count=remaining["folder_count"],
                message=None if completed else "目录尚未完全删除，请重试",
            )
        except Exception as error:
            logger.warning("Removing library folder failed (%s)", type(error).__name__)
            try:
                remaining = await self.repository.folder_subtree_counts(owner, folder.path)
            except Exception:
                remaining = {"file_count": None, "folder_count": None}
            return LibraryFolderDeleteResponse(
                folder_id=folder.id, parent_id=folder.parent_id, completed=False, **progress,
                remaining_file_count=remaining["file_count"], remaining_folder_count=remaining["folder_count"],
                message="目录未能全部删除，请稍后重试",
            )

    async def get(self, owner: str, asset_id: str) -> DataLibraryAsset:
        row = await self.repository.get(owner, asset_id)
        if row is None:
            raise NotFoundError("数据条目不存在")
        return await self._asset(owner, row)

    async def create(self, owner: str, request: CreateLibraryAsset, *, acquisition_id: str | None = None) -> DataLibraryAsset:
        mutation = library_mutation_state(owner)
        revision = mutation.revision
        # Internal acquisition workers use a stable asset ID. Public requests
        # cannot supply it; a retry adds validated files to the same saved asset.
        if acquisition_id is not None and not re.fullmatch(r"[a-f0-9]{32}", acquisition_id):
            raise BadRequestError("获取任务标识无效")
        explicit_target = "target_folder_id" in request.model_fields_set
        target = await self.owned_folder(owner, request.target_folder_id) if request.target_folder_id else None
        if explicit_target:
            await self.repository.list_folders(owner)
        session = await self.owned_session(owner, request.session_id) if request.session_id else None
        session_files = {canonical_file_id(file.file_id) for file in session.files if file.file_id} if session else set()
        if session is not None and not set(request.file_ids).issubset(session_files):
            # A newer output at the same sandbox path replaces session.files,
            # but published versions remain immutable attachments in history.
            # Keep those versions saveable without trusting a client file list.
            from app.domain.models.event import MessageEvent
            for event in await self.sessions.get_events(request.session_id):
                if isinstance(event, MessageEvent) and event.role == "assistant":
                    session_files.update(canonical_file_id(file.file_id)
                                         for file in event.attachments or [] if file.file_id)
        if request.source_type == "analysis":
            if session is None:
                raise BadRequestError("保存分析成果时请选择来源探索")
            if not set(request.file_ids).issubset(session_files):
                raise BadRequestError("分析成果必须是来源探索中的文件")
        # Timestamp prefix gives deterministic newest-first keyset pagination.
        asset_id = acquisition_id or f"{int(time.time() * 1000):012x}{uuid4().hex[:20]}"
        retained = []
        inserting = False
        try:
            for file_id in request.file_ids:
                if not await self.repository.retain_file(owner, file_id, asset_id):
                    raise NotFoundError("文件正在删除或已删除，请重新选择")
                retained.append(file_id)
            # Saving a large acquisition must not open one storage reader per
            # file at once. Keep validation ordered and resource usage bounded.
            infos = []
            for start in range(0, len(request.file_ids), 4):
                infos.extend(await asyncio.gather(*(self._info(owner, file_id, validate_bytes=True)
                                                   for file_id in request.file_ids[start:start + 4])))
            receipts = await self.repository.acquisitions(owner, request.file_ids)
            verified = []
            public_files = []
            for info in infos:
                public_file = self._public_file(info)
                logical_path = request.file_paths.get(public_file.file_id)
                if logical_path is not None:
                    if logical_path.rsplit("/", 1)[-1] != public_file.filename:
                        raise BadRequestError("文件目录中的名称必须与原文件名一致")
                if explicit_target:
                    try:
                        relative = logical_path or public_file.filename
                        logical_path = logical_file_path(f"{target.path}/{relative}" if target else relative)
                    except ValueError as error:
                        raise BadRequestError(str(error)) from None
                    if len(logical_path.split("/")) <= 15:
                        folder_at_file_path = await self.repository.get_folder(owner, library_folder_id(owner, logical_path))
                        if folder_at_file_path is not None:
                            raise ConflictError("此位置已有同名文件夹，请选择其他保存位置")
                if logical_path is not None:
                    public_file.relative_path = logical_path
                receipt = receipts.get(canonical_file_id(info.file_id))
                if (receipt and receipt.get("size") == info.size
                        and re.fullmatch(r"[a-f0-9]{64}", str(receipt.get("sha256", "")))
                        and receipt["sha256"] == (info.metadata or {}).get("sha256")):
                    try:
                        source_url = CandidateInput.public_source(receipt["source_url"])
                        verified.append(source_url)
                        public_file.source_url = source_url
                        public_file.sha256 = receipt["sha256"]
                    except (KeyError, ValueError):
                        pass
                public_files.append(public_file)
            if explicit_target:
                paths = [file.relative_path for file in public_files]
                if len(paths) != len(set(paths)):
                    raise ConflictError("同一文件夹中不能保存同名文件")
                path_set = set(paths)
                for path in paths:
                    if await self.repository.file_path_exists(owner, path):
                        raise ConflictError("此文件夹中已有同名文件，请选择其他保存位置")
                    parts = path.split("/")
                    for depth in range(1, len(parts)):
                        prefix = "/".join(parts[:depth])
                        if prefix in path_set or await self.repository.file_path_exists(owner, prefix):
                            raise ConflictError("目标路径中有同名文件，不能作为文件夹使用")
            source_verified = len(verified) == len(infos)
            inferred_analysis = (
                session is not None and set(request.file_ids).issubset(session_files)
                and all((info.metadata or {}).get("source") == "sandbox_artifact"
                        and (info.metadata or {}).get("session_id") == request.session_id for info in infos)
            )
            # A legacy/default upload label must not erase authoritative source
            # evidence when saving a downloaded file from a generic file panel.
            source_type = ("open_data" if source_verified else "analysis" if inferred_analysis
                           else (request.source_type or "upload"))
            if source_type == "open_data" and not source_verified:
                raise BadRequestError("开放数据需要先通过数据源完成获取，再保存实际文件")
            now = datetime.now(UTC)
            asset = DataLibraryAsset(
                id=asset_id, name=request.name, description=request.description, tags=request.tags,
                source_type=source_type, source_verified=source_verified if source_type == "open_data" else False,
                source_url=verified[0] if source_type == "open_data" and len(set(verified)) == 1 else None,
                session_id=request.session_id, files=public_files,
                created_at=now, updated_at=now,
            )
            async def commit():
                nonlocal inserting
                mutation.validate_write(revision, [file.relative_path or file.filename for file in public_files])
                if target is not None:
                    current = await self.repository.get_folder(owner, target.id)
                    if current is None or self._folder_deletion_token(owner, DataLibraryFolder.model_validate(current)) != self._folder_deletion_token(owner, target):
                        raise ConflictError("保存期间目标文件夹已被删除，请重新选择保存位置")
                inserting = True
                if acquisition_id is not None:
                    if not source_verified:
                        raise BadRequestError("获取任务只能保存已验证来源的文件")
                    saved = await self.repository.merge_acquisition(owner, asset.model_dump())
                    return DataLibraryAsset.model_validate(saved)
                await self.repository.insert(owner, asset.model_dump())
                return asset
            return await run_library_mutation(owner, commit)
        except BaseException:
            # Failed validation releases reservations. An uncertain insert may
            # have committed: retaining is safer than leaving a saved asset
            # unprotected during a database/network failure.
            if not inserting:
                for file_id in retained:
                    await self.repository.release_file(owner, file_id, asset_id)
            raise

    async def update(self, owner: str, asset_id: str, request: UpdateLibraryAsset) -> DataLibraryAsset:
        changes = request.model_dump(exclude_unset=True)
        changes["updated_at"] = datetime.now(UTC)
        row = await self.repository.update(owner, asset_id, changes)
        if row is None:
            raise NotFoundError("数据条目不存在")
        return await self._asset(owner, row)

    async def delete(self, owner: str, asset_id: str) -> None:
        row = await self.repository.delete(owner, asset_id)
        if row is None:
            raise NotFoundError("数据条目不存在")
        # Removing a library entry does not delete its files or other references.
        for file in row["files"]:
            await self.repository.release_file(owner, file["file_id"], asset_id)

    async def delete_files(self, owner: str, request: DeleteLibraryFiles) -> LibraryFilesDeleteResponse:
        # A file may appear in several saved assets. Its immutable storage ID
        # alone is not sufficient to identify the library reference to remove.
        selected = {(file.asset_id, file.file_id): file for file in request.files}
        results = []
        try:
            # Persist legacy implicit directories before their last file is
            # removed, so file deletion never also deletes a user's folder.
            await self.repository.list_folders(owner)
        except Exception as error:
            logger.warning("Preparing library file deletion failed (%s)", type(error).__name__)
            return LibraryFilesDeleteResponse(
                results=[LibraryFileDeleteResult(
                    asset_id=asset_id, file_id=file_id, status="failed", message="删除失败，请稍后重试",
                ) for asset_id, file_id in selected], deleted_count=0, failed_count=len(selected),
            )
        for asset_id, file_id in selected:
            try:
                removed = await self.repository.remove_file(owner, asset_id, file_id)
                # Keep the retention reservation conservatively. Acquisition
                # workers can concurrently save into the same stable asset ID;
                # releasing here could remove their new protection. Removing a
                # library reference never authorizes collecting object bytes.
                results.append(LibraryFileDeleteResult(
                    asset_id=asset_id, file_id=file_id,
                    status="deleted" if removed else "not_found",
                    message=None if removed else "文件不存在或已从我的数据移除",
                ))
            except Exception as error:
                # An individual database failure must not abandon the rest of
                # the selection. Never return database messages or host paths.
                logger.warning("Removing library file reference failed (%s)", type(error).__name__)
                results.append(LibraryFileDeleteResult(
                    asset_id=asset_id, file_id=file_id, status="failed", message="删除失败，请稍后重试",
                ))
        deleted_count = sum(result.status == "deleted" for result in results)
        return LibraryFilesDeleteResponse(
            results=results, deleted_count=deleted_count, failed_count=len(results) - deleted_count,
        )

    async def get_context(self, owner: str, session_id: str) -> SelectedDataResponse:
        await self.owned_session(owner, session_id)
        file_ids = await self.repository.get_context(owner, session_id)
        files = []
        missing = []
        for file_id in file_ids:
            try:
                files.append(self._public_file(await self._info(owner, file_id)))
            except NotFoundError:
                missing.append(file_id)
        return SelectedDataResponse(files=files, missing_file_ids=missing)

    async def set_context(self, owner: str, session_id: str, request: SelectedDataInput) -> SelectedDataResponse:
        await self.owned_session(owner, session_id)
        infos = await asyncio.gather(*(self._info(owner, file_id, validate_bytes=True) for file_id in request.file_ids))
        await self.repository.set_context(owner, session_id, request.file_ids)
        return SelectedDataResponse(files=[self._public_file(info) for info in infos])
