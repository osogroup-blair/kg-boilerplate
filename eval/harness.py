"""A shared in-process API stack for the evaluation suites that need one (mock router, the test database): the same bootstrap `eval.context.run_eval` uses."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx

MOCK = "http://127.0.0.1:8782"


@asynccontextmanager
async def stack(**overrides: Any) -> AsyncIterator[Any]:
    from asgi_lifespan import LifespanManager
    from kg_api.main import build_app
    from kg_db import migrate
    from kg_db.engine import create_app_engine

    from tests.conftest import APP_URL, OWNER_URL, ApiStack, make_test_settings

    await asyncio.to_thread(migrate.upgrade, OWNER_URL, "head")
    settings = make_test_settings(
        oso_router_mock_url=f"{MOCK}/v1",
        oso_router_api_key_api="mock-key",
        oso_router_api_key_worker="mock-key",
        projection_request_budget_seconds=60.0,
        context_rate_per_minute=1_000_000,
        context_rate_burst=100_000,
        principal_requests_per_minute=1_000_000,
        **overrides,
    )
    engine = create_app_engine(settings, pool_size=3, max_overflow=2, url=APP_URL)
    app = build_app(settings)
    try:
        async with LifespanManager(app, startup_timeout=60, shutdown_timeout=60) as manager:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=manager.app), base_url="http://kg.test") as client:
                yield ApiStack(app, client, {}, engine, settings)
    finally:
        await engine.dispose()
