"""An in-process outbox worker for API-level tests: the production Dispatcher with the production handlers, over the app's own engine and services."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from kg_extraction.handler import GraphIngestJob
from kg_extraction.simulation import SimulationCleanupJob, SimulationJob
from kg_ingestion.normalize import NormalizeHandler
from kg_jobs.client import OpsClient
from kg_jobs.dispatcher import Dispatcher
from kg_knowledge.jobs import GraphProjectionHandler
from kg_memory.consolidate import MemoryConsolidateJob
from kg_memory.deletion_jobs import DeletionStageJob
from kg_memory.extract import MemoryProposeJob
from kg_memory.jobs import MemoryEmbedJob, MemoryExpireJob
from kg_reconcile.reindex import ReindexJob


@asynccontextmanager
async def running_worker(
    api: Any,
    *,
    stages: tuple[str, ...] = (
        "normalize",
        "graph_ingest",
        "graph_projection",
        "memory_propose",
        "memory_embed",
        "memory_expire",
        "memory_consolidate",
        "deletion_plan",
        "deletion_purge",
        "deletion_verify",
        "ontology_simulation",
        "simulation_cleanup",
        "graph_reindex",
    ),
    lease_seconds: int = 10,
    concurrency: int = 4,
) -> AsyncIterator[Dispatcher]:
    st = api.app.state.kg
    handlers: dict[str, Any] = {
        "normalize": NormalizeHandler(st.engine, st.ingestion.cipher, st.ingestion.hasher),
        "graph_projection": GraphProjectionHandler(st.knowledge.projector, st.knowledge.lock),
        "graph_ingest": GraphIngestJob(
            st.engine,
            st.ingestion.cipher,
            st.graph,
            st.knowledge.projector,
            st.knowledge.lock,
            st.knowledge.specs,
            st.knowledge.extracted,
            st.router.config,
            st.settings,
            flush_calls=st.call_sink.flush,
        ),
        "memory_propose": MemoryProposeJob(
            st.engine,
            st.ingestion.cipher,
            st.router.gateway,
            st.memory.writer,
            st.knowledge.specs,
            st.router.config,
            st.settings,
            flush_calls=st.call_sink.flush,
        ),
        "memory_embed": MemoryEmbedJob(st.engine, st.router.gateway, st.router.config, st.settings, service="kg-api"),
        "memory_expire": MemoryExpireJob(st.engine),
        "memory_consolidate": MemoryConsolidateJob(
            st.engine, st.router.gateway, st.memory.writer, st.knowledge.specs, st.router.config, st.settings, service="kg-api"
        ),
        "graph_reindex": ReindexJob(st.reindex),
        "ontology_simulation": SimulationJob(st.simulation),
        "simulation_cleanup": SimulationCleanupJob(st.simulation),
        "deletion_plan": DeletionStageJob(st.engine, st.deletion.service, "deletion_plan"),
        "deletion_purge": DeletionStageJob(st.engine, st.deletion.service, "deletion_purge"),
        "deletion_verify": DeletionStageJob(st.engine, st.deletion.service, "deletion_verify"),
    }
    dispatcher = Dispatcher(
        OpsClient(st.engine),
        {s: handlers[s] for s in stages},
        worker_id=f"test-worker-{uuid.uuid4().hex[:8]}",
        concurrency=concurrency,
        lease_seconds=lease_seconds,
        poll_busy=0.02,
        poll_idle=0.1,
        shutdown_grace=5.0,
        stats_interval=3600,
    )
    stop = asyncio.Event()
    task = asyncio.create_task(dispatcher.run(stop))
    try:
        yield dispatcher
    finally:
        stop.set()
        await asyncio.wait_for(task, 30)


async def wait_until(check: Callable[[], Awaitable[Any]], *, timeout: float = 60.0, interval: float = 0.1, what: str = "condition") -> Any:  # noqa: ASYNC109
    """Polls `check` until it returns something truthy; the host is often heavily loaded, so the default budget is generous."""
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = await check()
        if last:
            return last
        await asyncio.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {what} (last: {last!r})")
