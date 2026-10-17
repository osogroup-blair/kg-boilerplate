"""The knowledge services as one object, built once per process by the composition root (api, worker, cli)."""

from __future__ import annotations

from dataclasses import dataclass

from kg_common.settings import Settings, get_settings
from kg_graph.compile import GraphCompiler
from kg_graph.service import GraphService
from kg_modelgw.config import ModelConfig
from sqlalchemy.ext.asyncio import AsyncEngine

from kg_knowledge.commands import CommandPipeline
from kg_knowledge.corrections import Corrections
from kg_knowledge.extracted import ExtractedKnowledge
from kg_knowledge.graph_reads import GraphReads
from kg_knowledge.graphlock import GraphLock
from kg_knowledge.ontology_service import OntologyService
from kg_knowledge.projection import Projector
from kg_knowledge.reads import Reads
from kg_knowledge.reviews import ReviewService
from kg_knowledge.search import GraphSearch
from kg_knowledge.specs import SpecCache


@dataclass
class KnowledgeServices:
    specs: SpecCache
    ontology: OntologyService
    pipeline: CommandPipeline
    reads: Reads
    projector: Projector
    extracted: ExtractedKnowledge
    reviews: ReviewService
    graph_reads: GraphReads
    search: GraphSearch
    lock: GraphLock
    corrections: Corrections


def build_knowledge(
    engine: AsyncEngine, graph: GraphService, config: ModelConfig, *, service: str, settings: Settings | None = None
) -> KnowledgeServices:
    """`service` is the process name written into the call log for projection embeddings (kg-api | kg-worker | kg-cli)."""
    settings = settings or get_settings()
    specs = SpecCache()
    reads = Reads(specs)
    pipeline = CommandPipeline(specs)
    extracted = ExtractedKnowledge()
    graph_reads = GraphReads(reads)
    return KnowledgeServices(
        specs=specs,
        ontology=OntologyService(GraphCompiler(), specs),
        pipeline=pipeline,
        reads=reads,
        projector=Projector(engine, graph, specs, config, service=service),
        extracted=extracted,
        reviews=ReviewService(specs, extracted, pipeline, reads),
        graph_reads=graph_reads,
        search=GraphSearch(graph, graph_reads, service=service),
        lock=GraphLock(engine),
        corrections=Corrections(
            specs,
            extracted,
            hmac_secret=settings.content_hmac_secret.get_secret_value().encode(),
            trace_retention_days=settings.context_trace_retention_days,
        ),
    )
