from __future__ import annotations

import asyncio
import os
import signal
import socket
import time
import uuid

from kg_common.settings import Settings
from kg_db.engine import UnsafeDatabaseRole, assert_role_is_safe, create_app_engine
from kg_db.session import system_session
from kg_governance.runtime import build_invalidator, namespace_overrides
from kg_graph.service import GraphService
from kg_ingestion.crypto import ContentHasher, PayloadCipher
from kg_jobs import faults
from kg_jobs.client import OpsClient
from kg_jobs.dispatcher import Dispatcher
from kg_knowledge.services import build_knowledge
from kg_memory.deletion import RetentionSweeper, build_deletion
from kg_memory.services import build_memory
from kg_modelgw.calllog import DbCallSink
from kg_modelgw.factory import build_router_stack
from kg_modelgw.health import router_health
from kg_obs import metrics
from kg_obs.logging import configure_logging, get_logger
from kg_obs.tracing import setup_tracing
from kg_reconcile import ReindexService
from prometheus_client import start_http_server
from sqlalchemy import text

from kg_worker import __version__
from kg_worker.jobs import build_registry
from kg_worker.release_tasks import ReleaseTasks

log = get_logger("kg.worker")
EXIT_UNSAFE_ROLE = 3

_BEAT = text("""
    INSERT INTO worker_heartbeats (worker_id, hostname, version, started_at, last_beat_at, status)
    VALUES (:id, :host, :ver, :started, now(), :status)
    ON CONFLICT (worker_id) DO UPDATE SET last_beat_at = now(), status = EXCLUDED.status""")


