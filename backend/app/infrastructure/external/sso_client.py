"""External single-sign-on and analysis usage integrations."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.application.errors.exceptions import UpstreamServiceError
from app.core.config import get_settings

logger = logging.getLogger(__name__)


def _response_payload(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except (ValueError, TypeError) as exc:
        raise UpstreamServiceError("单点登录系统返回了无效响应") from exc
    if not isinstance(payload, dict):
        raise UpstreamServiceError("单点登录系统返回了无效响应")
    return payload


async def resolve_sso_uid(token: str) -> str:
    normalized = token.strip()
    if not normalized:
        raise UpstreamServiceError("单点登录 token 不能为空")
    settings = get_settings()
    try:
        async with httpx.AsyncClient(
            timeout=settings.external_integration_timeout_seconds
        ) as client:
            response = await client.post(
                settings.sso_uid_url,
                headers={"Authorization": normalized},
            )
            response.raise_for_status()
    except (httpx.HTTPError, TimeoutError) as exc:
        logger.warning("SSO uid lookup failed: %s", type(exc).__name__)
        raise UpstreamServiceError("无法从单点登录系统获取用户信息") from exc
    payload = _response_payload(response)
    if payload.get("code") != 200:
        raise UpstreamServiceError("单点登录系统未返回有效用户")
    data = payload.get("data")
    uid = data.get("uid") if isinstance(data, dict) else None
    if not isinstance(uid, str) or not uid.strip():
        raise UpstreamServiceError("单点登录系统未返回有效 uid")
    return uid.strip()


async def record_analysis_tool_usage(
    *,
    uid: str,
    title: str,
    tool_id: str,
) -> None:
    settings = get_settings()
    params = {
        "uid": uid,
        "title": title,
        "toolId": tool_id,
        "source": settings.analysis_tool_source,
    }
    try:
        async with httpx.AsyncClient(
            timeout=settings.external_integration_timeout_seconds
        ) as client:
            response = await client.post(settings.analysis_tool_usage_url, params=params)
            response.raise_for_status()
            payload = _response_payload(response)
            if payload.get("code") != 200:
                raise ValueError("usage endpoint returned a non-success code")
    except Exception as exc:
        # Usage reporting is auxiliary; never fail the scientific task because
        # the external statistics service is unavailable.
        logger.warning(
            "Analysis Tool usage reporting failed for %s: %s",
            tool_id,
            type(exc).__name__,
        )
