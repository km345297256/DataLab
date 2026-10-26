"""Resumable browser downloads backed by bounded storage reads."""
import re
from urllib.parse import quote

from fastapi import Request, Response
from fastapi.responses import StreamingResponse

from app.application.errors.exceptions import NotFoundError
from app.application.services.file_preview import preview_version
from app.interfaces.schemas.file import public_filename


def download_range(value: str, size: int) -> tuple[int, int]:
    """Parse one HTTP byte range; multipart ranges are intentionally unsupported."""
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if not match or not any(match.groups()) or size <= 0:
        raise ValueError("Invalid range")
    start, end = match.groups()
    if not start:
        suffix = int(end)
        if not suffix:
            raise ValueError("Invalid range")
        return max(0, size - suffix), size - 1
    first, last = int(start), min(int(end), size - 1) if end else size - 1
    if first >= size or first > last:
        raise ValueError("Invalid range")
    return first, last


async def stored_file_response(request: Request, file_id: str, service, user_id: str | None = None):
    try:
        info = await service.get_file_info(file_id, user_id)
    except (FileNotFoundError, PermissionError):
        raise NotFoundError("File not found") from None
    if info is None or info.size is None or info.size < 0:
        raise NotFoundError("File not found")
    size = info.size
    version = preview_version(info)
    etag = f'"{version}"'
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(public_filename(info.filename), safe='')}",
        "Accept-Ranges": "bytes",
        "ETag": etag,
        "Cache-Control": "private, no-cache",
        "X-Accel-Buffering": "no",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox; default-src 'none'; base-uri 'none'",
    }
    first, last, status = 0, size - 1, 200
    requested = request.headers.get("range")
    # HEAD describes the full representation. An unmatched If-Range requests
    # a fresh full body instead of appending bytes from a different version.
    if request.method != "HEAD" and requested and request.headers.get("if-range", etag) == etag:
        try:
            first, last = download_range(requested, size)
        except (ValueError, OverflowError):
            return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
        status = 206
        headers["Content-Range"] = f"bytes {first}-{last}/{size}"
    length = max(0, last - first + 1)
    headers["Content-Length"] = str(length)
    mime = info.content_type or "application/octet-stream"
    if request.method == "HEAD":
        return Response(status_code=status, media_type=mime, headers=headers)
    return StreamingResponse(service.download_chunks(file_id, user_id, offset=first, length=length, version=version),
        status_code=status, media_type=mime, headers=headers)
