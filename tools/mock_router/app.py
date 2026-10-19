"""Mock Oso Model Router: reproduces docs/router-contract.md closely enough to catch integration bugs offline.

Faithful behaviours (each is asserted in tests/test_contract.py):
  * unknown top-level request fields are stripped (zod default), so logprobs/seed/n are dropped
  * response_format != text requires `structured_output`; alias candidates are filtered by the seed capability table
  * data_classification local_only -> data_policy_violation; key max-classification -> classification_not_permitted
  * error envelope + x-request-id on every response, x-gateway-request-id on success, Retry-After on 429
  * fault injection (routing.simulate_failure, plus a test-only fault queue at /__mock/faults)
  * `model` echoes the alias; usage counted with tiktoken o200k_base

Anything NOT verified against the live router is listed in docs/router-contract.md as [unverified].
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import random
import re
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Literal, Union

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .embeddings import embed_text
from .fixtures import FixtureStore, fixture_key
from .schema_faker import fake_from_schema
from .tokens import count_tokens

HERE = Path(__file__).parent
CATALOG = json.loads((HERE / "catalog.json").read_text())
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

CLASSIFICATIONS = ["public", "internal", "confidential", "restricted", "local_only"]
ERROR_STATUS = {
    "invalid_request": 400, "authentication_error": 401, "model_not_found": 404, "unsupported_capability": 422,
    "privacy_policy_violation": 422, "data_policy_violation": 422, "classification_not_permitted": 403,
    "cost_limit_exceeded": 422, "context_too_large": 400, "rate_limited": 429, "budget_exceeded": 429,
    "provider_unavailable": 503, "routing_failed": 503, "timeout": 504, "unknown_provider_error": 502, "forbidden": 403,
    "fixture_missing": 500,  # mock only (strict scenario mode): an unmatched structured request fails loudly instead of returning invented data
}  # mirrors router src/errors/codes.ts HTTP_STATUS_BY_CODE


class GatewayError(Exception):
    def __init__(self, code: str, message: str, headers: dict[str, str] | None = None, details: dict | None = None):
        self.code, self.message, self.headers, self.details = code, message, headers or {}, details


# ---- request models (mirror router src/api/chat/schema.ts; unknown keys ignored like zod's default strip) ----------
class _Strip(BaseModel):
    model_config = ConfigDict(extra="ignore")


class TextPart(_Strip):
    type: Literal["text"]
    text: str


class ImageUrl(_Strip):
    url: str


class ImagePart(_Strip):
    type: Literal["image_url"]
    image_url: ImageUrl


class SystemMsg(_Strip):
    role: Literal["system"]
    content: str


class UserMsg(_Strip):
    role: Literal["user"]
    content: str | list[TextPart | ImagePart]


class ToolCallFn(_Strip):
    name: str
    arguments: str


class ToolCall(_Strip):
    id: str
    type: Literal["function"]
    function: ToolCallFn


class AssistantMsg(_Strip):
    role: Literal["assistant"]
    content: str | None
    tool_calls: list[ToolCall] | None = None


class ToolMsg(_Strip):
    role: Literal["tool"]
    tool_call_id: str
    content: str


Message = Union[SystemMsg, UserMsg, AssistantMsg, ToolMsg]


class JsonSchemaSpec(_Strip):
    name: str
    schema_: dict[str, Any] = Field(alias="schema")
    strict: bool | None = None


class ResponseFormat(_Strip):
    type: Literal["text", "json_object", "json_schema"]
    json_schema: JsonSchemaSpec | None = None


class Routing(_Strip):
    privacy: Literal["local_only", "open_models_only", "cloud_allowed", "closed_allowed"] | None = None
    max_cost_usd: float | None = Field(default=None, gt=0)
    prefer: Literal["local"] | None = None
    data_classification: Literal["public", "internal", "confidential", "restricted", "local_only"] | None = None
    task_type: str | None = None
    fallback: bool | None = None
    simulate_failure: Literal["primary_timeout", "provider_unavailable", "rate_limited", "http_500", "circuit_breaker_open"] | None = None


class Metadata(BaseModel):
    model_config = ConfigDict(extra="allow")  # zod .passthrough()
    application_id: str | None = None
    user_id: str | None = None
    conversation_id: str | None = None
    request_id: str | None = None
    task_type: str | None = None
    agent: str | None = None
    data_classification: Literal["public", "internal", "confidential", "restricted", "local_only"] | None = None
    simulate_failure: str | None = None


class ToolFn(_Strip):
    name: str
    description: str | None = None
    parameters: dict[str, Any]


class Tool(_Strip):
    type: Literal["function"]
    function: ToolFn


class ChatRequest(_Strip):
    model: str = Field(min_length=1)
    messages: list[Message] = Field(min_length=1)
    stream: bool = False
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    max_tokens: int | None = Field(default=None, gt=0)
    max_completion_tokens: int | None = Field(default=None, gt=0)
    stop: list[str] | None = None
    tools: list[Tool] | None = None
    tool_choice: Any | None = None
    response_format: ResponseFormat | None = None
    user: str | None = None
    metadata: Metadata | None = None
    routing: Routing | None = None


class EmbeddingRequest(_Strip):
    model: str
    input: str | list[str]
    user: str | None = None


# ---- runtime state ---------------------------------------------------------------------------------------------------
class MockConfig(BaseModel):
    api_key: str = "mock-key"
    max_classification: str = "restricted"        # the real seeded client is "internal" (router-contract §7)
    retry_after_seconds: int = 1
    e5_overlong: Literal["truncate", "accept"] = "truncate"   # behaviour for 512 < tokens <= 8192 [unverified: LV-05]
    embedding_dim: int = 384
    fixtures_dir: Path = HERE / "fixtures"
    # Strict scenario mode (P4 Q12): a structured (json_schema) request that no fixture matches answers 500 `fixture_missing` instead of the schema faker's
    # junk, so a gap in a scenario fails loudly. Off by default; tests switch it with POST /__mock/strict.
    strict_fixtures: bool = False

    @classmethod
    def from_env(cls) -> MockConfig:
        return cls(
            api_key=os.environ.get("MOCK_ROUTER_API_KEY", "mock-key"),
            max_classification=os.environ.get("MOCK_ROUTER_MAX_CLASSIFICATION", "restricted"),
            retry_after_seconds=int(os.environ.get("MOCK_ROUTER_RETRY_AFTER", "1")),
            e5_overlong=os.environ.get("MOCK_ROUTER_E5_OVERLONG", "truncate"),  # type: ignore[arg-type]
            strict_fixtures=os.environ.get("MOCK_ROUTER_STRICT_FIXTURES", "").lower() in ("1", "true", "yes"),
        )


def _caps_ok(model: str, required: set[str]) -> bool:
    return required.issubset(set(CATALOG["models"][model]["capabilities"]))


def _alias_stats(alias: str) -> dict:
    cands = [m for m in CATALOG["aliases"][alias] if m in CATALOG["models"]]
    caps = set.intersection(*(set(CATALOG["models"][m]["capabilities"]) for m in cands)) if cands else set()
    union = set().union(*(set(CATALOG["models"][m]["capabilities"]) for m in cands)) if cands else set()
    return {
        "id": alias, "object": "model",
        "capabilities": sorted(union),
        "context_length": min(CATALOG["models"][m]["context_window"] for m in cands),
        "max_output_tokens": min(CATALOG["models"][m]["max_output_tokens"] for m in cands),
    }


class LatencyModel:
    """A response-time model for the load baselines (phase-9 WP4): per alias, a lognormal fitted to a p50 and a p95 in milliseconds. `MOCK_ROUTER_LATENCY_PROFILE` is a JSON file
    {"chat": {"oso/general": {"p50_ms": 1200, "p95_ms": 4000}, ...}, "embedding": {"oso/embedding": {...}}}. The real numbers come from the live session (WP1 step g);
    until then the shipped provisional profile (tools/mock_router/latency.provisional.json) is used and any report made with it says so. Off (no sleep) when unset."""

    def __init__(self, profile: dict | None, seed: int = 7):
        self.profile, self.rng = profile or {}, random.Random(seed)

    @classmethod
    def from_env(cls) -> LatencyModel:
        path = os.environ.get("MOCK_ROUTER_LATENCY_PROFILE")
        return cls(json.loads(Path(path).read_text()) if path else None)

    def sample_seconds(self, kind: str, alias: str) -> float:
        row = self.profile.get(kind, {}).get(alias)
        if not row:
            return 0.0
        p50, p95 = float(row["p50_ms"]), float(row["p95_ms"])
        sigma = math.log(max(p95, p50 * 1.0001) / p50) / 1.645
        return max(0.0, self.rng.lognormvariate(math.log(p50), sigma) / 1000.0)


def create_app(config: MockConfig | None = None) -> FastAPI:
    cfg = config or MockConfig.from_env()
    app = FastAPI(title="Mock Oso Model Router", docs_url=None, redoc_url=None, openapi_url=None)
    store = FixtureStore(cfg.fixtures_dir)
    latency = LatencyModel.from_env()
    faults: deque[dict] = deque()
    request_log: list[dict] = []
    app.state.cfg, app.state.faults, app.state.request_log = cfg, faults, request_log

    # ---- helpers -----------------------------------------------------------------------------------------------------
    def request_id_for(request: Request) -> str:
        h = request.headers.get("x-request-id")
        return h if h and UUID_RE.match(h) else str(uuid.uuid4())  # reuse only valid UUIDs (router logger.ts genRequestId)

    def envelope(err: GatewayError, rid: str) -> JSONResponse:
        body: dict[str, Any] = {"error": {"message": err.message, "type": err.code, "code": err.code, "request_id": rid}}
        if err.details:
            body["error"]["details"] = err.details
        return JSONResponse(body, status_code=ERROR_STATUS.get(err.code, 422), headers={**err.headers, "x-request-id": rid})

    def authenticate(request: Request) -> None:
        auth = request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer ") or auth[7:].strip() != cfg.api_key:
            raise GatewayError("authentication_error", "Invalid or missing API key")

    @app.middleware("http")
    async def _wrap(request: Request, call_next):  # attach request id to every response (router app.ts onSend hook)
        rid = request_id_for(request)
        request.state.rid = rid
        try:
            response = await call_next(request)
        except GatewayError as e:
            return envelope(e, rid)
        response.headers["x-request-id"] = rid
        return response

    @app.exception_handler(GatewayError)
    async def _gw(request: Request, err: GatewayError):
        return envelope(err, request.state.rid)

    async def read_json(request: Request) -> dict:
        try:
            body = await request.json()
        except Exception:
            raise GatewayError("invalid_request", "Request body is not valid JSON")
        if not isinstance(body, dict):
            raise GatewayError("invalid_request", "Request body must be a JSON object")
        return body

    def validate(model_cls: type[BaseModel], body: dict):
        try:
            return model_cls.model_validate(body)
        except ValidationError as e:
            first = e.errors()[0]
            raise GatewayError("invalid_request", f"{'.'.join(str(p) for p in first['loc'])}: {first['msg']}")

    def pop_fault(kind: str) -> dict | None:
        if faults and faults[0].get("endpoint", kind) in (kind, "any"):
            f = faults[0]
            f["count"] = f.get("count", 1) - 1
            if f["count"] <= 0:
                faults.popleft()
            return f
        return None

    def raise_for_fault(code: str) -> None:
        headers = {"Retry-After": str(cfg.retry_after_seconds)} if code == "rate_limited" else {}
        raise GatewayError(code, f"injected fault: {code}", headers=headers)

    # ---- endpoints ---------------------------------------------------------------------------------------------------
    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/v1/models")
    async def list_models(request: Request):
        authenticate(request)
        return {"object": "list", "data": [_alias_stats(a) for a in CATALOG["aliases"]]}

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        authenticate(request)
        raw = await read_json(request)
        req: ChatRequest = validate(ChatRequest, raw)  # type: ignore[assignment]
        received = sorted(raw.keys())
        stripped = sorted(set(raw.keys()) - set(ChatRequest.model_fields.keys()))
        request_log.append({"endpoint": "chat", "rid": request.state.rid, "received_fields": received, "stripped_fields": stripped,
                            "model": req.model, "headers": {k: v for k, v in request.headers.items() if k.startswith("x-")}})

        # injected faults (test-only queue) behave like provider failures after routing
        f = pop_fault("chat")
        if f:
            if f.get("delay_ms"):  # P9 FI-17: a router that answers, slowly
                await asyncio.sleep(float(f["delay_ms"]) / 1000.0)
            if f["code"] == "malformed":  # P9 FI-23: a 200 whose body is not a chat completion
                return JSONResponse({"unexpected": True, "choices": "not-a-list"})
            if f["code"] != "slow":
                raise_for_fault(f["code"])

        if (pause := latency.sample_seconds("chat", req.model)) > 0:
            await asyncio.sleep(pause)

        # ---- classification (routing.* wins over metadata.*, router schema.ts comment) ----
        routing, meta = req.routing or Routing(), req.metadata or Metadata()
        classification = routing.data_classification or meta.data_classification or "internal"  # router default: internal
        if classification == "local_only" or routing.privacy == "local_only":
            raise GatewayError("data_policy_violation", "local_only data cannot be routed: no local deployment is configured")
        if CLASSIFICATIONS.index(classification) > CLASSIFICATIONS.index(cfg.max_classification):
            raise GatewayError("classification_not_permitted", f"Key may not send data classified '{classification}'")

        # ---- target resolution ----
        if req.model in CATALOG["aliases"]:
            chain, direct = list(CATALOG["aliases"][req.model]), False
        elif req.model in CATALOG["models"]:
            chain, direct = [req.model], True
        else:
            raise GatewayError("model_not_found", f"Unknown model or alias '{req.model}'")

        # ---- capability gating (router src/models/requirements.ts) ----
        required = {"text"}
        if req.response_format and req.response_format.type != "text":
            required.add("structured_output")
        if req.tools:
            required.add("tools")
        if req.stream:
            required.add("streaming")
        for m in req.messages:
            if isinstance(m, UserMsg) and isinstance(m.content, list) and any(isinstance(p, ImagePart) for p in m.content):
                required.add("vision")
        eligible = [m for m in chain if _caps_ok(m, required)]
        if routing.privacy == "open_models_only":
            eligible = [m for m in eligible if CATALOG["models"][m]["weights"] == "open"]
        if not eligible:
            raise GatewayError("unsupported_capability",
                               f"No target for \"{req.model}\" supports all required capabilities [{', '.join(sorted(required))}]")

        # ---- simulated failures (routing.simulate_failure / metadata.simulate_failure) ----
        sim = routing.simulate_failure or meta.simulate_failure
        if sim:
            fallback_ok = routing.fallback is not False and len(eligible) > 1 and not direct
            code = {"primary_timeout": "timeout", "provider_unavailable": "provider_unavailable", "rate_limited": "rate_limited",
                    "http_500": "unknown_provider_error", "circuit_breaker_open": "provider_unavailable"}.get(sim, "unknown_provider_error")
            if not fallback_ok:
                raise_for_fault(code)
            eligible = eligible[1:]  # primary failed, router falls through to the next candidate [unverified: LV-07]

        # ---- content ----
        last_user = next((m for m in reversed(req.messages) if isinstance(m, UserMsg)), None)
        norm = [{"role": m.role, "content": _text_of(m)} for m in req.messages]
        schema_name = req.response_format.json_schema.name if req.response_format and req.response_format.json_schema else None
        request_log[-1]["schema_name"] = schema_name  # lets a test count how often each structured question was asked
        key = fixture_key(req.model, norm, schema_name)
        hit = store.lookup(key, req.model, norm, schema_name)
        mode = "fixture"
        scripted: dict | None = None  # a rule may script a whole assistant message, tool calls included: {"_message": {"content": ..., "tool_calls": [...]}}
        if isinstance(hit, dict) and "_message" in hit:
            scripted = dict(hit["_message"])
            calls = [{"id": c.get("id") or f"call_{uuid.uuid4().hex[:12]}", "type": "function",
                      "function": {"name": c["function"]["name"], "arguments": c["function"]["arguments"] if isinstance(c["function"]["arguments"], str)
                                   else json.dumps(c["function"]["arguments"])}} for c in scripted.get("tool_calls") or []]
            scripted = {"role": "assistant", "content": scripted.get("content"), **({"tool_calls": calls} if calls else {})}
            hit = scripted.get("content") or ""
        if hit is not None:
            content = hit if isinstance(hit, str) else json.dumps(hit)
        elif req.response_format and req.response_format.type == "json_schema" and req.response_format.json_schema:
            if cfg.strict_fixtures:
                request_log[-1]["content_mode"] = "fixture_missing"
                request_log[-1]["unmatched"] = {"schema_name": schema_name, "alias": req.model, "text_head": _norm_head(norm)}
                raise GatewayError("fixture_missing", f"no fixture matches this structured request (schema {schema_name})", details={"schema_name": schema_name})
            mode = "schema_faker"
            content = json.dumps(fake_from_schema(req.response_format.json_schema.schema_))
        elif req.response_format and req.response_format.type == "json_object":
            mode, content = "schema_faker", "{}"
        else:
            mode, content = "echo", f"echo: {_text_of(last_user) if last_user else ''}"
        request_log[-1]["content_mode"] = mode

        prompt_tokens = sum(count_tokens(x["content"]) + 4 for x in norm) + 3
        completion_tokens = count_tokens(content)
        usage = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": prompt_tokens + completion_tokens}
        headers = {"x-gateway-request-id": request.state.rid}
        body = {
            "id": f"chatcmpl_{uuid.uuid4().hex[:24]}", "object": "chat.completion", "created": int(time.time()), "model": req.model,
            "choices": [{"index": 0, "message": scripted or {"role": "assistant", "content": content},
                         "finish_reason": "tool_calls" if scripted and scripted.get("tool_calls") else "stop"}],
            "usage": usage,
        }
        if req.stream:
            def gen():
                chunk = {"id": body["id"], "object": "chat.completion.chunk", "created": body["created"], "model": req.model,
                         "choices": [{"index": 0, "delta": {"role": "assistant", "content": content}, "finish_reason": None}]}
                yield f"data: {json.dumps(chunk)}\n\n"
                last = {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                yield f"data: {json.dumps(last)}\n\n"
                yield "data: [DONE]\n\n"
            return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)
        return JSONResponse(body, headers=headers)

    @app.post("/v1/embeddings")
    async def embeddings(request: Request):
        authenticate(request)
        raw = await read_json(request)
        req: EmbeddingRequest = validate(EmbeddingRequest, raw)  # type: ignore[assignment]
        request_log.append({"endpoint": "embeddings", "rid": request.state.rid, "received_fields": sorted(raw.keys()),
                            "stripped_fields": sorted(set(raw.keys()) - set(EmbeddingRequest.model_fields.keys())), "model": req.model,
                            "n_inputs": 1 if isinstance(req.input, str) else len(req.input)})
        if req.model in CATALOG["aliases"]:
            target = next((m for m in CATALOG["aliases"][req.model] if "embedding" in CATALOG["models"][m]["capabilities"]), None)
        elif req.model in CATALOG["models"]:
            target = req.model if "embedding" in CATALOG["models"][req.model]["capabilities"] else None
        else:
            raise GatewayError("model_not_found", f"Unknown model or alias '{req.model}'")
        if target is None:
            raise GatewayError("unsupported_capability", f"No target for \"{req.model}\" supports all required capabilities [embedding]")
        f = pop_fault("embeddings")
        if f:
            if f.get("delay_ms"):
                await asyncio.sleep(float(f["delay_ms"]) / 1000.0)
            if f["code"] == "malformed":
                return JSONResponse({"unexpected": True, "data": "not-a-list"})
            if f["code"] != "slow":
                raise_for_fault(f["code"])

        if (pause := latency.sample_seconds("embedding", req.model)) > 0:
            await asyncio.sleep(pause)
        inputs = [req.input] if isinstance(req.input, str) else req.input
        limit = CATALOG["models"][target]["context_window"]  # 8192 advertised by the deployment; the model itself handles 512
        total, vectors, truncated = 0, [], False
        for text in inputs:
            n = count_tokens(text)
            if n > limit:
                raise GatewayError("context_too_large", f"Input of {n} tokens exceeds the {limit}-token context window")
            if n > 512 and cfg.e5_overlong == "truncate":
                truncated = True
                text = _truncate_tokens(text, 512)
                n = 512
            total += n
            vectors.append(embed_text(text, cfg.embedding_dim))
        headers = {"x-gateway-request-id": request.state.rid}
        if truncated:
            headers["x-mock-truncated"] = "true"  # test-only visibility; the real router may truncate silently
        return JSONResponse({
            "object": "list", "model": req.model, "usage": {"prompt_tokens": total, "total_tokens": total},
            "data": [{"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vectors)],
        }, headers=headers)

    # ---- test-only control plane (not part of the real router) --------------------------------------------------------
    @app.post("/__mock/strict")
    async def set_strict(request: Request):
        """Body: {"enabled": true|false}. Strict scenario mode (see MockConfig.strict_fixtures)."""
        body = await request.json()
        cfg.strict_fixtures = bool(body.get("enabled"))
        return {"strict_fixtures": cfg.strict_fixtures}

    @app.get("/__mock/strict")
    async def get_strict():
        return {"strict_fixtures": cfg.strict_fixtures}

    @app.post("/__mock/fixtures")
    async def add_fixtures(request: Request):
        """Body: {"rules": [{"when": {...}, "response": ...}]}. Prepends run-time rules (they win over the files); DELETE clears them."""
        body = await request.json()
        store.dynamic[:0] = list(body.get("rules", []))
        return {"dynamic": len(store.dynamic)}

    @app.delete("/__mock/fixtures")
    async def clear_fixtures():
        store.dynamic.clear()
        return {"dynamic": 0}

    @app.post("/__mock/reload")
    async def reload_fixtures():
        store.reload()
        return {"rules": len(store.rules), "keyed": len(store.keyed)}

    @app.post("/__mock/faults")
    async def set_faults(request: Request):
        """Body: {"faults":[{"code":"rate_limited","count":2,"endpoint":"chat"}]} replaces the queue."""
        body = await request.json()
        faults.clear()
        faults.extend(body.get("faults", []))
        return {"queued": len(faults)}

    @app.get("/__mock/requests")
    async def get_requests():
        return {"requests": request_log}

    @app.delete("/__mock/requests")
    async def clear_requests():
        request_log.clear()
        faults.clear()
        return {"cleared": True}

    return app


def _text_of(msg: Any) -> str:
    if msg is None:
        return ""
    c = msg.content
    if isinstance(c, str):
        return c
    if c is None:
        return ""
    return " ".join(p.text for p in c if isinstance(p, TextPart))


def _norm_head(messages: list[dict], n: int = 400) -> str:
    """The start of the last message, for fixture authors (test tooling only; never used by the platform)."""
    return " ".join(messages[-1]["content"].split())[:n] if messages else ""


def _truncate_tokens(text: str, n: int) -> str:
    from .tokens import truncate_to_tokens
    return truncate_to_tokens(text, n)

