from __future__ import annotations

from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

from app.application.errors.exceptions import BadRequestError, NotFoundError
from app.domain.models.data_product import DataProduct, DataProductFile
from app.domain.models.file import FileInfo
from app.domain.external.file import FileStorage
from app.infrastructure.external.file.factory import get_file_storage
from app.infrastructure.models.documents import DataProductDocument


def _role(file: FileInfo) -> str:
    content_type = (file.content_type or "").lower()
    name = (file.filename or "").lower()
    if content_type.startswith("image/") or name.endswith((".png", ".jpg", ".jpeg", ".svg")):
        return "chart"
    if name.endswith((".py", ".r", ".ipynb", ".js", ".ts")):
        return "source"
    if name.endswith((".md", ".txt", ".html", ".pdf", ".docx")):
        return "report"
    if name.endswith((".nc", ".nc4", ".cdf", ".tif", ".tiff", ".csv", ".xlsx", ".xls")):
        return "data"
    return "other"


def _relative(file: FileInfo) -> str:
    raw = (file.file_path or file.filename or "file").replace("\\", "/")
    path = PurePosixPath(raw)
    if path.is_absolute():
        parts = path.parts
        for marker in ("output", "upload"):
            if marker in parts:
                path = PurePosixPath(*parts[parts.index(marker) + 1 :])
                break
        else:
            path = PurePosixPath(file.filename or "file")
    if ".." in path.parts:
        return file.filename or "file"
    return path.as_posix()


