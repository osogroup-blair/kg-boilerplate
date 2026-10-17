"""The shared limiter (phase-9 plan §5 WP7, V4): one budget across API replicas, in Postgres, with no Redis or broker.

* `hit`: a fixed window per key. One upsert on an UNLOGGED table (`ops.rate_hit`); the window edge and the retry-after are the DATABASE clock's, so a skewed API host cannot
  open a window early (FI-22). Fixed windows allow up to twice the limit across a window edge; the limits here are abuse guards, not billing.
* `acquire` / `release`: a concurrency slot with a lease (`ops.slot_acquire`). A process that dies holding a slot frees it when the lease ends.

Keys are made of ids and a purpose, never content. A database error is `LimiterUnavailable`; the CALLER decides what that means (closed for an expensive call, open for the
cheap ones), and the decision is counted."""

from __future__ import annotations

import math
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from kg_db.session import system_session
from kg_obs import metrics
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


class LimiterUnavailable(Exception):
    """The shared limiter's database could not be reached or answered with an error."""


@dataclass(frozen=True)
class Decision:
    admitted: bool
    hits: int
    retry_after: int  # whole seconds until the window ends (at least 1)


class SharedLimiter:
    def __init__(self, engine: AsyncEngine):
        self._engine = engine

    async def hit(self, key: str, *, window_seconds: int, limit: int, cost: int = 1) -> Decision:
        try:
            async with system_session(self._engine) as conn:
                row = (
                    await conn.execute(
                        text("SELECT hits, seconds_left FROM ops.rate_hit(:k, :w, :c)"), {"k": key, "w": window_seconds, "c": cost}
                    )
                ).one()
        except Exception as exc:
            raise LimiterUnavailable(type(exc).__name__) from exc
        return Decision(admitted=int(row.hits) <= limit, hits=int(row.hits), retry_after=max(1, math.ceil(float(row.seconds_left))))

    async def acquire(self, key: str, *, cap: int, lease_seconds: int = 120) -> uuid.UUID | None:
        holder = uuid.uuid4()
        try:
            async with system_session(self._engine) as conn:
                ok = (
                    await conn.execute(
                        text("SELECT ops.slot_acquire(:k, :h, :cap, :lease)"), {"k": key, "h": holder, "cap": cap, "lease": lease_seconds}
                    )
                ).scalar_one()
        except Exception as exc:
            raise LimiterUnavailable(type(exc).__name__) from exc
        return holder if ok else None

    async def release(self, key: str, holder: uuid.UUID) -> None:
        try:
            async with system_session(self._engine) as conn:
                await conn.execute(text("SELECT ops.slot_release(:k, :h)"), {"k": key, "h": holder})
        except Exception:  # the lease expires on its own; a failed release must never fail a finished request
            metrics.limiter_errors_total.labels(limiter="slot", mode="release_failed").inc()

    @asynccontextmanager
    async def slot(self, key: str, *, cap: int, lease_seconds: int = 120) -> AsyncIterator[bool]:
        """Yields True with a slot held (released on exit) or False when the cap is reached."""
        holder = await self.acquire(key, cap=cap, lease_seconds=lease_seconds)
        try:
            yield holder is not None
        finally:
            if holder is not None:
                await self.release(key, holder)

    async def sweep(self) -> int:
        try:
            async with system_session(self._engine) as conn:
                return int((await conn.execute(text("SELECT ops.rate_sweep(3600)"))).scalar_one())
        except Exception as exc:
            raise LimiterUnavailable(type(exc).__name__) from exc
