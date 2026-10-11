"""Builds the private Graphiti instance. Wiring invariants (each asserted by a test):
* OpenAIGenericClient (chat/completions + json_schema); the default client calls /v1/responses, which the router lacks (GR-02, L11)
* the embedder is E5-prefixed, 384 dims
* NoCrossEncoder (logprobs are dropped by the router)
* the OpenAI SDK's HTTP client sits on RouterTransport, so egress, auth, metadata and the call log apply to Graphiti too (L7)
"""

from __future__ import annotations

import asyncio

import httpx
from graphiti_core import Graphiti
from graphiti_core.driver.neo4j_driver import Neo4jDriver
from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
from kg_common.settings import Settings
from kg_modelgw.config import ModelConfig
from kg_modelgw.transport import RouterTransport
from openai import AsyncOpenAI

from kg_graph.adapters import E5Embedder, NoCrossEncoder


class QuietNeo4jDriver(Neo4jDriver):
    """Graphiti's driver schedules an index build at construction, racing any explicit build (GR-01, L12). Indices are built once, by the
    worker, through GraphService.ensure_indices(); every other process constructs this driver with the automatic task cancelled."""

    def __init__(self, uri: str, user: str, password: str):
        super().__init__(uri, user, password)
        task = getattr(self, "_init_task", None)
        if task is not None:
            task.cancel()
            self._init_task = None


def build_graphiti(settings: Settings, transport: RouterTransport, config: ModelConfig) -> Graphiti:
    base_url = settings.router_base_url
    http = httpx.AsyncClient(transport=transport, base_url=base_url, timeout=180)
    # the real key is set by RouterTransport; openai 3.x annotates httpx2 but httpx.AsyncClient works at runtime (asserted by T-GR-05)
    oai = AsyncOpenAI(api_key="platform-managed", base_url=base_url, http_client=http, max_retries=0)  # type: ignore[arg-type]
    extraction = config.purposes["graph_extraction"].alias
    small = config.purposes["graph_extraction_small"].alias
    llm = OpenAIGenericClient(LLMConfig(api_key="platform-managed", base_url=base_url, model=extraction, small_model=small), client=oai)
    emb = config.embedding
    embedder = E5Embedder(
        OpenAIEmbedder(
            OpenAIEmbedderConfig(api_key="platform-managed", base_url=base_url, embedding_model=emb.alias, embedding_dim=emb.dim),
            client=oai,
        )
    )
    driver = QuietNeo4jDriver(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password.get_secret_value())
    return Graphiti(graph_driver=driver, llm_client=llm, embedder=embedder, cross_encoder=NoCrossEncoder())


async def wait_cancelled(task: asyncio.Task[object] | None) -> None:
    if task is not None:
        await asyncio.gather(task, return_exceptions=True)
