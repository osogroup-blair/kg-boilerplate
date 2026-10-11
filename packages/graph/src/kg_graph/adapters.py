"""Graphiti adapters for the router: the cross-encoder that refuses to run, and the E5-prefixing embedder."""

from __future__ import annotations

from typing import Any

from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.embedder.client import EmbedderClient
from graphiti_core.embedder.openai import OpenAIEmbedder


class NoCrossEncoder(CrossEncoderClient):
    """Graphiti's default OpenAIRerankerClient needs logprobs, which the router drops (GR-10). This fails loudly if anything uses it."""

    calls = 0

    async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
        NoCrossEncoder.calls += 1
        raise RuntimeError("cross-encoder reranking is disabled on this platform (router drops logprobs)")


class E5Embedder(EmbedderClient):
    """Adds the E5 prefix for Graphiti's internal embeddings. Graphiti cannot say query vs passage, so it uses `query: ` on BOTH sides
    (E5 guidance for symmetric tasks). The platform's own pgvector memory search uses the asymmetric query/passage pair (kg_modelgw)."""

    PREFIX = "query: "

    def __init__(self, inner: OpenAIEmbedder):
        self.inner, self.created = inner, 0

    async def create(self, input_data: Any) -> list[float]:
        self.created += 1
        # Graphiti passes a str or a list[str]. Token-id inputs are not supported.
        if isinstance(input_data, str):
            input_data = self.PREFIX + input_data
        elif isinstance(input_data, list) and all(isinstance(x, str) for x in input_data):
            input_data = [self.PREFIX + x for x in input_data]
        else:
            raise TypeError(f"E5Embedder cannot prefix input of type {type(input_data)} (token-id inputs are not supported)")
        return await self.inner.create(input_data)

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        self.created += len(input_data_list)
        return await self.inner.create_batch([self.PREFIX + t for t in input_data_list])
