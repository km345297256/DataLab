"""Private artificial-only cache transport for opt-in acceptance continuations."""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import re
import stat
import tempfile
import time


def validate_state(value, scenario):
    assert set(value) == {"version", "scenario", "entries"}
    assert value["version"] == 1 and value["scenario"] == scenario
    assert isinstance(value["entries"], dict)
    for key, entry in value["entries"].items():
        assert re.fullmatch(r"datalab:skill-build:v1:(artifact|adverse|revision):[a-f0-9]{64}", key)
        assert set(entry) == {"expires_at", "value"} and isinstance(entry["value"], dict)
        assert type(entry["expires_at"]) in (int, float) and math.isfinite(entry["expires_at"])
    return json.loads(json.dumps(value, allow_nan=False))


class ArtificialCheckpointCache:
    """Production BuildCheckpoints retains all scope, validation and TTL rules."""
    def __init__(self, scenario, snapshot=None, *, persist=None, clock=time.time):
        self.scenario, self.persist, self.clock = scenario, persist, clock
        self.state = validate_state(snapshot or {"version": 1, "scenario": scenario, "entries": {}}, scenario)
        self.stats = {"reads": 0, "hits": 0, "writes": 0, "deletes": 0}

    def snapshot(self):
        return copy.deepcopy(self.state)

    def _saved(self):
        if self.persist is not None:
            self.persist(self.snapshot())

    async def get(self, key):
        self.stats["reads"] += 1
        entry = self.state["entries"].get(key)
        if entry is None or self.clock() >= entry["expires_at"]:
            return None
        self.stats["hits"] += 1
        return copy.deepcopy(entry["value"])

    async def set(self, key, value, ttl=None):
        assert type(ttl) is int and ttl > 0
        candidate = self.snapshot()
        candidate["entries"][key] = {"expires_at": self.clock() + ttl, "value": value}
        self.state = validate_state(candidate, self.scenario)
        self.stats["writes"] += 1
        self._saved()
        return True

    async def delete(self, key):
        existed = self.state["entries"].pop(key, None) is not None
        self.stats["deletes"] += 1
        self._saved()
        return existed


def _private_location(path):
    path = Path(path).absolute()
    parent = path.parent
    if not parent.exists():
        parent.mkdir(mode=0o700, parents=True)
    assert not parent.is_symlink() and stat.S_IMODE(parent.stat().st_mode) == 0o700, "Checkpoint directory must be private (0700)"
    assert not path.is_symlink(), "Checkpoint state must not follow a symlink"
    if path.exists():
        info = path.stat()
        assert stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600, "Checkpoint state must be a private regular file (0600)"
    return path


def load_private_state(path, scenario):
    path = _private_location(path)
    if not path.exists():
        return {"version": 1, "scenario": scenario, "entries": {}}
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as source:
        return validate_state(json.load(source), scenario)


def save_private_state(path, value, scenario):
    path = _private_location(path)
    value = validate_state(value, scenario)
    fd, temporary = tempfile.mkstemp(prefix=".checkpoint-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as target:
            json.dump(value, target, ensure_ascii=False, allow_nan=False)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
