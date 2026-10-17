"""Phase 1 demo: workspace isolation, role gating, immediate revocation, readiness. Exits non-zero if any assertion fails.

Run after `make up && make seed` (or via `make demo-p1`). Reads API keys from .local/dev-credentials.json.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
from kg_cli.seed import workspace_id_for
from kg_common.settings import Settings
from kg_db.engine import create_app_engine
from kg_db.session import tenant_session
from kg_domain.principal import WorkspaceContext
from kg_governance.repositories import admin

ROOT = Path(__file__).resolve().parents[1]
BASE = os.environ.get("KG_API_URL", "http://127.0.0.1:8000")
DEMO_WS, OTHER_WS = workspace_id_for("demo"), workspace_id_for("other_demo")
Row = tuple[str, str, str, bool]


def bearer(key: str) -> dict[str, str]:
    return {"authorization": f"Bearer {key}"}


async def make_throwaway_key(settings: Settings) -> tuple[str, str]:
    """A throwaway key for the seeded conversation_app principal (created through the same helpers `kg keys create` uses)."""
    engine = create_app_engine(settings, pool_size=2, max_overflow=0)
    ctx = WorkspaceContext.for_system_task(DEMO_WS, reason="demo-p1 throwaway key")
    try:
        async with tenant_session(engine, ctx) as conn:
            pid = await admin.principal_id_by_handle(conn, ctx, "conversation_app")
            _, minted = await admin.create_key(conn, ctx, principal_id=pid, pepper=settings.auth_key_pepper.get_secret_value())
        return minted.plaintext, minted.prefix
    finally:
        await engine.dispose()


async def revoke(settings: Settings, prefix: str) -> None:
    engine = create_app_engine(settings, pool_size=2, max_overflow=0)
    ctx = WorkspaceContext.for_system_task(DEMO_WS, reason="demo-p1 revoke")
    try:
        async with tenant_session(engine, ctx) as conn:
            assert await admin.revoke_key(conn, ctx, prefix=prefix)
    finally:
        await engine.dispose()


async def main() -> int:
    creds_path = ROOT / ".local" / "dev-credentials.json"
    if not creds_path.exists():
        print("missing .local/dev-credentials.json: run `make seed` first", file=sys.stderr)
        return 2
    keys: dict[str, str] = json.loads(creds_path.read_text())
    settings = Settings()
    rows: list[Row] = []

    async def step(
        name: str, expect: str, fn: Callable[[httpx.AsyncClient], Awaitable[tuple[bool, str]]], client: httpx.AsyncClient
    ) -> None:
        try:
            ok, actual = await fn(client)
        except Exception as exc:
            ok, actual = False, f"{type(exc).__name__}"
        rows.append((name, expect, actual, ok))

    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:

        async def s1(c: httpx.AsyncClient) -> tuple[bool, str]:
            r = await c.get("/v1/me", headers=bearer(keys["demo/agent_planning"]))
            slug = r.json()["workspace"]["slug"] if r.status_code == 200 else "-"
            return r.status_code == 200 and slug == "demo", f"{r.status_code} workspace={slug}"

        async def s2(c: httpx.AsyncClient) -> tuple[bool, str]:
            r = await c.get("/v1/workspaces/current", headers=bearer(keys["demo/agent_planning"]))
            return r.status_code == 200 and r.json()["id"] == str(
                DEMO_WS
            ), f"{r.status_code} id matches demo={r.json().get('id') == str(DEMO_WS)}"

        async def s3(c: httpx.AsyncClient) -> tuple[bool, str]:
            h = bearer(keys["other_demo/agent_other"])
            foreign, rand = await c.get(f"/v1/workspaces/{DEMO_WS}", headers=h), await c.get(f"/v1/workspaces/{uuid.uuid4()}", headers=h)
            strip = lambda r: {k: v for k, v in r.json()["error"].items() if k != "request_id"}  # noqa: E731
            same = foreign.status_code == rand.status_code == 404 and strip(foreign) == strip(rand)
            return same, f"{foreign.status_code} vs {rand.status_code}, identical body={strip(foreign) == strip(rand)}"

        async def s4(c: httpx.AsyncClient) -> tuple[bool, str]:
            r = await c.get("/v1/principals", headers=bearer(keys["other_demo/admin@other"]))
            handles = sorted(i["handle"] for i in r.json()["items"]) if r.status_code == 200 else []
            return handles == ["admin@other", "agent_other"], f"{r.status_code} {handles}"

        async def s5(c: httpx.AsyncClient) -> tuple[bool, str]:
            r = await c.get("/v1/principals", headers=bearer(keys["demo/agent_planning"]))
            return r.status_code == 403, f"{r.status_code} {r.json()['error']['code']}"

        async def s6(c: httpx.AsyncClient) -> tuple[bool, str]:
            r = await c.get("/v1/principals", headers=bearer(keys["demo/admin@demo"]))
            n = len(r.json()["items"]) if r.status_code == 200 else -1
            return n == 7, f"{r.status_code} {n} principals"

        async def s7(c: httpx.AsyncClient) -> tuple[bool, str]:
            key, prefix = await make_throwaway_key(settings)
            before = (await c.get("/v1/me", headers=bearer(key))).status_code
            token = (await c.post("/v1/auth/token", headers=bearer(key))).json()["access_token"]
            jwt_before = (await c.get("/v1/me", headers=bearer(token))).status_code
            await revoke(settings, prefix)
            key_after = (await c.get("/v1/me", headers=bearer(key))).status_code
            jwt_after = (await c.get("/v1/me", headers=bearer(token))).status_code
            return (before, jwt_before, key_after, jwt_after) == (
                200,
                200,
                401,
                401,
            ), f"key {before}->{key_after}, jwt {jwt_before}->{jwt_after}"

        async def s8(c: httpx.AsyncClient) -> tuple[bool, str]:
            r = await c.get("/health/ready")
            checks = r.json()["checks"]
            return r.status_code == 200 and all(v == "ok" for v in checks.values()), f"{r.status_code} {checks}"

        await step("1 agent sees its own workspace", "200, workspace=demo", s1, c)
        await step("2 current workspace is demo", "200, demo id", s2, c)
        await step("3 other_demo agent: demo workspace id", "404 identical to a random id", s3, c)
        await step("4 other_demo admin lists principals", "only other_demo principals", s4, c)
        await step("5 demo agent lists principals", "403 forbidden (no principal.read)", s5, c)
        await step("6 demo admin lists principals", "7 demo principals (P5 added agent_support)", s6, c)
        await step("7 revoke a throwaway key", "key and its JWT -> 401", s7, c)
        await step("8 readiness", "postgres, neo4j, router(mock), worker ok", s8, c)

    width = max(len(r[0]) for r in rows)
    print(f"{'step'.ljust(width)}  result  expected -> actual")
    for name, expect, actual, ok in rows:
        print(f"{name.ljust(width)}  {'PASS' if ok else 'FAIL'}    {expect} -> {actual}")
    failed = [r for r in rows if not r[3]]
    print(f"\n{len(rows) - len(failed)}/{len(rows)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
