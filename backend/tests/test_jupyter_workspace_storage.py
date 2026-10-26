"""Host-backed notebook storage must preserve ownership and mount boundaries."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import docker
import pytest

from app.application.services import jupyter_service as module
from app.application.services.jupyter_service import JupyterService


class Volumes:
    def __init__(self, volume=None):
        self.volume = volume
        self.created = []

    def get(self, name):
        if self.volume is None:
            raise docker.errors.NotFound("missing")
        return self.volume

    def create(self, **kwargs):
        self.created.append(kwargs)
        self.volume = SimpleNamespace(attrs={
            "Labels": kwargs["labels"], "Driver": kwargs.get("driver", "local"),
            "Options": kwargs.get("driver_opts"),
        }, remove=Mock())
        return self.volume


def service(tmp_path, *, host_root="/data2/state/jupyter", hostfs=""):
    result = JupyterService()
    result._settings = SimpleNamespace(
        jupyter_workspaces_host_root=host_root,
        jupyter_workspaces_storage_root=str(tmp_path.resolve()),
        dataset_docker_host_root=hostfs,
    )
    return result


def owned_volume(options, **labels):
    return SimpleNamespace(attrs={
        "Labels": {"ai-dataseek.session_id": "session-a", "ai-dataseek.kind": "jupyter-work", **labels},
        "Driver": "local", "Options": options,
    }, remove=Mock())


def test_new_host_workspace_is_private_and_snap_mapped(tmp_path):
    notebook = service(tmp_path, hostfs="/var/lib/snapd/hostfs")
    volumes = Volumes()
    notebook._ensure_work_volume(SimpleNamespace(volumes=volumes), "session-a", "user-a")
    assert (tmp_path / "session-a").is_dir()
    assert json.loads((tmp_path / ".owner-session-a.json").read_text()) == {
        "session_id": "session-a", "user_id": "user-a"}
    assert volumes.created[0]["driver_opts"] == {
        "type": "none", "o": "bind", "device": "/var/lib/snapd/hostfs/data2/state/jupyter/session-a"}
    assert volumes.created[0]["name"] == "ai-dataseek-jupyter-work-session-a"
    assert volumes.created[0]["labels"]["ai-dataseek.user_id"] == "user-a"


def test_default_mode_does_not_touch_host_storage(tmp_path):
    notebook = service(tmp_path, host_root="")
    volumes = Volumes()
    notebook._ensure_work_volume(SimpleNamespace(volumes=volumes), "session-a", "user-a")
    assert "driver_opts" not in volumes.created[0]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("session", ["../escape", "a/b", "/root", "..", "", "a\n"])
def test_invalid_session_cannot_make_directory_or_volume(tmp_path, session):
    volumes = Volumes()
    with pytest.raises(RuntimeError, match="session identity"):
        service(tmp_path)._ensure_work_volume(SimpleNamespace(volumes=volumes), session, "user-a")
    assert not volumes.created
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("options", [{"type": "none", "o": "bind", "device": "/old/session-a"}])
def test_existing_same_name_volume_must_actually_be_migrated(tmp_path, options):
    volumes = Volumes(owned_volume(options))
    with pytest.raises(RuntimeError, match="storage mismatch"):
        service(tmp_path)._ensure_work_volume(SimpleNamespace(volumes=volumes), "session-a", "user-a")
    assert list(tmp_path.iterdir()) == []
    assert not volumes.created


def test_valid_migrated_workspace_preserves_notebook_and_adopts_owner_record(tmp_path):
    notebook = service(tmp_path)
    directory = tmp_path / "session-a"
    directory.mkdir()
    (directory / "DataSeek.ipynb").write_text("saved notebook")
    volumes = Volumes(owned_volume(notebook._workspace_options("session-a")))
    notebook._ensure_work_volume(SimpleNamespace(volumes=volumes), "session-a", "user-a")
    assert (directory / "DataSeek.ipynb").read_text() == "saved notebook"
    assert not volumes.created


def test_unowned_existing_directory_is_not_adopted(tmp_path):
    (tmp_path / "session-a").mkdir()
    with pytest.raises(RuntimeError, match="Unowned"):
        service(tmp_path)._ensure_work_volume(SimpleNamespace(volumes=Volumes()), "session-a", "user-a")


def test_archived_workspace_reopens_only_for_original_user(tmp_path):
    notebook = service(tmp_path)
    notebook._ensure_work_volume(SimpleNamespace(volumes=Volumes()), "session-a", "user-a")
    saved = tmp_path / "session-a" / "saved.txt"
    saved.write_text("preserved")
    with pytest.raises(RuntimeError, match="identity mismatch"):
        notebook._ensure_work_volume(SimpleNamespace(volumes=Volumes()), "session-a", "user-b")
    notebook._ensure_work_volume(SimpleNamespace(volumes=Volumes()), "session-a", "user-a")
    assert saved.read_text() == "preserved"


@pytest.mark.parametrize("location", ["root", "session", "owner"])
def test_symlinks_cannot_escape_workspace_root_or_leak_paths(tmp_path, location):
    external = tmp_path / "external"
    external.mkdir()
    root = tmp_path / "workspaces"
    if location == "root":
        root.symlink_to(external, target_is_directory=True)
    else:
        root.mkdir()
        if location == "session":
            (root / "session-a").symlink_to(external, target_is_directory=True)
        else:
            (external / "owner.json").write_text(json.dumps({"session_id": "session-a", "user_id": "user-a"}))
            (root / ".owner-session-a.json").symlink_to(external / "owner.json")
    notebook = service(root)
    # Preserve the requested symlink spelling to verify each ancestor is checked.
    notebook._settings.jupyter_workspaces_storage_root = str(root)
    volumes = Volumes(owned_volume(notebook._workspace_options("session-a")))
    with pytest.raises(RuntimeError) as error:
        notebook._ensure_work_volume(SimpleNamespace(volumes=volumes), "session-a", "user-a")
    assert str(tmp_path) not in str(error.value)
    assert not (external / "session-a").exists()
    assert not volumes.created


def test_successful_create_does_not_adopt_racing_foreign_volume(tmp_path):
    volumes = Volumes()
    volumes.create = Mock(return_value=owned_volume(None, **{"ai-dataseek.session_id": "someone-else"}))
    with pytest.raises(RuntimeError, match="identity mismatch"):
        service(tmp_path, host_root="")._ensure_work_volume(SimpleNamespace(volumes=volumes), "session-a", "user-a")


@pytest.mark.asyncio
async def test_cleanup_retains_host_archive_and_never_follows_child_symlink(tmp_path, monkeypatch):
    notebook = service(tmp_path)
    volumes = Volumes()
    notebook._ensure_work_volume(SimpleNamespace(volumes=volumes), "session-a", "user-a")
    archive = tmp_path / "session-a" / "DataSeek.ipynb"
    archive.write_text("saved")
    other = tmp_path / "unrelated"
    other.mkdir()
    (tmp_path / "session-a" / "link").symlink_to(other, target_is_directory=True)
    document = SimpleNamespace(container_name=notebook._name("session-a"), work_volume=notebook._volume("session-a"), delete=AsyncMock())
    monkeypatch.setattr(module, "JupyterSessionDocument", SimpleNamespace(
        session_id="session_id", user_id="user_id", find_one=AsyncMock(return_value=document)))
    client = SimpleNamespace(volumes=volumes, containers=SimpleNamespace(get=Mock(side_effect=docker.errors.NotFound("missing"))), close=Mock())
    monkeypatch.setattr(module.docker, "from_env", lambda **kwargs: client)
    await notebook.delete(session_id="session-a", user_id="user-a")
    volumes.volume.remove.assert_called_once_with(force=True)
    assert archive.read_text() == "saved"
    assert other.is_dir()
    assert (tmp_path / ".owner-session-a.json").is_file()


def test_enabling_host_archives_preserves_existing_named_volume(tmp_path):
    notebook = service(tmp_path)
    original = owned_volume(None)
    volumes = Volumes(original)
    notebook._ensure_work_volume(SimpleNamespace(volumes=volumes), "session-a", "user-a")
    assert volumes.volume is original and not volumes.created
    original.remove.assert_not_called()
    assert list(tmp_path.iterdir()) == []  # No empty host directory replaces saved work.


@pytest.mark.asyncio
@pytest.mark.parametrize("host_backed", [False, True])
async def test_idle_reaping_retains_workspace_and_reopen_uses_same_storage(tmp_path, monkeypatch, host_backed):
    notebook = service(tmp_path, host_root="/data2/state/jupyter" if host_backed else "")
    notebook._settings.jupyter_ttl_minutes = 60
    volumes = Volumes()
    notebook._ensure_work_volume(SimpleNamespace(volumes=volumes), "session-a", "user-a")
    original = volumes.volume
    if host_backed:
        (tmp_path / "session-a" / "DataSeek.ipynb").write_text("saved notebook")
    document = SimpleNamespace(session_id="session-a", user_id="user-a", container_name=notebook._name("session-a"),
        work_volume=notebook._volume("session-a"), delete=AsyncMock())
    from datetime import datetime, UTC
    model = SimpleNamespace(session_id="session_id", user_id="user_id", last_used_at=datetime.now(UTC),
        find=Mock(return_value=SimpleNamespace(to_list=AsyncMock(return_value=[document]))),
        find_one=AsyncMock(return_value=document))
    monkeypatch.setattr(module, "JupyterSessionDocument", model)
    client = SimpleNamespace(volumes=volumes, containers=SimpleNamespace(get=Mock(side_effect=docker.errors.NotFound("missing"))), close=Mock())
    monkeypatch.setattr(module.docker, "from_env", lambda **kwargs: client)
    assert await notebook.reap_idle() == 1
    original.remove.assert_not_called()
    document.delete.assert_awaited_once()
    notebook._ensure_work_volume(client, "session-a", "user-a")
    assert volumes.volume is original and len(volumes.created) == 1
    if host_backed:
        assert (tmp_path / "session-a" / "DataSeek.ipynb").read_text() == "saved notebook"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["disabled", "deleted", "foreign-session", "sandbox-changed"])
async def test_owner_check_rejects_revoked_or_rebound_session_before_docker(monkeypatch, case):
    from app.application.errors.exceptions import NotFoundError
    notebook = JupyterService()
    session = SimpleNamespace(user_id="foreign" if case == "foreign-session" else "user-a", sandbox_id="sandbox-a")
    user = None if case == "deleted" else SimpleNamespace(is_active=case != "disabled")
    monkeypatch.setattr(module, "SessionDocument", SimpleNamespace(find_one=AsyncMock(return_value=session)))
    monkeypatch.setattr(module, "UserDocument", SimpleNamespace(find_one=AsyncMock(return_value=user)))
    docker_factory = Mock(side_effect=AssertionError("Unowned request must not reach Docker"))
    monkeypatch.setattr(module.docker, "from_env", docker_factory)
    with pytest.raises(NotFoundError):
        await notebook.open_notebook(session_id="session-a", user_id="user-a", code="print(1)", language="python",
            sandbox_id="different" if case == "sandbox-changed" else "sandbox-a")
    docker_factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["valid", "foreign-user", "wrong-workspace", "writable-dataset",
    "valid-input", "writable-input", "input-volume", "input-root", "old-layout"])
async def test_runtime_reuse_rechecks_volume_container_and_readonly_mounts(tmp_path, monkeypatch, case):
    notebook = service(tmp_path, host_root="")
    volume_name = notebook._volume("session-a")
    volumes = Volumes(owned_volume(None, **{"ai-dataseek.user_id": "user-a"}))
    container = SimpleNamespace(labels={"ai-dataseek.session_id": "session-a", "ai-dataseek.user_id": "other" if case == "foreign-user" else "user-a",
        "ai-dataseek.kind": "jupyter", "ai-dataseek.jupyter_proxy": "v1",
        "ai-dataseek.jupyter_layout": "visible-datasets-v1" if case == "old-layout" else "visible-inputs-v2",
        "ai-dataseek.sandbox_id": "sandbox-a"}, attrs={"Mounts": [
            {"Destination": "/home/jovyan/work", "Type": "volume", "Name": "foreign-volume" if case == "wrong-workspace" else volume_name},
            {"Destination": "/home/ubuntu/datasets/input", "Type": "bind", "RW": case == "writable-dataset"}]},
        reload=Mock(), status="running", start=Mock())
    if "input" in case:
        container.attrs["Mounts"].append({
            "Destination": "/home/ubuntu/inputs" if case == "input-root" else "/home/ubuntu/inputs/" + "a" * 24 + "/data.csv",
            "Source": "/validated-object/data.csv", "Type": "volume" if case == "input-volume" else "bind",
            "RW": case == "writable-input",
        })
    client = SimpleNamespace(containers=SimpleNamespace(get=Mock(return_value=container)), volumes=volumes, close=Mock())
    monkeypatch.setattr(module.docker, "from_env", lambda **kwargs: client)
    document = SimpleNamespace(session_id="session-a", user_id="user-a", container_name=notebook._name("session-a"), work_volume=volume_name)
    if case == "old-layout":
        assert not await notebook._ensure_running(document, sandbox_id="sandbox-a")
    elif case in {"valid", "valid-input"}:
        assert await notebook._ensure_running(document, sandbox_id="sandbox-a")
    else:
        with pytest.raises(RuntimeError):
            await notebook._ensure_running(document, sandbox_id="sandbox-a")
    container.start.assert_not_called()
    client.close.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["valid", "writable", "traversal", "unsupported", "valid-input",
    "writable-input", "input-volume", "input-root", "input-namespace", "input-extra",
    "input-traversal", "input-control", "input-backslash", "input-unsafe-source"])
async def test_create_copies_only_current_readonly_sandbox_mounts(tmp_path, monkeypatch, case):
    notebook = service(tmp_path, host_root="")
    for name, value in {"jupyter_image": "test-image", "sandbox_network": "isolated-test", "jupyter_memory_limit": "2g",
                        "jupyter_nano_cpus": 2_000_000_000, "jupyter_pids_limit": 256, "jupyter_network_disabled": False}.items():
        setattr(notebook._settings, name, value)
    notebook._authorize = AsyncMock()
    destination = "/home/ubuntu/datasets/source/../escape" if case == "traversal" else "/home/ubuntu/datasets/source/data.csv"
    if "input" in case:
        destination = "/home/ubuntu/inputs/" + "a" * 24 + "/data.csv"
        destination = {
            "input-root": "/home/ubuntu/inputs",
            "input-namespace": "/home/ubuntu/inputs/arbitrary/data.csv",
            "input-extra": destination + "/other",
            "input-traversal": destination.rsplit("/", 1)[0] + "/..",
            "input-control": destination + "\n",
            "input-backslash": destination + "\\escape",
        }.get(case, destination)
    sandbox = SimpleNamespace(reload=Mock(), attrs={"Mounts": [{"Destination": destination,
        "Source": "relative/path" if case == "input-unsafe-source" else "/validated-host/data.csv",
        "Type": "tmpfs" if case == "unsupported" else "volume" if case == "input-volume" else "bind",
        "RW": case in {"writable", "writable-input"}}]})
    def get(name):
        if name == "sandbox-a":
            return sandbox
        raise docker.errors.NotFound("no prior runtime")
    containers = SimpleNamespace(get=Mock(side_effect=get), run=Mock())
    client = SimpleNamespace(containers=containers, volumes=Volumes(), close=Mock())
    monkeypatch.setattr(module.docker, "from_env", lambda **kwargs: client)
    class Document(SimpleNamespace):
        async def insert(self):
            self.inserted = True
    monkeypatch.setattr(module, "JupyterSessionDocument", Document)
    if case not in {"valid", "valid-input"}:
        with pytest.raises(RuntimeError, match="read-only|source"):
            await notebook._create("session-a", "user-a", "sandbox-a")
        containers.run.assert_not_called()
        return
    document = await notebook._create("session-a", "user-a", "sandbox-a")
    notebook._authorize.assert_awaited_once_with("session-a", "user-a", sandbox_id="sandbox-a")
    assert document.inserted and document.work_volume == notebook._volume("session-a")
    initialization, runtime = [item.kwargs for item in containers.run.call_args_list]
    assert len(initialization["mounts"]) == 1  # Root chown never includes read-only source data.
    assert runtime["labels"]["ai-dataseek.user_id"] == "user-a"
    assert runtime["labels"]["ai-dataseek.jupyter_layout"] == "visible-inputs-v2"
    assert runtime["network"] == "isolated-test"
    datasets = runtime["mounts"][1:]
    assert len(datasets) == 2 and all(item["ReadOnly"] for item in datasets)
    assert {item["Target"] for item in datasets} == {destination, "/home/jovyan/work/datasets/data.csv"}
    assert all(item["Source"] == "/validated-host/data.csv" for item in datasets)


@pytest.mark.asyncio
@pytest.mark.parametrize("active", [False, True])
async def test_proxy_rechecks_account_and_current_sandbox_before_releasing_token(monkeypatch, active):
    from app.application.errors.exceptions import NotFoundError
    notebook = JupyterService()
    monkeypatch.setattr(module, "SessionDocument", SimpleNamespace(find_one=AsyncMock(
        return_value=SimpleNamespace(user_id="user-a", sandbox_id="current-sandbox"))))
    monkeypatch.setattr(module, "UserDocument", SimpleNamespace(find_one=AsyncMock(
        return_value=SimpleNamespace(is_active=active))))
    document = SimpleNamespace(container_name=notebook._name("session-a"), token="private-token", save=AsyncMock())
    model = SimpleNamespace(session_id="session_id", user_id="user_id", find_one=AsyncMock(return_value=document))
    monkeypatch.setattr(module, "JupyterSessionDocument", model)
    notebook._ensure_running = AsyncMock(return_value=True)
    if active:
        origin, token = await notebook.proxy_target(session_id="session-a", user_id="user-a")
        assert origin == f"http://{notebook._name('session-a')}:8888" and token == "private-token"
        notebook._ensure_running.assert_awaited_once_with(document, sandbox_id="current-sandbox")
    else:
        with pytest.raises(NotFoundError):
            await notebook.proxy_target(session_id="session-a", user_id="user-a")
        model.find_one.assert_not_awaited()
        notebook._ensure_running.assert_not_awaited()
        document.save.assert_not_awaited()
