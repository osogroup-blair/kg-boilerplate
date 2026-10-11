"""Prototype of the P1 ModelGateway (disposable; P1 re-implements it in packages/ with the same contract).

Everything that talks to the router goes through here. See docs/router-contract.md.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import random
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Sequence, Type

import httpx
import yaml
from pydantic import BaseModel, ValidationError

from tools.mock_router.tokens import TOKENIZER_ID, count_tokens, truncate_to_tokens

from .errors import (Category, EgressDenied, EmbeddingDimensionError, RouterError, StructuredOutputError, categorize)

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Correlation:
    correlation_id: str                    # must be a UUID for the router to reuse it as x-request-id
    workspace_id: str
    principal_id: str
    service: str = "kg-worker"             # kg-api | kg-worker


@dataclass
class CallRecord:
    """Metadata-only log row. No message content, no vectors."""
    purpose: str
    alias: str
    operation: str                         # chat | embeddings
    classification: str
    correlation_id: str
    router_request_id: Optional[str]
    prompt_tokens: Optional[int]
    completion_tokens: Optional[int]
    latency_ms: float
    attempts: int
    outcome: str                           # ok | <router error code> | egress_denied
    usage_estimated: bool = False
    est_cost_usd: Optional[float] = None
    fallback_used: bool = False
    tokenizer: str = TOKENIZER_ID


@dataclass
class CompletionResult:
    text: str
    parsed: Optional[BaseModel]
    alias: str
    prompt_tokens: int
    completion_tokens: int
    router_request_id: Optional[str]
    attempts: int
    fallback_used: bool = False


@dataclass
class EmbedResult:
    vectors: list[list[float]]             # exactly one per input text (chunk vectors are mean-pooled and re-normalised)
    chunked: list[bool]                    # True where the input exceeded the model limit and was split
    prompt_tokens: int
    router_request_ids: list[str] = field(default_factory=list)


def _l2(v: Sequence[float]) -> list[float]:
    n = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / n for x in v]


class ModelGateway:
    def __init__(self, base_url: str, api_key: str, *, purposes_path: Path = ROOT / "compat/model-purposes.yaml",
                 pricing_path: Path = ROOT / "compat/pricing.yaml", transport: Optional[httpx.AsyncBaseTransport] = None,
                 max_attempts: int = 4, base_backoff: float = 0.5, max_backoff: float = 30.0, embed_batch_size: int = 32,
                 hash_secret: bytes = b"dev-only-secret", timeout: float = 120.0,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, jitter: Callable[[], float] = random.random,
                 call_log_path: Optional[Path] = None):
        cfg = yaml.safe_load(purposes_path.read_text())
        self.purposes: dict[str, dict] = cfg["model_purposes"]
        self.egress = cfg["egress"]
        self.pricing = yaml.safe_load(pricing_path.read_text())
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/") + "/", headers={"Authorization": f"Bearer {api_key}"},
                                         transport=transport, timeout=timeout)
        self.max_attempts, self.base_backoff, self.max_backoff = max_attempts, base_backoff, max_backoff
        self.embed_batch_size, self._secret = embed_batch_size, hash_secret
        self._sleep, self._jitter = sleep, jitter
        self.call_log: list[CallRecord] = []
        self.call_log_path = call_log_path
        self._emb = self.purposes["embedding"]

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- policy ------------------------------------------------------------------------------------------------------
    def check_egress(self, classification: str) -> None:
        if classification in self.egress["denied_classifications"] or classification not in self.egress["allowed_classifications"]:
            raise EgressDenied(classification)

    def _hash(self, value: str) -> str:
        return hmac.new(self._secret, value.encode(), hashlib.sha256).hexdigest()[:16]

    def _headers_and_meta(self, corr: Correlation, purpose: str, classification: str, task_type: str) -> tuple[dict, dict]:
        try:
            uuid.UUID(corr.correlation_id)
            rid = corr.correlation_id
        except ValueError:
            rid = str(uuid.uuid4())        # router would ignore a non-UUID anyway; keep the join key honest
        meta = {"application_id": "kg-platform", "request_id": corr.correlation_id, "task_type": task_type,
                "agent": corr.service, "user_id": self._hash(corr.principal_id), "conversation_id": self._hash(corr.workspace_id),
                "data_classification": classification}
        return {"x-request-id": rid}, meta

    # ---- transport with retry ------------------------------------------------------------------------------------------
    async def _post(self, path: str, body: dict, headers: dict) -> tuple[dict, httpx.Headers, int]:
        attempts = 0
        while True:
            attempts += 1
            try:
                r = await self._client.post(path, json=body, headers=headers)
            except httpx.TimeoutException:
                err = RouterError("timeout", "client-side timeout")
            except httpx.TransportError as e:
                err = RouterError("provider_unavailable", f"transport error: {type(e).__name__}")
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
            err.attempts = attempts                                             # type: ignore[attr-defined]
            if err.category is not Category.RETRYABLE or attempts >= self.max_attempts:
                raise err
            delay = err.retry_after if err.retry_after is not None else min(self.max_backoff, self.base_backoff * 2 ** (attempts - 1))
            await self._sleep(min(self.max_backoff, delay + (0 if err.retry_after is not None else self._jitter() * 0.1)))

    def _estimate_cost(self, alias: str, prompt: Optional[int], completion: Optional[int]) -> Optional[float]:
        model = self.pricing["alias_primary"].get(alias)
        p = self.pricing["prices"].get(model) if model else None
        if not p or prompt is None:
            return None
        return round((prompt * p["input"] + (completion or 0) * p["output"]) / 1_000_000, 8)

    def _log(self, rec: CallRecord) -> None:
        self.call_log.append(rec)
        if self.call_log_path:
            with self.call_log_path.open("a") as f:
                f.write(json.dumps(rec.__dict__) + "\n")

    # ---- chat -----------------------------------------------------------------------------------------------------------
    async def complete(self, purpose: str, messages: list[dict], *, classification: str, correlation: Correlation,
                       response_schema: Optional[Type[BaseModel]] = None, max_tokens: Optional[int] = None) -> CompletionResult:
        cfg = self.purposes[purpose]
        task_type = cfg.get("task_type", "general")
        try:
            self.check_egress(classification)
        except EgressDenied:
            self._log(CallRecord(purpose, cfg["alias"], "chat", classification, correlation.correlation_id, None, None, None, 0.0, 0, "egress_denied"))
            raise
        aliases = [cfg["alias"]] + ([cfg["fallback_alias"]] if cfg.get("fallback_alias") else [])
        last: Optional[RouterError] = None
        for i, alias in enumerate(aliases):
            headers, meta = self._headers_and_meta(correlation, purpose, classification, task_type)
            body: dict[str, Any] = {"model": alias, "messages": messages, "metadata": meta, "user": meta["user_id"],
                                    "routing": {"data_classification": classification, "task_type": task_type}}
            if max_tokens:
                body["max_tokens"] = max_tokens
            if response_schema is not None:
                body["response_format"] = {"type": "json_schema", "json_schema": {
                    "name": response_schema.__name__, "schema": response_schema.model_json_schema(), "strict": True}}
            t0 = time.perf_counter()
            try:
                data, hdrs, attempts = await self._post("chat/completions", body, headers)
            except RouterError as e:
                last = e
                self._log(CallRecord(purpose, alias, "chat", classification, correlation.correlation_id, e.request_id, None, None,
                                     (time.perf_counter() - t0) * 1000, getattr(e, "attempts", 1), e.code, fallback_used=i > 0))
                # fall back only when the primary is exhausted on a retryable failure or is not capable (config-terminal on capability)
                if i + 1 < len(aliases) and (e.category is Category.RETRYABLE or e.code == "unsupported_capability"):
                    continue
                raise
            usage = data.get("usage")
            content = data["choices"][0]["message"]["content"] or ""
            p_tok = usage["prompt_tokens"] if usage else None
            c_tok = usage["completion_tokens"] if usage else count_tokens(content)
            estimated = usage is None
            if p_tok is None:
                p_tok = sum(count_tokens(m["content"]) if isinstance(m["content"], str) else 0 for m in messages)
            rid = hdrs.get("x-gateway-request-id")
            parsed = None
            if response_schema is not None:
                try:
                    parsed = response_schema.model_validate_json(content)
                except ValidationError as ve:
                    self._log(CallRecord(purpose, alias, "chat", classification, correlation.correlation_id, rid, p_tok, c_tok,
                                         (time.perf_counter() - t0) * 1000, attempts, "invalid_structured_output", estimated, fallback_used=i > 0))
                    raise StructuredOutputError(f"content does not match {response_schema.__name__}: {ve.error_count()} errors") from ve
            self._log(CallRecord(purpose, alias, "chat", classification, correlation.correlation_id, rid, p_tok, c_tok,
                                 (time.perf_counter() - t0) * 1000, attempts, "ok", estimated, self._estimate_cost(alias, p_tok, c_tok), i > 0))
            return CompletionResult(content, parsed, data["model"], p_tok, c_tok, rid, attempts, i > 0)
        assert last is not None
        raise last

    # ---- embeddings ----------------------------------------------------------------------------------------------------
    def _chunks(self, text: str, prefix: str) -> list[str]:
        limit = self._emb["max_input_tokens"] - count_tokens(prefix) - 2    # 2 tokens of slack for special tokens
        if count_tokens(text) <= limit:
            return [text]
        from tools.mock_router.tokens import _enc
        enc = _enc()
        ids = enc.encode(text, disallowed_special=()) if enc else None
        if ids is None:
            step = limit * 4
            return [text[i:i + step] for i in range(0, len(text), step)]
        return [enc.decode(ids[i:i + limit]) for i in range(0, len(ids), limit)]

    async def embed(self, purpose: str, texts: list[str], *, kind: str, classification: str, correlation: Correlation) -> EmbedResult:
        cfg = self.purposes[purpose]
        assert kind in ("query", "passage")
        try:
            self.check_egress(classification)
        except EgressDenied:
            self._log(CallRecord(purpose, cfg["alias"], "embeddings", classification, correlation.correlation_id, None, None, None, 0.0, 0, "egress_denied"))
            raise
        prefix = cfg["prefix"][kind]
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
        for start in range(0, len(flat), self.embed_batch_size):
            batch = flat[start:start + self.embed_batch_size]
            headers, meta = self._headers_and_meta(correlation, purpose, classification, "embedding")
            body = {"model": cfg["alias"], "input": batch, "user": meta["user_id"]}
            t0 = time.perf_counter()
            try:
                data, hdrs, attempts = await self._post("embeddings", body, headers)
            except RouterError as e:
                self._log(CallRecord(purpose, cfg["alias"], "embeddings", classification, correlation.correlation_id, e.request_id, None, None,
                                     (time.perf_counter() - t0) * 1000, getattr(e, "attempts", 1), e.code))
                raise
            rows = sorted(data["data"], key=lambda d: d["index"])
            if len(rows) != len(batch):
                raise EmbeddingDimensionError(f"router returned {len(rows)} vectors for {len(batch)} inputs")
            for row in rows:
                if len(row["embedding"]) != cfg["dim"]:
                    raise EmbeddingDimensionError(f"expected {cfg['dim']} dims, got {len(row['embedding'])}")
                vectors.append(row["embedding"])
            used = data.get("usage", {}).get("prompt_tokens")
            tokens += used if used is not None else sum(count_tokens(x) for x in batch)
            rid = hdrs.get("x-gateway-request-id")
            rids.append(rid or "")
            self._log(CallRecord(purpose, cfg["alias"], "embeddings", classification, correlation.correlation_id, rid, used, 0,
                                 (time.perf_counter() - t0) * 1000, attempts, "ok", used is None, self._estimate_cost(cfg["alias"], used, 0)))
        # pool chunk vectors back to one per input
        pooled: list[list[float]] = []
        for i in range(len(texts)):
            mine = [v for v, o in zip(vectors, owner) if o == i]
            pooled.append(mine[0] if len(mine) == 1 else _l2([sum(col) for col in zip(*mine)]))
        return EmbedResult(pooled, chunked, tokens, rids)
