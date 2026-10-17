"""P7 WP0: migration 0007. The cycle and its publish guard, the backfill (every old grant is {read, propose}; the demo app is a trusted source), RLS on the new table
(and a pooled connection reused across tenants), the CHECKs that keep a registration well-formed, and the column grants (a key's hash is still unreadable)."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from kg_db import migrate
from kg_db.session import system_session, tenant_session
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError

from tests.conftest import SCRATCH_URL, require
from tests.support.p4 import ctx_of, sql

pytestmark = pytest.mark.integration


async def run(api, t, statement: str, **params) -> None:
    async with tenant_session(api.engine, ctx_of(t)) as c:
        await c.execute(text(statement), {"ws": t.ws, **params})


async def test_up_down_up_and_the_publish_guard():
    require(SCRATCH_URL, "postgres")
    from sqlalchemy.ext.asyncio import create_async_engine

    def drop() -> None:
        migrate.downgrade(
            SCRATCH_URL,
            "base",
            force_drop_extracted=True,
            force_drop_memories=True,
            force_forget_privileges=True,
            force_drop_governance=True,
        )

    await asyncio.to_thread(drop)
    await asyncio.to_thread(migrate.upgrade, SCRATCH_URL, "0006")
    engine = create_async_engine(SCRATCH_URL)
    ws, agent, app = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    try:
        async with engine.begin() as c:  # state as 0006 left it: grants without privileges, the demo app without a trust flag
            await c.execute(
                text("INSERT INTO workspaces (id, slug, display_name) VALUES (:w, :s, 's')"), {"w": ws, "s": f"p7-{ws.hex[:6]}"}
            )
            for pid, kind, handle in ((agent, "agent", "agent_x"), (app, "app", "conversation_app")):
                await c.execute(
                    text("INSERT INTO principals (id, workspace_id, kind, handle, display_name) VALUES (:p, :w, :k, :h, :h)"),
                    {"p": pid, "w": ws, "k": kind, "h": handle},
                )
            await c.execute(
                text(
                    "INSERT INTO principal_scopes (id, principal_id, workspace_id, scope_kind, scope_ref) VALUES (gen_random_uuid(), :p, :w, 'project', 'newco-plan')"
                ),
                {"p": agent, "w": ws},
            )
        await asyncio.to_thread(migrate.upgrade, SCRATCH_URL, "head")
        async with engine.begin() as c:
            assert (await c.execute(text("SELECT privileges FROM principal_scopes"))).scalar_one() == [
                "read",
                "propose",
            ]  # publish is never implicit
            rows = {
                r.handle: r
                for r in (await c.execute(text("SELECT handle, trusted_source, model_disclosure, allowed_profiles FROM principals"))).all()
            }
            assert (
                rows["conversation_app"].trusted_source is True and rows["agent_x"].trusted_source is False
            )  # the demo's system of record keeps its authority
            assert rows["agent_x"].model_disclosure == "allowed" and list(rows["agent_x"].allowed_profiles) == []
            await c.execute(text("UPDATE principal_scopes SET privileges = '{read,propose,publish}'"))
        with pytest.raises(Exception, match="publish"):  # the guard refuses to forget a publish privilege silently
            await asyncio.to_thread(migrate.downgrade, SCRATCH_URL, "0006")
        async with engine.begin() as c:
            assert (await c.execute(text("SELECT count(*) FROM principal_scopes WHERE 'publish' = ANY(privileges)"))).scalar_one() == 1
        await asyncio.to_thread(lambda: migrate.downgrade(SCRATCH_URL, "0006", force_forget_privileges=True, force_drop_governance=True))
        async with engine.begin() as c:
            assert (await c.execute(text("SELECT to_regclass('agent_usage_daily') IS NULL"))).scalar_one() is True
            cols = {
                r[0]
                for r in (await c.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name = 'principals'"))).all()
            }
            assert not ({"trusted_source", "model_disclosure", "revoke_reason", "quota_requests_per_minute"} & cols)
            assert (await c.execute(text("SELECT count(*) FROM principal_scopes"))).scalar_one() == 1  # the grant itself survives the cycle
        await asyncio.to_thread(migrate.upgrade, SCRATCH_URL, "head")
    finally:
        await engine.dispose()
        await asyncio.to_thread(drop)


async def test_the_new_table_has_forced_rls_and_a_tenant_policy(app_engine):
    async with system_session(app_engine) as c:
        row = (
            await c.execute(
                text(
                    "SELECT c.relrowsecurity, c.relforcerowsecurity, (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) AS policies "
                    "FROM pg_class c WHERE c.relname = 'agent_usage_daily'"
                )
            )
        ).one()
    assert row.relrowsecurity and row.relforcerowsecurity and row.policies >= 1


async def test_a_tenant_sees_only_its_own_usage_rows_and_a_reused_connection_forgets_the_last_tenant(api, tenant, bare_tenant):
    a, b = tenant, bare_tenant
    for t in (a, b):
        pid = await t.principal_id("agent")
        await run(
            api, t, "INSERT INTO agent_usage_daily (workspace_id, principal_id, day, requests) VALUES (:ws, :p, current_date, 7)", p=pid
        )
    for _ in range(4):  # a pool of three connections, alternating tenants: no row crosses
        for t, other in ((a, b), (b, a)):
            seen = await sql(api, t, "SELECT workspace_id FROM agent_usage_daily")  # NO workspace predicate on purpose: RLS alone
            assert [r.workspace_id for r in seen] == [t.ws]
            assert (await sql(api, t, "SELECT count(*) AS n FROM agent_usage_daily WHERE workspace_id = :o", o=other.ws))[0].n == 0
    async with system_session(api.engine) as c:  # no workspace set: nothing at all (fail closed)
        assert (await c.execute(text("SELECT count(*) FROM agent_usage_daily"))).scalar_one() == 0
    async with tenant_session(api.engine, ctx_of(a)) as c:  # and a row cannot be written into another tenant
        with pytest.raises((IntegrityError, DBAPIError, ProgrammingError)):
            await c.execute(
                text("INSERT INTO agent_usage_daily (workspace_id, principal_id, day) VALUES (:o, gen_random_uuid(), current_date)"),
                {"o": b.ws},
            )


async def test_the_registration_checks_hold(api, tenant):
    pid = await tenant.principal_id("agent")
    app = await tenant.principal_id("app")
    cases = [
        ("a user cannot be a trusted source", "UPDATE principals SET trusted_source = true WHERE id = :p", pid),
        ("disclosure is allowed or denied", "UPDATE principals SET model_disclosure = 'maybe' WHERE id = :p", pid),
        ("a revocation has a known reason", "UPDATE principals SET revoke_reason = 'bored' WHERE id = :p", pid),
        ("a quota is positive", "UPDATE principals SET quota_requests_per_minute = 0 WHERE id = :p", pid),
        (
            "privileges are a subset of read, propose, publish",
            "UPDATE principal_scopes SET privileges = '{read,own}' WHERE principal_id = :p",
            pid,
        ),
        ("a grant carries at least one privilege", "UPDATE principal_scopes SET privileges = '{}' WHERE principal_id = :p", pid),
    ]
    for why, statement, who in cases:
        with pytest.raises((IntegrityError, DBAPIError)):
            await run(api, tenant, statement, p=who)
        _ = why
    await run(api, tenant, "UPDATE principals SET trusted_source = true WHERE id = :p", p=app)  # an app can


async def test_a_key_hash_is_still_unreadable_by_the_application_role(api, tenant):
    with pytest.raises((ProgrammingError, DBAPIError)):
        await sql(api, tenant, "SELECT key_hash FROM api_keys")
    assert (await sql(api, tenant, "SELECT name FROM api_keys LIMIT 1")) is not None  # the new column is readable
