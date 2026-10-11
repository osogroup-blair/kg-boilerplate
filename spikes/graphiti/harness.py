"""Shared wiring for the Graphiti spikes: Graphiti -> (spy) -> in-process mock router, and a real Neo4j from docker compose."""
from __future__ import annotations

import json
import os
import uuid
from typing import Any

import httpx
from graphiti_core import Graphiti
from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.driver.neo4j_driver import Neo4jDriver
from graphiti_core.embedder.client import EmbedderClient
from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
from openai import AsyncOpenAI

from spy import SpyTransport
from tools.mock_router.app import MockConfig, create_app

NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "spike-password")
KEY = "test-key"


class NoCrossEncoder(CrossEncoderClient):
    """Graphiti's default OpenAIRerankerClient needs logprobs, which the router drops. This one fails loudly if anything uses it."""

    calls = 0

    async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
        NoCrossEncoder.calls += 1
        raise RuntimeError("cross-encoder reranking is disabled on this platform (router drops logprobs)")


class E5Embedder(EmbedderClient):
    """Adds the E5 prefix for Graphiti's internal embeddings. Graphiti cannot say query vs passage, so it uses `query: ` on BOTH
    sides (E5 guidance for symmetric tasks). The platform's own pgvector memory search uses the asymmetric query/passage pair."""

    PREFIX = "query: "

    def __init__(self, inner: OpenAIEmbedder):
        self.inner, self.created = inner, 0

    async def create(self, input_data):
        self.created += 1
        # Graphiti passes either a str or a one-element list[str] (EntityNode/EntityEdge.generate_*_embedding). Prefix both shapes.
        if isinstance(input_data, str):
            input_data = self.PREFIX + input_data
        elif isinstance(input_data, list) and all(isinstance(x, str) for x in input_data):
            input_data = [self.PREFIX + x for x in input_data]
        else:
            raise TypeError(f"E5Embedder cannot prefix input of type {type(input_data)} (token-id inputs are not supported)")
        return await self.inner.create(input_data)

    async def create_batch(self, input_data_list):
        self.created += len(input_data_list)
        return await self.inner.create_batch([self.PREFIX + t for t in input_data_list])


class Rig:
    """One Graphiti instance wired to the mock router through a request spy."""

    def __init__(self, fixtures_dir=None, mock_router_kwargs: dict | None = None, model: str = "oso/general", small_model: str = "oso/general",
                 live: bool = False, keep_responses: bool = False):
        if live:   # the real router (LIVE-DEFERRED checks): base url + key come from the environment, never from code
            base_url, key = os.environ["OSO_ROUTER_BASE_URL"], os.environ["OSO_ROUTER_API_KEY"]
            self.mock = None
            self.spy = SpyTransport(httpx.AsyncHTTPTransport(), keep_responses=keep_responses)
        else:
            base_url, key = "http://mock/v1", KEY
            kw = dict(api_key=KEY, **(mock_router_kwargs or {}))
            if fixtures_dir:
                kw["fixtures_dir"] = fixtures_dir
            self.mock = create_app(MockConfig(**kw))
            self.spy = SpyTransport(httpx.ASGITransport(app=self.mock))
        http = httpx.AsyncClient(transport=self.spy, base_url=base_url, timeout=180)
        oai = AsyncOpenAI(api_key=key, base_url=base_url, http_client=http, max_retries=0)
        llm = OpenAIGenericClient(LLMConfig(api_key=key, base_url=base_url, model=model, small_model=small_model), client=oai)
        emb_cfg = OpenAIEmbedderConfig(api_key=key, base_url=base_url, embedding_model="oso/embedding", embedding_dim=384)
        self.embedder = E5Embedder(OpenAIEmbedder(emb_cfg, client=oai))
        self.driver = Neo4jDriver(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD)
        self.g = Graphiti(graph_driver=self.driver, llm_client=llm, embedder=self.embedder, cross_encoder=NoCrossEncoder())

    # ---- spy helpers ----
    def chat_calls(self) -> list[dict]:
        return [r for r in self.spy.requests if r["path"].endswith("/chat/completions")]

    def embed_calls(self) -> list[dict]:
        return [r for r in self.spy.requests if r["path"].endswith("/embeddings")]

    def paths(self) -> set[str]:
        return {r["path"] for r in self.spy.requests}

    def reset_spy(self) -> None:
        self.spy.requests.clear()

    async def wipe(self) -> None:
        await self.driver.execute_query("MATCH (n) DETACH DELETE n")

    async def close(self) -> None:
        await self.g.close()


def schema_name(call: dict) -> str | None:
    rf = (call["body"] or {}).get("response_format") or {}
    return (rf.get("json_schema") or {}).get("name")


def new_group() -> str:
    return "g_" + uuid.uuid4().hex[:10]
