"""Per-principal rate limit and per-workspace concurrency for context builds (phase-6 plan §5.14). `BuildLimiter` is in-process; `PgBuildLimiter` (P9, LIMITER_BACKEND=postgres)
shares one budget across replicas. Both offer `aadmit`, an async context manager, which is what the API uses.

A token bucket per principal (`CONTEXT_RATE_PER_MINUTE`, burst `CONTEXT_RATE_BURST`) and a cap on builds in flight per workspace. Either refusal is a 429 `rate_limited` with
`retry_after`, counted without any identifier. The monotonic clock is injectable so the tests need no sleeping."""

from __future__ import annotations

import math
import time
import uuid
from collections import defaultdict
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass

from kg_governance.limiter import LimiterUnavailable, SharedLimiter
from kg_obs import metrics

from kg_context.errors import RateLimited, RateLimiterUnavailable


@dataclass
class _Bucket:
    tokens: float
    at: float


class BuildLimiter:
    def __init__(self, per_minute: int, burst: int, workspace_concurrency: int, *, clock: Callable[[], float] = time.monotonic):
        self._rate, self._burst, self._cap, self._clock = per_minute / 60.0, float(burst), workspace_concurrency, clock
        self._buckets: dict[uuid.UUID, _Bucket] = {}
        self._inflight: dict[uuid.UUID, int] = defaultdict(int)

    def _take(self, principal_id: uuid.UUID) -> None:
        now = self._clock()
        b = self._buckets.get(principal_id)
        if b is None:
            b = self._buckets[principal_id] = _Bucket(self._burst, now)
        b.tokens = min(self._burst, b.tokens + (now - b.at) * self._rate)
        b.at = now
        if b.tokens < 1.0:
            metrics.context_rate_limited_total.inc()
            raise RateLimited(math.ceil((1.0 - b.tokens) / self._rate) if self._rate > 0 else 60)
        b.tokens -= 1.0

    @contextmanager
    def admit(self, principal_id: uuid.UUID, workspace_id: uuid.UUID) -> Iterator[None]:
        """Takes a token and a concurrency slot for the length of one build."""
        if self._inflight[workspace_id] >= self._cap:
            metrics.context_rate_limited_total.inc()
            raise RateLimited(1)
        self._take(principal_id)
        self._inflight[workspace_id] += 1
        try:
            yield
        finally:
            self._inflight[workspace_id] -= 1
            if self._inflight[workspace_id] <= 0:
                self._inflight.pop(workspace_id, None)

    @asynccontextmanager
    async def aadmit(self, principal_id: uuid.UUID, workspace_id: uuid.UUID) -> AsyncIterator[None]:
        with self.admit(principal_id, workspace_id):
            yield


class PgBuildLimiter:
    """The same two limits, shared by every API replica through Postgres. The rate is a fixed one-minute window (`per_minute`) plus a short burst window (`burst` per 10 s);
    the concurrency cap is a set of leases, so a replica that dies mid-build frees its slot. When the limiter's database cannot answer, a build is REFUSED by default
    (`fail_closed`: a context build is the expensive call; the rate limit exists to protect the router and the databases) and admitted when the operator chose `open`."""

    def __init__(
        self,
        shared: SharedLimiter,
        per_minute: int,
        burst: int,
        workspace_concurrency: int,
        *,
        fail_closed: bool = True,
        lease_seconds: int = 120,
    ):
        self._shared, self._per_minute, self._burst, self._cap = shared, per_minute, burst, workspace_concurrency
        self._fail_closed, self._lease = fail_closed, lease_seconds

    @asynccontextmanager
    async def aadmit(self, principal_id: uuid.UUID, workspace_id: uuid.UUID) -> AsyncIterator[None]:
        slot_key = f"ctx:slots:{workspace_id}"
        holder: uuid.UUID | None = None
        try:
            holder = await self._shared.acquire(slot_key, cap=self._cap, lease_seconds=self._lease)
            if holder is None:
                metrics.context_rate_limited_total.inc()
                metrics.limiter_decisions_total.labels(limiter="context", backend="postgres", outcome="refused_concurrency").inc()
                raise RateLimited(1)
            minute = await self._shared.hit(f"ctx:rpm:{principal_id}", window_seconds=60, limit=self._per_minute)
            burst = await self._shared.hit(f"ctx:burst:{principal_id}", window_seconds=10, limit=self._burst)
            if not minute.admitted or not burst.admitted:
                metrics.context_rate_limited_total.inc()
                metrics.limiter_decisions_total.labels(limiter="context", backend="postgres", outcome="refused_rate").inc()
                raise RateLimited(minute.retry_after if not minute.admitted else burst.retry_after)
            metrics.limiter_decisions_total.labels(limiter="context", backend="postgres", outcome="admitted").inc()
        except LimiterUnavailable as exc:
            if self._fail_closed:
                metrics.limiter_errors_total.labels(limiter="context", mode="failed_closed").inc()
                raise RateLimiterUnavailable() from exc
            metrics.limiter_errors_total.labels(limiter="context", mode="failed_open").inc()
        except RateLimited:
            if holder is not None:
                await self._shared.release(slot_key, holder)
            raise
        try:
            yield
        finally:
            if holder is not None:
                await self._shared.release(slot_key, holder)
