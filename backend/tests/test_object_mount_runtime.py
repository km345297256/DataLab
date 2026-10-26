"""Object inputs stay readonly scoped mounts; no hydration/copy fallback."""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.domain.models.analysis_input import assign_upload_namespace, upload_runtime_path
from app.domain.models.file import FileInfo
from app.domain.models.object_file import ObjectFileMountError
from app.domain.models.execution_node import ExecutionNodeType
from app.domain.models.session import Session
from app.infrastructure.external.sandbox import object_mount as module
from app.infrastructure.external.sandbox import runtime


def selected_files():
    return assign_upload_namespace([
        FileInfo(file_id="minio:first", filename="example.h5", size=4_500_000_000),
        FileInfo(file_id="minio:second", filename="notes.csv", size=12),
    ])


def settings():
    return SimpleNamespace(
        object_mount_image="rclone@sha256:" + "a" * 64,
        object_mount_backend_url="http://backend:8000",
        object_mount_start_timeout_seconds=1, object_mount_host_root="/var/lib/datalab/object-mounts",
        sandbox_image="sandbox",
        sandbox_name_prefix="datalab-sandbox", sandbox_network="datalab-network",
    )


def helper_client():
    helper = SimpleNamespace(
        name="private-helper", attrs={"State": {"Running": True, "Pid": 1234}},
        reload=Mock(), remove=Mock(), stop=Mock(), exec_run=Mock(return_value=SimpleNamespace(exit_code=0)),
    )
    client = SimpleNamespace(containers=SimpleNamespace(run=Mock(return_value=helper)))
    return client, helper


def test_helper_uses_scoped_http_grant_no_disk_cache_or_storage_credentials(monkeypatch):
    monkeypatch.setattr(module, "canonical_host_source", lambda *args, **kw: kw["source"])
    client, helper = helper_client()
    files = selected_files()
    spec = module.ObjectMountSpec("g" * 32, "private-token", tuple(files))
    handle = module.create_object_mount(client, sandbox_name="datalab-sandbox-example", spec=spec, settings=settings())
    arguments = client.containers.run.call_args.kwargs
    assert "private-token" not in repr(spec)
    assert "private-token" not in repr(arguments["command"])
    assert "private-token" not in repr(arguments["labels"])
    assert arguments["environment"] == {
        "RCLONE_CONFIG_SELECTED_TYPE": "http",
        "RCLONE_CONFIG_SELECTED_URL": "http://backend:8000/api/v1/internal/object-mounts/" + "g" * 32 + "/",
        "RCLONE_CONFIG_SELECTED_HEADERS": "Authorization,Bearer private-token",
    }
    command = arguments["command"]
    assert command[command.index("--vfs-cache-mode") + 1] == "off"
    assert command[command.index("--buffer-size") + 1] == "0"
    assert command[command.index("--vfs-read-chunk-size-limit") + 1] == "4M"
    assert "--read-only" in command and "--no-seek" not in command
    assert arguments["read_only"] and arguments["cap_drop"] == ["ALL"]
    assert arguments["cap_add"] == ["SYS_ADMIN"]
    assert arguments["devices"] == ["/dev/fuse:/dev/fuse:rwm"]
    assert "mounts" not in arguments
    host_source = next(iter(arguments["volumes"]))
    assert host_source.startswith("/var/lib/datalab/object-mounts/datalab-object-mount-")
    assert arguments["volumes"][host_source] == {"bind": "/data", "mode": "rshared"}
    assert set(arguments["tmpfs"]) == {"/tmp"}
    assert handle.signature == module.file_input_signature(files)
    assert {item.target for item in handle.mounts} == {upload_runtime_path(info) for info in files}
    assert all(item.source.startswith(host_source + "/") for item in handle.mounts)
    assert "source" not in repr(handle)
    helper.remove.assert_not_called()


def test_signature_is_order_independent_and_changes_with_file_binding():
    files = selected_files()
    assert module.file_input_signature(files) == module.file_input_signature(list(reversed(files)))
    assert module.file_input_signature(files) != module.file_input_signature(files[:1])
    assert module.file_input_signature([]) is None
    changed = [files[0].model_copy(update={"size": files[0].size + 1}), files[1]]
    assert module.file_input_signature(changed) != module.file_input_signature(files)


