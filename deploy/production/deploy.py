#!/usr/bin/env python3
"""Check or publish the current production release without building or pulling.

Usage: ./run.sh production [--apply] [--services frontend | --stack] [--repair-frontend]
The default is a preflight check. A shared advisory lock covers the entire run;
other publishers must use this entry point too. It cannot constrain root users
who bypass it and call Docker directly. No credentials or command output escape
into diagnostics. Requires Python 3 and the existing release's ./run.sh.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

# A release snapshot contains only Compose metadata and this deploy helper.
# Its parent layout identifies the same lock when ./run.sh is re-entered.
ENTRY_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_ROOT = Path(os.environ.get("DATALAB_DEPLOY_ROOT") or (
    str(ENTRY_ROOT.parent.parent) if (ENTRY_ROOT / ".datalab-release").is_file()
    else str(ENTRY_ROOT / ".local" / "production"))).absolute()
CURRENT = DEPLOY_ROOT / "current"
RELEASES = DEPLOY_ROOT / "releases"
LEGACY_ROOTS = (Path("/root/DataLab"), Path("/root/datalab-releases"), Path("/root/datalab-current"))
LOCK = DEPLOY_ROOT / "deploy.lock"
LOCK_FD_ENV = "_DATALAB_PRODUCTION_LOCK_FD"
ORIGIN = "http://localhost:10030"
DIRECT = "http://127.0.0.1:10030"
PORT = "10030"
VOLUME_IDENTITIES = {}

KINDS = ("frontend", "backend", "sandbox", "jupyter")
INFRA_KINDS = ("mongodb", "redis", "minio")
STACK_SERVICES = (*INFRA_KINDS, "sandbox-image", "jupyter-image", "backend", "frontend")
STATE_VOLUMES = {
    "mongodb-data": "mongodb", "mongodb-config": "mongodb-config", "redis-data": "redis",
    "minio-data": "minio", "dataset-data": "datasets", "skills-data": "skills",
    "jupyter-workspaces": "jupyter", "open-data-downloads": "open-data-downloads",
}
PERSISTENT_MOUNTS = {
    "backend": {"/data/datasets": "dataset-data", "/data/skills": "skills-data", "/data/jupyter-workspaces": "jupyter-workspaces", "/data/open-data-downloads": "open-data-downloads"},
    "mongodb": {"/data/db": "mongodb-data", "/data/configdb": "mongodb-config"},
    "redis": {"/data": "redis-data"}, "minio": {"/data": "minio-data"},
}


def legacy_container_name(service):
    return "datalab-" + service + "-1"


def v2_container_name(service):
    return "DataLab" + ("" if service == "frontend" else "-" + service)


def container_names(config=None):
    """Names are presentation only; project, service and volume identity stay fixed."""
    services = (config or {}).get("services", {})
    result = {}
    for service in STACK_SERVICES:
        name = services.get(service, {}).get("container_name", legacy_container_name(service))
        require(name in {legacy_container_name(service), v2_container_name(service)},
                "Production container name is outside the approved naming scheme")
        result[service] = name
    return result


def volume_name(key):
    return VOLUME_IDENTITIES.get(key, {}).get("name", "datalab-" + key)


class DeploymentError(RuntimeError):
    """Messages are fixed operator diagnostics, never subprocess/provider text."""


def release_environment(source=None):
    """Only the private release dotenv supplies application/Compose variables."""
    source = os.environ if source is None else source
    allowed = {"PATH", "HOME", "USER", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT",
               "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH",
               "DOCKER_CONFIG", "SSH_AUTH_SOCK"}
    return {key: value for key, value in source.items() if key in allowed}


def require(condition, message):
    if not condition:
        raise DeploymentError(message)


@contextmanager
def deployment_lock(path=None):
    path = path or LOCK
    Path(path).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DeploymentError("Another production deployment holds the lock") from None
        yield fd
    finally:
        os.close(fd)


def command(arguments, *, cwd, env, timeout=180, pass_fds=()):
    if (arguments[:1] == ["docker"] and shutil.which("docker", path=env.get("PATH")) == "/snap/bin/docker"
            and os.access("/snap/docker/current/bin/docker", os.X_OK)):
        arguments = ["/snap/docker/current/bin/docker", *arguments[1:]]
    try:
        result = subprocess.run(arguments, cwd=cwd, env=env, capture_output=True,
                                text=True, timeout=timeout, check=False, pass_fds=pass_fds)
    except (OSError, subprocess.TimeoutExpired):
        raise DeploymentError("Deployment command could not complete; inspect private server logs") from None
    require(result.returncode == 0, "Deployment command failed; inspect private server logs")
    return result.stdout


def inherited_deployment_lock():
    """Require the inherited open-file description that already owns our lock."""
    message = "Production changes must use ./run.sh production; ordinary Compose changes are blocked"
    value = os.environ.get(LOCK_FD_ENV, "")
    require(value.isdecimal() and int(value) >= 3, message)
    descriptor = int(value)
    probe = None
    try:
        inherited = os.fstat(descriptor)
        probe = os.open(LOCK, os.O_RDONLY | os.O_NOFOLLOW)
        expected = os.fstat(probe)
        require((inherited.st_dev, inherited.st_ino) == (expected.st_dev, expected.st_ino), message)
        # A separate description must be unable to acquire even a shared lock:
        # this proves an exclusive lock existed before inspecting the inherited FD.
        try:
            fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            fcntl.flock(probe, fcntl.LOCK_UN)
            raise DeploymentError(message)
        # Only the very same open-file description can reassert that exclusive lock.
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise DeploymentError(message) from None
    finally:
        if probe is not None:
            os.close(probe)


def compose_query(arguments):
    """Recognize queries after Compose global flags, never infer from substrings."""
    options_with_value = {"--ansi", "--env-file", "--file", "-f", "--profile", "--project-name", "-p",
                          "--project-directory", "--parallel", "--progress"}
    index = 0
    while index < len(arguments) and arguments[index].startswith("-"):
        value = arguments[index]
        if value in {"--all-resources", "--compatibility", "--dry-run"}:
            index += 1
        elif value in options_with_value and index + 1 < len(arguments):
            index += 2
        elif "=" in value and value.split("=", 1)[0] in options_with_value:
            index += 1
        else:
            return False
    if index == len(arguments):
        return False
    action = arguments[index]
    if action not in {"config", "ps", "logs", "images", "version"}:
        return False
    if action == "config" and any(arg == "-o" or arg.startswith("-o") or arg == "--output"
                                  or arg.startswith("--output=") or arg == "--lock-image-digests"
                                  or arg.startswith("--lock-image-digests=")
                                  for arg in arguments[index + 1:]):
        return False
    return True


def check_compose_entry(directory, arguments):
    logical = Path(directory).absolute()
    root = Path(directory).resolve(strict=True)
    releases = RELEASES.resolve()
    if root == ENTRY_ROOT and CURRENT.is_symlink() and not (root / ".datalab-release").is_file():
        require(compose_query(arguments), "An active release owns this stack; use ./run.sh production")
        return
    legacy = any(logical == old or old in logical.parents or root == old or old in root.parents
                 for old in LEGACY_ROOTS)
    if legacy:
        require(compose_query(arguments), "Legacy production entry is retired; publish from /data/datalab/current")
        return
    if (logical != CURRENT and logical != RELEASES and RELEASES not in logical.parents
            and root != CURRENT.resolve() and root != releases and releases not in root.parents):
        return
    if not compose_query(arguments):
        inherited_deployment_lock()


def decoded(value):
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        raise DeploymentError("Invalid deployment metadata or Docker response") from None


def load_manifest(path, *, stack=False):
    manifest = decoded(path.read_text())
    require(isinstance(manifest, dict), "Invalid release image manifest")
    for kind in (*KINDS, *INFRA_KINDS) if stack else KINDS:
        entry = manifest.get(kind)
        require(isinstance(entry, dict), "Release image manifest is incomplete")
        reference, identity = entry.get("reference"), entry.get("id")
        require(isinstance(reference, str) and bool(reference) and not any(c.isspace() for c in reference),
                "Invalid release image reference")
        tagged = reference.rsplit("/", 1)[-1]
        require(bool(re.fullmatch(r".+@sha256:[0-9a-f]{64}", reference))
                or (":" in tagged and tagged.rsplit(":", 1)[1].lower() not in {"", "latest"}),
                "Production images must have explicit non-latest versions or digests")
        require(isinstance(identity, str) and bool(re.fullmatch(r"sha256:[0-9a-f]{64}", identity)),
                "Invalid release image identity")
    return manifest


def validate_config(config, manifest, *, stack=False):
    require(isinstance(config, dict) and config.get("name") == "datalab", "Wrong Compose project")
    services = config.get("services", {})
    container_names(config)
    require(all(isinstance(services.get(name), dict) for name in
                ("frontend", "backend", "sandbox-image", "jupyter-image")), "Missing production service")
    ports = services["frontend"].get("ports", [])
    require(len(ports) == 1 and isinstance(ports[0], dict)
            and str(ports[0].get("published")) == PORT and str(ports[0].get("target")) == "80"
            and ports[0].get("protocol", "tcp") == "tcp"
            and ports[0].get("host_ip", "0.0.0.0") in {"0.0.0.0", "::", "", "127.0.0.1", "::1"},
            "Production frontend must publish only port 10030 to container port 80")
    backend_env = services["backend"].get("environment", {})
    require(backend_env.get("AUTH_PROVIDER") == "password", "DataLab requires authenticated access")
    require(str(backend_env.get("SERVER_HOST", "")).rstrip("/") == ORIGIN, "Wrong production public URL")
    require(not services["backend"].get("ports"), "Backend must not publish an independent public port")
    for kind in KINDS:
        service = kind if kind in {"frontend", "backend"} else kind + "-image"
        require(services[service].get("image") == manifest[kind]["reference"], "Compose image differs from release manifest")
    for kind in ("sandbox", "jupyter"):
        require(backend_env.get(kind.upper() + "_IMAGE") == manifest[kind]["reference"],
                "Backend runtime image differs from release manifest")

    if stack:
        for kind in INFRA_KINDS:
            require(isinstance(services.get(kind), dict) and services[kind].get("image") == manifest[kind]["reference"],
                    "Infrastructure image differs from release manifest")
            require(not services[kind].get("ports"), "Infrastructure service must not publish a public port")


def validate_storage_config(config):
    volumes = config.get("volumes", {})
    require(isinstance(volumes, dict), "Missing persistent storage configuration")
    for key in STATE_VOLUMES:
        entry = volumes.get(key, {})
        require(isinstance(entry, dict) and entry.get("name") == volume_name(key),
                "Persistent volume identity differs from the approved release policy")
    services = config["services"]
    backend_env = services["backend"].get("environment", {})
    require(backend_env.get("DATASET_MANAGED_VOLUME") == volume_name("dataset-data"),
            "Backend dataset volume differs from release policy")
    require(backend_env.get("JUPYTER_WORKSPACES_STORAGE_ROOT") == "/data/jupyter-workspaces",
            "Invalid backend Jupyter storage mount")
    workspace_host = backend_env.get("JUPYTER_WORKSPACES_HOST_ROOT", "")
    expected_device = VOLUME_IDENTITIES.get("jupyter-workspaces", {}).get("options", {}).get("device", "")
    require(not workspace_host or workspace_host == expected_device,
            "Jupyter host workspace does not match its validated parent volume")
    for service, destinations in PERSISTENT_MOUNTS.items():
        mounts = services.get(service, {}).get("volumes", [])
        require(isinstance(mounts, list), "Missing persistent mounts")
        for destination, key in destinations.items():
            candidates = [item for item in mounts if isinstance(item, dict) and item.get("target") == destination]
            require(len(candidates) == 1 and candidates[0].get("type") == "volume"
                    and candidates[0].get("source") == key,
                    "Persistent service mounts differ from approved configuration")


def validate_volumes(volumes):
    require(isinstance(volumes, list) and len(volumes) == len(STATE_VOLUMES), "Missing persistent volume")
    expected = {volume_name(key): key for key in STATE_VOLUMES}
    seen = set()
    for volume in volumes:
        name = volume.get("Name")
        require(name in expected and name not in seen, "Unexpected persistent volume")
        seen.add(name)
        policy = VOLUME_IDENTITIES.get(expected[name], {})
        require(volume.get("Driver") == policy.get("driver", "local"), "Persistent volume driver changed")
        require((volume.get("Options") or {}) == policy.get("options", {}), "Persistent volume mount options changed")


def validate_persistent_mounts(container, service):
    mounts = container.get("Mounts", [])
    require(isinstance(mounts, list), "Missing running persistent service mounts")
    for destination, key in PERSISTENT_MOUNTS.get(service, {}).items():
        candidates = [item for item in mounts if isinstance(item, dict) and item.get("Destination") == destination]
        require(len(candidates) == 1 and candidates[0].get("Type") == "volume"
                and candidates[0].get("Name") == volume_name(key),
                "Running service does not use the approved persistent volume")


def validate_images(images, manifest, kinds=KINDS):
    require(isinstance(images, list) and len(images) == len(kinds), "Missing local release image")
    for kind, image in zip(kinds, images):
        require(isinstance(image, dict) and image.get("Id") == manifest[kind]["id"],
                "Local image identity differs from release manifest")


def validate_runtime(containers, manifest, release, services=("backend", "frontend"), *, names=None):
    names = names or container_names()
    require(isinstance(containers, list) and len(containers) == 2, "Expected one frontend and one backend container")
    seen = set()
    for container in containers:
        labels = container.get("Config", {}).get("Labels", {})
        service = labels.get("com.docker.compose.service")
        require(service in {"frontend", "backend"} and service not in seen, "Unexpected production container")
        seen.add(service)
        require(container.get("Name") == "/" + names[service], "Running container has the wrong configured name")
        require(labels.get("com.docker.compose.project") == "datalab"
                and labels.get("com.docker.compose.config-hash")
                and labels.get("com.docker.compose.container-number") == "1",
                "Container was not created from the current Compose release")
        working_dir = Path(labels.get("com.docker.compose.project.working_dir", ""))
        require(working_dir.is_absolute(), "Container has no approved release directory")
        try:
            working_dir = working_dir.resolve(strict=True)
        except (OSError, RuntimeError):
            raise DeploymentError("Container has no approved release directory") from None
        require(working_dir.is_dir() and working_dir.parent == release.parent
                and (service not in services or working_dir == release),
                "Container was not created from an approved release directory")
        require(container.get("State", {}).get("Running") is True, "Production container is not running")
        require(container.get("Image") == manifest[service]["id"], "Running image differs from release manifest")
        bindings = container.get("HostConfig", {}).get("PortBindings") or {}
        if service == "frontend":
            require(set(bindings) == {"80/tcp"} and bool(bindings["80/tcp"])
                    and all(item.get("HostPort") == PORT and item.get("HostIp", "") in {"", "0.0.0.0", "::", "127.0.0.1", "::1"}
                            for item in bindings["80/tcp"]), "Running frontend has wrong port bindings")
            active = container.get("NetworkSettings", {}).get("Ports") or {}
            require(bool(active.get("80/tcp"))
                    and all(item.get("HostPort") == PORT and item.get("HostIp", "") in {"", "0.0.0.0", "::", "127.0.0.1", "::1"}
                            for item in active["80/tcp"])
                    and all(not value for key, value in active.items() if key != "80/tcp"),
                    "Running frontend has wrong active port bindings")
        else:
            validate_persistent_mounts(container, service)
            require(not bindings, "Running backend publishes an independent port")
            environment = dict(item.split("=", 1) for item in container["Config"].get("Env", []) if "=" in item)
            require(environment.get("AUTH_PROVIDER") == "password"
                    and environment.get("SERVER_HOST", "").rstrip("/") == ORIGIN,
                    "Running backend has wrong public access settings")
            require(environment.get("DATASET_MANAGED_VOLUME") == volume_name("dataset-data")
                    and environment.get("JUPYTER_WORKSPACES_STORAGE_ROOT") == "/data/jupyter-workspaces",
                    "Running backend has wrong persistent storage settings")
            require(all(environment.get(kind.upper() + "_IMAGE") == manifest[kind]["reference"]
                        for kind in ("sandbox", "jupyter")), "Running backend has wrong runtime images")


def validate_infrastructure_runtime(containers, manifest, release, *, names=None):
    names = names or container_names()
    require(isinstance(containers, list) and len(containers) == len(INFRA_KINDS),
            "Expected one container for each infrastructure service")
    seen = set()
    for container in containers:
        labels = container.get("Config", {}).get("Labels", {})
        service = labels.get("com.docker.compose.service")
        require(service in INFRA_KINDS and service not in seen, "Unexpected infrastructure container")
        seen.add(service)
        require(container.get("Name") == "/" + names[service], "Running infrastructure has the wrong configured name")
        require(labels.get("com.docker.compose.project") == "datalab"
                and labels.get("com.docker.compose.config-hash")
                and labels.get("com.docker.compose.container-number") == "1",
                "Infrastructure container is not managed by the current Compose project")
        working_dir = Path(labels.get("com.docker.compose.project.working_dir", ""))
        require(working_dir.is_absolute() and working_dir.resolve(strict=True) == release,
                "Infrastructure container is not from the current release")
        require(container.get("State", {}).get("Running") is True,
                "Infrastructure container is not running")
        require(container.get("Image") == manifest[service]["id"],
                "Running infrastructure image differs from release manifest")
        validate_persistent_mounts(container, service)
        require(not container.get("HostConfig", {}).get("PortBindings")
                and not any((container.get("NetworkSettings", {}).get("Ports") or {}).values()),
                "Running infrastructure publishes an independent port")


def validate_repair_target(container, manifest, *, name=None):
    """Allow only the known stateless frontend recreated outside Compose."""
    labels = container.get("Config", {}).get("Labels") or {}
    allowed_names = {"/" + legacy_container_name("frontend"), "/" + (name or legacy_container_name("frontend"))}
    require(container.get("Name") in allowed_names
            and labels.get("com.docker.compose.project") == "datalab"
            and labels.get("com.docker.compose.service") == "frontend"
            and container.get("Image") == manifest["frontend"]["id"]
            and container.get("Mounts") == []
            and not labels.get("com.docker.compose.config-hash")
            and not labels.get("com.docker.compose.container-number")
            and bool(re.fullmatch(r"[0-9a-f]{64}", container.get("Id", ""))),
            "Frontend repair refused: container does not match the exact stateless unmanaged frontend")


def repair_frontend(run, manifest, release, *, name=None):
    name = name or legacy_container_name("frontend")
    require(name in {legacy_container_name("frontend"), v2_container_name("frontend")}, "Invalid frontend repair name")
    allowed_names = sorted({legacy_container_name("frontend"), name})
    name_pattern = "name=^/(" + "|".join(re.escape(item) for item in allowed_names) + ")$"
    identifiers = run(["docker", "container", "ls", "--all", "--quiet", "--no-trunc",
                       "--filter", name_pattern]).split()
    if not identifiers:
        return
    require(len(identifiers) == 1 and bool(re.fullmatch(r"[0-9a-f]{64}", identifiers[0])),
            "Frontend repair found an unexpected container")
    containers = decoded(run(["docker", "container", "inspect", identifiers[0]]))
    require(isinstance(containers, list) and len(containers) == 1, "Frontend repair found an unexpected container")
    target = containers[0]
    validate_repair_target(target, manifest, name=name)
    require(target["Id"] == identifiers[0], "Frontend repair container identity changed")
    # Store only operational metadata. Never persist Env, command arguments, or secrets.
    snapshot = {"id": target["Id"], "name": target["Name"], "image": target["Image"], "mount_count": 0,
                "running": target.get("State", {}).get("Running"),
                "port_bindings": target.get("HostConfig", {}).get("PortBindings"),
                "labels": {key: target["Config"]["Labels"].get(key) for key in
                           ("com.docker.compose.project", "com.docker.compose.service",
                            "com.docker.compose.version", "com.docker.compose.config-hash", "com.docker.compose.container-number")}}
    directory = release / ".local" / "production-repairs"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / (str(time.time_ns()) + "-frontend.json")
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(snapshot, output, indent=2)
        output.flush()
        os.fsync(output.fileno())
    run(["docker", "container", "stop", "--time", "30", target["Id"]])
    # No force and no volume deletion; the exact validated container is the only target.
    run(["docker", "container", "rm", target["Id"]])
    print(json.dumps({"status": "unmanaged_frontend_removed", "snapshot": str(path.relative_to(release))}), flush=True)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def verify_http():
    # Ignore caller proxy variables; verify the real production endpoints.
    opener = build_opener(ProxyHandler({}), NoRedirect())
    for base in (DIRECT, ORIGIN):
        try:
            with opener.open(base + "/", timeout=5) as response:
                require(response.status == 200 and response.headers.get_content_type() == "text/html",
                        "Production homepage is not ready")
            with opener.open(base + "/api/v1/config/client-contract", timeout=5) as response:
                require(response.status == 200 and response.headers.get_content_type() == "application/json",
                        "Production API is not ready")
                payload = decoded(response.read(65537))
                require(isinstance(payload, dict) and payload.get("schema_version") == 1
                        and 1 in payload.get("supported_client_protocols", []),
                        "Client protocol contract is incompatible")
        except (HTTPError, URLError, TimeoutError, OSError):
            raise DeploymentError("Production HTTP verification failed; no redirects are accepted") from None


def verify_stopped(run, services):
    identifiers = run(["docker", "container", "ls", "--all", "--quiet", "--no-trunc",
                       "--filter", "label=com.docker.compose.project=datalab"]).split()
    require(all(re.fullmatch(r"[0-9a-f]{12,64}", item) for item in identifiers),
            "Unexpected production container identifier")
    if not identifiers:
        return
    containers = decoded(run(["docker", "container", "inspect", *identifiers]))
    require(isinstance(containers, list) and len(containers) == len(identifiers),
            "Production stop verification could not inspect all containers")
    for container in containers:
        labels = container.get("Config", {}).get("Labels", {})
        require(labels.get("com.docker.compose.project") == "datalab", "Unexpected project during stop verification")
        service = labels.get("com.docker.compose.service")
        if service in services and container.get("Name") in {
                "/" + legacy_container_name(service), "/" + v2_container_name(service)}:
            require(container.get("State", {}).get("Running") is False,
                    "A production service is still running after controlled stop")


def archive_dynamic_sandboxes(run, manifest, *, cwd, env):
    source = Path(__file__).with_name("sandbox_archive.py")
    spec = importlib.util.spec_from_file_location("datalab_production_sandbox_archive", source)
    require(spec is not None and spec.loader is not None, "Sandbox migration archive helper is unavailable")
    helper = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = helper
    spec.loader.exec_module(helper)
    try:
        return helper.archive_and_stop_sandboxes(run, manifest, cwd=cwd, env=env, directory=DEPLOY_ROOT / "backups" / "sandboxes")
    except helper.SandboxArchiveError as error:
        raise DeploymentError(str(error)) from None


@contextmanager
def frozen_backend(run, compose, names):
    """Prevent application shutdown from deleting unarchived dynamic sandboxes."""
    identifiers = compose("ps", "--all", "--quiet", "backend").split()
    require(len(identifiers) == 1 and bool(re.fullmatch(r"[0-9a-f]{12,64}", identifiers[0])),
            "Expected one backend before archival")
    values = decoded(run(["docker", "container", "inspect", identifiers[0]]))
    require(isinstance(values, list) and len(values) == 1, "Invalid backend archive barrier")
    target = values[0]
    labels = target.get("Config", {}).get("Labels") or {}
    require(target.get("Name") == "/" + names["backend"]
            and labels.get("com.docker.compose.project") == "datalab"
            and labels.get("com.docker.compose.service") == "backend",
            "Foreign backend cannot be frozen for archival")
    state = target.get("State", {})
    require(not state.get("Paused"), "Backend is already paused; inspect it before controlled stop")
    paused_here = state.get("Running") is True
    if paused_here:
        run(["docker", "container", "pause", identifiers[0]])
    try:
        yield
    finally:
        if paused_here:
            run(["docker", "container", "unpause", identifiers[0]])


def load_policy(path):
    """Pin endpoints and existing volume identities without copying service secrets."""
    global ORIGIN, DIRECT, PORT, VOLUME_IDENTITIES
    from urllib.parse import urlsplit
    policy = decoded(path.read_text())
    require(isinstance(policy, dict) and policy.get("project") == "datalab", "Invalid release policy")
    port = str(policy.get("port", ""))
    require(port.isdecimal() and 1 <= int(port) <= 65535, "Invalid release port")
    endpoints = [policy.get("origin"), policy.get("direct")]
    for endpoint in endpoints:
        require(isinstance(endpoint, str), "Invalid release endpoint")
        parsed = urlsplit(endpoint)
        require(parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not parsed.username
                and not parsed.password and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment,
                "Invalid release endpoint")
    volumes = policy.get("volumes")
    require(isinstance(volumes, dict) and set(volumes) == set(STATE_VOLUMES), "Incomplete release storage policy")
    for value in volumes.values():
        require(isinstance(value, dict) and isinstance(value.get("name"), str)
                and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", value["name"])
                and value.get("driver") == "local" and isinstance(value.get("options"), dict),
                "Invalid release storage identity")
        require(not value["name"].startswith("ai-dataseek"), "DataSeek volumes cannot be used by DataLab")
    require(len({item["name"] for item in volumes.values()}) == len(volumes), "Release volumes must be distinct")
    ORIGIN, DIRECT, PORT, VOLUME_IDENTITIES = endpoints[0].rstrip("/"), endpoints[1].rstrip("/"), port, volumes


def deploy(*, apply=False, services=None, repair=False, stack=False, stop=False):
    require(not stop or (not apply and services is None and not repair and not stack),
            "Stop cannot combine with publication, services, full stack, or frontend repair")
    require(not stack or (apply and services is None and not repair),
            "Full stack publication requires --apply and cannot combine with --services or --repair-frontend")
    services = STACK_SERVICES if stack else (services if services is not None else ("backend", "frontend"))
    kinds = (*KINDS, *INFRA_KINDS) if stack else KINDS
    require(not repair or (apply and services == ("frontend",)),
            "Frontend repair requires --apply --services frontend")
    with deployment_lock() as lock_fd:
        require(CURRENT.is_symlink(), "Production current release must be a symbolic link")
        release = CURRENT.resolve(strict=True)
        require(release.is_dir() and release.parent == RELEASES.resolve(strict=True), "Current release is outside the release directory")
        require((release / "run.sh").is_file() and (release / ".env").is_file(), "Current release is incomplete")
        env = release_environment()
        env.pop(LOCK_FD_ENV, None)
        env[LOCK_FD_ENV] = str(lock_fd)
        env["PWD"] = str(release)
        env["DATALAB_DEPLOY_ROOT"] = str(DEPLOY_ROOT)
        for key in list(env):
            if key.startswith("COMPOSE_"):
                env.pop(key)
        env["COMPOSE_REMOVE_ORPHANS"] = "false"

        def run(arguments):
            return command(arguments, cwd=release, env=env, pass_fds=(lock_fd,))

        def compose(*arguments):
            return run(["./run.sh", "--env-file", ".env", *arguments])

        load_policy(release / "release-policy.json")
        config = decoded(compose("config", "--format", "json"))
        if stop:
            require(isinstance(config, dict) and config.get("name") == "datalab", "Wrong Compose project")
            require(all(isinstance(config.get("services", {}).get(name), dict) for name in STACK_SERVICES),
                    "Missing production service for controlled stop")
            compose("stop", "--timeout", "60", "frontend")
            verify_stopped(run, ("frontend",))
            archive_manifest_path = release / "release-images.json"
            archive_manifest = decoded(archive_manifest_path.read_text()) if archive_manifest_path.is_file() else {}
            require(isinstance(archive_manifest, dict), "Invalid sandbox archive identity manifest")
            # Pause the backend before archival: its normal shutdown destroys
            # active task sandboxes. Only resume it after every export is durable.
            with frozen_backend(run, compose, container_names(config)):
                archive_dynamic_sandboxes(run, archive_manifest, cwd=release, env=env)
            compose("stop", "--timeout", "60", "backend")
            verify_stopped(run, ("frontend", "backend"))
            compose("stop", "--timeout", "60", *[service for service in STACK_SERVICES if service not in {"frontend", "backend"}])
            verify_stopped(run, STACK_SERVICES)
            require(CURRENT.resolve(strict=True) == release, "Current release changed during controlled stop")
            print(json.dumps({"status": "stopped_and_verified", "release": release.name,
                              "services": list(STACK_SERVICES)}), flush=True)
            return
        policy_snapshot = (release / "release-policy.json").read_bytes()
        manifest = load_manifest(release / "release-images.json", stack=stack)
        validate_config(config, manifest, stack=stack)
        validate_storage_config(config)
        validate_volumes(decoded(run(["docker", "volume", "inspect", *[volume_name(key) for key in STATE_VOLUMES]])))
        validate_images(decoded(run(["docker", "image", "inspect", *[manifest[k]["reference"] for k in kinds]])), manifest, kinds)
        print(json.dumps({"status": "preflight_passed", "release": release.name, "port": int(PORT), "origin": ORIGIN}), flush=True)
        if not apply:
            return
        require(CURRENT.resolve(strict=True) == release, "Current release changed during validation")
        # Same parsed Compose configuration must still be selected when publishing.
        require(decoded(compose("config", "--format", "json")) == config, "Compose configuration changed during validation")
        require(load_manifest(release / "release-images.json", stack=stack) == manifest, "Release image manifest changed during validation")
        require((release / "release-policy.json").read_bytes() == policy_snapshot, "Release policy changed during validation")
        if repair:
            repair_frontend(run, manifest, release, name=container_names(config)["frontend"])
        compose("up", "-d", *(["--force-recreate"] if stack else ["--no-deps"]), "--no-build", "--pull", "never", *services)
        deadline = time.monotonic() + 60
        while True:
            try:
                identifiers = compose("ps", "--all", "--quiet", "frontend", "backend").split()
                require(len(identifiers) == 2 and all(re.fullmatch(r"[0-9a-f]{12,64}", item) for item in identifiers),
                        "Expected one frontend and one backend container")
                validate_runtime(decoded(run(["docker", "container", "inspect", *identifiers])), manifest, release, services, names=container_names(config))
                if stack:
                    infrastructure_ids = compose("ps", "--all", "--quiet", *INFRA_KINDS).split()
                    require(len(infrastructure_ids) == len(INFRA_KINDS)
                            and all(re.fullmatch(r"[0-9a-f]{12,64}", item) for item in infrastructure_ids),
                            "Expected one container for each infrastructure service")
                    validate_infrastructure_runtime(decoded(run(["docker", "container", "inspect", *infrastructure_ids])),
                                                    manifest, release, names=container_names(config))
                validate_volumes(decoded(run(["docker", "volume", "inspect", *[volume_name(key) for key in STATE_VOLUMES]])))
                verify_http()
                validate_images(decoded(run(["docker", "image", "inspect", *[manifest[k]["reference"] for k in kinds]])), manifest, kinds)
                require(CURRENT.resolve(strict=True) == release, "Current release changed during publication")
                break
            except DeploymentError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(2)
        print(json.dumps({"status": "deployed_and_verified", "release": release.name, "services": list(services),
                          "port": int(PORT), "origin": ORIGIN}), flush=True)


def build_images(services=None):
    """Build changed applications under the release lock, without touching services."""
    require(not (ENTRY_ROOT / ".datalab-release").exists()
            and (ENTRY_ROOT / "backend" / "Dockerfile").is_file(),
            "Build from the source workspace, not a release metadata snapshot")
    services = services or ("backend", "frontend")
    require(bool(services) and all(value in {"backend", "frontend"} for value in services),
            "Only affected application images are built by this entry")
    env = release_environment()
    args = ["docker", "compose", "-f", "docker-compose.yml", "--env-file", ".env"]
    with deployment_lock():
        config = decoded(command([*args, "config", "--format", "json"], cwd=ENTRY_ROOT, env=env))
        require(config.get("name") == "datalab", "Wrong Compose project")
        command([*args, "build", *services], cwd=ENTRY_ROOT, env=env, timeout=1800)
    print(json.dumps({"status": "application_images_built", "services": list(services), "services_restarted": False}))


def main():
    if sys.argv[1:2] == ["--check-compose-entry"]:
        try:
            require(len(sys.argv) >= 3, "Invalid production entry check")
            check_compose_entry(sys.argv[2], sys.argv[3:])
        except (DeploymentError, OSError, ValueError, TypeError, RuntimeError):
            print("Production changes must use ./run.sh production; ordinary Compose changes are blocked", file=sys.stderr)
            return 1
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", action="store_true", help="Build frontend/backend from this source workspace under the deployment lock; no restart")
    parser.add_argument("--stop", action="store_true", help="Stop only the fixed production services without removing containers or volumes")
    parser.add_argument("--apply", action="store_true", help="Publish after preflight; otherwise only validate")
    parser.add_argument("--services", "--service", nargs="+", choices=("frontend", "backend"), default=None)
    parser.add_argument("--stack", action="store_true", help="Start the fixed full stack using local manifest images; requires --apply and excludes --services")
    parser.add_argument("--repair-frontend", action="store_true", help="Replace only the exact stateless frontend recreated outside Compose; requires --apply --services frontend")
    args = parser.parse_args()
    try:
        if args.build:
            require(not (args.apply or args.stack or args.stop or args.repair_frontend),
                    "Image build cannot combine with publication or stop")
            build_images(tuple(dict.fromkeys(args.services)) if args.services else None)
            return 0
        deploy(apply=args.apply, services=tuple(dict.fromkeys(args.services)) if args.services is not None else None,
               repair=args.repair_frontend, stack=args.stack, stop=args.stop)
    except (DeploymentError, OSError, ValueError, TypeError, KeyError, AttributeError):
        # Do not print raw exception/provider bodies, paths, environment or commands.
        error = sys.exc_info()[1]
        print(str(error) if isinstance(error, DeploymentError) else "Production deployment metadata is unavailable or invalid", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
