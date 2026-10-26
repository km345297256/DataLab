"""
Pytest configuration and fixtures
"""
import sys
import pytest
import socket
import threading
import time
from pathlib import Path

# Add the parent directory to Python path so we can import app modules
sys.path.insert(0, str(Path(__file__).parent.parent))

import requests
import uvicorn
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1.file import router as file_router
from app.api.v1.shell import router as shell_router
from app.core.exceptions import (
    AppException,
    app_exception_handler,
    general_exception_handler,
    http_exception_handler,
    validation_exception_handler,
)

app = FastAPI()
app.add_exception_handler(AppException, app_exception_handler)
app.add_exception_handler(StarletteHTTPException, http_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(Exception, general_exception_handler)
app.include_router(file_router, prefix="/api/v1/file")
app.include_router(shell_router, prefix="/api/v1/shell")

with socket.socket() as port_socket:
    port_socket.bind(("127.0.0.1", 0))
    TEST_PORT = port_socket.getsockname()[1]

BASE_URL = f"http://127.0.0.1:{TEST_PORT}"


@pytest.fixture(autouse=True)
def isolated_analysis_workspace(tmp_path_factory, monkeypatch):
    """Real launches prepare a private output dir, never the developer's home."""
    from app.services import analysis_workspace
    # Keep framework startup files out of a case's own tmp_path: several
    # readers assert that no disk spill or temporary snapshots remain there.
    root = tmp_path_factory.mktemp("sandbox-runtime").resolve()
    monkeypatch.setattr(analysis_workspace, "WORKSPACE_ROOT", root)
    return root


@pytest.fixture
def sandbox_api_server():
    """Own a real HTTP server only for the test that needs it.

    Reader unit tests deliberately patch shared stdlib functions such as
    time.monotonic and socket.socket. A session-wide server races those patches:
    its event loop can consume a finite fake clock and exit between HTTP tests.
    Stop/join this server before any unrelated test installs its patches.
    """
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=TEST_PORT,
                                           log_level="error", loop="asyncio"))
    errors = []

    def run():
        try:
            server.run()
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 5
    try:
        while time.monotonic() < deadline:
            if not thread.is_alive():
                raise RuntimeError("Sandbox test server exited during startup") from (errors[0] if errors else None)
            if server.started:
                break
            time.sleep(0.01)
        else:
            raise RuntimeError("Sandbox test server did not start")

        yield
        if not thread.is_alive():
            raise RuntimeError("Sandbox test server exited during the HTTP test") from (errors[0] if errors else None)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        if thread.is_alive():
            raise RuntimeError("Sandbox test server did not stop after the HTTP test")
        if errors:
            raise RuntimeError("Sandbox test server failed") from errors[0]


class _BoundedTestSession(requests.Session):
    def request(self, method, url, **kwargs):
        # Keep explicit per-case deadlines, but make a dead server a test
        # failure instead of an indefinitely blocked suite.
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = (3, 30)
        return super().request(method, url, **kwargs)

@pytest.fixture
def client(sandbox_api_server):
    """Create a session targeting the managed in-process test server."""
    with _BoundedTestSession() as session:
        yield session


@pytest.fixture
def temp_test_file(client, tmp_path):
    """Create a temporary file through the in-process API."""
    temp_file = str(tmp_path / "test_file.txt")
    content = "Line 1: Hello World\nLine 2: This is a test\nLine 3: Python testing"
    response = client.post(f"{BASE_URL}/api/v1/file/write", json={
        "file": temp_file,
        "content": content
    })
    assert response.status_code == 200
    yield temp_file