def test_duplicate_target_rejected_before_creating_helper():
    client, _ = helper_client()
    files = selected_files()
    with pytest.raises(ObjectFileMountError):
        module.create_object_mount(client, sandbox_name="test", spec=module.ObjectMountSpec("g" * 32, "token", (files[0], files[0])), settings=settings())
    client.containers.run.assert_not_called()


def test_mount_root_symlink_is_rejected_without_changing_or_copying_source(monkeypatch):
    client, helper = helper_client()
    monkeypatch.setattr(module, "canonical_host_source", lambda *args, **kw: "/private/other-root")
    with pytest.raises(ObjectFileMountError):
        module.create_object_mount(client, sandbox_name="test", spec=module.ObjectMountSpec("g" * 32, "token", tuple(selected_files())), settings=settings())
    helper.stop.assert_called_once_with(timeout=10)
    helper.remove.assert_called_once_with(force=True)


def test_helper_unreadable_input_retires_helper_and_hides_gateway_details(monkeypatch):
    monkeypatch.setattr(module, "canonical_host_source", lambda *args, **kw: kw["source"])
    client, helper = helper_client()
    helper.exec_run.side_effect = [SimpleNamespace(exit_code=0), SimpleNamespace(exit_code=1)]
    with pytest.raises(ObjectFileMountError) as error:
        module.create_object_mount(client, sandbox_name="test", spec=module.ObjectMountSpec("g" * 32, "private-token", tuple(selected_files())), settings=settings())
    helper.remove.assert_called_once_with(force=True)
    assert "private-token" not in str(error.value) and "backend" not in str(error.value)


def restore_fixture():
    files = selected_files()
    signature = module.input_mount_signature(files)
    helper_labels = {module.LABEL_PREFIX + "sandbox": "sandbox-one",
                     module.LABEL_PREFIX + "grant": "g" * 32,
                     module.LABEL_PREFIX + "signature": signature}
    helper = SimpleNamespace(attrs={"State": {"Running": True, "Pid": 1234},
                                   "Config": {"Labels": helper_labels, "Image": settings().object_mount_image},
                                   "Mounts": [{"Destination": "/data", "Type": "bind", "Propagation": "rshared", "Source": "/var/lib/datalab/object-mounts/helper-one"}]}, reload=Mock(),
                             exec_run=Mock(return_value=SimpleNamespace(exit_code=0, output=b"12\n")))
    container = SimpleNamespace(name="sandbox-one", attrs={
        "Config": {"Labels": {module.LABEL_PREFIX + "helper": "helper-one",
                               module.LABEL_PREFIX + "grant": "g" * 32,
                               module.LABEL_PREFIX + "signature": signature}},
        "Mounts": [{"Type": "bind", "RW": False, "Destination": upload_runtime_path(info),
                    "Source": "/var/lib/datalab/object-mounts/helper-one/" + upload_runtime_path(info).split("/inputs/", 1)[1]} for info in files],
    })
    client = SimpleNamespace(containers=SimpleNamespace(get=Mock(return_value=helper)))
    return client, helper, container


@pytest.mark.parametrize("fault", ["writable", "wrong_source", "wrong_owner", "stopped", "image"])
def test_restore_rejects_untrusted_changed_or_missing_mount_identity(fault):
    client, helper, container = restore_fixture()
    if fault == "writable":
        container.attrs["Mounts"][0]["RW"] = True
    elif fault == "wrong_source":
        container.attrs["Mounts"][0]["Source"] = "/private/other-data"
    elif fault == "wrong_owner":
        helper.attrs["Config"]["Labels"][module.LABEL_PREFIX + "sandbox"] = "another-sandbox"
    elif fault == "stopped":
        helper.attrs["State"]["Running"] = False
    else:
        helper.attrs["Config"]["Image"] = "untrusted-helper"
    with pytest.raises(ObjectFileMountError):
        module.restore_object_mount(client, container=container, settings=settings())


def test_restore_accepts_original_exact_readonly_bindings():
    client, _, container = restore_fixture()
    handle = module.restore_object_mount(client, container=container, settings=settings())
    assert len(handle.mounts) == 2 and handle.grant_id == "g" * 32


def test_restore_checks_live_fuse_before_accepting_labels_and_bindings():
    client, helper, container = restore_fixture()
    helper.exec_run.return_value = SimpleNamespace(exit_code=1)
    with pytest.raises(module.ObjectMountIdentityError):
        module.restore_object_mount(client, container=container, settings=settings())


