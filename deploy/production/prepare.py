#!/usr/bin/env python3
"""Prepare a private DataLab release from already built local images.

No service is restarted. Existing persistent volume identities are preserved;
only the empty Jupyter parent volume may be created. --activate selects the
snapshot for ./run.sh production (which still defaults to a read-only check).
--from-current requires an expected release name and an explicitly pinned local
backend image, optionally paired with a frontend image; that mode preserves
all other image references and storage.
"""
from __future__ import annotations
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

SPEC = importlib.util.spec_from_file_location("datalab_release_guard", Path(__file__).with_name("deploy.py"))
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)
ENV_IMAGES = {"frontend": "FRONTEND_IMAGE", "backend": "BACKEND_IMAGE", "sandbox": "SANDBOX_IMAGE",
              "jupyter": "JUPYTER_IMAGE", "mongodb": "MONGODB_IMAGE", "redis": "REDIS_IMAGE", "minio": "MINIO_IMAGE"}
RELEASE_NAME = r"[a-z0-9][a-z0-9._-]{0,63}"
SNAPSHOT_FILES = ("run.sh", "docker-compose.yml", "deploy/production/deploy.py", "deploy/production/sandbox_archive.py")


def write_private(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(value)
        output.flush()
        os.fsync(output.fileno())


def rewrite_environment(source, updates):
    """Never execute dotenv values or include their contents in diagnostics."""
    result, pending = [], dict(updates)
    for line in source.splitlines():
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if match and match[1] in updates:
            if match[1] in pending:
                result.append(match[1] + "=" + pending.pop(match[1]))
        else:
            result.append(line)
    result.extend(key + "=" + value for key, value in pending.items())
    return "\n".join(result) + "\n"


def image_reference(kind, identity, release_name, previous):
    # Unchanged runtime image references must stay stable across app releases.
    # Otherwise a frontend-only release would demand a new SANDBOX_IMAGE/JUPYTER_IMAGE
    # environment from the intentionally untouched backend.
    old = previous.get(kind, {})
    if kind in {"sandbox", "jupyter"} and old.get("id") == identity:
        return old["reference"]
    return "datalab-" + kind + ":" + release_name


def inspect_existing_storage(containers, config):
    """Use the existing application's actual mounts, including legacy anonymous volumes."""
    result = {}
    seen = set()
    for container in containers:
        labels = container.get("Config", {}).get("Labels") or {}
        service = labels.get("com.docker.compose.service")
        guard.require(labels.get("com.docker.compose.project") == "datalab" and service not in seen,
                      "Unexpected service identity while pinning storage")
        seen.add(service)
        for destination, key in guard.PERSISTENT_MOUNTS.get(service, {}).items():
            matches = [mount for mount in container.get("Mounts", []) if mount.get("Destination") == destination]
            if not matches and key == "jupyter-workspaces":
                continue  # New empty parent mount; existing per-session workspaces stay untouched.
            guard.require(len(matches) == 1 and matches[0].get("Type") == "volume" and matches[0].get("Name"),
                          "Existing persistent mount cannot be safely preserved")
            name = matches[0]["Name"]
            guard.require(not name.startswith("ai-dataseek"), "DataSeek storage cannot be used by DataLab")
            result[key] = name
    guard.require(seen == {"backend", "mongodb", "redis", "minio"}, "Running DataLab storage services are incomplete")
    for key in guard.STATE_VOLUMES:
        if key not in result:
            guard.require(key == "jupyter-workspaces", "Existing persistent volume is missing")
            result[key] = config.get("volumes", {}).get(key, {}).get("name", "datalab-jupyter-workspaces")
    guard.require(len(set(result.values())) == len(result), "Persistent volumes must be distinct")
    return result


def _current_release(expected):
    """Called only while holding the deployment lock, including before selection."""
    guard.require(guard.CURRENT.is_symlink(), "Production current release must be a symbolic link")
    current = guard.CURRENT.resolve(strict=True)
    guard.require(current.parent == guard.RELEASES.resolve(strict=True) and current.name == expected
                  and current.is_dir() and (current / ".datalab-release").is_file(),
                  "Current release changed or is outside the approved release directory")
    return current


def _write_snapshot(release, repository, private_environment, manifest, policy):
    release.mkdir(mode=0o700, parents=True)
    for relative in SNAPSHOT_FILES:
        destination = release / relative
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copyfile(repository / relative, destination)
    (release / "run.sh").chmod(0o700)
    write_private(release / ".env", private_environment)
    office_secrets = repository / ".local" / "office-viewer.env"
    if office_secrets.is_file():
        (release / ".local").mkdir(mode=0o700)
        write_private(release / ".local" / "office-viewer.env", office_secrets.read_text())
    write_private(release / ".datalab-release", "DataLab release metadata; no source credentials in build contexts.\n")
    write_private(release / "release-images.json", json.dumps(manifest, indent=2) + "\n")
    write_private(release / "release-policy.json", json.dumps(policy, indent=2) + "\n")


def _select_release(release):
    link = guard.DEPLOY_ROOT / (".current-" + release.name)
    guard.require(not link.exists() and not link.is_symlink(), "Temporary release selector already exists")
    link.symlink_to(release)
    os.replace(link, guard.CURRENT)


def _prepare_current_applications(release, expected, candidates, *, activate, env):
    repository = _current_release(expected)
    # The trusted source is an existing private release, never a build context
    # or a shell override. Reject indirection outside that release.
    for relative in (*SNAPSHOT_FILES, ".env", "release-images.json", "release-policy.json"):
        path = repository / relative
        guard.require(path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(repository),
                      "Current release metadata is incomplete or redirected")
    office_secrets = repository / ".local" / "office-viewer.env"
    guard.require(not office_secrets.is_symlink()
                  and (not office_secrets.exists() or (office_secrets.is_file()
                       and office_secrets.resolve().is_relative_to(repository))),
                  "Current private service configuration is redirected")
    previous = guard.load_manifest(repository / "release-images.json", stack=True)
    policy = guard.decoded((repository / "release-policy.json").read_text())
    guard.load_policy(repository / "release-policy.json")
    env = dict(env, DATALAB_DEPLOY_ROOT=str(guard.DEPLOY_ROOT))

    def run(arguments):
        return guard.command(arguments, cwd=repository, env=env)

    config = guard.decoded(run(["./run.sh", "--env-file", ".env", "config", "--format", "json"]))
    guard.validate_config(config, previous, stack=True)
    guard.validate_storage_config(config)
    kinds = tuple(ENV_IMAGES)
    images = guard.decoded(run(["docker", "image", "inspect", *[previous[kind]["reference"] for kind in kinds]]))
    guard.validate_images(images, previous, kinds)
    for kind, image in candidates.items():
        candidate = guard.decoded(run(["docker", "image", "inspect", image["reference"]]))
        guard.require(isinstance(candidate, list) and len(candidate) == 1 and isinstance(candidate[0], dict)
                      and candidate[0].get("Id") == image["id"],
                      kind.capitalize() + " candidate image differs from its expected local identity")
    identifiers = run(["./run.sh", "--env-file", ".env", "ps", "--all", "--quiet",
                       "backend", "mongodb", "redis", "minio"]).split()
    guard.require(len(identifiers) == 4 and len(set(identifiers)) == 4
                  and all(re.fullmatch(r"[0-9a-f]{12,64}", value) for value in identifiers),
                  "Expected the existing DataLab backend and storage services")
    containers = guard.decoded(run(["docker", "container", "inspect", *identifiers]))
    volume_names = inspect_existing_storage(containers, config)
    guard.require(volume_names == {key: value["name"] for key, value in policy["volumes"].items()},
                  "Running storage differs from the current release policy")
    for container in containers:
        labels = container.get("Config", {}).get("Labels") or {}
        service = labels.get("com.docker.compose.service")
        guard.require(container.get("State", {}).get("Running") is True
                      and container.get("Image") == previous[service]["id"],
                      "Running service differs from the current release image")
        guard.validate_persistent_mounts(container, service)
    volumes = guard.decoded(run(["docker", "volume", "inspect", *volume_names.values()]))
    guard.validate_volumes(volumes)
    manifest = dict(previous, **candidates)
    private_environment = rewrite_environment((repository / ".env").read_text(),
                                              {ENV_IMAGES[kind]: image["reference"] for kind, image in candidates.items()})
    guard.require(_current_release(expected) == repository, "Current release changed during preparation")
    _write_snapshot(release, repository, private_environment, manifest, policy)
    snapshot = guard.decoded(guard.command(["./run.sh", "--env-file", ".env", "config", "--format", "json"],
                                           cwd=release, env=env))
    guard.validate_config(snapshot, manifest, stack=True)
    guard.validate_storage_config(snapshot)
    # Recheck the candidate tag before selecting, without mutating any image tag.
    for kind, image in candidates.items():
        candidate = guard.decoded(run(["docker", "image", "inspect", image["reference"]]))
        guard.validate_images(candidate, manifest, (kind,))
    guard.require(_current_release(expected) == repository, "Current release changed during preparation")
    if activate:
        _select_release(release)


def prepare(name, *, activate=False, repository=None, from_current=None, backend_image=None, backend_image_id=None,
            frontend_image=None, frontend_image_id=None):
    guard.require(isinstance(name, str) and bool(re.fullmatch(RELEASE_NAME, name)), "Invalid release name")
    explicit = (from_current, backend_image, backend_image_id, frontend_image, frontend_image_id)
    if any(value is not None for value in explicit):
        guard.require(all(isinstance(value, str) and value for value in explicit[:3]) and repository is None,
                      "Current-release preparation requires the release, backend reference and expected image identity together")
        guard.require(bool(re.fullmatch(RELEASE_NAME, from_current)), "Invalid expected current release name")
        match = re.fullmatch(r"datalab-backend:([A-Za-z0-9_][A-Za-z0-9_.-]{0,127})", backend_image)
        guard.require(match is not None and match[1].lower() != "latest", "Backend candidate must use an explicit datalab-backend version")
        guard.require(bool(re.fullmatch(r"sha256:[0-9a-f]{64}", backend_image_id)), "Invalid expected backend image identity")
        if frontend_image is not None or frontend_image_id is not None:
            guard.require(isinstance(frontend_image, str) and isinstance(frontend_image_id, str),
                          "Frontend reference and expected image identity are required together")
            match = re.fullmatch(r"datalab-frontend:([A-Za-z0-9_][A-Za-z0-9_.-]{0,127})", frontend_image)
            guard.require(match is not None and match[1].lower() != "latest", "Frontend candidate must use an explicit datalab-frontend version")
            guard.require(bool(re.fullmatch(r"sha256:[0-9a-f]{64}", frontend_image_id)), "Invalid expected frontend image identity")
    repository = Path(repository or Path(__file__).resolve().parents[2]).resolve()
    env = guard.release_environment()
    for key in list(env):
        if key.startswith("COMPOSE_"):
            env.pop(key)
    def run(arguments):
        return guard.command(arguments, cwd=repository, env=env)
    def compose(*arguments):
        return run(["./run.sh", "--env-file", ".env", *arguments])
    with guard.deployment_lock():
        release = guard.RELEASES / name
        guard.require(not release.exists() and not release.is_symlink(), "Release already exists; snapshots are never overwritten")
        if from_current is not None:
            candidates = {"backend": {"reference": backend_image, "id": backend_image_id}}
            if frontend_image is not None:
                candidates["frontend"] = {"reference": frontend_image, "id": frontend_image_id}
            _prepare_current_applications(release, from_current, candidates, activate=activate, env=env)
            print(json.dumps({"status": "release_prepared", "release": name, "selected": activate,
                              "services_restarted": False, "existing_data_moved": False}))
            return
        config = guard.decoded(compose("config", "--format", "json"))
        guard.require(config.get("name") == "datalab", "Wrong Compose project")
        services = config["services"]
        identifiers = compose("ps", "--all", "--quiet", "backend", "mongodb", "redis", "minio").split()
        guard.require(len(identifiers) == 4 and all(re.fullmatch(r"[0-9a-f]{12,64}", value) for value in identifiers),
                      "Expected the existing DataLab backend and storage services")
        containers = guard.decoded(run(["docker", "container", "inspect", *identifiers]))
        volume_names = inspect_existing_storage(containers, config)
        # Prepare only the new parent, never replace, remove or copy existing data.
        parent = volume_names["jupyter-workspaces"]
        found = run(["docker", "volume", "ls", "--quiet", "--filter", "name=^" + re.escape(parent) + "$"]).split()
        if not found:
            guard.require(parent == "datalab-jupyter-workspaces" and not services["backend"]["environment"].get("JUPYTER_WORKSPACES_HOST_ROOT"),
                          "Host-backed workspace parent must be provisioned explicitly")
            run(["docker", "volume", "create", "--label", "com.docker.compose.project=datalab",
                 "--label", "com.docker.compose.volume=jupyter-workspaces", parent])
        volumes = guard.decoded(run(["docker", "volume", "inspect", *volume_names.values()]))
        by_name = {item["Name"]: item for item in volumes}
        policy_volumes = {key: {"name": value, "driver": by_name[value]["Driver"], "options": by_name[value].get("Options") or {}}
                          for key, value in volume_names.items()}
        image_sources = {kind: services[kind if kind in (*guard.INFRA_KINDS, "backend", "frontend") else kind + "-image"]["image"]
                         for kind in ENV_IMAGES}
        images = guard.decoded(run(["docker", "image", "inspect", *image_sources.values()]))
        guard.require(len(images) == len(image_sources), "Missing local release images")
        previous = (guard.load_manifest(guard.CURRENT.resolve(strict=True) / "release-images.json", stack=True)
                    if guard.CURRENT.is_symlink() else {})
        manifest, updates = {}, {}
        for (kind, _), image in zip(image_sources.items(), images):
            identity = image.get("Id", "")
            guard.require(bool(re.fullmatch(r"sha256:[0-9a-f]{64}", identity)), "Invalid local image identity")
            reference = image_reference(kind, identity, name, previous)
            if reference == previous.get(kind, {}).get("reference"):
                pinned = guard.decoded(run(["docker", "image", "inspect", reference]))
                guard.require(len(pinned) == 1 and pinned[0].get("Id") == identity,
                              "Existing runtime image reference changed; inspect it before preparing a release")
            else:
                run(["docker", "image", "tag", identity, reference])
            manifest[kind] = {"reference": reference, "id": identity}
            updates[ENV_IMAGES[kind]] = reference
        port = str(services["frontend"]["ports"][0]["published"])
        origin = services["backend"].get("environment", {}).get("SERVER_HOST", "http://localhost:" + port).rstrip("/")
        policy = {"project": "datalab", "port": int(port), "origin": origin,
                  "direct": "http://127.0.0.1:" + port, "volumes": policy_volumes}
        updates.update({"COMPOSE_PROJECT_NAME": "datalab", "STACK_PREFIX": "datalab",
                        "MONGODB_CONFIG_VOLUME": volume_names["mongodb-config"], "MONGODB_CONFIG_EXTERNAL": "true",
                        "REDIS_DATA_VOLUME": volume_names["redis-data"], "REDIS_DATA_EXTERNAL": "true",
                        "JUPYTER_WORKSPACES_VOLUME": parent, "JUPYTER_WORKSPACES_EXTERNAL": "true"})
        private_environment = rewrite_environment((repository / ".env").read_text(), updates)
        _write_snapshot(release, repository, private_environment, manifest, policy)
        guard.load_policy(release / "release-policy.json")
        # Validate the actual snapshot before selecting it; this emits no secret config.
        snapshot_env = dict(env, DATALAB_DEPLOY_ROOT=str(guard.DEPLOY_ROOT))
        snapshot = guard.decoded(guard.command(["./run.sh", "--env-file", ".env", "config", "--format", "json"], cwd=release, env=snapshot_env))
        guard.validate_config(snapshot, manifest, stack=True)
        guard.validate_storage_config(snapshot)
        guard.validate_volumes(volumes)
        if activate:
            _select_release(release)
        print(json.dumps({"status": "release_prepared", "release": name, "selected": activate,
                          "services_restarted": False, "existing_data_moved": False}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True)
    parser.add_argument("--activate", action="store_true")
    parser.add_argument("--from-current", metavar="EXPECTED_RELEASE")
    parser.add_argument("--backend-image", metavar="REFERENCE")
    parser.add_argument("--backend-image-id", metavar="SHA256")
    parser.add_argument("--frontend-image", metavar="REFERENCE")
    parser.add_argument("--frontend-image-id", metavar="SHA256")
    args = parser.parse_args()
    try:
        prepare(args.name, activate=args.activate, from_current=args.from_current,
                backend_image=args.backend_image, backend_image_id=args.backend_image_id,
                frontend_image=args.frontend_image, frontend_image_id=args.frontend_image_id)
    except (guard.DeploymentError, OSError, ValueError, TypeError, KeyError):
        error = sys.exc_info()[1]
        print(str(error) if isinstance(error, guard.DeploymentError) else "Release preparation failed; inspect private local state", file=sys.stderr)
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
