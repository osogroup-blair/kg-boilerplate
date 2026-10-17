"""Pure-ASGI request middleware, in the order the plan requires: (1) request/correlation ids, (2) metadata-only access log, (3) metrics by
route template, (4) body size limit. Every response (including errors raised below us) carries x-request-id and x-correlation-id."""

from __future__ import annotations

import json
import time

from kg_common.errors import PayloadTooLarge
from kg_common.ids import is_uuid, uuid7_str
from kg_obs import context, metrics
from kg_obs.logging import get_logger
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = get_logger("kg.http")


class _BodyTooLarge(Exception):
    pass


def envelope_bytes(code: str, message: str, request_id: str) -> bytes:
    return json.dumps(
        {"error": {"code": code, "message": message, "request_id": request_id, "retry_after": None, "details": None}}
    ).encode()


LARGE_BODY_PREFIXES = (
    "/v1/interactions",
    "/v1/ontologies",
)  # the routes that legitimately take a large document (an event's content, an ontology YAML)


class RequestMiddleware:
    def __init__(self, app: ASGIApp, *, max_body_bytes: int, default_body_bytes: int | None = None):
        self.app, self.max_body_bytes = app, max_body_bytes
        self.default_body_bytes = min(default_body_bytes or max_body_bytes, max_body_bytes)

    def limit_for(self, path: str) -> int:
        """A body limit per route class (P9): large only where a large document is the point."""
        return self.max_body_bytes if path.startswith(LARGE_BODY_PREFIXES) else self.default_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        incoming = headers.get("x-request-id")
        request_id = incoming if incoming and is_uuid(incoming) else uuid7_str()  # L6: the router ignores non-UUID ids
        corr_in = headers.get("x-correlation-id")
        correlation_id = corr_in if corr_in and is_uuid(corr_in) else request_id
        context.clear()
        context.bind(request_id=request_id, correlation_id=correlation_id)
        started, status_holder = time.perf_counter(), {"status": 500, "started": False}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"], status_holder["started"] = message["status"], True
                h = MutableHeaders(scope=message)
                h["x-request-id"], h["x-correlation-id"] = request_id, correlation_id
            await send(message)

        received = {"n": 0}
        limit = self.limit_for(scope.get("path", ""))

        async def limited_receive() -> Message:
            message = await receive()
            if message["type"] == "http.request":
                received["n"] += len(message.get("body", b""))
                if received["n"] > limit:
                    raise _BodyTooLarge()
            return message

        try:
            length = headers.get("content-length")
            if length and length.isdigit() and int(length) > limit:
                raise _BodyTooLarge()
            await self.app(scope, limited_receive, send_wrapper)
        except _BodyTooLarge:
            if not status_holder["started"]:
                body = envelope_bytes(PayloadTooLarge.code, PayloadTooLarge.default_message, request_id)
                await send_wrapper(
                    {
                        "type": "http.response.start",
                        "status": 413,
                        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
                    }
                )
                await send_wrapper({"type": "http.response.body", "body": body})
        finally:
            elapsed = time.perf_counter() - started
            route = scope.get("route")
            template = getattr(route, "path", None) or "unmatched"  # route template, never the raw path
            status = int(status_holder["status"])
            metrics.http_requests_total.labels(route=template, method=scope["method"], status=str(status)).inc()
            metrics.http_request_duration_seconds.labels(route=template, method=scope["method"]).observe(elapsed)
            log.info(
                "http_request",
                method=scope["method"],
                route=template,
                status=status,
                duration_ms=round(elapsed * 1000, 2),
                client_request_ref=incoming if incoming and not is_uuid(incoming) else None,
            )
            context.clear()


READ_ONLY_ALLOWED = frozenset(
    {
        "/v1/context/build",  # a build is a read of knowledge (its trace is bookkeeping)
        "/v1/memories/search",
        "/v1/graph/search",
        "/v1/auth/token",
        "/v1/auth/oidc/callback",
        "/v1/auth/oidc/refresh",
        "/v1/auth/oidc/logout",
    }
)
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class MaintenanceMiddleware:
    """MAINTENANCE_MODE=read_only (phase-9 restore rehearsal): a restored environment answers reads and builds contexts, and refuses every other write with 503
    `maintenance_read_only` until a person has verified it. Pure ASGI, so a refused write never reaches a handler (nothing is half-done)."""

    def __init__(self, app: ASGIApp, *, mode: str):
        self.app, self.read_only = app, mode == "read_only"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self.read_only or scope["type"] != "http" or scope["method"] not in _WRITE_METHODS or scope["path"] in READ_ONLY_ALLOWED:
            await self.app(scope, receive, send)
            return
        metrics.maintenance_refusals_total.inc()
        request_id = Headers(scope=scope).get("x-request-id") or uuid7_str()
        body = envelope_bytes("maintenance_read_only", "This environment is read-only while it is being verified", request_id)
        await send(
            {
                "type": "http.response.start",
                "status": 503,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()), (b"retry-after", b"60")],
            }
        )
        await send({"type": "http.response.body", "body": body})


class SecurityHeadersMiddleware:
    """Headers every response carries (phase-9 WP6). The API serves JSON only, so its CSP is the strictest there is; the admin console's CSP (no inline scripts, no framing, the IdP
    as the only extra connect target) is set by the proxy that serves its static files (deploy/compose/Caddyfile). HSTS only when the request came over TLS."""

    STATIC = [
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"no-referrer"),
        (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
        (b"cross-origin-resource-policy", b"same-site"),
        (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
        (b"x-frame-options", b"DENY"),
    ]

    def __init__(self, app: ASGIApp, *, hsts: bool):
        self.app, self.hsts = app, hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        https = scope.get("scheme") == "https" or Headers(scope=scope).get("x-forwarded-proto") == "https"

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                h = MutableHeaders(scope=message)
                for k, v in self.STATIC:
                    if k.decode() not in h:
                        h[k.decode()] = v.decode()
                if self.hsts and https:
                    h["strict-transport-security"] = "max-age=63072000; includeSubDomains"
            await send(message)

        await self.app(scope, receive, wrapped)
