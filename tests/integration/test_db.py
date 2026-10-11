"""WP3: database foundation. T-DB-01..06."""

from __future__ import annotations

import asyncio
import re
import uuid

import pytest
from kg_db import migrate
from kg_db.engine import UnsafeDatabaseRole, assert_role_is_safe, create_app_engine
from kg_db.session import system_session, tenant_session
from kg_domain.principal import WorkspaceContext
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from tests.conftest import APP_URL, OWNER_URL, SCRATCH_URL, require

pytestmark = pytest.mark.integration


def ctx(ws: uuid.UUID) -> WorkspaceContext:
    return WorkspaceContext.for_system_task(ws, reason="test")


async def make_workspace(engine, slug: str, handles: list[str]) -> uuid.UUID:
    ws = uuid.uuid4()
    async with tenant_session(engine, ctx(ws)) as c:
        await c.execute(text("INSERT INTO workspaces (id, slug, display_name) VALUES (:i, :s, :s)"), {"i": ws, "s": f"{slug}-{ws.hex[:6]}"})
        for h in handles:
            await c.execute(
                text("INSERT INTO principals (id, workspace_id, kind, handle, display_name) VALUES (:i, :w, 'agent', :h, :h)"),
                {"i": uuid.uuid4(), "w": ws, "h": h},
            )
    return ws


async def test_db_01_role_assertion(test_settings, app_engine, migrated_db):
    assert await assert_role_is_safe(app_engine) == "kg_app"
    owner = create_app_engine(test_settings, url=OWNER_URL)
    try:
        with pytest.raises(UnsafeDatabaseRole, match="bypass"):
            await assert_role_is_safe(owner)
    finally:
        await owner.dispose()