def test_restore_gateway_outage_is_not_classified_as_lost_mount_identity():
    client, helper, container = restore_fixture()
    helper.exec_run.side_effect = [SimpleNamespace(exit_code=0), SimpleNamespace(exit_code=1)]
    with pytest.raises(ObjectFileMountError) as error:
        module.restore_object_mount(client, container=container, settings=settings())
    assert not isinstance(error.value, module.ObjectMountIdentityError)


def test_retirement_stops_notebooks_then_helper_without_touching_work_volumes():
    events = []
    helper = SimpleNamespace(attrs={"Config": {"Labels": {
        module.LABEL_PREFIX + "sandbox": "sandbox-one", module.LABEL_PREFIX + "grant": "g" * 32,
    }}}, remove=lambda **kw: events.append(("helper", kw)), stop=lambda **kw: events.append(("stop", kw)))
    notebook = SimpleNamespace(remove=lambda **kw: events.append(("notebook", kw)))
    client = SimpleNamespace(containers=SimpleNamespace(list=Mock(side_effect=[[helper], [notebook]])))
    assert module.remove_object_mount_helpers(client, sandbox_name="sandbox-one") == ["g" * 32]
    assert events == [("notebook", {"force": True}), ("stop", {"timeout": 10}), ("helper", {"force": True})]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [ExecutionNodeType.REMOTE_DOCKER, ExecutionNodeType.WORKER_AGENT])
async def test_unsupported_execution_node_never_transfers_or_allocates_input(kind, monkeypatch):
    node = SimpleNamespace(type=kind, base_url="https://worker.invalid", node_id="remote", runtime_config={})
    create = AsyncMock()
    monkeypatch.setattr(runtime.DockerSandbox, "create", create)
    with pytest.raises(ObjectFileMountError):
        await runtime.LocalDockerRuntime()._allocate_on_node(node, Session(id="s", user_id="u", agent_id="a"), input_files=selected_files())
    create.assert_not_called()


@pytest.mark.asyncio
async def test_anonymous_mount_request_rejected_before_node_selection(monkeypatch):
    select = AsyncMock()
    monkeypatch.setattr(runtime, "_list_execution_node_candidates", select)
    with pytest.raises(ObjectFileMountError):
        await runtime.LocalDockerRuntime().allocate(input_files=selected_files())
    select.assert_not_called()


@pytest.mark.asyncio
async def test_confirmed_missing_mount_is_retired_before_replacement(monkeypatch):
    from app.domain.external.sandbox_runtime import SandboxNotFoundError
    monkeypatch.setattr(runtime.DockerSandbox, "get", AsyncMock(side_effect=module.ObjectMountIdentityError(module.MOUNT_ERROR)))
    retired = []

    async def destroy(self):
        retired.append(self.id)
        await self.client.aclose()
        return True

    monkeypatch.setattr(runtime.DockerSandbox, "destroy", destroy)
    with pytest.raises(SandboxNotFoundError):
        await runtime._restore_local_sandbox("owned-sandbox")
    assert retired == ["owned-sandbox"]


@pytest.mark.asyncio
async def test_transient_mount_inspection_failure_does_not_discard_analysis_work(monkeypatch):
    monkeypatch.setattr(runtime.DockerSandbox, "get", AsyncMock(side_effect=ObjectFileMountError(module.MOUNT_ERROR)))
    destroy = AsyncMock()
    monkeypatch.setattr(runtime.DockerSandbox, "destroy", destroy)
    with pytest.raises(ObjectFileMountError):
        await runtime._restore_local_sandbox("owned-sandbox")
    destroy.assert_not_called()


