"""Per-principal quotas and usage (phase-7 plan §5.5, T8).

Three limits, each from the principal's registration (null: the platform default):

* requests per minute, on every authenticated endpoint: an in-process token bucket, or (LIMITER_BACKEND=postgres, P9) one fixed one-minute window shared by every replica;
* context tokens per UTC day: the sum of `used_tokens` over the principal's context traces today, so a restart loses nothing;
* interactions per UTC day: the count of its accepted interactions today.

A refusal is `429 quota_exceeded` with `retry_after` (the next minute's refill, or the next UTC midnight by the DATABASE clock) and `details.quota`.

Usage (`agent_usage_daily`): the request counter is buffered in process and flushed every few seconds, like `model_calls`; everything else is recomputed from the durable tables
(traces, interactions, memories) at flush and at read, so a crash before a flush loses at most the last window of request counts and nothing else.
"""

from __future__ import annotations

import asyncio
import math
import time
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass

from kg_common.errors import PlatformError
from kg_db.session import tenant_session
from kg_domain.principal import Principal, WorkspaceContext
from kg_obs import metrics
from kg_obs.logging import get_logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from kg_governance.audit import AuditMetadata, AuditWriter
from kg_governance.limiter import LimiterUnavailable, SharedLimiter
from kg_governance.principals import QuotasOut, UsageDay, UsageOut

log = get_logger("kg.quotas")
QuotaName = str  # requests_per_minute | context_tokens_per_day | interactions_per_day
_DAY_START = "(date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC')"  # the database clock's UTC midnight
_SECONDS_TO_MIDNIGHT = f"EXTRACT(EPOCH FROM ({_DAY_START} + interval '1 day' - now()))::float8"


class QuotaExceeded(PlatformError):
    code, http_status, default_message = "quota_exceeded", 429, "A quota of this principal is exhausted"

    def __init__(self, quota: QuotaName, retry_after: int):
        super().__init__(retryable=True, retry_after=max(1, retry_after), details={"quota": quota})


@dataclass
class _Bucket:
    tokens: float
    at: float
    rate_per_minute: int


@dataclass
class _Cached:
    quotas: QuotasOut
    at: float
    workspace_id: uuid.UUID | None = None


