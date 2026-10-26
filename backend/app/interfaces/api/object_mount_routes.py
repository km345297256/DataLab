"""Private, capability-scoped HTTP filesystem for execution mount helpers."""
from __future__ import annotations

import html
import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, StreamingResponse

from app.application.services.object_mount_service import (
    ObjectMountAccessError,
    ObjectMountService,
    READ_CHUNK_BYTES,
    get_object_mount_service,
)


router = APIRouter(prefix="/internal/object-mounts", include_in_schema=False)
_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"]
_BASE_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def _range(value: str | None, size: int) -> tuple[int, int]:
    if value is None:
        return 0, size
    if len(value) > 64 or not re.fullmatch(r"bytes=[0-9]*-[0-9]*", value):
        raise ValueError("Invalid range")
    first, last = value[6:].split("-")
    if not size or not (first or last):
        raise ValueError("Invalid range")
    if not first:
        suffix = int(last)
        if suffix <= 0:
            raise ValueError("Invalid range")
        length = min(suffix, size)
        return size - length, length
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start >= size or end < start:
        raise ValueError("Invalid range")
    return start, end - start + 1


def _failure(status_code: int) -> Response:
    return Response("Forbidden" if status_code == 403 else "Not found", status_code=status_code,
                    headers=_BASE_HEADERS, media_type="text/plain")


@router.api_route("/{grant_id}", methods=_METHODS)
@router.api_route("/{grant_id}/{relative_path:path}", methods=_METHODS)
async def object_mount_view(
    request: Request,
    grant_id: str,
    relative_path: str = "",
    service: ObjectMountService = Depends(get_object_mount_service),
):
    if request.method not in {"GET", "HEAD"}:
        return _failure(403)
    # Tokens belong exclusively in Authorization, never in query strings.
    if request.url.query:
        return _failure(404)
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        return _failure(403)
    try:
        view = await service.resolve(grant_id, token, relative_path)
        if view.children is not None:
            links = []
            for name, is_directory in view.children:
                suffix = "/" if is_directory else ""
                links.append(f'<li><a href="{quote(name, safe="")}{suffix}">{html.escape(name)}{suffix}</a></li>')
            content = ("<!doctype html><html><body><ul>" + "".join(links) + "</ul></body></html>").encode("utf-8")
            return Response(content if request.method == "GET" else b"", media_type="text/html",
                            headers={**_BASE_HEADERS, "Content-Length": str(len(content))})
        reference = view.reference
        headers = {**_BASE_HEADERS, "Accept-Ranges": "bytes", "ETag": '"' + reference.etag + '"'}
        if request.method == "HEAD":
            return Response(b"", media_type="application/octet-stream", headers={
                **headers, "Content-Length": str(reference.size),
            })
        # Honor version preconditions without falling back to an unbounded body.
        if request.headers.get("if-match") not in (None, "*", headers["ETag"]):
            return Response(status_code=412, headers=headers)
        if request.headers.get("if-range") not in (None, headers["ETag"]):
            return Response(status_code=412, headers=headers)
        try:
            start, length = _range(request.headers.get("range"), reference.size)
        except ValueError:
            return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{reference.size}"})
        status_code = 206 if request.headers.get("range") is not None else 200
        headers["Content-Length"] = str(length)
        if status_code == 206:
            headers["Content-Range"] = f"bytes {start}-{start + length - 1}/{reference.size}"
        if not length:
            return Response(b"", status_code=status_code, headers=headers, media_type="application/octet-stream")
        # Read the first bounded range before sending successful response headers.
        first = await service.read_chunk(grant_id, token, view, start, min(length, READ_CHUNK_BYTES))

        async def stream():
            yield first
            async for block in service.chunks(grant_id, token, view, start + len(first), length - len(first)):
                yield block

        return StreamingResponse(stream(), status_code=status_code, headers=headers, media_type="application/octet-stream")
    except ObjectMountAccessError as error:
        return _failure(error.status_code)
