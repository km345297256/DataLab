from functools import lru_cache

from app.core.config import get_settings
from app.domain.external.file import FileStorage
from app.infrastructure.storage.mongodb import get_mongodb
from app.infrastructure.external.file.gridfsfile import GridFSFileStorage


@lru_cache()
def get_file_storage() -> FileStorage:
    from app.infrastructure.external.file.datasetfile import DatasetPreviewFileStorage
    from app.infrastructure.external.file.retainedfile import RetainedFileStorage
    from app.infrastructure.repositories.mongo_data_library_repository import MongoDataLibraryRepository
    settings = get_settings()
    provider = (settings.file_storage_provider or "hybrid").lower()
    gridfs_storage = GridFSFileStorage(mongodb=get_mongodb())

    if provider == "minio":
        from app.infrastructure.external.file.miniofile import MinIOFileStorage

        storage = MinIOFileStorage()
    elif provider in {"hybrid", "gridfs"}:
        from app.infrastructure.external.file.hybridfile import HybridFileStorage
        from app.infrastructure.external.file.miniofile import MinIOFileStorage

        # Legacy deployments may still say "gridfs". Keep their old reads,
        # but all new inputs and artifacts must have canonical MinIO objects.
        storage = HybridFileStorage(MinIOFileStorage(), gridfs_storage)
    else:
        raise ValueError(f"Unsupported FILE_STORAGE_PROVIDER: {settings.file_storage_provider}")
    return DatasetPreviewFileStorage(RetainedFileStorage(storage, MongoDataLibraryRepository()))
