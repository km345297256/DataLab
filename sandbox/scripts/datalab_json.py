"""Strict, atomic JSON artifacts for analysis programs.

Use ``from datalab_json import write_json, normalize_json, not_estimable``.
Non-string mapping keys (including MultiIndex tuples) use a tagged ``entries``
array, never lossy string coercion. DataFrames/Series preserve labelled axes.
Missing pandas values become null. NaN/Infinity are explicit ``non_finite``
records; an estimate that cannot be calculated must use ``not_estimable`` with
a scientific reason. Neither condition is converted to zero or a valid result.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time
import json
import math
import os
from pathlib import Path
import secrets
import stat
import sys


MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_JSON_NODES = 1_000_000
MAX_JSON_DEPTH = 100
DEFAULT_OUTPUT_ROOT = Path("/home/ubuntu/output")


def not_estimable(reason: str, *, method: str | None = None) -> dict:
    """Represent an unavailable estimate without inventing a numeric value."""
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 1024:
        raise ValueError("not_estimable requires a nonempty scientific reason (up to 1024 characters)")
    result = {"value": None, "status": "not_estimable", "reason": reason.strip()}
    if method is not None:
        if not isinstance(method, str) or not method.strip() or len(method) > 256:
            raise ValueError("method must be a nonempty string (up to 256 characters)")
        result["method"] = method.strip()
    return result


def normalize_json(payload: object) -> object:
    """Return bounded JSON-native values without generic str/default fallbacks."""
    active: set[int] = set()
    nodes = 0
    # A pandas/NumPy value cannot exist without its defining package loaded.
    # Ordinary JSON payloads do not trigger either expensive optional import.
    np = sys.modules.get("numpy")
    pd = sys.modules.get("pandas")

    def normalize(value: object, depth: int = 0) -> object:
        nonlocal nodes
        nodes += 1
        if depth > MAX_JSON_DEPTH or nodes > MAX_JSON_NODES:
            raise ValueError("JSON artifact exceeds normalization limits")
        if value is None or type(value) in (str, bool, int):
            return value
        if isinstance(value, float):
            if math.isfinite(value):
                return float(value)
            reason = "nan" if math.isnan(value) else "positive_infinity" if value > 0 else "negative_infinity"
            return {"value": None, "status": "non_finite", "reason": reason}
        if pd is not None and (value is pd.NA or value is pd.NaT):
            return None
        if isinstance(value, (datetime, date, time)):
            return value.isoformat()
        identity = id(value)
        if identity in active:
            raise ValueError("JSON artifact contains a circular reference")
        active.add(identity)
        try:
            if pd is not None:
                if isinstance(value, pd.DataFrame):
                    if value.size > MAX_JSON_NODES:
                        raise ValueError("JSON table exceeds normalization limits")
                    return normalize({"$type": "dataframe", "columns": list(value.columns),
                                      "column_names": list(value.columns.names),
                                      "index": list(value.index), "index_names": list(value.index.names),
                                      "data": list(value.itertuples(index=False, name=None))}, depth + 1)
                if isinstance(value, pd.Series):
                    if len(value) > MAX_JSON_NODES:
                        raise ValueError("JSON series exceeds normalization limits")
                    return normalize({"$type": "series", "name": value.name,
                                      "index": list(value.index), "index_names": list(value.index.names),
                                      "data": list(value)}, depth + 1)
                if isinstance(value, pd.Index):
                    if len(value) > MAX_JSON_NODES:
                        raise ValueError("JSON index exceeds normalization limits")
                    return normalize({"$type": "multi_index" if isinstance(value, pd.MultiIndex) else "index",
                                      "names": list(value.names), "values": list(value)}, depth + 1)
            if np is not None:
                if isinstance(value, np.ndarray):
                    if value.size > MAX_JSON_NODES:
                        raise ValueError("JSON array exceeds normalization limits")
                    return normalize(value.tolist(), depth + 1)
                if isinstance(value, np.generic):
                    item = value.item()
                    if isinstance(item, np.generic):
                        raise TypeError("NumPy scalar has no lossless JSON representation")
                    return normalize(item, depth + 1)
            if isinstance(value, Mapping):
                if len(value) > MAX_JSON_NODES:
                    raise ValueError("JSON mapping exceeds normalization limits")
                if all(isinstance(key, str) for key in value):
                    return {key: normalize(item, depth + 1) for key, item in value.items()}
                return {"$type": "mapping", "entries": [
                    {"key": normalize(key, depth + 1), "value": normalize(item, depth + 1)}
                    for key, item in value.items()
                ]}
            if isinstance(value, (list, tuple)):
                return [normalize(item, depth + 1) for item in value]
            raise TypeError(f"Unsupported JSON value type: {type(value).__name__}")
        finally:
            active.remove(identity)

    return normalize(payload)


def _open_output_parent(destination: Path, root: Path) -> int:
    """Resolve directories via no-follow descriptors, including new parents."""
    if (not root.is_absolute() or root == Path("/") or ".." in root.parts
            or not destination.is_absolute() or ".." in destination.parts
            or destination == root or not destination.is_relative_to(root)):
        raise ValueError("write_json path must be inside the analysis output directory")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for index, part in enumerate(destination.parts[1:-1], start=1):
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                dir_fd=descriptor)
            except FileNotFoundError:
                # Never create ancestors outside the configured output root.
                prefix = Path("/", *destination.parts[1:index + 1])
                if not prefix.is_relative_to(root):
                    raise
                try:
                    os.mkdir(part, mode=0o750, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def write_json(path: str | os.PathLike, payload: object, *, output_root: str | os.PathLike | None = None,
               max_bytes: int = MAX_JSON_BYTES) -> None:
    """Normalize and validate before atomically replacing a regular artifact.

    ``output_root`` is supplied by trusted wrappers/tests; the default is the
    program artifact directory. No partial JSON file becomes a deliverable.
    """
    if not isinstance(path, (str, os.PathLike)):
        raise TypeError("write_json path must be a string or path-like object")
    if type(max_bytes) is not int or max_bytes <= 0 or max_bytes > MAX_JSON_BYTES:
        raise ValueError("max_bytes must be between 1 and 32 MiB")
    destination = Path(path)
    root = Path(output_root) if output_root is not None else DEFAULT_OUTPUT_ROOT
    normalized = normalize_json(payload)
    encoded = json.dumps(normalized, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8") + b"\n"
    if len(encoded) > max_bytes:
        raise ValueError("JSON artifact exceeds the output byte limit")
    # Decode the exact bytes that will be committed, before touching the target.
    json.loads(encoded)
    parent = _open_output_parent(destination, root)
    temporary = ".datalab-json-" + secrets.token_hex(12)
    created = False
    try:
        try:
            target = os.stat(destination.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(target.st_mode):
                raise ValueError("write_json destination must be a regular non-symlink file")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o640, dir_fd=parent)
        created = True
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination.name, src_dir_fd=parent, dst_dir_fd=parent)
        created = False
        os.fsync(parent)
    finally:
        if created:
            os.unlink(temporary, dir_fd=parent)
        os.close(parent)
