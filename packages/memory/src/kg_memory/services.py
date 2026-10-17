"""The memory services as one object, built once per process by the composition root (api, worker, cli)."""

from __future__ import annotations

from dataclasses import dataclass

from kg_common.settings import Settings
from kg_ingestion.crypto import ContentHasher
from kg_knowledge.services import KnowledgeServices
from kg_modelgw.config import ModelConfig
from kg_modelgw.gateway import ModelGateway

from kg_memory.corrections import MemoryCorrectionPort, MemoryCorrections
from kg_memory.reads import MemoryReads
from kg_memory.reviewing import MemoryReviewHandler
from kg_memory.search import MemorySearch
from kg_memory.writers import MemoryWriter


@dataclass
class MemoryServices:
    reads: MemoryReads
    writer: MemoryWriter
    search: MemorySearch
    review_handler: MemoryReviewHandler
    corrections: MemoryCorrections


def build_memory(
    settings: Settings,
    knowledge: KnowledgeServices,
    gateway: ModelGateway | None,
    config: ModelConfig,
    *,
    service: str,
    hasher: ContentHasher | None = None,
) -> MemoryServices:
    """`service` is the process name written into the call log for the query embeddings (kg-api | kg-worker | kg-cli). Registers the memory review decisions
    with the review service (kg_knowledge cannot import this package, so it asks for a handler)."""
    version = settings.memory_embedding_version
    reads = MemoryReads(version)
    writer = MemoryWriter(hasher or ContentHasher.from_settings(settings), reads, knowledge.specs, settings, embedding_version=version)
    handler = MemoryReviewHandler(writer, reads, knowledge.specs)
    for item_type in ("memory_proposal", "memory_conflict", "source_compromised", "consolidation_proposal"):
        knowledge.reviews.register_handler(item_type, handler)
    knowledge.corrections.register_memory(MemoryCorrectionPort())
    return MemoryServices(
        reads=reads,
        writer=writer,
        search=MemorySearch(gateway, config, settings, service=service),
        review_handler=handler,
        corrections=MemoryCorrections(writer, reads, trace_retention_days=settings.context_trace_retention_days),
    )
