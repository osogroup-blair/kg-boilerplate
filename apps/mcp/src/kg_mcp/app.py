"""The ASGI app: `/mcp` (Streamable HTTP, stateless, JSON responses), `/health` and `/metrics`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.types import Receive, Scope, Send

from kg_mcp.config import McpSettings
from kg_mcp.server import REGISTRY, build_server


def create_app(settings: McpSettings | None = None, *, transport: httpx.AsyncBaseTransport | None = None) -> Starlette:
    """`transport` lets a test point the SDK at an in-process API; in production it is None and the SDK speaks HTTP to `KG_API_URL`."""
    settings = settings or McpSettings.from_env()
    manager = StreamableHTTPSessionManager(app=build_server(settings, transport=transport), stateless=True, json_response=True)

    async def health(_: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def metrics(_: Request) -> Response:
        return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    async def mcp_endpoint(scope: Scope, receive: Receive, send: Send) -> None:
        await manager.handle_request(scope, receive, send)

    @asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            yield

    return Starlette(
        routes=[Route("/health", health), Route("/metrics", metrics), Mount("/mcp", app=mcp_endpoint)],
        lifespan=lifespan,
    )


def main() -> None:  # pragma: no cover
    import uvicorn

    s = McpSettings.from_env()
    uvicorn.run(create_app(s), host=s.host, port=s.port, log_level="info")
