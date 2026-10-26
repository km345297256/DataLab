"""Delivery inspection is independent of content and preview capabilities."""
import json
from unittest.mock import AsyncMock

import httpx
import pytest

from app.infrastructure.external.sandbox.delivery_receipt import legacy_delivery_receipts
from app.infrastructure.external.sandbox.docker_sandbox import DockerSandbox


ITEM = {"path": "/home/ubuntu/output/report.svg", "kind": "image"}
HASH = "a" * 64


def legacy(**changes):
    return {"version": 1, "files": [{"path": ITEM["path"], "sha256": HASH, "size": 0}], "errors": [], **changes}


def test_legacy_fingerprint_proves_only_uploaded_bytes_not_content_or_preview():
    receipt = legacy_delivery_receipts([ITEM], legacy())["files"][0]
    assert receipt["valid"] and receipt["size"] == 0
    assert receipt["reason"] == "delivery_ready"
    assert receipt["metadata"] == {"validation_scope": "file_integrity", "content_validation": "not_performed"}
    assert receipt["proof"]["content_validation"] == "not_performed"


@pytest.mark.parametrize("data", [legacy(version=True), legacy(files=[]),
    legacy(files=[{"path": "/private/input", "sha256": HASH, "size": 10}]),
    legacy(files=[{"path": ITEM["path"], "sha256": HASH, "size": True}]),
    legacy(files=[{"path": ITEM["path"], "sha256": "preview_hash", "size": 10}]),
    legacy(errors=[{"path": ITEM["path"], "code": "unavailable_or_changed"}]),
    {"version": 1, "files": [{"path": ITEM["path"], "valid": False, "reason": "invalid_content"}]},
])
def test_malformed_or_content_verdicts_cannot_be_promoted_to_delivery(data):
    with pytest.raises(ValueError):
        legacy_delivery_receipts([ITEM], data)


@pytest.mark.asyncio
async def test_old_sandbox_uses_hash_api_without_content_inspection_or_execution():
    calls = []
    def respond(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/inspect-deliveries"):
            return httpx.Response(404)
        assert request.url.path.endswith("/fingerprints")
        assert json.loads(request.content) == {"paths": [ITEM["path"]]}
        return httpx.Response(200, json={"success": True, "data": legacy()})
    adapter = object.__new__(DockerSandbox)
    adapter.base_url = "http://sandbox"
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter.client = client
        result = await adapter.validate_artifacts([ITEM], purpose="delivery")
    assert result.success and result.data["files"][0]["valid"]
    assert calls == ["/api/v1/file/inspect-deliveries", "/api/v1/file/fingerprints"]


@pytest.mark.asyncio
async def test_missing_legacy_snapshot_remains_unavailable_without_permission_to_regenerate():
    adapter = object.__new__(DockerSandbox)
    adapter.base_url = "http://sandbox"
    responses = [httpx.Response(404), httpx.Response(200, json={"success": True,
        "data": legacy(files=[], errors=[{"path": ITEM["path"], "code": "unavailable_or_changed"}])})]
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: responses.pop(0))) as client:
        adapter.client = client
        result = await adapter.validate_artifacts([ITEM])
    record = result.data["files"][0]
    assert not record["valid"] and record["reason"] == "validation_unavailable"


@pytest.mark.asyncio
async def test_no_fallback_on_permission_denial(monkeypatch):
    adapter = object.__new__(DockerSandbox)
    adapter.base_url = "http://sandbox"
    calls = []
    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(403)
    pause = AsyncMock()
    monkeypatch.setattr("app.infrastructure.external.sandbox.docker_sandbox.asyncio.sleep", pause)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter.client = client
        result = await adapter.validate_artifacts([ITEM])
    assert not result.success and len(calls) == 1
    pause.assert_not_awaited()