@pytest.mark.asyncio
async def test_local_allocation_issues_owned_grant_and_revokes_if_creation_fails(monkeypatch):
    from app.application.services import object_mount_service
    node = SimpleNamespace(type=ExecutionNodeType.LOCAL_DOCKER, node_id="local-default", runtime_config={})
    files = selected_files()
    service = SimpleNamespace(issue=AsyncMock(return_value=SimpleNamespace(grant_id="a" * 32, token="secret")), revoke=AsyncMock())
    monkeypatch.setattr(object_mount_service, "get_object_mount_service", lambda: service)
    create = AsyncMock(side_effect=ObjectFileMountError(module.MOUNT_ERROR))
    monkeypatch.setattr(runtime.DockerSandbox, "create", create)
    with pytest.raises(ObjectFileMountError):
        await runtime.LocalDockerRuntime()._allocate_on_node(node, Session(id="session", user_id="owner", agent_id="agent"), input_files=files)
    service.issue.assert_awaited_once_with("session", "owner", files)
    service.revoke.assert_awaited_once_with("a" * 32)
    spec = create.call_args.kwargs["object_mount"]
    assert spec.files == tuple(files) and spec.grant_id == "a" * 32


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["active", "expired", "redis_unavailable"])
async def test_restore_checks_grant_before_blocking_fuse_stat(monkeypatch, state):
    from app.application.services import object_mount_service
    from app.infrastructure.external.sandbox import docker_sandbox
    from app.infrastructure.external.sandbox.object_mount import ObjectMountHandle
    config = settings()
    config.sandbox_address = None
    config.sandbox_isolation = "session"
    config.sandbox_docker_create_timeout_seconds = 30
    container = SimpleNamespace(name="sandbox-one", reload=Mock(), attrs={
        "Config": {"Labels": {module.LABEL_PREFIX + "helper": "helper-one", module.LABEL_PREFIX + "grant": "a" * 32}},
        "NetworkSettings": {"IPAddress": "127.0.0.1"},
    })
    client = SimpleNamespace(containers=SimpleNamespace(get=Mock(return_value=container)), close=Mock())
    active = AsyncMock(return_value=state == "active")
    if state == "redis_unavailable":
        active.side_effect = ObjectFileMountError("unavailable")
    monkeypatch.setattr(object_mount_service, "get_object_mount_service", lambda: SimpleNamespace(is_active=active))
    monkeypatch.setattr(docker_sandbox, "get_settings", lambda: config)
    monkeypatch.setattr(docker_sandbox.docker, "from_env", lambda: client)
    probe = Mock(return_value=ObjectMountHandle("helper-one", "a" * 32, "b" * 64))
    monkeypatch.setattr(docker_sandbox, "restore_object_mount", probe)
    if state == "active":
        restored = await docker_sandbox.DockerSandbox.get("sandbox-one")
        try:
            probe.assert_called_once()
        finally:
            await restored.client.aclose()
    else:
        with pytest.raises(ObjectFileMountError) as error:
            await docker_sandbox.DockerSandbox.get("sandbox-one")
        assert isinstance(error.value, module.ObjectMountIdentityError) is (state == "expired")
        probe.assert_not_called()
    active.assert_awaited_once_with("a" * 32)
    client.close.assert_called_once()


@pytest.mark.asyncio
async def test_restore_fuse_probe_keeps_gateway_event_loop_available(monkeypatch):
    from app.application.services import object_mount_service
    from app.infrastructure.external.sandbox import docker_sandbox
    config = settings()
    config.sandbox_address = None
    config.sandbox_isolation = "session"
    container = SimpleNamespace(name="sandbox-one", reload=Mock(), attrs={
        "Config": {"Labels": {module.LABEL_PREFIX + "helper": "helper-one", module.LABEL_PREFIX + "grant": "a" * 32}},
        "NetworkSettings": {"IPAddress": "127.0.0.1"},
    })
    client = SimpleNamespace(containers=SimpleNamespace(get=Mock(return_value=container)), close=Mock())
    monkeypatch.setattr(object_mount_service, "get_object_mount_service", lambda: SimpleNamespace(is_active=AsyncMock(return_value=True)))
    monkeypatch.setattr(docker_sandbox, "get_settings", lambda: config)
    monkeypatch.setattr(docker_sandbox.docker, "from_env", lambda: client)
    started, gateway_response = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()

    def probe(*args, **kwargs):
        assert threading.get_ident() != loop_thread
        started.set()
        assert gateway_response.wait(1), "FUSE blocked the event loop needed to serve its gateway request"
        return module.ObjectMountHandle("helper-one", "a" * 32, "b" * 64)

    async def serve_gateway():
        while not started.is_set():
            await asyncio.sleep(0.001)
        gateway_response.set()

    monkeypatch.setattr(docker_sandbox, "restore_object_mount", probe)
    heartbeat = asyncio.create_task(serve_gateway())
    try:
        restored = await docker_sandbox.DockerSandbox.get("sandbox-one")
        await restored.client.aclose()
        await heartbeat
        assert gateway_response.is_set()
    finally:
        gateway_response.set()
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
