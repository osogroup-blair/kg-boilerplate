"""WP4: migration 0002. Immutable published versions, append-only activations, ledger column grants, RLS on every new table, the ops function."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

import pytest
from kg_db.engine import create_app_engine
from kg_db.session import system_session, tenant_session
from kg_domain.principal import WorkspaceContext
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError

from tests.conftest import OWNER_URL, make_test_settings
from tests.integration.test_db import ctx, make_workspace

pytestmark = pytest.mark.integration
HASH = "a" * 64
P2_TABLES = [
    "ontology_versions",
    "ontology_activations",
    "workspace_ontology",
    "commands",
    "entities",
    "entity_external_ids",
    "facts",
    "review_items",
    "idempotency_records",
]


@dataclass
class Rows:
    ws: uuid.UUID
    principal: uuid.UUID
    version: uuid.UUID
    command: uuid.UUID
    person: uuid.UUID
    org: uuid.UUID
    fact: uuid.UUID


async def publish(c, ws: uuid.UUID, version: uuid.UUID, number: str = "1.0.0") -> None:  # type: ignore[no-untyped-def]
    await c.execute(
        text(
            "UPDATE ontology_versions SET state = 'published', content_hash = :h, published_at = now(), spec = CAST(:s AS jsonb), version = :v, ontology_key = 'k' "
            "WHERE id = :i AND workspace_id = :w"
        ),
        {"h": HASH, "s": '{"x": 1}', "v": number, "i": version, "w": ws},
    )


async def draft(c, ws: uuid.UUID, principal: uuid.UUID | None = None) -> uuid.UUID:  # type: ignore[no-untyped-def]
    vid = uuid.uuid4()
    await c.execute(
        text("INSERT INTO ontology_versions (id, workspace_id, created_by) VALUES (:i, :w, :p)"), {"i": vid, "w": ws, "p": principal}
    )
    return vid


async def seed(engine) -> Rows:  # type: ignore[no-untyped-def]
    ws = await make_workspace(engine, "p2", ["admin"])
    async with tenant_session(engine, ctx(ws)) as c:
        principal = (await c.execute(text("SELECT id FROM principals"))).scalar_one()
        v = await draft(c, ws, principal)
        await publish(c, ws, v)
        await c.execute(text("INSERT INTO workspace_ontology (workspace_id, active_version_id) VALUES (:w, :v)"), {"w": ws, "v": v})
        await c.execute(
            text(
                "INSERT INTO ontology_activations (id, workspace_id, version_id, activated_by, reason_code) VALUES (:i, :w, :v, :p, 'publish')"
            ),
            {"i": uuid.uuid4(), "w": ws, "v": v, "p": principal},
        )
        cmd, person, org, fact = (uuid.uuid4() for _ in range(4))
        await c.execute(
            text(
                "INSERT INTO commands (id, workspace_id, idempotency_key, request_hash, command_type, actor_principal_id, authority_class, classification, "
                "ontology_version_id, payload, status, http_status) VALUES (:i, :w, 'k1', 'h', 'entity.create', :p, 'admin_asserted', 'internal', :v, "
                "CAST('{}' AS jsonb), 'applied', 201)"
            ),
            {"i": cmd, "w": ws, "p": principal, "v": v},
        )
        for eid, name in ((person, "Sarah"), (org, "Acme")):
            await c.execute(
                text(
                    "INSERT INTO entities (id, workspace_id, type_key, display_name, ontology_version_id, created_by_command_id, last_command_id) "
                    "VALUES (:i, :w, 'Person', :n, :v, :c, :c)"
                ),
                {"i": eid, "w": ws, "n": name, "v": v, "c": cmd},
            )
        await c.execute(
            text(
                "INSERT INTO entity_external_ids (workspace_id, entity_id, type_key, system, value) VALUES (:w, :e, 'Person', 'hr', 'E-1')"
            ),
            {"w": ws, "e": person},
        )
        await c.execute(
            text(
                "INSERT INTO facts (id, workspace_id, rel_type, source_entity_id, target_entity_id, authority_class, source_command_id, last_command_id, ontology_version_id) "
                "VALUES (:f, :w, 'WORKS_AT', :s, :t, 'admin_asserted', :c, :c, :v)"
            ),
            {"f": fact, "w": ws, "s": person, "t": org, "c": cmd, "v": v},
        )
        await c.execute(
            text(
                "INSERT INTO review_items (id, workspace_id, item_type, command_id, rule_kind, reason_code) VALUES (:i, :w, 'policy_violation', :c, 'queue_if', 'disallowed_endpoint')"
            ),
            {"i": uuid.uuid4(), "w": ws, "c": cmd},
        )
        await c.execute(
            text(
                "INSERT INTO idempotency_records (workspace_id, key, route, request_hash, status_code, response) VALUES (:w, 'k', 'r', 'h', 201, CAST('{}' AS jsonb))"
            ),
            {"w": ws},
        )
    return Rows(ws, principal, v, cmd, person, org, fact)


@pytest.fixture()
def owner_engine():  # type: ignore[no-untyped-def]
    return create_app_engine(make_test_settings(), url=OWNER_URL)


async def test_published_versions_are_immutable_even_for_the_owner(app_engine, owner_engine):
    rows = await seed(app_engine)
    mutations = [
        "UPDATE ontology_versions SET spec = CAST('{\"y\": 2}' AS jsonb)",
        "UPDATE ontology_versions SET source_text = 'edited'",
        "UPDATE ontology_versions SET content_hash = repeat('b', 64)",
        "UPDATE ontology_versions SET version = '9.9.9'",
        "UPDATE ontology_versions SET ontology_key = 'other'",
        "DELETE FROM ontology_versions",
    ]
    try:
        for stmt in mutations:
            with pytest.raises(DBAPIError, match="immutable|cannot be deleted"):
                async with owner_engine.begin() as c:
                    await c.execute(text(f"{stmt} WHERE id = :i"), {"i": rows.version})
    finally:
        await owner_engine.dispose()
    for stmt in mutations:  # and for the application role
        with pytest.raises(DBAPIError, match="immutable|cannot be deleted"):
            async with tenant_session(app_engine, ctx(rows.ws)) as c:
                await c.execute(text(f"{stmt} WHERE id = :i"), {"i": rows.version})
    async with tenant_session(app_engine, ctx(rows.ws)) as c:  # the allowed edits: notes and the validation report
        await c.execute(text("UPDATE ontology_versions SET notes = 'ok' WHERE id = :i"), {"i": rows.version})


async def test_drafts_are_editable_deletable_and_versions_start_as_drafts(app_engine):
    ws = await make_workspace(app_engine, "p2", ["admin"])
    async with tenant_session(app_engine, ctx(ws)) as c:
        v = await draft(c, ws)
        await c.execute(
            text("UPDATE ontology_versions SET spec = CAST('{\"a\": 1}' AS jsonb), version = '1.0.0', ontology_key = 'k' WHERE id = :i"),
            {"i": v},
        )
        await c.execute(text("DELETE FROM ontology_versions WHERE id = :i"), {"i": v})
    with pytest.raises(DBAPIError, match="created as a draft"):
        async with tenant_session(app_engine, ctx(ws)) as c:
            await c.execute(
                text(
                    "INSERT INTO ontology_versions (id, workspace_id, state, ontology_key, version, content_hash, published_at) VALUES (:i, :w, 'published', 'k', '1', :h, now())"
                ),
                {"i": uuid.uuid4(), "w": ws, "h": HASH},
            )


async def test_state_transitions_are_one_way(app_engine):
    ws = await make_workspace(app_engine, "p2", ["admin"])
    async with tenant_session(app_engine, ctx(ws)) as c:
        v = await draft(c, ws)
        with pytest.raises(IntegrityError):  # a published row needs its hash (CHECK)
            async with c.begin_nested():
                await c.execute(text("UPDATE ontology_versions SET state = 'published' WHERE id = :i"), {"i": v})
        await publish(c, ws, v)
        await c.execute(text("UPDATE ontology_versions SET state = 'retired', retired_at = now() WHERE id = :i"), {"i": v})
    for target in ("published", "draft"):
        with pytest.raises(DBAPIError, match="invalid ontology version transition"):
            async with tenant_session(app_engine, ctx(ws)) as c:
                await c.execute(text(f"UPDATE ontology_versions SET state = '{target}' WHERE id = :i"), {"i": v})


async def test_the_active_version_cannot_be_retired_and_must_be_published(app_engine):
    rows = await seed(app_engine)
    with pytest.raises(DBAPIError, match="cannot be retired"):
        async with tenant_session(app_engine, ctx(rows.ws)) as c:
            await c.execute(text("UPDATE ontology_versions SET state = 'retired', retired_at = now() WHERE id = :i"), {"i": rows.version})
    async with tenant_session(app_engine, ctx(rows.ws)) as c:
        d = await draft(c, rows.ws)
    with pytest.raises(DBAPIError, match="must be published"):
        async with tenant_session(app_engine, ctx(rows.ws)) as c:
            await c.execute(text("UPDATE workspace_ontology SET active_version_id = :v WHERE workspace_id = :w"), {"v": d, "w": rows.ws})


async def test_activations_are_append_only_even_for_the_owner(app_engine, owner_engine):
    rows = await seed(app_engine)
    try:
        for stmt in (
            "UPDATE ontology_activations SET reason_code = 'x'",
            "DELETE FROM ontology_activations",
            "TRUNCATE ontology_activations",
        ):
            with pytest.raises(DBAPIError, match="append-only"):
                async with owner_engine.begin() as c:
                    await c.execute(text(stmt))
    finally:
        await owner_engine.dispose()
    with pytest.raises(DBAPIError, match="permission denied|append-only"):  # the application role has no UPDATE/DELETE privilege at all
        async with tenant_session(app_engine, ctx(rows.ws)) as c:
            await c.execute(text("UPDATE ontology_activations SET reason_code = 'x' WHERE workspace_id = :w"), {"w": rows.ws})


async def test_the_command_ledger_is_immutable_except_projection_columns(app_engine):
    rows = await seed(app_engine)
    for stmt in (
        "payload = CAST('{\"x\": 1}' AS jsonb)",
        "status = 'rejected'",
        "decision = CAST('[]' AS jsonb)",
        "result = CAST('{}' AS jsonb)",
        "idempotency_key = 'z'",
        "request_hash = 'z'",
    ):
        with pytest.raises(ProgrammingError, match="permission denied"):
            async with tenant_session(app_engine, ctx(rows.ws)) as c:
                await c.execute(text(f"UPDATE commands SET {stmt} WHERE id = :i"), {"i": rows.command})
    with pytest.raises(ProgrammingError, match="permission denied"):
        async with tenant_session(app_engine, ctx(rows.ws)) as c:
            await c.execute(text("DELETE FROM commands WHERE id = :i"), {"i": rows.command})
    async with tenant_session(app_engine, ctx(rows.ws)) as c:
        await c.execute(
            text("UPDATE commands SET projection_status = 'projected', projection_attempts = 1, projected_at = now() WHERE id = :i"),
            {"i": rows.command},
        )
    with pytest.raises(IntegrityError):  # same key twice in a workspace
        async with tenant_session(app_engine, ctx(rows.ws)) as c:
            await c.execute(
                text(
                    "INSERT INTO commands (id, workspace_id, idempotency_key, request_hash, command_type, actor_principal_id, authority_class, classification, "
                    "ontology_version_id, payload, status, http_status) VALUES (:i, :w, 'k1', 'h', 'entity.create', :p, 'admin_asserted', 'internal', :v, CAST('{}' AS jsonb), 'applied', 201)"
                ),
                {"i": uuid.uuid4(), "w": rows.ws, "p": rows.principal, "v": rows.version},
            )


async def test_facts_and_entities_columns_that_define_history_cannot_be_rewritten_by_the_app(app_engine):
    rows = await seed(app_engine)
    for table, ident, column in (
        ("facts", rows.fact, "recorded_at = now()"),
        ("facts", rows.fact, "source_command_id = last_command_id"),
        ("facts", rows.fact, "rel_type = 'X'"),
        ("entities", rows.person, "type_key = 'Org'"),
        ("entities", rows.person, "recorded_at = now()"),
    ):
        with pytest.raises(ProgrammingError, match="permission denied"):
            async with tenant_session(app_engine, ctx(rows.ws)) as c:
                await c.execute(text(f"UPDATE {table} SET {column} WHERE id = :i"), {"i": ident})
    async with tenant_session(app_engine, ctx(rows.ws)) as c:
        await c.execute(
            text(
                "INSERT INTO facts (id, workspace_id, rel_type, source_entity_id, target_entity_id, authority_class, source_command_id, last_command_id, ontology_version_id, valid_from, valid_to) VALUES (:f, :w, 'WORKS_AT', :s, :t, 'admin_asserted', :c, :c, :v, '2020-01-01', '2021-01-01')"
            ),
            {"f": uuid.uuid4(), "w": rows.ws, "s": rows.person, "t": rows.org, "c": rows.command, "v": rows.version},
        )
    with pytest.raises(IntegrityError):
        async with tenant_session(app_engine, ctx(rows.ws)) as c:
            await c.execute(
                text(
                    "INSERT INTO facts (id, workspace_id, rel_type, source_entity_id, target_entity_id, authority_class, source_command_id, last_command_id, ontology_version_id, valid_from, valid_to) VALUES (:f, :w, 'WORKS_AT', :s, :t, 'admin_asserted', :c, :c, :v, '2022-01-01', '2021-01-01')"
                ),
                {"f": uuid.uuid4(), "w": rows.ws, "s": rows.person, "t": rows.org, "c": rows.command, "v": rows.version},
            )


async def test_external_ids_are_unique_per_workspace_and_type(app_engine):
    rows = await seed(app_engine)
    with pytest.raises(IntegrityError):
        async with tenant_session(app_engine, ctx(rows.ws)) as c:
            await c.execute(
                text(
                    "INSERT INTO entity_external_ids (workspace_id, entity_id, type_key, system, value) VALUES (:w, :e, 'Person', 'hr', 'E-1')"
                ),
                {"w": rows.ws, "e": rows.org},
            )
    other = await seed(app_engine)  # the same identifier in another workspace is a different identity
    assert other.ws != rows.ws


async def test_every_new_table_is_isolated_by_rls(app_engine):
    a = await seed(app_engine)
    b = await seed(app_engine)
    for table in P2_TABLES:
        async with tenant_session(app_engine, ctx(b.ws)) as c:
            mine = (await c.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one()  # noqa: S608 (constant table names)
            foreign = (await c.execute(text(f"SELECT count(*) FROM {table} WHERE workspace_id = :w"), {"w": a.ws})).scalar_one()  # noqa: S608
        assert mine >= 1 and foreign == 0, table
        async with system_session(app_engine) as c:
            assert (await c.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one() == 0, table  # no workspace set: fail closed  # noqa: S608
    async with tenant_session(app_engine, ctx(b.ws)) as c:  # writes into another workspace are refused (WITH CHECK), updates match nothing
        for stmt, params in (
            (
                "INSERT INTO idempotency_records (workspace_id, key, route, request_hash, status_code, response) VALUES (:w, 'x', 'r', 'h', 200, CAST('{}' AS jsonb))",
                {"w": a.ws},
            ),
        ):
            with pytest.raises(DBAPIError):
                async with c.begin_nested():
                    await c.execute(text(stmt), params)
        for table, col in (
            ("entities", "display_name = 'hijacked'"),
            ("facts", "status = 'deleted'"),
            ("review_items", "status = 'dismissed'"),
            ("workspace_ontology", "updated_at = now()"),
        ):
            res = await c.execute(text(f"UPDATE {table} SET {col} WHERE workspace_id = :w"), {"w": a.ws})  # noqa: S608
            assert res.rowcount == 0, table
    async with tenant_session(app_engine, ctx(a.ws)) as c:
        assert (await c.execute(text("SELECT display_name FROM entities WHERE id = :i"), {"i": a.person})).scalar_one() == "Sarah"


async def test_composite_foreign_keys_forbid_cross_workspace_links(app_engine):
    a = await seed(app_engine)
    b = await seed(app_engine)
    with pytest.raises(IntegrityError):  # B's fact pointing at A's entity: impossible at the schema level
        async with tenant_session(app_engine, ctx(b.ws)) as c:
            await c.execute(
                text(
                    "INSERT INTO facts (id, workspace_id, rel_type, source_entity_id, target_entity_id, authority_class, source_command_id, last_command_id, ontology_version_id) VALUES (:f, :w, 'WORKS_AT', :s, :t, 'admin_asserted', :c, :c, :v)"
                ),
                {"f": uuid.uuid4(), "w": b.ws, "s": a.person, "t": b.org, "c": b.command, "v": b.version},
            )


async def test_pooled_connection_reuse_never_leaks_into_the_new_tables(test_settings, migrated_db):
    from tests.conftest import APP_URL

    engine = create_app_engine(test_settings, url=APP_URL, pool_size=1, max_overflow=0)  # T-DB-02 extended: one physical connection
    try:
        a, b = await seed(engine), await seed(engine)
        for first, second in ((a, b), (b, a)):
            async with tenant_session(engine, ctx(first.ws)) as c:
                pid = (await c.execute(text("SELECT pg_backend_pid()"))).scalar_one()
                assert (await c.execute(text("SELECT count(*) FROM entities"))).scalar_one() == 2
            async with system_session(engine) as c:
                assert (await c.execute(text("SELECT pg_backend_pid()"))).scalar_one() == pid
                for table in P2_TABLES:
                    assert (await c.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one() == 0  # noqa: S608
            async with tenant_session(engine, ctx(second.ws)) as c:
                ids = {r[0] for r in (await c.execute(text("SELECT id FROM entities"))).all()}
                assert ids == {second.person, second.org}
    finally:
        await engine.dispose()


async def test_every_ops_function_is_hardened_and_the_sweeper_function_is_gone(app_engine):
    async with system_session(app_engine) as c:
        fn = (
            await c.execute(
                text("""SELECT p.proname, p.prosecdef, pg_get_userbyid(p.proowner) AS owner, p.proconfig, COALESCE(p.proacl::text, '') AS acl FROM pg_proc p
                        JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'ops'""")
            )
        ).all()
        # every ops.* function (P3's outbox functions; P2's sweeper discovery function was dropped by 0004) is SECURITY DEFINER, owned by kg_owner,
        # with a pinned search_path and no PUBLIC execute
        assert "workspaces_with_pending_projection" not in {f.proname for f in fn}
        assert {"claim_jobs", "complete_job"} <= {f.proname for f in fn}
        for f in fn:
            assert f.prosecdef and f.owner == "kg_owner" and any("search_path=pg_catalog, public" in s for s in f.proconfig), f.proname
            assert not re.search(r"(^|[{,])=X/", f.acl) and "kg_app=X/kg_owner" in f.acl, f.proname


async def test_trigram_search_index_exists_and_pg_trgm_works(app_engine):
    rows = await seed(app_engine)
    async with tenant_session(app_engine, ctx(rows.ws)) as c:
        hit = (
            await c.execute(
                text("SELECT display_name FROM entities WHERE workspace_id = :w AND lower(display_name) LIKE '%' || lower(:q) || '%'"),
                {"w": rows.ws, "q": "sar"},
            )
        ).scalar_one()
        assert hit == "Sarah"
        assert (await c.execute(text("SELECT count(*) FROM pg_indexes WHERE indexname = 'entities_name_trgm_idx'"))).scalar_one() == 1


def test_workspace_context_is_still_the_only_way_in() -> None:
    with pytest.raises(TypeError):
        WorkspaceContext()  # type: ignore[call-arg]
