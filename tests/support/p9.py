"""P9 test rig: extra API replicas against the same database (the shared limiter and the invalidation listener need more than one process)."""

from __future__ import annotations

import asyncio
import os
import subprocess
import uuid as _uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from asgi_lifespan import LifespanManager

from tests.conftest import ROOT_COMPOSE_ARGS, make_test_settings


@dataclass
class Replica:
    app: Any
    client: httpx.AsyncClient

    @property
    def state(self) -> Any:
        return self.app.state.kg


@asynccontextmanager
async def replicas(api: Any, n: int = 2, **overrides: Any) -> AsyncIterator[list[Replica]]:
    """`n` independent API apps (own pools, own caches, own listeners) on the test database, sharing nothing but Postgres and Neo4j."""
    from kg_api.main import build_app

    base = dict(
        oso_router_mock_url="http://127.0.0.1:8782/v1",
        oso_router_api_key_api="mock-key",
        projection_request_budget_seconds=60.0,
        limiter_backend="postgres",
        principal_requests_per_minute=1_000_000,
        context_rate_per_minute=1_000_000,
        context_rate_burst=100_000,
    )
    settings = make_test_settings(**{**base, **overrides})
    out: list[Replica] = []
    managers: list[LifespanManager] = []
    clients: list[httpx.AsyncClient] = []
    try:
        for _ in range(n):
            app = build_app(settings)
            manager = LifespanManager(app, startup_timeout=60, shutdown_timeout=60)
            await manager.__aenter__()
            managers.append(manager)
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=manager.app), base_url="http://kg.test")
            clients.append(client)
            out.append(Replica(app, client))
        yield out
    finally:
        for c in clients:
            await c.aclose()
        for m in reversed(managers):
            await m.__aexit__(None, None, None)


# ---- a real backup / restore round trip against the compose Postgres (the same mechanism tools/backup uses) ----------------------------------------------------
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _compose_exec(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603
        ["docker", "compose", *ROOT_COMPOSE_ARGS, "exec", "-T", "-e", "PGPASSWORD=kg_admin_dev", "postgres", *args],  # noqa: S607
        cwd=_REPO,
        capture_output=True,
        check=False,
    )


def _must(proc: subprocess.CompletedProcess[bytes], what: str) -> None:
    if proc.returncode != 0:
        raise RuntimeError(f"{what} failed: {proc.stderr.decode()[-800:]}")


async def dump_database(db: str = "kg_test") -> str:
    """`pg_dump -Fc` inside the Postgres container; returns the path INSIDE the container."""
    path = f"/tmp/p9-{_uuid.uuid4().hex[:8]}.dump"  # noqa: S108
    proc = await asyncio.to_thread(_compose_exec, "pg_dump", "-U", "postgres", "-Fc", "-f", path, db)
    _must(proc, "pg_dump")
    return path


async def reset_scratch(scratch: str = "kg_scratch") -> None:
    """An empty scratch database exactly as postgres-init creates it (the migration round-trip tests expect that and nothing else)."""
    for cmd in (
        ["psql", "-U", "postgres", "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-c", f"DROP DATABASE IF EXISTS {scratch} WITH (FORCE)"],
        ["psql", "-U", "postgres", "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-c", f"CREATE DATABASE {scratch} OWNER kg_owner"],
        [
            "psql",
            "-U",
            "postgres",
            "-d",
            scratch,
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            f"CREATE EXTENSION IF NOT EXISTS vector; REVOKE ALL ON DATABASE {scratch} FROM PUBLIC; GRANT CONNECT ON DATABASE {scratch} TO kg_app, kg_owner",
        ],
    ):
        _must(await asyncio.to_thread(_compose_exec, *cmd), cmd[0])


async def restore_into_scratch(path: str, scratch: str = "kg_scratch") -> None:
    """Recreates the scratch database, then pg_restore (as the superuser, ownership kept), the way the restore rehearsal does. Call `reset_scratch` afterwards."""
    await reset_scratch(scratch)
    # the dump creates the vector extension itself: start from a database without it
    _must(await asyncio.to_thread(_compose_exec, "psql", "-U", "postgres", "-d", scratch, "-c", "DROP EXTENSION IF EXISTS vector"), "psql")
    _must(await asyncio.to_thread(_compose_exec, "pg_restore", "-U", "postgres", "-d", scratch, "--exit-on-error", path), "pg_restore")


async def drop_dump(path: str) -> None:
    await asyncio.to_thread(_compose_exec, "rm", "-f", path)