class Worker:
    def __init__(self, settings: Settings, *, serve_metrics: bool = False):
        self.settings, self.serve_metrics = settings, serve_metrics
        self.worker_id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.started_at = time.time()
        self.indices_ready = False
        self._last_trace_purge = 0.0
        self._last_retention = 0.0

    async def _beat(self, engine, status: str = "running") -> None:  # type: ignore[no-untyped-def]
        async with system_session(engine) as conn:
            await conn.execute(
                _BEAT,
                {
                    "id": self.worker_id,
                    "host": socket.gethostname(),
                    "ver": __version__,
                    "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.started_at)),
                    "status": status,
                },
            )
        if status == "running":
            metrics.worker_heartbeat_unixtime.set(time.time())

    async def _review_gauges(self, engine) -> None:  # type: ignore[no-untyped-def]
        """Open review items and the age of the oldest, per type, across all workspaces (aggregates only: ops.review_stats carries no ids)."""
        async with system_session(engine) as conn:
            rows = (await conn.execute(text("SELECT * FROM ops.review_stats()"))).all()
        seen = set()
        for r in rows:
            seen.add(r.item_type)
            metrics.review_items_open.labels(type=r.item_type).set(r.open_items)
            metrics.review_age_seconds.labels(type=r.item_type).set(r.oldest_age_seconds or 0)
        for kind in (
            "identity_match",
            "contradiction",
            "graphiti_invalidation",
            "policy_violation",
            "memory_proposal",
            "memory_conflict",
            "source_compromised",
        ):
            if kind not in seen:  # a type that drained must read 0, not its last value
                metrics.review_items_open.labels(type=kind).set(0)
                metrics.review_age_seconds.labels(type=kind).set(0)

    async def _memory_gauges(self, engine) -> None:  # type: ignore[no-untyped-def]
        """Memories by type and status, and how many live ones have a vector, across all workspaces (aggregates only: ops.memory_stats carries no ids)."""
        async with system_session(engine) as conn:
            rows = (await conn.execute(text("SELECT * FROM ops.memory_stats()"))).all()
        counts: dict[tuple[str, str], int] = {}
        live = embedded = 0
        for r in rows:
            counts[(r.type, r.status)] = counts.get((r.type, r.status), 0) + int(r.memories)
            if r.status == "active" and r.embedding_status != "skipped_by_policy":
                live += int(r.memories)
                embedded += int(r.memories) if r.embedding_status == "ready" else 0
        for memory_type in ("working", "episodic", "semantic", "procedural"):
            for status in ("proposed", "active", "superseded", "disputed", "expired", "rejected", "deleted"):
                metrics.memories_total.labels(type=memory_type, status=status).set(counts.get((memory_type, status), 0))
        metrics.memory_embedding_coverage.set(embedded / live if live else 1.0)

    async def _principal_gauges(self, engine) -> None:  # type: ignore[no-untyped-def]
        """Principals by kind and status and grants by scope kind, across all workspaces (P7; aggregates only: ops.principal_stats / ops.grant_stats carry no ids or handles)."""
        async with system_session(engine) as conn:
            principals = (await conn.execute(text("SELECT * FROM ops.principal_stats()"))).all()
            grants = (await conn.execute(text("SELECT * FROM ops.grant_stats()"))).all()
        have = {(r.kind, r.status): int(r.principals) for r in principals}
        for kind in ("user", "app", "agent", "service"):
            for status in ("active", "revoked"):
                metrics.principals_total.labels(kind=kind, status=status).set(have.get((kind, status), 0))
        held = {r.scope_kind: int(r.grants) for r in grants}
        for scope_kind in ("workspace", "subject", "project", "agent", "session", "classification"):
            metrics.grants_total.labels(scope_kind=scope_kind).set(held.get(scope_kind, 0))

    async def _context_housekeeping(self, engine) -> None:  # type: ignore[no-untyped-def]
        """Retention for the context engine (P6): expired debug snapshots go every beat (they hold task text, encrypted), traces older than CONTEXT_TRACE_RETENTION_DAYS
        every ten minutes. The functions return counts and nothing else; no workspace is named."""
        async with system_session(engine) as conn:
            snapshots = (await conn.execute(text("SELECT ops.purge_expired_context_debug() AS n"))).one().n
            if time.monotonic() - self._last_trace_purge >= 600.0:
                self._last_trace_purge = time.monotonic()
                purged = (
                    (
                        await conn.execute(
                            text("SELECT ops.purge_expired_context_traces(make_interval(days => :d)) AS n"),
                            {"d": self.settings.context_trace_retention_days},
                        )
                    )
                    .one()
                    .n
                )
                if purged:
                    log.info("context_traces_purged", count=int(purged))
        if snapshots:
            log.info("context_debug_snapshots_purged", count=int(snapshots))

    async def _governance_housekeeping(self, engine) -> None:  # type: ignore[no-untyped-def]
        """P8: the deletion gauges every beat (aggregates only: ops.deletion_stats carries no ids), and the retention sweep every RETENTION_SWEEP_SECONDS."""
        async with system_session(engine) as conn:
            rows = (await conn.execute(text("SELECT * FROM ops.deletion_stats()"))).all()
        have = {(r.kind, r.status): int(r.requests) for r in rows}
        for kind in ("subject", "interaction", "memory", "principal_data", "workspace", "retention"):
            for status in ("frozen", "planned", "purging", "verifying", "blocked_legal_hold", "failed", "completed_with_exceptions"):
                metrics.deletion_requests_open.labels(kind=kind, status=status).set(have.get((kind, status), 0))
        if time.monotonic() - self._last_retention >= self.settings.retention_sweep_seconds:
            self._last_retention = time.monotonic()
            swept = await self._sweeper.sweep()
            if swept:
                log.info("retention_swept", **{k: v for k, v in swept.items()})

    async def _ensure_indices(self, graph: GraphService, *, attempts: int) -> None:
        for i in range(attempts):
            try:
                await graph.ensure_indices()
                self.indices_ready = True
                metrics.dependency_up.labels(dependency="neo4j").set(1)
                return
            except Exception as exc:
                metrics.dependency_up.labels(dependency="neo4j").set(0)
                log.warning("graph_index_bootstrap_failed", attempt=i + 1, error_type=type(exc).__name__)
                await asyncio.sleep(min(2.0 * (i + 1), 10.0))

    async def run(self, stop: asyncio.Event) -> int:
        s = self.settings
        configure_logging(s.log_level, service="kg-worker", json_output=True)
        setup_tracing("kg-worker", s.otel_exporter_otlp_endpoint)
        engine = create_app_engine(
            s, pool_size=6, max_overflow=4
        )  # a graph job holds one connection for its lock and needs another for its transactions
        try:
            await assert_role_is_safe(engine)  # L1
        except UnsafeDatabaseRole as exc:
            log.error("unsafe_database_role", error_type=type(exc).__name__)
            await engine.dispose()
            return EXIT_UNSAFE_ROLE
        sink = DbCallSink(engine)
        router = build_router_stack(s, service="kg-worker", sink=sink)
        graph = GraphService(s, router.transport, router.config, namespace_loader=lambda: namespace_overrides(engine))
        try:
            await graph.namespaces.refresh()
        except Exception:
            log.warning("graph_namespaces_unreadable_at_startup")
        invalidator = build_invalidator(s, egress=router.narrowing, namespaces=graph.namespaces)
        if invalidator is not None:
            invalidator.start()
        sink.start()
        knowledge = build_knowledge(engine, graph, router.config, service="kg-worker", settings=s)
        faults.configure(s.kg_faults, s.kg_faults_dir)  # test-only failure injection; a no-op unless KG_FAULTS / KG_FAULTS_DIR is set
        hasher = ContentHasher.from_settings(s)
        memory = build_memory(s, knowledge, router.gateway, router.config, service="kg-worker", hasher=hasher)
        cipher = PayloadCipher.from_settings(s)
        deletion = build_deletion(s, engine, knowledge, graph, cipher, hasher, memory.writer)
        registry = build_registry(engine, knowledge, graph, cipher, hasher, router.config, s, sink.flush, memory, router.gateway, deletion)
        self._sweeper = RetentionSweeper(engine, deletion.service, graph)
        release = ReleaseTasks(
            s, engine, knowledge, graph, ReindexService(engine, graph, knowledge, cipher, router.config, s, service="kg-worker")
        )
        dispatcher = Dispatcher(
            OpsClient(engine),
            registry.as_mapping(),
            worker_id=self.worker_id,
            concurrency=s.worker_concurrency,
            stage_limits=s.stage_limits(),
            lease_seconds=s.worker_lease_seconds,
            poll_busy=s.worker_poll_busy_seconds,
            poll_idle=s.worker_poll_idle_seconds,
            shutdown_grace=s.worker_shutdown_grace_seconds,
        )
        if self.serve_metrics:
            start_http_server(s.metrics_port_worker, addr=s.metrics_host, registry=metrics.REGISTRY)
        # The dispatcher starts first and never waits for Neo4j: a worker that (re)starts during a graph outage must still normalize interactions, and its
        # projection jobs simply back off until the graph is back (AT-02). Indices (L12) are built once, retried on every heartbeat until they exist.
        if s.maintenance_mode == "read_only":
            log.warning("worker_read_only", detail="MAINTENANCE_MODE=read_only: the dispatcher is not started, no job runs")
            outbox: asyncio.Task[object] = asyncio.get_running_loop().create_task(_idle(stop), name="outbox-dispatcher-idle")
        else:
            outbox = asyncio.get_running_loop().create_task(dispatcher.run(stop), name="outbox-dispatcher")
        await self._ensure_indices(graph, attempts=1)
        health = await router_health(s.router_origin, router.http, router.config)
        log.info(
            "worker_started",
            worker_id=self.worker_id,
            router_mode=s.oso_router_mode,
            router_status=health.status,
            handlers=len(registry),
            concurrency=s.worker_concurrency,
            lease_seconds=s.worker_lease_seconds,
            indices_ready=self.indices_ready,
        )  # router health is logged, never fatal
        try:
            while not stop.is_set():
                try:
                    await self._beat(engine)
                    await self._review_gauges(engine)
                    await self._memory_gauges(engine)
                    await self._principal_gauges(engine)
                    if s.maintenance_mode != "read_only":  # a restored environment under verification changes nothing by itself
                        await self._context_housekeeping(engine)
                        await self._governance_housekeeping(engine)
                    await release.beat()
                    if not self.indices_ready:
                        await self._ensure_indices(graph, attempts=1)
                except Exception as exc:
                    log.warning("heartbeat_failed", error_type=type(exc).__name__)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=s.worker_heartbeat_seconds)
                except TimeoutError:
                    pass
        finally:
            stop.set()
            await asyncio.gather(outbox, return_exceptions=True)  # graceful: waits for in-flight jobs, then hands their leases back
            try:
                await self._beat(engine, status="stopped")
            except Exception:
                log.warning("final_heartbeat_failed")
            await release.aclose()
            if invalidator is not None:
                await invalidator.aclose()
            await sink.aclose()
            await graph.aclose()
            await router.aclose()
            await engine.dispose()
            log.info("worker_stopped", worker_id=self.worker_id)
        return 0


async def _idle(stop: asyncio.Event) -> None:
    await stop.wait()


def install_signal_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
