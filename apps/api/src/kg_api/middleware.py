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


class RequestMiddleware:
    def __init__(self, app: ASGIApp, *, max_body_bytes: int):
        self.app, self.max_body_bytes = app, max_body_bytes

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

        async def limited_receive() -> Message:
            message = await receive()
            if message["type"] == "http.request":
                received["n"] += len(message.get("body", b""))
                if received["n"] > self.max_body_bytes:
                    raise _BodyTooLarge()
            return message

        try:
            length = headers.get("content-length")
            if length and length.isdigit() and int(length) > self.max_body_bytes:
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
