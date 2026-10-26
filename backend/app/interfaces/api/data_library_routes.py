"""Private persistent data and reusable conversation inputs."""
import re
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from pydantic import AwareDatetime, BeforeValidator

from app.application.services.data_library_service import DataLibraryService
from app.domain.models.data_library import (
    CreateLibraryAsset, CreateLibraryFolder, DataLibraryAsset, DataLibraryFolder, DataLibraryFolders,
    DataLibraryPage, DataLibraryStats, DeleteLibraryFiles, LibraryFilesDeleteResponse,
    LibraryFolderDeleteResponse, LibraryFolderDeletionPreview, SelectedDataInput,
    SelectedDataResponse, SourceType, UpdateLibraryAsset,
)
from app.infrastructure.external.file.factory import get_file_storage
from app.infrastructure.repositories.mongo_data_library_repository import MongoDataLibraryRepository
from app.infrastructure.repositories.mongo_session_repository import MongoSessionRepository
from app.interfaces.dependencies import get_current_user
from app.interfaces.schemas.base import APIResponse

router = APIRouter(tags=["data-library"])


def _iso_datetime(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}[Tt].*(?:[Zz]|[+-]\d{2}:\d{2})", value):
        raise ValueError("更新时间必须是包含时区的 ISO 日期时间")
    return value


UpdatedDate = Annotated[AwareDatetime, BeforeValidator(_iso_datetime)]


def get_data_library_service():
    return DataLibraryService(MongoDataLibraryRepository(), get_file_storage(), MongoSessionRepository())


@router.get("/data-library", response_model=APIResponse[DataLibraryPage])
async def list_assets(
    q: str = Query("", max_length=200), source_type: SourceType | None = None,
    cursor: str | None = Query(None, pattern=r"^[a-f0-9]{32}$"),
    filename: str | None = Query(None, max_length=200),
    file_format: str | None = Query(None, max_length=200),
    folder_id: str | None = Query(None, pattern=r"^(?:root|[a-f0-9]{32})$"),
    updated_from: UpdatedDate | None = None,
    updated_to: UpdatedDate | None = None,
    user=Depends(get_current_user), service=Depends(get_data_library_service),
):
    filters = {name: value for name, value in (
        ("filename", filename), ("file_format", file_format),
        ("folder_id", folder_id),
        ("updated_from", updated_from), ("updated_to", updated_to),
    ) if value is not None}
    return APIResponse.success(await service.list(user.id, cursor=cursor, query=q, source_type=source_type, **filters))


@router.get("/data-library/stats", response_model=APIResponse[DataLibraryStats])
async def get_stats(user=Depends(get_current_user), service=Depends(get_data_library_service)):
    return APIResponse.success(await service.stats(user.id))


@router.get("/data-library/folders", response_model=APIResponse[DataLibraryFolders])
async def list_folders(user=Depends(get_current_user), service=Depends(get_data_library_service)):
    return APIResponse.success(await service.list_folders(user.id))


@router.post("/data-library/folders", response_model=APIResponse[DataLibraryFolder])
async def create_folder(request: CreateLibraryFolder, user=Depends(get_current_user), service=Depends(get_data_library_service)):
    return APIResponse.success(await service.create_folder(user.id, request))


@router.get("/data-library/folders/{folder_id}/deletion-preview", response_model=APIResponse[LibraryFolderDeletionPreview])
async def folder_deletion_preview(folder_id: str = Path(pattern=r"^[a-f0-9]{32}$"), user=Depends(get_current_user), service=Depends(get_data_library_service)):
    return APIResponse.success(await service.folder_deletion_preview(user.id, folder_id))


@router.delete("/data-library/folders/{folder_id}", response_model=APIResponse[LibraryFolderDeleteResponse])
async def delete_folder(
    folder_id: str = Path(pattern=r"^[a-f0-9]{32}$"),
    deletion_token: str = Query(pattern=r"^[a-f0-9]{64}$"),
    user=Depends(get_current_user), service=Depends(get_data_library_service),
):
    return APIResponse.success(await service.delete_folder(user.id, folder_id, deletion_token))


@router.post("/data-library", response_model=APIResponse[DataLibraryAsset])
async def create_asset(request: CreateLibraryAsset, user=Depends(get_current_user), service=Depends(get_data_library_service)):
    return APIResponse.success(await service.create(user.id, request))


@router.post("/data-library/files/delete", response_model=APIResponse[LibraryFilesDeleteResponse])
async def delete_files(request: DeleteLibraryFiles, user=Depends(get_current_user), service=Depends(get_data_library_service)):
    return APIResponse.success(await service.delete_files(user.id, request))


@router.get("/data-library/{asset_id}", response_model=APIResponse[DataLibraryAsset])
async def get_asset(asset_id: str = Path(pattern=r"^[a-f0-9]{32}$"), user=Depends(get_current_user), service=Depends(get_data_library_service)):
    return APIResponse.success(await service.get(user.id, asset_id))


@router.patch("/data-library/{asset_id}", response_model=APIResponse[DataLibraryAsset])
async def update_asset(request: UpdateLibraryAsset, asset_id: str = Path(pattern=r"^[a-f0-9]{32}$"), user=Depends(get_current_user), service=Depends(get_data_library_service)):
    return APIResponse.success(await service.update(user.id, asset_id, request))


@router.delete("/data-library/{asset_id}", response_model=APIResponse[None])
async def delete_asset(asset_id: str = Path(pattern=r"^[a-f0-9]{32}$"), user=Depends(get_current_user), service=Depends(get_data_library_service)):
    await service.delete(user.id, asset_id)
    return APIResponse.success()


@router.get("/sessions/{session_id}/data-context", response_model=APIResponse[SelectedDataResponse])
async def get_context(session_id: str, user=Depends(get_current_user), service=Depends(get_data_library_service)):
    return APIResponse.success(await service.get_context(user.id, session_id))


@router.put("/sessions/{session_id}/data-context", response_model=APIResponse[SelectedDataResponse])
async def set_context(request: SelectedDataInput, session_id: str, user=Depends(get_current_user), service=Depends(get_data_library_service)):
    return APIResponse.success(await service.set_context(user.id, session_id, request))
