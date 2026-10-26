"""Owner-only durable discovery, downloads and recovery inside a conversation."""
import asyncio

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.application.errors.exceptions import AppException
from app.application.services.open_data_service import get_open_data_service
from app.domain.models.open_data import AcquisitionInput, AcquisitionJob, OpenDataSearch, ResolvedFiles, SearchInput
from app.interfaces.dependencies import get_current_user
from app.interfaces.schemas.base import APIResponse

router = APIRouter(tags=["open-data"])
RESOLUTION_HEARTBEAT_SECONDS = 10


async def _long_json(operation):
    """Keep a complete manifest request alive without changing its JSON contract.

    Fast responses retain normal HTTP status handling. Slow operations send
    insignificant JSON whitespace until the final envelope is ready, so an
    active large manifest cannot hit the proxy's idle timeout. Disconnecting
    cancels and joins the work rather than leaving a detached resolution.
    """
    task = asyncio.create_task(operation)
    try:
        done, _ = await asyncio.wait({task}, timeout=RESOLUTION_HEARTBEAT_SECONDS)
        if done:
            return APIResponse.success(task.result())
    except BaseException:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        raise

    async def body():
        try:
            while not task.done():
                yield b"\n"
                await asyncio.wait({task}, timeout=RESOLUTION_HEARTBEAT_SECONDS)
            try:
                result = APIResponse.success(task.result())
            except AppException as error:
                result = APIResponse.error(error.code, error.msg)
            except Exception:
                result = APIResponse.error(500, "文件处理暂未完成，请稍后重试。")
            yield result.model_dump_json().encode("utf-8")
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    return StreamingResponse(body(), media_type="application/json", headers={
        "Cache-Control": "no-store", "X-Accel-Buffering": "no",
    })


@router.post("/sessions/{session_id}/data-searches", response_model=APIResponse[OpenDataSearch])
async def create_search(session_id: str, request: SearchInput, user=Depends(get_current_user), service=Depends(get_open_data_service)):
    return APIResponse.success(await service.create_search(user.id, session_id, request))


@router.get("/sessions/{session_id}/data-searches", response_model=APIResponse[list[OpenDataSearch]])
async def list_searches(session_id: str, user=Depends(get_current_user), service=Depends(get_open_data_service)):
    return APIResponse.success(await service.list_searches(user.id, session_id))


@router.get("/sessions/{session_id}/data-searches/{search_id}", response_model=APIResponse[OpenDataSearch])
async def get_search(session_id: str, search_id: str, user=Depends(get_current_user), service=Depends(get_open_data_service)):
    return APIResponse.success(await service.get_search(user.id, session_id, search_id))


@router.post("/sessions/{session_id}/data-searches/{search_id}/continue", response_model=APIResponse[OpenDataSearch])
async def continue_search(session_id: str, search_id: str, user=Depends(get_current_user), service=Depends(get_open_data_service)):
    return APIResponse.success(await service.continue_search(user.id, session_id, search_id))


@router.post("/sessions/{session_id}/data-searches/{search_id}/candidates/{candidate_id}/resolve", response_model=APIResponse[ResolvedFiles])
async def resolve(session_id: str, search_id: str, candidate_id: str, user=Depends(get_current_user), service=Depends(get_open_data_service)):
    await service.get_search(user.id, session_id, search_id)
    return await _long_json(service.resolve(user.id, session_id, search_id, candidate_id))


@router.post("/sessions/{session_id}/data-searches/{search_id}/acquisitions", response_model=APIResponse[AcquisitionJob])
async def create_acquisition(session_id: str, search_id: str, request: AcquisitionInput, user=Depends(get_current_user), service=Depends(get_open_data_service)):
    await service.get_search(user.id, session_id, search_id)
    return await _long_json(service.create_acquisition(user.id, session_id, search_id, request))


@router.get("/sessions/{session_id}/acquisitions", response_model=APIResponse[list[AcquisitionJob]])
async def list_acquisitions(session_id: str, search_id: str | None = None, user=Depends(get_current_user), service=Depends(get_open_data_service)):
    return APIResponse.success(await service.list_acquisitions(user.id, session_id, search_id))


@router.get("/sessions/{session_id}/acquisitions/{job_id}", response_model=APIResponse[AcquisitionJob])
async def get_acquisition(session_id: str, job_id: str, user=Depends(get_current_user), service=Depends(get_open_data_service)):
    return APIResponse.success(await service.get_acquisition(user.id, session_id, job_id))


@router.post("/sessions/{session_id}/acquisitions/{job_id}/cancel", response_model=APIResponse[AcquisitionJob])
async def cancel_acquisition(session_id: str, job_id: str, user=Depends(get_current_user), service=Depends(get_open_data_service)):
    return APIResponse.success(await service.cancel_acquisition(user.id, session_id, job_id))


@router.post("/sessions/{session_id}/acquisitions/{job_id}/retry", response_model=APIResponse[AcquisitionJob])
async def retry_acquisition(session_id: str, job_id: str, user=Depends(get_current_user), service=Depends(get_open_data_service)):
    return APIResponse.success(await service.retry_acquisition(user.id, session_id, job_id))
