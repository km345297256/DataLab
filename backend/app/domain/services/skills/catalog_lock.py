"""Serialize filesystem/registry mutations in the deployed single async worker.

The task-aware reentrant lock lets installation and deletion include database
writes in the same critical section as nested registry loading. A child task
never inherits ownership. Multiple workers would require a distributed lock.
"""
import asyncio
from contextlib import asynccontextmanager
from functools import wraps
from weakref import WeakValueDictionary

_locks = WeakValueDictionary()


class _CatalogLock:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.owner = None


@asynccontextmanager
async def skill_catalog_lock():
    key = id(asyncio.get_running_loop())
    state = _locks.get(key)
    if state is None:
        state = _CatalogLock()
        _locks[key] = state
    task = asyncio.current_task()
    if state.owner is task:
        yield
        return
    async with state.lock:
        state.owner = task
        try:
            yield
        finally:
            state.owner = None


def serialized_skill_catalog(operation):
    @wraps(operation)
    async def wrapped(*args, **kwargs):
        async with skill_catalog_lock():
            return await operation(*args, **kwargs)
    return wrapped
