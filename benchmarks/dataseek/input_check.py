"""Check the existing managed data volume against privately frozen input bytes."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from urllib.parse import quote
from urllib.parse import urlsplit

from .environment import _docker_python, _run, DEFAULT_BACKEND_CONTAINER, EnvironmentInspectionError
from .protocol import validate_task


def verify_api_origin(base_url):
    """Local Docker fingerprints can only attest to that stack's local API."""
    parts = urlsplit(base_url)
    if parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "localhost"} or parts.path not in {"", "/"}:
        raise EnvironmentInspectionError("This pilot attests only to the existing local frontend; remote services require a separate inspector")
    ports = json.loads(_run(["docker", "inspect", "--type", "container", "--format", "{{json .NetworkSettings.Ports}}", "datalab-frontend-1"]))
    allowed = {int(item["HostPort"]) for item in ports.get("80/tcp", []) if item.get("HostIp") in {"0.0.0.0", "127.0.0.1", "::"}}
    if (parts.port or 80) not in allowed:
        raise EnvironmentInspectionError("API port does not match the inspected DataLab frontend")
    return {"local_frontend_verified": True, "port": parts.port or 80}



def validate_bindings(bindings):
    """Only logical catalog keys and opaque current-user file IDs, never paths."""
    if not isinstance(bindings, dict) or not bindings:
        raise ValueError("A nonempty file binding map is required")
    for dataset_id, files in bindings.items():
        if (not isinstance(dataset_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", dataset_id)
                or not isinstance(files, dict) or not files):
            raise ValueError("Invalid catalog file binding")
        for name, file_id in files.items():
            if (not isinstance(name, str) or not name or PurePosixPath(name).is_absolute()
                    or "\\" in name or any(part in {"", ".", ".."} for part in name.split("/"))
                    or not isinstance(file_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}", file_id)):
                raise ValueError("Bindings accept relative logical names and file IDs only")
    return bindings


def current_user(client):
    if not client.token:
        raise EnvironmentInspectionError("Set DATALAB_BENCHMARK_TOKEN for the current DataLab user")
    user = client.api("GET", "/api/v1/auth/me")
    user_id = user.get("id") if isinstance(user, dict) else None
    if not isinstance(user_id, str) or not user_id or user_id == "anonymous":
        raise EnvironmentInspectionError("An authenticated non-anonymous user is required")
    return user_id


def verify_service_inputs(tasks, client, bindings):
    """Read only owner-authorized storage bytes; keep oracle answers off the API."""
    validate_bindings(bindings)
    user_id = current_user(client)
    verify_user_controls(user_id)
    results, selected = [], {}
    for task in tasks:
        validate_task(task)
        identities = task.get("metadata", {}).get("input_identities")
        if not identities:
            raise ValueError("input identities required before a live pilot")
        by_name = bindings.get(task["dataset_id"], {})
        files = []
        for identity in identities:
            name, size, expected = identity["name"], identity["size"], identity["sha256"]
            file_id = by_name.get(name)
            if file_id is None:
                raise EnvironmentInspectionError("Missing current-user file binding")
            info = client.api("GET", "/api/v1/files/" + quote(file_id, safe="") + "/info")
            if (not isinstance(info, dict) or info.get("file_id") != file_id
                    or info.get("filename") != PurePosixPath(name).name or info.get("size") != size
                    or type(size) is not int or not 0 <= size <= 256 * 1024 * 1024):
                raise EnvironmentInspectionError("Bound input identity differs from the frozen input")
            raw = client.download(file_id, max_bytes=size)
            if len(raw) != size or hashlib.sha256(raw).hexdigest() != expected:
                raise EnvironmentInspectionError("Bound input bytes differ from frozen oracle inputs")
            files.append({"file_id": file_id, "filename": info["filename"], "size": size, "sha256": expected})
        if len({f["filename"] for f in files}) != len(files):
            raise EnvironmentInspectionError("Benchmark sidecars require distinct filenames")
        selected[task["id"]] = files
        results.append({"task_id": task["id"], "matched": True, "checked_files": len(files)})
    return {"inputs": results, "all_inputs_match": True, "user_id": user_id, "selected_files": selected}


def verify_user_controls(user_id, backend_container=DEFAULT_BACKEND_CONTAINER):
    """Read the authenticated user's persisted controls without mutating preferences."""
    source = """import json
from pymongo import MongoClient
from app.core.config import get_settings
s = get_settings()
client = MongoClient(s.mongodb_uri, serverSelectionTimeoutMS=5000)
try:
    users = client[s.mongodb_database]['users']
    user = users.find_one({'user_id': USER_ID}, {'_id':0,'auto_enabled_skills':1})
    if user is None:
        user = users.find_one({'id': USER_ID}, {'_id':0,'auto_enabled_skills':1})
    print(json.dumps({'found': user is not None, 'auto_enabled_skills': (user or {}).get('auto_enabled_skills', [])}))
finally:
    client.close()
""".replace("USER_ID", repr(user_id))
    result = _docker_python(source, backend_container, 30)
    if not isinstance(result, dict) or result.get("found") is not True:
        raise EnvironmentInspectionError("Authenticated user unavailable in inspected backend")
    if result.get("auto_enabled_skills"):
        raise EnvironmentInspectionError("Current user has auto-enabled skills; freeze an explicit controlled variant")
    return result
