import httpx
import pytest
from fastapi import FastAPI, Request

from app.application.errors.exceptions import UpstreamServiceError
from app.interfaces.middleware import sso_auth
from app.interfaces.middleware.sso_auth import SSOAuthorizationMiddleware


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(SSOAuthorizationMiddleware)

    @app.get("/api/v1/protected")
    async def protected(request: Request):
        return {"uid": request.state.sso_uid}

    @app.get("/unprotected")
    async def unprotected():
        return {"ok": True}

    @app.post("/api/v1/datasets/submissions")
    async def submission():
        return {"accepted": True}

    @app.get("/api/v1/files/{file_id}")
    async def signed_file(file_id: str):
        return {"file_id": file_id}

    return app


@pytest.mark.asyncio
async def test_missing_authorization_redirects_to_sso():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        response = await client.get("/api/v1/protected")

    assert response.status_code == 307
    assert response.headers["location"] == "https://space.4fair.cn"


@pytest.mark.asyncio
async def test_invalid_or_expired_authorization_redirects_to_sso(monkeypatch):
    async def reject(_token: str):
        raise UpstreamServiceError("expired")

    monkeypatch.setattr(sso_auth, "resolve_sso_uid", reject)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        response = await client.get(
            "/api/v1/protected",
            headers={"Authorization": "expired-token"},
        )

    assert response.status_code == 307
    assert response.headers["location"] == "https://space.4fair.cn"


@pytest.mark.asyncio
async def test_browser_request_gets_detectable_rejection_for_top_level_redirect():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        response = await client.get(
            "/api/v1/protected",
            headers={"X-DataSeek-Browser-Request": "1"},
        )

    assert response.status_code == 401
    assert response.headers["location"] == "https://space.4fair.cn"


@pytest.mark.asyncio
async def test_valid_authorization_exposes_verified_uid(monkeypatch):
    seen = []

    async def resolve(token: str):
        seen.append(token)
        return "user-1"

    monkeypatch.setattr(sso_auth, "resolve_sso_uid", resolve)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/api/v1/protected",
            headers={"Authorization": "valid-token"},
        )

    assert response.status_code == 200
    assert response.json() == {"uid": "user-1"}
    assert seen == ["valid-token"]


@pytest.mark.asyncio
async def test_submission_body_token_route_bypasses_header_authentication():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()),
        base_url="http://test",
    ) as client:
        response = await client.post("/api/v1/datasets/submissions", json={})

    assert response.status_code == 200
    assert response.json() == {"accepted": True}


@pytest.mark.asyncio
async def test_valid_signed_file_url_bypasses_header_authentication():
    signed_url = sso_auth.get_token_service().create_signed_url(
        "/api/v1/files/artifact-1",
        expire_minutes=5,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()),
        base_url="http://test",
    ) as client:
        response = await client.get(signed_url)

    assert response.status_code == 200
    assert response.json() == {"file_id": "artifact-1"}


@pytest.mark.asyncio
async def test_invalid_signed_file_url_remains_protected():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()),
        base_url="http://test",
        follow_redirects=False,
    ) as client:
        response = await client.get(
            "/api/v1/files/artifact-1?signature=invalid&expires=4102444800"
        )

    assert response.status_code == 307


@pytest.mark.asyncio
async def test_non_api_request_bypasses_sso():
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app()),
        base_url="http://test",
    ) as client:
        response = await client.get("/unprotected")

    assert response.status_code == 200
