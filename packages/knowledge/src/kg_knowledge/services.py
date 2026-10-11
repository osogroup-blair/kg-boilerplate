"""The knowledge services as one object, built once per process by the composition root (api, worker, cli)."""

from __future__ import annotations

from dataclasses import dataclass

from kg_graph.compile import GraphCompiler
from kg_graph.service import GraphService
from kg_modelgw.config import ModelConfig
from sqlalchemy.ext.asyncio import AsyncEngine

from kg_knowledge.commands import CommandPipeline
from kg_knowledge.ontology_service import OntologyService
from kg_knowledge.projection import Projector
from kg_knowledge.reads import Reads
from kg_knowledge.specs import SpecCache


@dataclass
class KnowledgeServices:
    specs: SpecCache
    ontology: OntologyService
    pipeline: CommandPipeline
    reads: Reads
    projector: Projector


def build_knowledge(engine: AsyncEngine, graph: GraphService, config: ModelConfig, *, service: str) -> KnowledgeServices:
    """`service` is the process name written into the call log for projection embeddings (kg-api | kg-worker | kg-cli)."""
    specs = SpecCache()
    return KnowledgeServices(
        specs=specs,
        ontology=OntologyService(GraphCompiler(), specs),
        pipeline=CommandPipeline(specs),
        reads=Reads(specs),
        projector=Projector(engine, graph, specs, config, service=service),
    )
