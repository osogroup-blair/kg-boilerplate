"""The handler registry of the outbox dispatcher. A handler is registered per stage name; `build_handlers` is the composition root for the worker."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping

from kg_common.settings import Settings
from kg_extraction.handler import GraphIngestJob
from kg_graph.service import GraphService
from kg_ingestion.crypto import ContentHasher, PayloadCipher
from kg_ingestion.normalize import NormalizeHandler
from kg_jobs.dispatcher import JobHandler
from kg_knowledge.jobs import GraphProjectionHandler
from kg_knowledge.services import KnowledgeServices
from kg_modelgw.config import ModelConfig
from sqlalchemy.ext.asyncio import AsyncEngine


class JobRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, JobHandler] = {}

    def register(self, handler: JobHandler, *, replace: bool = False) -> None:
        if handler.stage in self._handlers and not replace:
            raise ValueError(f"duplicate handler for stage '{handler.stage}'")
        self._handlers[handler.stage] = handler

    def get(self, stage: str) -> JobHandler | None:
        return self._handlers.get(stage)

    def as_mapping(self) -> Mapping[str, JobHandler]:
        return dict(self._handlers)

    def __len__(self) -> int:
        return len(self._handlers)


def build_registry(
    engine: AsyncEngine,
    knowledge: KnowledgeServices,
    graph: GraphService,
    cipher: PayloadCipher,
    hasher: ContentHasher,
    config: ModelConfig,
    settings: Settings,
    flush_calls: Callable[[], Awaitable[None]],
) -> JobRegistry:
    """P3: normalize and graph_projection. P4: graph_ingest (and the projection job also restores an interaction's extraction). P5 adds memory_propose."""
    registry = JobRegistry()
    registry.register(NormalizeHandler(engine, cipher, hasher))
    registry.register(GraphProjectionHandler(knowledge.projector, knowledge.lock))
    registry.register(
        GraphIngestJob(
            engine,
            cipher,
            graph,
            knowledge.projector,
            knowledge.lock,
            knowledge.specs,
            knowledge.extracted,
            config,
            settings,
            flush_calls=flush_calls,
        )
    )
    return registry
