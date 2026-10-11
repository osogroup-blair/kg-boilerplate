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
from kg_graph.service import GraphService
from kg_ingestion.crypto import ContentHasher, PayloadCipher
from kg_jobs import faults
from kg_jobs.client import OpsClient
from kg_jobs.dispatcher import Dispatcher
from kg_knowledge.jobs import run_sweeper
from kg_knowledge.services import build_knowledge
from kg_modelgw.calllog import DbCallSink
from kg_modelgw.factory import build_router_stack
from kg_modelgw.health import router_health
from kg_obs import metrics
from kg_obs.logging import configure_logging, get_logger
from kg_obs.tracing import setup_tracing
from prometheus_client import start_http_server
from sqlalchemy import text

from kg_worker import __version__
from kg_worker.jobs import build_registry

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
        engine = create_app_engine(s, pool_size=3, max_overflow=2)
        try:
            await assert_role_is_safe(engine)  # L1
        except UnsafeDatabaseRole as exc:
            log.error("unsafe_database_role", error_type=type(exc).__name__)
            await engine.dispose()
            return EXIT_UNSAFE_ROLE
        sink = DbCallSink(engine)
        router = build_router_stack(s, service="kg-worker", sink=sink)
        graph = GraphService(s, router.transport, router.config)
        sink.start()
        knowledge = build_knowledge(engine, graph, router.config, service="kg-worker")
        faults.configure(s.kg_faults, s.kg_faults_dir)  # test-only failure injection; a no-op unless KG_FAULTS / KG_FAULTS_DIR is set
        registry = build_registry(engine, knowledge.projector, PayloadCipher.from_settings(s), ContentHasher.from_settings(s))
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
            start_http_server(s.metrics_port_worker, registry=metrics.REGISTRY)
        # The dispatcher starts first and never waits for Neo4j: a worker that (re)starts during a graph outage must still normalize interactions, and its
        # projection jobs simply back off until the graph is back (AT-02). Indices (L12) are built once, retried on every heartbeat until they exist.
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
            sweeper=s.projection_sweeper_enabled,
        )  # router health is logged, never fatal
        sweeper: asyncio.Task[None] | None = None
        if s.projection_sweeper_enabled:  # superseded by the outbox; kept only for rollback to P2
            sweeper = asyncio.get_running_loop().create_task(
                run_sweeper(knowledge.projector, stop, interval=s.projection_sweep_interval_seconds), name="projection-sweeper"
            )
        try:
            while not stop.is_set():
                try:
                    await self._beat(engine)
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
            if sweeper is not None:
                sweeper.cancel()
                await asyncio.gather(sweeper, return_exceptions=True)
            try:
                await self._beat(engine, status="stopped")
            except Exception:
                log.warning("final_heartbeat_failed")
            await sink.aclose()
            await graph.aclose()
            await router.aclose()
            await engine.dispose()
            log.info("worker_stopped", worker_id=self.worker_id)
        return 0


def install_signal_handlers(loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
