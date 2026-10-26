"""Private, cache-free object mounts owned by one analysis sandbox.

Only a scoped HTTP grant enters the FUSE helper. Analysis containers receive
read-only binds to the selected files, never credentials or a bucket mount.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import asyncio
import hashlib
import json
from pathlib import PurePosixPath
import re
import time
from typing import Sequence
from urllib.parse import urlsplit
import uuid

import docker

from app.domain.models.analysis_input import UPLOAD_INPUT_ROOT, upload_runtime_path
from app.domain.models.file import FileInfo
from app.domain.models.object_file import ObjectFileMountError
from app.infrastructure.external.sandbox.dataset_mount_validator import canonical_host_source

LABEL_PREFIX = "io.datalab.object-mount."
MOUNT_ERROR = "分析文件的只读对象挂载不可用，请检查对象存储挂载服务后重试。"
_GRANT_ID = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


class ObjectMountIdentityError(ObjectFileMountError):
    """A confirmed invalid/missing mount identity, distinct from a transient IO failure."""


@dataclass(frozen=True, slots=True)
class ObjectInputMount:
    source: str = field(repr=False)
    target: str


@dataclass(frozen=True, slots=True)
class ObjectMountSpec:
    grant_id: str
    token: str = field(repr=False)
    files: tuple[FileInfo, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class ObjectMountHandle:
    helper_name: str
    grant_id: str
    signature: str
    mounts: tuple[ObjectInputMount, ...] = field(default=(), repr=False)

    @property
    def labels(self) -> dict[str, str]:
        return {
            LABEL_PREFIX + "helper": self.helper_name,
            LABEL_PREFIX + "grant": self.grant_id,
            LABEL_PREFIX + "signature": self.signature,
        }


def input_mount_signature(files: Sequence[FileInfo]) -> str:
    entries = []
    seen = set()
    for info in files:
        target = upload_runtime_path(info)
        if target in seen or not isinstance(info.size, int) or isinstance(info.size, bool) or info.size < 0:
            raise ObjectFileMountError(MOUNT_ERROR)
        seen.add(target)
        entries.append((str(info.file_id), target, info.size))
    return hashlib.sha256(json.dumps(sorted(entries), ensure_ascii=False).encode()).hexdigest()


def file_input_signature(files: Sequence[FileInfo] | None) -> str | None:
    """Session binding identity; an empty selection requires no object mount."""
    return input_mount_signature(files) if files else None


def _relative_path(info: FileInfo) -> str:
    return str(PurePosixPath(upload_runtime_path(info)).relative_to(UPLOAD_INPUT_ROOT))


def _host_root(settings) -> str:
    root = settings.object_mount_host_root
    path = PurePosixPath(root)
    if (not isinstance(root, str) or not path.is_absolute() or len(path.parts) < 4 or path.name != "object-mounts"
            or str(path) != root or ".." in path.parts or "\\" in root
            or any(ord(c) < 32 or ord(c) == 127 for c in root)
            or path == PurePosixPath(UPLOAD_INPUT_ROOT) or PurePosixPath(UPLOAD_INPUT_ROOT) in path.parents):
        raise ObjectFileMountError(MOUNT_ERROR)
    return root


def create_object_mount(client, *, sandbox_name: str, spec: ObjectMountSpec, settings) -> ObjectMountHandle:
    """Start one minimal FUSE process; no source bytes are persisted locally."""
    helper = None
    helper_name = None
    try:
        if not spec.files or not _GRANT_ID.fullmatch(spec.grant_id) or not spec.token or any(ord(c) < 32 for c in spec.token):
            raise ValueError
        signature = input_mount_signature(spec.files)
        base = settings.object_mount_backend_url.rstrip("/")
        parsed = urlsplit(base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError
        prefix = (settings.sandbox_name_prefix or "datalab-sandbox").removesuffix("-sandbox")
        helper_name = f"{prefix}-object-mount-{uuid.uuid4().hex[:16]}"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{1,127}", helper_name):
            raise ValueError
        host_root = _host_root(settings)
        host_source = f"{host_root}/{helper_name}"
        labels = {
            LABEL_PREFIX + "sandbox": sandbox_name,
            LABEL_PREFIX + "grant": spec.grant_id,
            LABEL_PREFIX + "signature": signature,
            "com.docker.compose.project": prefix,
            "com.docker.compose.service": "object-mount-helper",
        }
        helper = client.containers.run(
            image=settings.object_mount_image, name=helper_name, detach=True,
            command=["mount", "selected:", "/data", "--read-only", "--allow-non-empty", "--vfs-cache-mode", "off",
                     "--buffer-size", "0", "--vfs-read-chunk-size", "4M",
                     "--vfs-read-chunk-size-limit", "4M", "--vfs-read-chunk-streams", "0",
                     "--uid", "1000", "--gid", "1000", "--file-perms", "0444",
                     "--dir-perms", "0555", "--allow-other", "--config", "/dev/null",
                     "--cache-dir", "/tmp/cache", "--log-level", "ERROR"],
            environment={
                "RCLONE_CONFIG_SELECTED_TYPE": "http",
                "RCLONE_CONFIG_SELECTED_URL": f"{base}/api/v1/internal/object-mounts/{spec.grant_id}/",
                "RCLONE_CONFIG_SELECTED_HEADERS": f"Authorization,Bearer {spec.token}",
            },
            labels=labels, network=settings.sandbox_network, read_only=True,
            # Shared propagation exposes only this FUSE view in the Docker
            # daemon namespace; it never creates a data cache in this directory.
            volumes={host_source: {"bind": "/data", "mode": "rshared"}},
            cap_drop=["ALL"], cap_add=["SYS_ADMIN"], devices=["/dev/fuse:/dev/fuse:rwm"],
            security_opt=["no-new-privileges:true", "apparmor=unconfined"],
            tmpfs={"/tmp": "rw,nosuid,nodev,noexec,size=16m,mode=0700"},
            mem_limit="256m", memswap_limit="256m", pids_limit=64,
            log_config=docker.types.LogConfig(type="json-file", config={"max-size": "1m", "max-file": "1"}),
        )
        deadline = time.monotonic() + settings.object_mount_start_timeout_seconds
        while True:
            helper.reload()
            state = helper.attrs.get("State", {})
            if not state.get("Running"):
                raise ValueError
            probe = helper.exec_run(["sh", "-c", "awk '$3 == \"fuse.rclone\" {ok=1} END {exit !ok}' /proc/mounts"])
            if probe.exit_code == 0:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError
            time.sleep(0.1)
        # Check from the daemon host namespace, including every symlink, before
        # the private source is admitted to an untrusted analysis container.
        canonical = canonical_host_source(client, image=settings.sandbox_image,
                                          source=host_source, candidate_roots=[host_root])
        if canonical != host_source:
            raise ValueError
        mounts = []
        for info in spec.files:
            relative = _relative_path(info)
            probe = helper.exec_run(["sh", "-c", 'test -r "$1" && test "$(stat -c %s -- "$1")" = "$2"',
                                     "check", f"/data/{relative}", str(info.size)])
            if probe.exit_code != 0:
                raise ValueError
            mounts.append(ObjectInputMount(source=f"{host_source}/{relative}", target=upload_runtime_path(info)))
        return ObjectMountHandle(helper_name, spec.grant_id, signature, tuple(mounts))
    except Exception:
        if helper is None and helper_name is not None:
            try:
                candidate = client.containers.get(helper_name)
                labels = candidate.attrs.get("Config", {}).get("Labels", {}) or {}
                if labels.get(LABEL_PREFIX + "sandbox") == sandbox_name:
                    helper = candidate
            except docker.errors.NotFound:
                pass
        if helper is not None:
            try:
                helper.stop(timeout=10)
                helper.remove(force=True)
            except docker.errors.NotFound:
                pass
        raise ObjectFileMountError(MOUNT_ERROR) from None


def restore_object_mount(client, *, container, settings, expected_files: Sequence[FileInfo] | None = None) -> ObjectMountHandle | None:
    """Verify the original helper and every exact readonly file binding."""
    labels = container.attrs.get("Config", {}).get("Labels", {}) or {}
    helper_name = labels.get(LABEL_PREFIX + "helper")
    if not helper_name:
        if any(str(m.get("Destination", "")).startswith(UPLOAD_INPUT_ROOT + "/") for m in container.attrs.get("Mounts", [])):
            raise ObjectMountIdentityError(MOUNT_ERROR)
        return None
    try:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{1,127}", helper_name):
            raise ValueError
        helper = client.containers.get(helper_name)
        helper.reload()
        helper_labels = helper.attrs.get("Config", {}).get("Labels", {}) or {}
        grant_id = labels[LABEL_PREFIX + "grant"]
        signature = labels[LABEL_PREFIX + "signature"]
        if (not _GRANT_ID.fullmatch(grant_id) or not re.fullmatch(r"[0-9a-f]{64}", signature)
                or helper_labels.get(LABEL_PREFIX + "sandbox") != container.name
                or helper_labels.get(LABEL_PREFIX + "grant") != grant_id
                or helper_labels.get(LABEL_PREFIX + "signature") != signature
                or not helper.attrs.get("State", {}).get("Running")
                or helper.attrs.get("Config", {}).get("Image") != settings.object_mount_image):
            raise ValueError
        host_source = f"{_host_root(settings)}/{helper_name}"
        helper_mounts = helper.attrs.get("Mounts", [])
        if not any(m.get("Destination") == "/data" and m.get("Source") == host_source
                   and m.get("Type") == "bind" and m.get("Propagation") == "rshared" for m in helper_mounts):
            raise ValueError
        probe = helper.exec_run(["sh", "-c", "awk '$3 == \"fuse.rclone\" {ok=1} END {exit !ok}' /proc/mounts"])
        if probe.exit_code != 0:
            raise ValueError
        expected_sizes = {upload_runtime_path(info): info.size for info in expected_files or []}
        mounts = []
        for mount in container.attrs.get("Mounts", []):
            target = str(mount.get("Destination", ""))
            if not target.startswith(UPLOAD_INPUT_ROOT + "/"):
                continue
            relative = PurePosixPath(target).relative_to(UPLOAD_INPUT_ROOT)
            if (".." in relative.parts or str(PurePosixPath(target)) != target
                    or len(relative.parts) != 2 or not re.fullmatch(r"[0-9a-f]{24}", relative.parts[0])
                    or mount.get("RW") is not False or mount.get("Type") != "bind"
                    or mount.get("Source") != f"{host_source}/{relative}"):
                raise ValueError
            probe = helper.exec_run([
                "sh", "-c", 'test -f "$1" && test -r "$1" && stat -c %s -- "$1"',
                "check", f"/data/{relative}",
            ])
            if probe.exit_code != 0:
                # The FUSE mount exists but its backing gateway may currently
                # be unreachable; do not discard writable analysis work.
                raise ObjectFileMountError(MOUNT_ERROR)
            if target in expected_sizes:
                observed_size = probe.output.decode("ascii").strip()
                if observed_size != str(expected_sizes[target]):
                    raise ObjectFileMountError(MOUNT_ERROR)
            mounts.append(ObjectInputMount(source=mount["Source"], target=target))
        if not mounts:
            raise ValueError
        return ObjectMountHandle(helper_name, grant_id, signature, tuple(mounts))
    except (docker.errors.NotFound, ValueError, KeyError, TypeError):
        raise ObjectMountIdentityError(MOUNT_ERROR) from None
    except Exception:
        raise ObjectFileMountError(MOUNT_ERROR) from None


def remove_object_mount_helpers(client, *, sandbox_name: str) -> list[str]:
    """Retire dependent notebook runtimes, preserve their work volumes, then FUSE."""
    helpers = client.containers.list(all=True, filters={"label": LABEL_PREFIX + "sandbox=" + sandbox_name})
    if not helpers:
        return []
    notebooks = client.containers.list(all=True, filters={"label": [
        "ai-dataseek.kind=jupyter", "ai-dataseek.sandbox_id=" + sandbox_name,
    ]})
    for notebook in notebooks:
        notebook.remove(force=True)
    grants = []
    for helper in helpers:
        labels = helper.attrs.get("Config", {}).get("Labels", {}) or {}
        if labels.get(LABEL_PREFIX + "sandbox") != sandbox_name:
            raise ObjectFileMountError(MOUNT_ERROR)
        grant_id = labels.get(LABEL_PREFIX + "grant", "")
        if _GRANT_ID.fullmatch(grant_id):
            grants.append(grant_id)
        try:
            # rclone handles SIGTERM by unmounting the shared FUSE view. Stop
            # first; force-removal alone can leave a dead host mount behind.
            helper.stop(timeout=10)
            helper.remove(force=True)
        except docker.errors.NotFound:
            pass
    return grants


async def release_object_mount_helpers(client, *, sandbox_name: str, extra_grant_ids=()) -> None:
    grants = await asyncio.to_thread(remove_object_mount_helpers, client, sandbox_name=sandbox_name)
    grants = list(dict.fromkeys([*grants, *extra_grant_ids]))
    if grants:
        from app.application.services.object_mount_service import get_object_mount_service

        service = get_object_mount_service()
        for grant_id in grants:
            await service.revoke(grant_id)
