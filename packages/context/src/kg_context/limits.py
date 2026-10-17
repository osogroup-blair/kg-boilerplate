"""Per-principal rate limit and per-workspace concurrency for context builds (phase-6 plan §5.14). In-process: the multi-replica limiter is P9.

A token bucket per principal (`CONTEXT_RATE_PER_MINUTE`, burst `CONTEXT_RATE_BURST`) and a cap on builds in flight per workspace. Either refusal is a 429 `rate_limited` with
`retry_after`, counted without any identifier. The monotonic clock is injectable so the tests need no sleeping."""

from __future__ import annotations

import math
import time
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from kg_obs import metrics

from kg_context.errors import RateLimited


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