async def test_db_02_pooled_connection_reuse_never_leaks_tenant(test_settings, migrated_db):
    engine = create_app_engine(test_settings, url=APP_URL, pool_size=1, max_overflow=0)  # one physical connection serves every request
    try:
        demo = await make_workspace(engine, "demo", ["a1", "a2", "a3"])
        other = await make_workspace(engine, "other", ["b1"])
        async with tenant_session(engine, ctx(demo)) as c:  # request A
            pid_a = (await c.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            assert (await c.execute(text("SELECT count(*) FROM principals"))).scalar_one() == 3
        async with system_session(engine) as c:  # request B: same connection, no context
            assert (await c.execute(text("SELECT pg_backend_pid()"))).scalar_one() == pid_a
            assert (await c.execute(text("SELECT count(*) FROM principals"))).scalar_one() == 0
            assert (await c.execute(text("SELECT count(*) FROM workspaces"))).scalar_one() == 0
            assert (await c.execute(text("SELECT current_setting('app.workspace_id', true)"))).scalar_one() in (None, "")
        async with tenant_session(engine, ctx(other)) as c:  # request C
            assert (await c.execute(text("SELECT pg_backend_pid()"))).scalar_one() == pid_a
            handles = [r[0] for r in (await c.execute(text("SELECT handle FROM principals"))).all()]
            assert handles == ["b1"]
        with pytest.raises(RuntimeError):  # an error also resets the setting
            async with tenant_session(engine, ctx(demo)) as c:
                raise RuntimeError("boom")
        async with system_session(engine) as c:
            assert (await c.execute(text("SELECT count(*) FROM principals"))).scalar_one() == 0
    finally:
        await engine.dispose()


async def test_db_03_rls_backstop_on_unscoped_query(app_engine):
    demo = await make_workspace(app_engine, "demo", ["x1", "x2"])
    other = await make_workspace(app_engine, "other", ["y1"])
    async with tenant_session(app_engine, ctx(demo)) as c:
        rows = (await c.execute(text("SELECT workspace_id, handle FROM principals"))).all()  # deliberately no WHERE
        assert {r.workspace_id for r in rows} == {demo} and {r.handle for r in rows} == {"x1", "x2"}
        assert (await c.execute(text("SELECT count(*) FROM workspaces"))).scalar_one() == 1
        assert other not in {r.workspace_id for r in rows}


async def test_db_04_audit_log_is_append_only_and_tenant_checked(app_engine):
    demo = await make_workspace(app_engine, "demo", ["a"])
    other = await make_workspace(app_engine, "other", ["b"])
    audit_id = uuid.uuid4()
    async with tenant_session(app_engine, ctx(demo)) as c:
        await c.execute(
            text("INSERT INTO audit_log (id, workspace_id, action, outcome) VALUES (:i, :w, 'test.event', 'allowed')"),
            {"i": audit_id, "w": demo},
        )
    with pytest.raises(DBAPIError):
        async with tenant_session(app_engine, ctx(demo)) as c:
            await c.execute(text("UPDATE audit_log SET action = 'tampered' WHERE id = :i"), {"i": audit_id})
    with pytest.raises(DBAPIError):
        async with tenant_session(app_engine, ctx(demo)) as c:
            await c.execute(text("DELETE FROM audit_log WHERE id = :i"), {"i": audit_id})
    with pytest.raises(DBAPIError):  # WITH CHECK: foreign workspace_id
        async with tenant_session(app_engine, ctx(demo)) as c:
            await c.execute(
                text("INSERT INTO audit_log (id, workspace_id, action, outcome) VALUES (:i, :w, 'x', 'allowed')"),
                {"i": uuid.uuid4(), "w": other},
            )
    # the trigger holds even for the table owner (who bypasses RLS and has every privilege)
    owner = create_app_engine(__import__("tests.conftest", fromlist=["x"]).make_test_settings(), url=OWNER_URL)
    try:
        for stmt in ("UPDATE audit_log SET action = 'tampered'", "DELETE FROM audit_log", "TRUNCATE audit_log"):
            with pytest.raises(DBAPIError, match="append-only"):
                async with owner.begin() as c:
                    await c.execute(text(stmt))
    finally:
        await owner.dispose()


async def test_db_05_credential_functions_and_key_hash_protection(app_engine):
    ws = await make_workspace(app_engine, "demo", ["agent"])
    key_id, prefix = uuid.uuid4(), "pfx" + uuid.uuid4().hex[:5]
    async with tenant_session(app_engine, ctx(ws)) as c:
        pid = (await c.execute(text("SELECT id FROM principals"))).scalar_one()
        await c.execute(
            text("INSERT INTO role_assignments (principal_id, workspace_id, role) VALUES (:p, :w, 'agent')"), {"p": pid, "w": ws}
        )
        await c.execute(
            text("INSERT INTO api_keys (id, workspace_id, principal_id, prefix, key_hash) VALUES (:k, :w, :p, :x, 'h')"),
            {"k": key_id, "w": ws, "p": pid, "x": prefix},
        )
    async with system_session(app_engine) as c:  # no workspace set: still resolvable
        rows = (await c.execute(text("SELECT * FROM auth.lookup_credential(:p)"), {"p": prefix})).all()
        assert len(rows) == 1 and rows[0].workspace_id == ws and rows[0].roles == ["agent"] and rows[0].key_hash == "h"
        assert (await c.execute(text("SELECT count(*) FROM auth.lookup_credential('nope')"))).scalar_one() == 0
        st = (await c.execute(text("SELECT * FROM auth.credential_status(:k, :p)"), {"k": key_id, "p": pid})).all()
        assert len(st) == 1 and st[0].key_revoked_at is None
        assert (
            await c.execute(text("SELECT count(*) FROM auth.credential_status(:k, :p)"), {"k": key_id, "p": uuid.uuid4()})
        ).scalar_one() == 0
        # without a workspace the table itself yields nothing, and key_hash is not selectable by the app role at all
        assert (await c.execute(text("SELECT count(*) FROM api_keys"))).scalar_one() == 0
    with pytest.raises(ProgrammingError, match="permission denied"):
        async with tenant_session(app_engine, ctx(ws)) as c:
            await c.execute(text("SELECT key_hash FROM api_keys"))
    with pytest.raises(ProgrammingError, match="permission denied"):
        async with tenant_session(app_engine, ctx(ws)) as c:
            await c.execute(text("SELECT * FROM api_keys"))
    async with system_session(app_engine) as c:  # function shape
        fn = (
            await c.execute(
                text("""
            SELECT p.prosecdef, pg_get_userbyid(p.proowner) AS owner, p.proconfig, COALESCE(p.proacl::text, '') AS acl
            FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'auth' AND p.proname IN ('lookup_credential','credential_status','touch_key')""")
            )
        ).all()
        assert len(fn) == 3
        for f in fn:
            assert f.prosecdef and f.owner == "kg_owner" and any("search_path=pg_catalog, public" in s for s in f.proconfig)
            assert not re.search(r"(^|[{,])=X/", f.acl) and "kg_app=X/kg_owner" in f.acl  # nothing granted to PUBLIC; kg_app may execute


async def test_db_06_migration_roundtrip():
    """Runs on the scratch database so it never wipes kg_test while other suites hold seeded data."""
    require(SCRATCH_URL, "postgres")
    for step in (migrate.downgrade, migrate.upgrade, migrate.downgrade, migrate.upgrade):
        await asyncio.to_thread(step, SCRATCH_URL)
