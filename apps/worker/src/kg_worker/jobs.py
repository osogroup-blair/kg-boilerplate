"""The handler registry of the outbox dispatcher. A handler is registered per stage name; `build_handlers` is the composition root for the worker."""

from __future__ import annotations

from collections.abc import Mapping

from kg_ingestion.crypto import ContentHasher, PayloadCipher
from kg_ingestion.normalize import NormalizeHandler
from kg_jobs.dispatcher import JobHandler
from kg_knowledge.jobs import GraphProjectionHandler
from kg_knowledge.projection import Projector
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


def build_registry(engine: AsyncEngine, projector: Projector, cipher: PayloadCipher, hasher: ContentHasher) -> JobRegistry:
    """P3: normalize and graph_projection. P4 adds graph_ingest, P5 memory_propose."""
    registry = JobRegistry()
    registry.register(NormalizeHandler(engine, cipher, hasher))
    registry.register(GraphProjectionHandler(projector))
    return registry