class Quotas:
    """Built once per API process. `clock` is injectable so the tests need no sleeping."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        default_requests_per_minute: int,
        flush_seconds: float = 10.0,
        cache_seconds: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        shared: SharedLimiter | None = None,
    ):
        self._engine, self._default_rpm, self._flush_s, self._cache_s, self._clock = (
            engine,
            default_requests_per_minute,
            flush_seconds,
            cache_seconds,
            clock,
        )
        self._shared = shared  # P9: one budget across replicas; None keeps the in-process bucket
        self._buckets: dict[uuid.UUID, _Bucket] = {}
        self._cache: dict[uuid.UUID, _Cached] = {}
        self._buffer: dict[tuple[uuid.UUID, uuid.UUID], int] = defaultdict(int)  # (workspace, principal) -> requests since the last flush
        self._first_exceeded: dict[tuple[uuid.UUID, str], float] = {}
        self._task: asyncio.Task[None] | None = None

    @property
    def defaults(self) -> QuotasOut:
        return QuotasOut(requests_per_minute=self._default_rpm, context_tokens_per_day=None, interactions_per_day=None)

    # ---- the registration's limits ---------------------------------------------------------------------------------------------------------

    async def limits(self, principal: Principal) -> QuotasOut:
        cached = self._cache.get(principal.principal_id)
        now = self._clock()
        if cached is not None and now - cached.at < self._cache_s:
            return cached.quotas
        async with tenant_session(self._engine, WorkspaceContext.of(principal), principal.principal_id) as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT quota_requests_per_minute, quota_context_tokens_per_day, quota_interactions_per_day FROM principals "
                        "WHERE workspace_id = :ws AND id = :p"
                    ),
                    {"ws": principal.workspace_id, "p": principal.principal_id},
                )
            ).first()
        quotas = QuotasOut(
            requests_per_minute=row.quota_requests_per_minute if row else None,
            context_tokens_per_day=row.quota_context_tokens_per_day if row else None,
            interactions_per_day=row.quota_interactions_per_day if row else None,
        )
        self._cache[principal.principal_id] = _Cached(quotas, now, principal.workspace_id)
        return quotas

    def forget(self, principal_id: uuid.UUID) -> None:
        """A registration changed in this process: the next request reads it afresh (other processes learn from the invalidation notification, or within the cache window)."""
        self._cache.pop(principal_id, None)
        self._buckets.pop(principal_id, None)

    def forget_workspace(self, workspace_id: uuid.UUID | None) -> None:
        """A principal, grant or key of this workspace changed somewhere (None: drop everything, e.g. after the listener reconnected)."""
        for pid in [p for p, c in self._cache.items() if workspace_id is None or c.workspace_id == workspace_id]:
            self.forget(pid)

    # ---- requests per minute ---------------------------------------------------------------------------------------------------------------

    async def admit_request(self, principal: Principal) -> None:
        limits = await self.limits(principal)
        rpm = limits.requests_per_minute or self._default_rpm
        if self._shared is not None:
            await self._admit_shared(principal, rpm)
            return
        now = self._clock()
        b = self._buckets.get(principal.principal_id)
        if b is None or b.rate_per_minute != rpm:
            b = self._buckets[principal.principal_id] = _Bucket(float(rpm), now, rpm)
        rate = rpm / 60.0
        b.tokens = min(float(rpm), b.tokens + (now - b.at) * rate)
        b.at = now
        if b.tokens < 1.0:
            await self._refused(principal, "requests_per_minute", math.ceil((1.0 - b.tokens) / rate))
        b.tokens -= 1.0
        self._buffer[(principal.workspace_id, principal.principal_id)] += 1

    async def _admit_shared(self, principal: Principal, rpm: int) -> None:
        assert self._shared is not None
        try:
            decision = await self._shared.hit(f"rpm:{principal.principal_id}", window_seconds=60, limit=rpm)
        except LimiterUnavailable:
            # a cheap call fails OPEN: if the database is down nothing else answers either, and blocking every request on a counter helps nobody (expensive calls fail closed)
            metrics.limiter_errors_total.labels(limiter="requests", mode="failed_open").inc()
            self._buffer[(principal.workspace_id, principal.principal_id)] += 1
            return
        metrics.limiter_decisions_total.labels(
            limiter="requests", backend="postgres", outcome="admitted" if decision.admitted else "refused"
        ).inc()
        if not decision.admitted:
            await self._refused(principal, "requests_per_minute", decision.retry_after)
        self._buffer[(principal.workspace_id, principal.principal_id)] += 1

    async def _refused(self, principal: Principal, quota: str, retry_after: int) -> None:
        metrics.quota_exceeded_total.labels(quota=quota).inc()
        key = (principal.principal_id, quota)
        now = self._clock()
        if now - self._first_exceeded.get(key, -1e9) > 3600:  # audited once per principal per quota per hour (sampled)
            self._first_exceeded[key] = now
            try:
                async with tenant_session(self._engine, WorkspaceContext.of(principal), principal.principal_id) as conn:
                    await AuditWriter(conn, principal, request_id=None, correlation_id=None).record(
                        "quota.exceeded",
                        outcome="denied",
                        resource_type="principal",
                        resource_id=principal.principal_id,
                        metadata=AuditMetadata(quota=quota),
                    )
            except Exception:
                log.warning("quota_audit_failed")
        raise QuotaExceeded(quota, retry_after)

    # ---- daily limits (read from the durable tables) -------------------------------------------------------------------------------------

    async def admit_context(self, conn: AsyncConnection, principal: Principal) -> None:
        limit = (await self.limits(principal)).context_tokens_per_day
        if limit is None:
            return
        used, seconds = await self._today(
            conn, principal, "context_traces", "COALESCE(sum(used_tokens), 0)", "principal_id", "created_at", extra="AND act_as_by IS NULL"
        )
        if used >= limit:
            await self._refused(principal, "context_tokens_per_day", math.ceil(seconds))

    async def admit_interaction(self, conn: AsyncConnection, principal: Principal) -> None:
        limit = (await self.limits(principal)).interactions_per_day
        if limit is None:
            return
        used, seconds = await self._today(conn, principal, "interactions", "count(*)", "submitted_by", "recorded_at")
        if used >= limit:
            await self._refused(principal, "interactions_per_day", math.ceil(seconds))

    @staticmethod
    async def _today(
        conn: AsyncConnection, principal: Principal, table: str, agg: str, who: str, when: str, *, extra: str = ""
    ) -> tuple[int, float]:
        # `table`, `agg`, `who`, `when` and `extra` are constants chosen by this module, never request values (a Lab build is the admin's looking, not the agent's use)
        sql = f"SELECT {agg}::bigint AS used, {_SECONDS_TO_MIDNIGHT} AS seconds FROM {table} WHERE workspace_id = :ws AND {who} = :p AND {when} >= {_DAY_START} {extra}"  # noqa: S608
        row = (await conn.execute(text(sql), {"ws": principal.workspace_id, "p": principal.principal_id})).one()
        return int(row.used), float(row.seconds)

    # ---- usage -----------------------------------------------------------------------------------------------------------------------------

    async def usage(self, conn: AsyncConnection, principal_id: uuid.UUID, workspace_id: uuid.UUID, limits: QuotasOut) -> UsageOut:
        row = (
            await conn.execute(
                text(
                    f"SELECT (now() AT TIME ZONE 'UTC')::date AS day, "  # noqa: S608 (constants)
                    f"(SELECT count(*) FROM context_traces WHERE workspace_id = :ws AND principal_id = :p AND created_at >= {_DAY_START} AND act_as_by IS NULL) AS builds, "
                    f"(SELECT COALESCE(sum(used_tokens), 0) FROM context_traces WHERE workspace_id = :ws AND principal_id = :p AND created_at >= {_DAY_START} AND act_as_by IS NULL) AS tokens, "
                    f"(SELECT count(*) FROM interactions WHERE workspace_id = :ws AND submitted_by = :p AND recorded_at >= {_DAY_START}) AS interactions, "
                    f"(SELECT count(*) FROM curated_memories WHERE workspace_id = :ws AND created_by = :p AND origin = 'proposal' AND recorded_at >= {_DAY_START}) AS proposals, "
                    f"COALESCE((SELECT requests FROM agent_usage_daily WHERE workspace_id = :ws AND principal_id = :p AND day = (now() AT TIME ZONE 'UTC')::date), 0) AS requests"
                ),
                {"ws": workspace_id, "p": principal_id},
            )
        ).one()
        past = (
            await conn.execute(
                text(
                    "SELECT day, requests, context_builds, context_tokens, interactions, proposals FROM agent_usage_daily "
                    "WHERE workspace_id = :ws AND principal_id = :p AND day < (now() AT TIME ZONE 'UTC')::date ORDER BY day DESC LIMIT 14"
                ),
                {"ws": workspace_id, "p": principal_id},
            )
        ).all()
        return UsageOut(
            history=[UsageDay(**dict(r._mapping)) for r in past],
            day=row.day,
            requests=int(row.requests) + self._buffer.get((workspace_id, principal_id), 0),
            context_builds=int(row.builds),
            context_tokens=int(row.tokens),
            interactions=int(row.interactions),
            proposals=int(row.proposals),
            quotas=limits,
            defaults=self.defaults,
        )

    # ---- the buffer ---------------------------------------------------------------------------------------------------------------------------

    async def flush(self) -> int:
        """Writes the buffered request counts and refreshes the durable counters of every principal touched. Returns how many principals were written."""
        pending, self._buffer = self._buffer, defaultdict(int)
        written = 0
        for (ws, pid), n in pending.items():
            try:
                async with tenant_session(self._engine, WorkspaceContext.for_system_task(ws, reason="usage flush")) as conn:
                    await conn.execute(
                        text(
                            f"INSERT INTO agent_usage_daily (workspace_id, principal_id, day, requests, context_builds, context_tokens, interactions, proposals) "  # noqa: S608
                            f"SELECT :ws, :p, (now() AT TIME ZONE 'UTC')::date, :n, "
                            f"(SELECT count(*) FROM context_traces WHERE workspace_id = :ws AND principal_id = :p AND created_at >= {_DAY_START} AND act_as_by IS NULL), "
                            f"(SELECT COALESCE(sum(used_tokens), 0) FROM context_traces WHERE workspace_id = :ws AND principal_id = :p AND created_at >= {_DAY_START} AND act_as_by IS NULL), "
                            f"(SELECT count(*) FROM interactions WHERE workspace_id = :ws AND submitted_by = :p AND recorded_at >= {_DAY_START}), "
                            f"(SELECT count(*) FROM curated_memories WHERE workspace_id = :ws AND created_by = :p AND origin = 'proposal' AND recorded_at >= {_DAY_START}) "
                            f"ON CONFLICT (workspace_id, principal_id, day) DO UPDATE SET requests = agent_usage_daily.requests + EXCLUDED.requests, "
                            f"context_builds = EXCLUDED.context_builds, context_tokens = EXCLUDED.context_tokens, interactions = EXCLUDED.interactions, "
                            f"proposals = EXCLUDED.proposals, updated_at = now()"
                        ),
                        {"ws": ws, "p": pid, "n": n},
                    )
                written += 1
            except Exception as exc:
                self._buffer[(ws, pid)] += n  # keep the count for the next window
                log.warning("usage_flush_failed", error_type=type(exc).__name__)
        return written

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="usage-flush")

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._flush_s)
            await self.flush()

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self.flush()
