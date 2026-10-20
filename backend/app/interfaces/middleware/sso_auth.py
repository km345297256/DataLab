"""SSO authorization boundary for the DataSeek API."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
import re

from fastapi import Request
from fastapi.responses import RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app.application.errors.exceptions import UpstreamServiceError
from app.infrastructure.external.sso_client import resolve_sso_uid
from app.interfaces.dependencies import get_token_service


SSO_LOGIN_URL = "https://space.4fair.cn"
SSO_TOKEN_COOKIE = "dataseek_sso_token"
BROWSER_REQUEST_HEADER = "X-DataSeek-Browser-Request"
SIGNED_FILE_PATH = re.compile(r"^/api/v1/files/[^/]+$")
JUPYTER_PROXY_PATH = re.compile(r"^/api/v1/sessions/[^/]+/jupyter-proxy(?:/.*)?$")


@dataclass(frozen=True)
class SSOIdentity:
    uid: str
    token: str


def sso_redirect_response() -> RedirectResponse:
    return RedirectResponse(url=SSO_LOGIN_URL, status_code=307)


def sso_rejection_response(request: Request) -> Response:
    # XMLHttpRequest/fetch follows a cross-origin 307 without navigating the
    # page. Return a detectable response so the frontend can perform the
    # required top-level redirect. Direct API callers retain normal 307 logic.
    if request.headers.get(BROWSER_REQUEST_HEADER) == "1":
        return Response(status_code=401, headers={"Location": SSO_LOGIN_URL})
    return sso_redirect_response()


def request_sso_identity(request: Request) -> SSOIdentity | None:
    uid = getattr(request.state, "sso_uid", None)
    token = getattr(request.state, "sso_token", None)
    if not isinstance(uid, str) or not uid or not isinstance(token, str) or not token:
        return None
    return SSOIdentity(uid=uid, token=token)


def tokens_match(left: str, right: str) -> bool:
    return secrets.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def is_valid_signed_file_request(request: Request) -> bool:
    if request.method != "GET" or not SIGNED_FILE_PATH.fullmatch(request.url.path):
        return False
    if not request.query_params.get("signature") or not request.query_params.get("expires"):
        return False
    return get_token_service().verify_signed_url(str(request.url))


class SSOAuthorizationMiddleware(BaseHTTPMiddleware):
    """Validate every API request against the data-center SSO service."""

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        if not request.url.path.startswith("/api/v1") or request.method == "OPTIONS":
            return await call_next(request)

        # The data-center submission contract carries its SSO token in the
        # validated JSON body. Its route performs the SSO lookup after FastAPI
        # parses that body; requiring an Authorization header here would reject
        # a valid submission before the route can inspect the token.
        if (
            request.method == "POST"
            and request.url.path == "/api/v1/datasets/submissions"
        ):
            return await call_next(request)

        # Browser image elements cannot attach Authorization headers. Artifact
        # previews use short-lived HMAC URLs, so allow only an exact file GET
        # after verifying its signature and expiry.
        if is_valid_signed_file_request(request):
            return await call_next(request)

        # Jupyter iframe assets and kernel requests cannot attach the DataSeek
        # bearer header. The proxy route validates its own short-lived,
        # task-scoped ticket on every request.
        if JUPYTER_PROXY_PATH.fullmatch(request.url.path):
            return await call_next(request)

        token = request.headers.get("Authorization", "").strip()
        if not token:
            return sso_rejection_response(request)

        try:
            uid = await resolve_sso_uid(token)
        except UpstreamServiceError:
            return sso_rejection_response(request)

        request.state.sso_uid = uid
        request.state.sso_token = token
        return await call_next(request)