class DataProductService:
    def __init__(self, file_storage: FileStorage | None = None):
        self._file_storage = file_storage or get_file_storage()

    async def draft(self, session_id: str, user_id: str, files: list[FileInfo]) -> dict[str, Any]:
        selected = [
            DataProductFile(
                file_id=str(item.file_id or ""),
                filename=item.filename or "file",
                relative_path=_relative(item),
                role=_role(item),
                content_type=item.content_type,
                size=int(item.size or 0),
                source_artifact_id=(item.metadata or {}).get("artifact_id"),
                source_tool=(item.metadata or {}).get("tool"),
            )
            for item in files
            if item.file_id
        ]
        return {
            "source_session_id": session_id,
            "suggested_name": "分析成果数据产品",
            "suggested_description": "由本次任务生成并整理的分析数据、图表和报告。",
            "generation_method": "agent_tool",
            "files": [item.model_dump() for item in selected],
        }

    async def create(
        self,
        *,
        dataset_id: str,
        session_id: str,
        user_id: str,
        name: str,
        description: str,
        generation_method: str,
        selected_file_ids: list[str],
        primary_file_id: str | None,
        files: list[FileInfo],
    ) -> DataProduct:
        if not name.strip():
            raise BadRequestError("Product name cannot be empty")
        allowed = {str(item.file_id): item for item in files if item.file_id}
        if not selected_file_ids:
            raise BadRequestError("At least one product file must be selected")
        unknown = set(selected_file_ids) - set(allowed)
        if unknown:
            raise BadRequestError("Selected file is not part of this task")
        if primary_file_id and primary_file_id not in selected_file_ids:
            raise BadRequestError("Primary file must be selected")
        previous = await DataProductDocument.find(
            {"dataset_id": dataset_id, "name": name.strip(), "created_by": user_id}
        ).sort("-version").first_or_none()
        version = (previous.version + 1) if previous else 1
        product_files: list[DataProductFile] = []
        uploaded_file_ids: list[str] = []
        try:
            for file_id in selected_file_ids:
                source = allowed[file_id]
                stream, _ = await self._file_storage.download_file(file_id, user_id)
                try:
                    stored = await self._file_storage.upload_file(
                        stream,
                        source.filename or "file",
                        user_id,
                        source.content_type,
                        {
                            "data_product": True,
                            "source_session_id": session_id,
                            "source_file_id": file_id,
                            "relative_path": _relative(source),
                        },
                    )
                finally:
                    stream.close()
                if not stored.file_id:
                    raise BadRequestError("Failed to persist product file")
                uploaded_file_ids.append(stored.file_id)
                product_files.append(DataProductFile(
                    file_id=stored.file_id,
                    filename=source.filename or "file",
                    relative_path=_relative(source),
                    role=_role(source),
                    content_type=source.content_type,
                    size=int(source.size or 0),
                    source_artifact_id=file_id,
                    source_tool=(source.metadata or {}).get("tool"),
                    is_primary=file_id == primary_file_id,
                    created_at=source.upload_date or datetime.now(UTC),
                ))
        except Exception:
            for uploaded_id in uploaded_file_ids:
                await self._file_storage.delete_file(uploaded_id, user_id)
            raise
        product = DataProduct(
            dataset_id=dataset_id,
            source_session_id=session_id,
            name=name.strip(),
            description=description.strip(),
            generation_method=generation_method.strip() or "agent_tool",
            created_by=user_id,
            owner_id=user_id,
            version=version,
            files=product_files,
            updated_at=datetime.now(UTC),
        )
        try:
            await DataProductDocument(**product.model_dump()).insert()
        except Exception:
            for uploaded_id in uploaded_file_ids:
                await self._file_storage.delete_file(uploaded_id, user_id)
            raise
        return product

    async def list_for_dataset(self, dataset_id: str, user_id: str) -> list[DataProduct]:
        products = await DataProductDocument.find(
            {"dataset_id": dataset_id, "$or": [{"owner_id": user_id}, {"owner_id": None, "created_by": user_id}]}
        ).sort("-created_at").to_list()
        return [item.to_domain() for item in products]

    async def get(self, product_id: str, user_id: str) -> DataProduct:
        item = await DataProductDocument.find_one(
            {"product_id": product_id, "$or": [{"owner_id": user_id}, {"owner_id": None, "created_by": user_id}]}
        )
        if not item:
            raise NotFoundError("Data product not found")
        return item.to_domain()

    async def update_metadata(
        self, product_id: str, user_id: str, name: str, description: str,
        generation_method: str, created_by: str, directories: list[str], files: list[dict[str, Any]],
    ) -> DataProduct:
        item = await DataProductDocument.find_one(
            {"product_id": product_id, "$or": [{"owner_id": user_id}, {"owner_id": None, "created_by": user_id}]}
        )
        if not item:
            raise NotFoundError("Data product not found")
        if not name.strip():
            raise BadRequestError("Product name cannot be empty")
        item.name = name.strip()
        item.description = description.strip()
        item.generation_method = generation_method.strip() or "agent_tool"
        item.created_by = created_by.strip()
        existing = {str(file.get("file_id")): file for file in item.files}
        cleaned_files = []
        cleaned_dirs = []
        for directory in directories:
            normalized = str(directory).replace("\\", "/").strip(" /")
            if normalized and ".." not in PurePosixPath(normalized).parts and normalized not in cleaned_dirs:
                cleaned_dirs.append(normalized)
        for value in files:
            file_id = str(value.get("file_id", ""))
            if file_id not in existing:
                raise BadRequestError("Product file is not part of this product")
            relative = str(value.get("relative_path") or existing[file_id].get("relative_path") or existing[file_id].get("filename") or "").replace("\\", "/").strip("/")
            path = PurePosixPath(relative)
            if not relative or path.is_absolute() or ".." in path.parts:
                raise BadRequestError("Invalid product file path")
            updated = dict(existing[file_id]); updated["relative_path"] = relative; updated["is_primary"] = bool(value.get("is_primary", False))
            cleaned_files.append(updated)
            parent = str(path.parent)
            if parent not in ("", ".") and parent not in cleaned_dirs: cleaned_dirs.append(parent)
        if not cleaned_files:
            raise BadRequestError("A product must contain at least one visible file")
        item.files = cleaned_files
        item.directories = sorted(cleaned_dirs)
        if getattr(item, "owner_id", None) is None: item.owner_id = user_id
        item.updated_at = datetime.now(UTC)
        await item.save()
        return item.to_domain()

    async def delete(self, product_id: str, user_id: str) -> None:
        item = await DataProductDocument.find_one(
            {"product_id": product_id, "$or": [{"owner_id": user_id}, {"owner_id": None, "created_by": user_id}]}
        )
        if not item:
            raise NotFoundError("Data product not found")
        for file in item.files:
            file_id = file.get("file_id") if isinstance(file, dict) else None
            if file_id:
                await self._file_storage.delete_file(file_id, user_id)
        await item.delete()
