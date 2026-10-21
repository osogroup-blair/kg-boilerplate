"""ModelGateway: purposes (not models) in, typed results out. Built on an httpx client whose transport is RouterTransport, so the same
choke point serves the gateway and Graphiti. Semantics promoted from the Phase 0 prototype (G-01..G-04)."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
from kg_common.tokens import count_tokens, split_by_tokens
from kg_obs import metrics
from pydantic import BaseModel, ValidationError

from kg_modelgw.calllog import CallRecord, CallSink
from kg_modelgw.config import ModelConfig
from kg_modelgw.egress import current_egress, egress
from kg_modelgw.errors import Category, EmbeddingDimensionError, RouterError, StructuredOutputError


@dataclass(frozen=True)
class Correlation:
    correlation_id: str  # must be a UUID for the router to reuse it as x-request-id
    workspace_id: str
    principal_id: str
    service: str = "kg-worker"  # kg-api | kg-worker


@dataclass
class CompletionResult:
    text: str
    parsed: BaseModel | None
    alias: str
    prompt_tokens: int
    completion_tokens: int
    router_request_id: str | None
    attempts: int
    fallback_used: bool = False


@dataclass
class EmbedResult:
    vectors: list[list[float]]  # exactly one per input text (chunk vectors are mean-pooled and re-normalised)
    chunked: list[bool]
    prompt_tokens: int
    router_request_ids: list[str] = field(default_factory=list)


def _l2(v: Sequence[float]) -> list[float]:
    n = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / n for x in v]


class ModelGateway:
    def __init__(
        self,
        client: httpx.AsyncClient,
        config: ModelConfig,
        sink: CallSink,
        *,
        max_attempts: int = 4,
        base_backoff: float = 0.5,
        max_backoff: float = 30.0,
        embed_batch_size: int = 32,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
    ):
        self._client, self.config, self._sink = client, config, sink
        self.max_attempts, self.base_backoff, self.max_backoff, self.embed_batch_size = (
            max_attempts,
            base_backoff,
            max_backoff,
            embed_batch_size,
        )
        self._sleep, self._jitter = sleep, jitter

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- HTTP with category-based retry --------------------------------------------------------------------------------------------
    async def _post(self, path: str, body: dict[str, Any]) -> tuple[dict[str, Any], httpx.Headers, int]:
        attempts = 0
        ctx = current_egress()
        if ctx is not None:
            ctx.attempt = 0  # attempts are counted per logical request (one batch / one chat call)
        while True:
            attempts += 1
            try:
                r = await self._client.post(path, json=body)
            except httpx.TimeoutException:
                err = RouterError("timeout", "client-side timeout")
            except httpx.TransportError as exc:
                err = RouterError("provider_unavailable", f"transport error: {type(exc).__name__}")
            else:
                if r.status_code < 400:
                    return r.json(), r.headers, attempts
                try:
                    e = r.json()["error"]
                    code, msg, rid = e.get("code", "unknown_provider_error"), e.get("message", ""), e.get("request_id")
                except Exception:
                    code, msg, rid = "unknown_provider_error", f"non-JSON error body (HTTP {r.status_code})", None
                ra = r.headers.get("retry-after")
                err = RouterError(code, msg, r.status_code, rid, float(ra) if ra and ra.replace(".", "").isdigit() else None)
            err.attempts = attempts
            if err.category is not Category.RETRYABLE or attempts >= self.max_attempts:
                raise err
            delay = err.retry_after if err.retry_after is not None else min(self.max_backoff, self.base_backoff * 2 ** (attempts - 1))
            await self._sleep(min(self.max_backoff, delay + (0 if err.retry_after is not None else self._jitter() * 0.1)))

    # ---- chat ----------------------------------------------------------------------------------------------------------------------
    async def complete(
        self,
        purpose: str,
        messages: list[dict[str, Any]],
        *,
        classification: str,
        correlation: Correlation,
        response_schema: type[BaseModel] | None = None,
        max_tokens: int | None = None,
    ) -> CompletionResult:
        spec = self.config.purposes[purpose]
        aliases = [spec.alias] + ([spec.fallback_alias] if spec.fallback_alias else [])
        last: RouterError | None = None
        with egress(
            classification=classification,
            purpose=purpose,
            workspace_id=correlation.workspace_id,
            principal_id=correlation.principal_id,
            correlation_id=correlation.correlation_id,
            service=correlation.service,
        ) as ctx:
            ctx.extra["task_type"] = spec.task_type
            for i, alias in enumerate(aliases):
                ctx.fallback_used = i > 0
                body: dict[str, Any] = {"model": alias, "messages": messages}
                if max_tokens:
                    body["max_tokens"] = max_tokens
                if spec.temperature is not None:
                    body["temperature"] = spec.temperature
                if response_schema is not None:
                    body["response_format"] = {
                        "type": "json_schema",
                        "json_schema": {"name": response_schema.__name__, "schema": response_schema.model_json_schema(), "strict": True},
                    }
                try:
                    data, headers, attempts = await self._post("chat/completions", body)
                except RouterError as e:
                    last = e
                    # fall back only when the primary is exhausted on a retryable failure, or is not capable (config-terminal on capability)
                    if i + 1 < len(aliases) and (e.category is Category.RETRYABLE or e.code == "unsupported_capability"):
                        continue
                    raise
                usage = data.get("usage") or {}
                content = data["choices"][0]["message"]["content"] or ""
                prompt = usage.get("prompt_tokens")
                if prompt is None:
                    prompt = sum(count_tokens(m["content"]) for m in messages if isinstance(m.get("content"), str))
                completion = usage.get("completion_tokens")
                if completion is None:
                    completion = count_tokens(content)
                rid = headers.get("x-gateway-request-id")
                parsed = None
                if response_schema is not None:
                    try:
                        parsed = response_schema.model_validate_json(content)
                    except ValidationError as ve:
                        self._sink.write(
                            CallRecord(
                                purpose=purpose,
                                alias=alias,
                                operation="chat",
                                classification=classification,
                                service=correlation.service,
                                outcome="invalid_structured_output",
                                workspace_id=correlation.workspace_id,
                                correlation_id=correlation.correlation_id,
                                router_request_id=rid,
                                attempts=attempts,
                                fallback_used=i > 0,
                            )
                        )
                        metrics.router_calls_total.labels(
                            purpose=purpose, alias=alias, operation="chat", outcome="invalid_structured_output"
                        ).inc()
                        raise StructuredOutputError(f"content does not match {response_schema.__name__}: {ve.error_count()} errors") from ve
                return CompletionResult(content, parsed, data.get("model", alias), prompt, completion, rid, attempts, i > 0)
        assert last is not None
        raise last

    # ---- embeddings ----------------------------------------------------------------------------------------------------------------
    def _chunks(self, text: str, prefix: str) -> list[str]:
        limit = self.config.embedding.max_input_tokens - count_tokens(prefix) - 2  # 2 tokens of slack for special tokens
        return [text] if count_tokens(text) <= limit else split_by_tokens(text, limit)

    async def embed(self, purpose: str, texts: list[str], *, kind: str, classification: str, correlation: Correlation) -> EmbedResult:
        if kind not in ("query", "passage"):
            raise ValueError("kind must be 'query' or 'passage'")
        emb = self.config.embedding
        prefix = emb.prefix[kind]
        flat: list[str] = []
        owner: list[int] = []
        chunked: list[bool] = []
        for i, t in enumerate(texts):
            parts = self._chunks(t, prefix)
            chunked.append(len(parts) > 1)
            for p in parts:
                flat.append(prefix + p)
                owner.append(i)
        vectors: list[list[float]] = []
        rids: list[str] = []
        tokens = 0
        with egress(
            classification=classification,
            purpose=purpose,
            workspace_id=correlation.workspace_id,
            principal_id=correlation.principal_id,
            correlation_id=correlation.correlation_id,
            service=correlation.service,
        ):
            for start in range(0, len(flat), self.embed_batch_size):
                batch = flat[start : start + self.embed_batch_size]
                data, headers, _ = await self._post("embeddings", {"model": emb.alias, "input": batch})
                rows = sorted(data["data"], key=lambda d: d["index"])
                if len(rows) != len(batch):
                    raise EmbeddingDimensionError(f"router returned {len(rows)} vectors for {len(batch)} inputs")
                for row in rows:
                    if len(row["embedding"]) != emb.dim:
                        raise EmbeddingDimensionError(f"expected {emb.dim} dims, got {len(row['embedding'])}")
                    vectors.append(row["embedding"])
                used = (data.get("usage") or {}).get("prompt_tokens")
                tokens += used if used is not None else sum(count_tokens(x) for x in batch)
                rids.append(headers.get("x-gateway-request-id") or "")
        pooled: list[list[float]] = []
        for i in range(len(texts)):
            mine = [v for v, o in zip(vectors, owner, strict=True) if o == i]
            pooled.append(mine[0] if len(mine) == 1 else _l2([sum(col) for col in zip(*mine, strict=True)]))
        return EmbedResult(pooled, chunked, tokens, rids)
