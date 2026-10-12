"""The per-workspace graph writer lock (P4 Q4).

Graphiti MERGEs on uuid and is not safe against a concurrent writer of the same graph, and its extraction for one workspace must see the previous episode's
result to deduplicate against it. Every writer of a workspace's graph therefore serialises on one lock:

* the outbox serialises jobs that share a `concurrency_key` (`graph:{workspace}`) across workers, and
* this session advisory lock covers the writers the outbox does not see: the command fast path in the API and the operator tools.

A SESSION lock is held on a dedicated pooled connection for the duration of the work and released before the connection returns to the pool. If the
process dies the connection closes and Postgres releases the lock, so a crash cannot leave a workspace locked.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from kg_obs import metrics
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

_TRY = text("SELECT pg_try_advisory_lock(hashtextextended(:k, 0))")
_UNLOCK = text("SELECT pg_advisory_unlock(hashtextextended(:k, 0))")


class GraphBusy(Exception):  # noqa: N818 (a control-flow signal, not an error)
    """Another writer holds this workspace's graph lock."""


def lock_key(workspace_id: uuid.UUID) -> str:
    return f"graph:{workspace_id}"


class GraphLock:
    def __init__(self, engine: AsyncEngine, *, poll_seconds: float = 0.05):
        self._engine, self._poll = engine, poll_seconds

    @asynccontextmanager
    async def hold(self, workspace_id: uuid.UUID, *, wait_seconds: float = 120.0) -> AsyncIterator[None]:
        """Waits up to `wait_seconds` for the lock (0 = a single try). Raises GraphBusy when it could not be taken."""
        key = lock_key(workspace_id)
        started = time.monotonic()
        async with self._engine.connect() as conn:
            delay = self._poll
            while True:
                got = (await conn.execute(_TRY, {"k": key})).scalar_one()
                await conn.rollback()  # a lock is session-scoped; do not keep a transaction open while waiting
                if got:
                    break
                if time.monotonic() - started >= wait_seconds:
                    metrics.graph_lock_wait_seconds.observe(time.monotonic() - started)
                    raise GraphBusy()
                await asyncio.sleep(delay)
                delay = min(delay * 1.5, 0.5)
            metrics.graph_lock_wait_seconds.observe(time.monotonic() - started)
            try:
                yield
            finally:
                try:
                    await conn.execute(_UNLOCK, {"k": key})
                    await conn.commit()
                except Exception:  # noqa: S110
                    pass  # the connection is gone: Postgres has released the session lock with it
