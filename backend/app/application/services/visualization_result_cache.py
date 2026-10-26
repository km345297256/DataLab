"""Short-lived, private reuse of bounded derived visualization JSON.

Never stores source files, byte responses, media, mount data or persistent input
copies. Callers must reauthorize the file and plugin on *every* hit. Each live
FileService owns one cache; restarting the backend discards it.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass


@dataclass(frozen=True)
class _Entry:
    data: bytes
    expires: float


class VisualizationResultCache:
    def __init__(self, *, max_bytes=32 * 1024 * 1024, max_entry_bytes=2 * 1024 * 1024,
                 max_entries=128, per_user_entries=8, per_user_bytes=8 * 1024 * 1024,
                 ttl=60, clock=time.monotonic):
        self.max_bytes = max_bytes
        self.max_entry_bytes = max_entry_bytes
        self.max_entries = max_entries
        self.per_user_entries = per_user_entries
        self.per_user_bytes = per_user_bytes
        self.ttl = ttl
        self.clock = clock
        self._entries: OrderedDict[tuple[str, str], _Entry] = OrderedDict()
        self.size_bytes = 0

    @staticmethod
    def key(*identity) -> str | None:
        try:
            encoded = json.dumps(identity, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
        except (TypeError, ValueError):
            return None  # The normal reader remains responsible for rejecting invalid options.
        return hashlib.sha256(encoded).hexdigest() if len(encoded) <= 64 * 1024 else None

    def _remove(self, key):
        self.size_bytes -= len(self._entries.pop(key).data)

    def _expire(self):
        now = self.clock()
        for key, entry in list(self._entries.items()):
            if entry.expires <= now:
                self._remove(key)

    def get(self, user_id: str, key: str | None) -> bytes | None:
        self._expire()
        if not user_id or key is None:
            return None
        scoped = (user_id, key)
        entry = self._entries.get(scoped)
        if entry is None:
            return None
        self._entries.move_to_end(scoped)
        return entry.data

    def put(self, user_id: str, key: str | None, data: bytes):
        self._expire()
        if not user_id or key is None or not 0 < len(data) <= min(self.max_entry_bytes, self.max_bytes, self.per_user_bytes):
            return
        scoped = (user_id, key)
        if scoped in self._entries:
            self._remove(scoped)
        owned = [item for item in self._entries if item[0] == user_id]
        owned_bytes = sum(len(self._entries[item].data) for item in owned)
        while owned and (len(owned) >= self.per_user_entries or owned_bytes + len(data) > self.per_user_bytes):
            oldest = owned.pop(0)
            owned_bytes -= len(self._entries[oldest].data)
            self._remove(oldest)
        while self._entries and (len(self._entries) >= self.max_entries or self.size_bytes + len(data) > self.max_bytes):
            self._remove(next(iter(self._entries)))
        self._entries[scoped] = _Entry(data, self.clock() + self.ttl)
        self.size_bytes += len(data)
