"""
File operation API interfaces
"""
from fastapi import APIRouter, UploadFile, File, Form, Request, Query
from fastapi.responses import FileResponse
from starlette.responses import Response as HTTPResponse
from app.core.exceptions import BadRequestException
from app.core.cancellable_checks import run_cancellable_check
from app.schemas.file import (
    FileReadRequest, FileWriteRequest, FileReplaceRequest,
    FileSearchRequest, FileFindRequest, ArtifactFingerprintsRequest,
    ArtifactValidationRequest, ArtifactDeliveryRequest, AnalysisFingerprintsRequest, FilePatchRequest,
)
from app.schemas.response import Response
from app.services.file import file_service
from app.services.artifact_manifest import fingerprint_artifacts
from app.services.artifact_validation import validate_artifacts
from app.services.analysis_fingerprints import fingerprint_analysis_files
from app.services.upload_stream import upload_stream, check_upload_capacity

router = APIRouter()


@router.post("/inspect-deliveries", response_model=Response)
async def inspect_output_deliveries(request: ArtifactDeliveryRequest, http_request: Request):
    """Mechanically inspect deliverable bytes; never inspect content semantics."""
    result = await run_cancellable_check(
        http_request, validate_artifacts, [item.model_dump() for item in request.items], purpose="delivery",
    )
    return Response(success=True, data=result)


@router.post("/validate-artifacts", response_model=Response)
async def validate_output_artifacts(request: ArtifactValidationRequest, http_request: Request):
    result = await run_cancellable_check(
        http_request, validate_artifacts, [item.model_dump() for item in request.items],
        purpose=request.purpose,
    )
    return Response(success=True, data=result)


@router.post("/analysis-fingerprints", response_model=Response)
async def analysis_fingerprints(request: AnalysisFingerprintsRequest, http_request: Request):
    result = await run_cancellable_check(
        http_request, fingerprint_analysis_files, request.paths,
        approved_upload_paths=request.approved_upload_paths,
    )
    return Response(success=True, data=result)


@router.post("/fingerprints", response_model=Response)
async def artifact_fingerprints(request: ArtifactFingerprintsRequest, http_request: Request):
    result = await run_cancellable_check(http_request, fingerprint_artifacts, request.paths)
    return Response(success=True, data=result)

@router.post("/read", response_model=Response)
async def read_file(request: FileReadRequest):
    """
    Read file content
    """
    result = await file_service.read_file(
        file=request.file,
        start_line=request.start_line,
        end_line=request.end_line,
        sudo=request.sudo,
        max_length=request.max_length,
        include_version=request.include_version,
    )
    
    # Construct response
    return Response(
        success=True,
        message="File read successfully",
        data=result.model_dump(exclude_none=True)
    )


@router.post("/apply-patch", response_model=Response)
async def apply_file_patch(request: FilePatchRequest):
    result = await file_service.apply_patch(request)
    return Response(success=True,
                    message="Patch saved atomically" if result.changed else "Saved content unchanged; reused existing version",
                    data=result.model_dump())

@router.post("/write", response_model=Response)
async def write_file(request: FileWriteRequest):
    """
    Write file content
    """
    result = await file_service.write_file(
        file=request.file,
        content=request.content,
        append=request.append,
        leading_newline=request.leading_newline,
        trailing_newline=request.trailing_newline,
        sudo=request.sudo
    )
    
    # Construct response
    return Response(
        success=True,
        message="Saved content unchanged; reused existing version" if result.changed is False else "File written successfully",
        data=result.model_dump(exclude_none=True)
    )

@router.post("/replace", response_model=Response)
async def replace_in_file(request: FileReplaceRequest):
    """
    Replace string in file
    """
    result = await file_service.str_replace(
        file=request.file,
        old_str=request.old_str,
        new_str=request.new_str,
        sudo=request.sudo
    )

    unchanged = result.changed is False and result.write_action == "reuse"
    succeeded = result.replaced_count > 0 or unchanged
    message = (
        "Saved content unchanged; reused existing version" if unchanged else
        f"Replacement completed, replaced {result.replaced_count} occurrences"
        if succeeded
        else "Replacement made no changes: target text was not found"
    )

    # Construct response
    return Response(
        success=succeeded,
        message=message,
        data=result.model_dump(exclude_none=True)
    )

@router.post("/search", response_model=Response)
async def search_in_file(request: FileSearchRequest):
    """
    Search in file content
    """
    result = await file_service.find_in_content(
        file=request.file,
        regex=request.regex,
        sudo=request.sudo
    )
    
    # Construct response
    return Response(
        success=True,
        message=f"Search completed, found {len(result.matches)} matches",
        data=result.model_dump()
    )

@router.post("/find", response_model=Response)
async def find_files(request: FileFindRequest):
    """
    Find files by name pattern
    """
    result = await file_service.find_by_name(
        path=request.path,
        glob_pattern=request.glob
    )
    
    # Construct response
    return Response(
        success=True,
        message=f"Search completed, found {len(result.files)} files",
        data=result.model_dump()
    )

@router.head("/upload-stream")
async def upload_stream_capability(path: str | None = None, size: int | None = Query(None, ge=0)):
    if path is not None:
        check_upload_capacity(path, size)
    return HTTPResponse(status_code=204)


@router.post("/upload-stream", response_model=Response)
async def upload_stream_file(request: Request, path: str):
    length = request.headers.get("content-length")
    if length is not None and (not length.isascii() or not length.isdigit()):
        raise BadRequestException("Invalid upload length")
    result = await upload_stream(path, request.stream(), int(length) if length is not None else None)
    return Response(success=True, message="File uploaded successfully", data=result.model_dump())


@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    path: str = Form(None)
):
    """
    Upload file using streaming
    """
    if not path:
        path = f"/tmp/{file.filename}"
    
    result = await file_service.upload_file(
        path=path,
        file_stream=file
    )
    
    return Response(
        success=True,
        message="File uploaded successfully",
        data=result.model_dump()
    )

@router.get("/download")
async def download_file(path: str):
    """
    Download file using FileResponse
    """
    # Check if file exists (this will raise appropriate exception if not found)
    file_service.ensure_file(path)
    
    # Determine filename from path
    filename = path.split('/')[-1]
    
    return FileResponse(
        path=path,
        filename=filename,
        media_type='application/octet-stream'
    )
