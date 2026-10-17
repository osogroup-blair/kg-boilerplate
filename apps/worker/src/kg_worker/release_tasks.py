"""The worker's phase-9 housekeeping, run from its heartbeat loop: the release gauges, the shared limiter's sweep and the nightly reconcile scan.

* gauges: `reconcile_drift{class}` from the latest completed scan of every workspace, `backup_age_seconds{store}`, `reindex_runs{status}` (aggregates through `ops.*`; no ids);
* the limiter sweep removes counter windows and expired slot leases older than an hour;
* the nightly scan (`RECONCILE_SCAN_HOURS`, 0 disables) is CHECK-ONLY: `ops.claim_reconcile_scan` hands one due workspace to one worker at a time and opens its run, the scan runs in a
  background task so the heartbeat never waits for it, and any non-zero class shows in the gauge (and so in the alert)."""

from __future__ import annotations

import asyncio
import time

from kg_common.settings import Settings
from kg_db.session import system_session
from kg_domain.principal import WorkspaceContext
from kg_graph.service import GraphService
from kg_knowledge.services import KnowledgeServices
from kg_obs import metrics
from kg_obs.logging import get_logger
from kg_reconcile import CLASSES, Reconcile, ReindexService
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

log = get_logger("kg.worker.release")
REINDEX_STATUSES = ("preview", "building", "catching_up", "ready", "activated", "failed", "cancelled", "rolled_back")


class ReleaseTasks:
    def __init__(self, settings: Settings, engine: AsyncEngine, knowledge: KnowledgeServices, graph: GraphService, reindex: ReindexService):
        self._s, self._engine, self._reindex = settings, engine, reindex
        self._last_retire = 0.0
        self._reconcile = Reconcile(engine, knowledge, graph, embedding_version=settings.memory_embedding_version)
        self._scan: asyncio.Task[None] | None = None
        self._last_claim = 0.0
        self._last_sweep = 0.0

    async def beat(self) -> None:
        await self._gauges()
        if self._s.maintenance_mode == "read_only":
            return
        if time.monotonic() - self._last_sweep >= 300.0:
            self._last_sweep = time.monotonic()
            async with system_session(self._engine) as conn:
                await conn.execute(text("SELECT ops.rate_sweep(3600)"))
        if time.monotonic() - self._last_retire >= 300.0:
            self._last_retire = time.monotonic()
            cleared = await self._reindex.clear_expired()
            if cleared:
                log.info("retired_namespaces_cleared", count=cleared)
        await self._maybe_scan()

    async def _gauges(self) -> None:
        async with system_session(self._engine) as conn:
            drift = (await conn.execute(text("SELECT * FROM ops.reconcile_drift()"))).all()
            ages = (await conn.execute(text("SELECT * FROM ops.backup_ages()"))).all()
            reindex = (await conn.execute(text("SELECT * FROM ops.reindex_stats()"))).all()
        seen = {r.drift_class for r in drift}
        for r in drift:
            metrics.reconcile_drift.labels(drift_class=r.drift_class).set(int(r.n))
        for name in CLASSES:
            if name not in seen:  # a class that drained must read 0, not its last value
                metrics.reconcile_drift.labels(drift_class=name).set(0)
        for r in ages:
            metrics.backup_age_seconds.labels(store=r.store).set(-1.0 if r.age_seconds is None else float(r.age_seconds))
        have = {r.status: int(r.runs) for r in reindex}
        for status in REINDEX_STATUSES:
            metrics.reindex_runs.labels(status=status).set(have.get(status, 0))

    async def _maybe_scan(self) -> None:
        hours = self._s.reconcile_scan_hours
        if hours <= 0 or (self._scan is not None and not self._scan.done()):
            return
        if time.monotonic() - self._last_claim < 30.0:
            return
        self._last_claim = time.monotonic()
        async with system_session(self._engine) as conn:
            claimed = (await conn.execute(text("SELECT * FROM ops.claim_reconcile_scan(:h)"), {"h": hours})).first()
        if claimed is None:
            return
        ctx = WorkspaceContext.for_system_task(claimed.ws_id, reason="nightly reconcile scan")
        self._scan = asyncio.create_task(self._run_scan(ctx, claimed.run), name="reconcile-scan")

    async def _run_scan(self, ctx: WorkspaceContext, run_id) -> None:  # type: ignore[no-untyped-def]
        try:
            result = await self._reconcile.run(ctx, scope="all", repair=False, trigger="scheduled", run_id=run_id)
            log.info("reconcile_scan_done", status=result.status, classes=len([c for c, n in result.drift.items() if n]))
        except Exception as exc:
            log.warning("reconcile_scan_failed", error_type=type(exc).__name__)

    async def aclose(self) -> None:
        if self._scan is not None and not self._scan.done():
            self._scan.cancel()
            await asyncio.gather(self._scan, return_exceptions=True)
