"""RouterTransport: the single choke point for ALL router traffic, including Graphiti's own OpenAI client (L7).

For every request it: requires an egress context (fail closed, L8), applies the egress policy, sets the per-service key (L9) and
x-request-id, injects metadata / routing hints, times the call, parses usage, writes a metadata-only CallRecord and updates metrics.
Streaming is not supported (the response body is read in full).
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import httpx
from kg_common.ids import is_uuid, uuid7_str
from kg_common.tokens import count_tokens
from kg_obs import metrics
from kg_obs.context import hmac_hash
from kg_obs.logging import get_logger

from kg_modelgw.calllog import CallRecord, CallSink
from kg_modelgw.config import CLASSIFICATION_ORDER, ModelConfig
from kg_modelgw.egress import EgressContext, current_egress
from kg_modelgw.errors import EgressDenied

log = get_logger("kg.modelgw.transport")


def stricter(a: str | None, b: str) -> str:
    """The more restrictive of two classifications (unknown values are treated as the most restrictive)."""
    rank = {name: i for i, name in enumerate(CLASSIFICATION_ORDER)}
    ra = rank.get(a, len(CLASSIFICATION_ORDER)) if a else -1
    return a if (a and ra > rank.get(b, len(CLASSIFICATION_ORDER))) else b


class RouterTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        service: str,
        config: ModelConfig,
        sink: CallSink,
        hmac_secret: str,
        inner: httpx.AsyncBaseTransport | None = None,
        production: bool = False,
    ):
        self._host = httpx.URL(base_url).host
        self._api_key, self._service, self._config, self._sink = api_key, service, config, sink
        self._secret, self._production = hmac_secret, production
        self._inner = inner or httpx.AsyncHTTPTransport()
        self.admin_key_detected = False

    async def aclose(self) -> None:
        await self._inner.aclose()

    # ------------------------------------------------------------------------------------------------------------------------------------
    def _deny(self, ctx: EgressContext | None, classification: str, reason: str, operation: str, alias: str) -> EgressDenied:
        outcome = "egress_denied"
        self._sink.write(
            CallRecord(
                purpose=ctx.purpose if ctx else "unclassified",
                alias=alias,
                operation=operation,
                classification=classification,
                service=self._service,
                outcome=outcome,
                workspace_id=ctx.workspace_id if ctx else None,
                correlation_id=ctx.correlation_id if ctx else None,
                attempts=0,
            )
        )
        metrics.router_calls_total.labels(
            purpose=ctx.purpose if ctx else "unclassified", alias=alias, operation=operation, outcome=outcome
        ).inc()
        log.warning("egress_denied", reason=reason, classification=classification, purpose=ctx.purpose if ctx else None)
        return EgressDenied(classification if reason != "unclassified" else "unclassified")

    @staticmethod
    def _operation(path: str) -> str:
        if path.endswith("/chat/completions"):
            return "chat"
        if path.endswith("/embeddings"):
            return "embeddings"
        return "models" if path.endswith("/models") else "other"

    def _meta(self, ctx: EgressContext, classification: str, task_type: str, request_id: str) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "application_id": "kg-platform",
            "request_id": request_id,
            "task_type": task_type,
            "agent": ctx.service,
            "data_classification": classification,
        }
        if ctx.principal_id:
            meta["user_id"] = hmac_hash(ctx.principal_id, self._secret)
        if ctx.workspace_id:
            meta["conversation_id"] = hmac_hash(ctx.workspace_id, self._secret)
        return meta

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        operation = self._operation(request.url.path)
        ctx = current_egress()
        body: dict[str, Any] | None = None
        if request.content and request.headers.get("content-type", "").startswith("application/json"):
            try:
                parsed = json.loads(request.content)
                body = parsed if isinstance(parsed, dict) else None
            except ValueError:
                body = None
        alias = str((body or {}).get("model", "unknown"))

        if request.url.host != self._host:
            raise self._deny(ctx, "unknown", "unexpected_host", operation, alias)
        if ctx is None:
            raise self._deny(None, "unknown", "unclassified", operation, alias)

        # the classification actually sent is the stricter of ours and anything the caller already set
        caller_cls = ((body or {}).get("metadata") or {}).get("data_classification") or ((body or {}).get("routing") or {}).get(
            "data_classification"
        )
        classification = stricter(caller_cls if isinstance(caller_cls, str) else None, ctx.classification)
        if not self._config.egress_allows(classification):
            raise self._deny(ctx, classification, "policy", operation, alias)

        ctx.attempt += 1
        rid = ctx.correlation_id if ctx.correlation_id and is_uuid(ctx.correlation_id) else uuid7_str()
        headers = httpx.Headers(request.headers)
        headers["authorization"] = f"Bearer {self._api_key}"
        headers["x-request-id"] = rid
        content = request.content
        if body is not None and operation in ("chat", "embeddings"):
            task_type = {"chat": ctx.extra.get("task_type", "general"), "embeddings": "embedding"}[operation]
            if operation == "chat":
                metadata = {**self._meta(ctx, classification, task_type, ctx.correlation_id or rid), **(body.get("metadata") or {})}
                metadata["data_classification"] = classification
                body["metadata"] = metadata
                routing = dict(body.get("routing") or {})
                routing["data_classification"] = classification
                routing.setdefault("task_type", task_type)
                body["routing"] = routing
            if ctx.principal_id:
                body.setdefault("user", hmac_hash(ctx.principal_id, self._secret))
            content = json.dumps(body).encode()
            headers.pop("content-length", None)
        outgoing = httpx.Request(request.method, request.url, headers=headers, content=content, extensions=request.extensions)

        started = time.perf_counter()
        try:
            response = await self._inner.handle_async_request(outgoing)
            await response.aread()
        except httpx.HTTPError:
            self._finish(ctx, alias, operation, classification, "transport_error", started, None, None, None, body, None)
            raise
        data: Any = None
        try:
            data = json.loads(response.content) if response.content else None
        except ValueError:
            data = None
        outcome = (
            "ok"
            if response.status_code < 400
            else str(((data or {}).get("error") or {}).get("code", f"http_{response.status_code}"))
            if isinstance(data, dict)
            else f"http_{response.status_code}"
        )
        if "x-gateway-provider" in response.headers and not self.admin_key_detected:
            self.admin_key_detected = True
            (log.error if self._production else log.warning)("router_key_is_admin", service=self._service)
        self._finish(
            ctx,
            alias,
            operation,
            classification,
            outcome,
            started,
            response.headers.get("x-gateway-request-id"),
            data,
            response,
            body,
            None,
        )
        return response

    def _finish(
        self,
        ctx: EgressContext,
        alias: str,
        operation: str,
        classification: str,
        outcome: str,
        started: float,
        router_request_id: str | None,
        data: Any,
        response: httpx.Response | None,
        body: dict[str, Any] | None,
        _: None,
    ) -> None:
        if operation == "models":
            return  # health probes are not model usage
        latency = (time.perf_counter() - started) * 1000
        usage = (data or {}).get("usage") if isinstance(data, dict) else None
        prompt = usage.get("prompt_tokens") if isinstance(usage, dict) else None
        completion = usage.get("completion_tokens") if isinstance(usage, dict) else None
        estimated = False
        if outcome == "ok" and operation in ("chat", "embeddings") and prompt is None:
            estimated = True
            prompt = self._estimate_prompt(body)
            completion = self._estimate_completion(data) if operation == "chat" else 0
        record = CallRecord(
            purpose=ctx.purpose,
            alias=alias,
            operation=operation,
            classification=classification,
            service=self._service,
            outcome=outcome,
            workspace_id=ctx.workspace_id,
            correlation_id=ctx.correlation_id,
            router_request_id=router_request_id,
            prompt_tokens=prompt,
            completion_tokens=completion,
            usage_estimated=estimated,
            est_cost_usd=self._config.estimate_cost(alias, prompt, completion),
            latency_ms=round(latency, 2),
            attempts=ctx.attempt,
            fallback_used=ctx.fallback_used,
        )
        self._sink.write(record)
        metrics.router_calls_total.labels(purpose=ctx.purpose, alias=alias, operation=operation, outcome=outcome).inc()
        if prompt:
            metrics.router_tokens_total.labels(purpose=ctx.purpose, direction="prompt").inc(prompt)
        if completion:
            metrics.router_tokens_total.labels(purpose=ctx.purpose, direction="completion").inc(completion)

    @staticmethod
    def _estimate_prompt(body: dict[str, Any] | None) -> int:
        if not body:
            return 0
        if "messages" in body:
            return sum(count_tokens(m["content"]) for m in body["messages"] if isinstance(m.get("content"), str))
        inp = body.get("input")
        texts = [inp] if isinstance(inp, str) else [t for t in (inp or []) if isinstance(t, str)]
        return sum(count_tokens(t) for t in texts)

    @staticmethod
    def _estimate_completion(data: Any) -> int:
        try:
            return count_tokens(data["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, TypeError):
            return 0


def new_correlation_id() -> str:
    return str(uuid.UUID(uuid7_str()))
