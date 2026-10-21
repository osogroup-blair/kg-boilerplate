"""Builds the private Graphiti instance. Wiring invariants (each asserted by a test):
* OpenAIGenericClient (chat/completions + json_schema); the default client calls /v1/responses, which the router lacks (GR-02, L11)
* the embedder is E5-prefixed, 384 dims
* NoCrossEncoder (logprobs are dropped by the router)
* the OpenAI SDK's HTTP client sits on RouterTransport, so egress, auth, metadata and the call log apply to Graphiti too (L7)
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from graphiti_core import Graphiti
from graphiti_core.driver.neo4j_driver import Neo4jDriver
from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
from graphiti_core.llm_client.client import is_server_or_retry_error
from graphiti_core.llm_client.config import DEFAULT_MAX_TOKENS, DEFAULT_TEMPERATURE, LLMConfig, ModelSize
from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
from graphiti_core.prompts.models import Message
from kg_common.settings import Settings
from kg_modelgw.config import ModelConfig
from kg_modelgw.transport import RouterTransport
from openai import AsyncOpenAI
from pydantic import BaseModel
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_random_exponential

from kg_graph.adapters import E5Embedder, NoCrossEncoder


class PlatformLLMClient(OpenAIGenericClient):
    """Graphiti's own retry waits 5-120 s between four tries of an unparseable answer, with the workspace's graph lock held the whole time. The platform
    bounds that: a few quick tries here, then the OUTBOX decides (one retry, then quarantine, P4 ING-03)."""

    attempts = 2

    async def _generate_response_with_retry(  # type: ignore[override]
        self,
        messages: list[Message],
        response_model: type[BaseModel] | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        model_size: ModelSize = ModelSize.medium,
    ) -> dict[str, Any]:
        wrapped = retry(
            stop=stop_after_attempt(self.attempts),
            wait=wait_random_exponential(multiplier=1, min=1, max=8),
            retry=retry_if_exception(is_server_or_retry_error),
            reraise=True,
        )(self._generate_response)
        return await wrapped(messages, response_model, max_tokens, model_size)


class QuietNeo4jDriver(Neo4jDriver):
    """Graphiti's driver schedules an index build at construction, racing any explicit build (GR-01, L12). Indices are built once, by the
    worker, through GraphService.ensure_indices(); every other process constructs this driver with the automatic task cancelled."""

    def __init__(self, uri: str, user: str, password: str):
        super().__init__(uri, user, password)
        task = getattr(self, "_init_task", None)
        if task is not None:
            task.cancel()
            self._init_task = None


def llm_config(config: ModelConfig, base_url: str) -> LLMConfig:
    """Graphiti's LLM settings from the purposes file: the extraction and small-extraction aliases and the extraction temperature (Graphiti defaults to 1.0)."""
    extraction, small = config.purposes["graph_extraction"], config.purposes["graph_extraction_small"]
    temperature = DEFAULT_TEMPERATURE if extraction.temperature is None else extraction.temperature
    return LLMConfig(
        api_key="platform-managed", base_url=base_url, model=extraction.alias, small_model=small.alias, temperature=temperature
    )


def build_graphiti(settings: Settings, transport: RouterTransport, config: ModelConfig) -> Graphiti:
    base_url = settings.router_base_url
    http = httpx.AsyncClient(transport=transport, base_url=base_url, timeout=180)
    # the real key is set by RouterTransport; openai 3.x annotates httpx2 but httpx.AsyncClient works at runtime (asserted by T-GR-05)
    oai = AsyncOpenAI(api_key="platform-managed", base_url=base_url, http_client=http, max_retries=0)  # type: ignore[arg-type]
    llm = PlatformLLMClient(llm_config(config, base_url), client=oai)
    emb = config.embedding
    embedder = E5Embedder(
        OpenAIEmbedder(
            OpenAIEmbedderConfig(api_key="platform-managed", base_url=base_url, embedding_model=emb.alias, embedding_dim=emb.dim),
            client=oai,
        )
    )
    driver = QuietNeo4jDriver(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password.get_secret_value())
    return Graphiti(
        graph_driver=driver,
        llm_client=llm,
        embedder=embedder,
        cross_encoder=NoCrossEncoder(),
        store_raw_episode_content=settings.graph_store_episode_content,
    )


async def wait_cancelled(task: asyncio.Task[object] | None) -> None:
    if task is not None:
        await asyncio.gather(task, return_exceptions=True)
