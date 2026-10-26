"""Adapt legacy private byte fingerprints without interpreting file content.

Old sandboxes already expose a no-follow, stable, regular-output-file hash API.
Only that API can supply compatibility receipts. Parser verdicts and model
claims are never promoted to delivery evidence.
"""
from pathlib import PurePosixPath
import re


def legacy_delivery_receipts(items: list[dict], data: dict) -> dict:
    if (not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1
            or not isinstance(data.get("files"), list) or not isinstance(data.get("errors"), list)):
        raise ValueError("unsupported_fingerprint_response")
    requested = {}
    for item in items:
        path, kind = item.get("path"), item.get("kind")
        if (not isinstance(path, str) or not path.startswith("/home/ubuntu/output/")
                or str(PurePosixPath(path)) != path or ".." in PurePosixPath(path).parts
                or "\\" in path or any(ord(c) < 32 or ord(c) == 127 for c in path)
                or kind not in {"image", "table", "report", "code", "data"}
                or path in requested and requested[path] != kind):
            raise ValueError("invalid_delivery_request")
        requested[path] = kind
    observed, failed = {}, set()
    for item in data["files"]:
        if not isinstance(item, dict) or item.get("path") not in requested:
            raise ValueError("unexpected_fingerprint")
        path, digest, size = item["path"], item.get("sha256"), item.get("size")
        if (path in observed or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
                or type(size) is not int or not 0 <= size <= 2**63 - 1):
            raise ValueError("invalid_fingerprint")
        observed[path] = (digest, size)
    for item in data["errors"]:
        if (not isinstance(item, dict) or item.get("path") not in requested
                or item["path"] in failed or item["path"] in observed):
            raise ValueError("invalid_fingerprint_error")
        failed.add(item["path"])
    if set(observed) | failed != set(requested):
        raise ValueError("incomplete_fingerprint_response")
    files = []
    for path, kind in requested.items():
        valid = path in observed
        digest, size = observed.get(path, (None, None))
        files.append({
            "path": path, "expected_kind": kind, "kind": kind, "valid": valid,
            "reason": "delivery_ready" if valid else "validation_unavailable",
            "sha256": digest, "size": size, "purpose": "delivery", "diagnostics": {},
            "metadata": {"validation_scope": "file_integrity", "content_validation": "not_performed"}
                        if valid else {},
            "proof": {"scope": "file_integrity", "source": "legacy_fingerprint",
                      "content_validation": "not_performed",
                      "checks": ["output_path", "regular_file", "readable", "size_match", "stable_snapshot", "sha256"]
                                if valid else []},
        })
    return {"version": 1, "purpose": "delivery", "files": files}
