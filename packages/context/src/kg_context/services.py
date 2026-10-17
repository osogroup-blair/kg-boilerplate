"""The context services as one object, built once per process by the composition root (api; the worker only purges)."""

from __future__ import annotations

from dataclasses import dataclass

from kg_common.settings import Settings
from kg_ingestion.crypto import ContentHasher, PayloadCipher
from kg_knowledge.services import KnowledgeServices
from kg_memory.services import MemoryServices
from kg_modelgw.config import ModelConfig
from kg_modelgw.gateway import ModelGateway
from sqlalchemy.ext.asyncio import AsyncEngine

from kg_context.limits import BuildLimiter
from kg_context.service import ContextService
from kg_context.trace import TraceReads, TraceStore


@dataclass
class ContextServices:
    service: ContextService
    limiter: BuildLimiter
    store: TraceStore
    reads: TraceReads


def build_context(
    settings: Settings,
    engine: AsyncEngine,
    knowledge: KnowledgeServices,
    memory: MemoryServices,
    gateway: ModelGateway | None,
    config: ModelConfig,
    cipher: PayloadCipher,
    hasher: ContentHasher,
    *,
    service: str,
) -> ContextServices:
    store = TraceStore(hasher, cipher)
    return ContextServices(
        service=ContextService(settings, engine, knowledge, memory, gateway, config, store, service=service),
        limiter=BuildLimiter(settings.context_rate_per_minute, settings.context_rate_burst, settings.context_workspace_concurrency),
        store=store,
        reads=TraceReads(),
    )
