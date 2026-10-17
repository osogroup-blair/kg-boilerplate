"""P8 WP1: migration 0008 and the purge gate (release blockers).

* every immutable table refuses DELETE and redaction without a purging deletion request;
* a purging request deletes only the rows listed for it, only in its own workspace, and only with the matching action;
* `audit_log` refuses always, whatever the request;
* `platform_deletion_receipts` is invisible to the application role except through `ops`;
* RLS (and a pooled connection reused across tenants), the lifecycle trigger, up/down/up with guards."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from kg_db import migrate
from kg_db.session import system_session, tenant_session
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError

from tests.conftest import SCRATCH_URL, require
from tests.support.p4 import ctx_of, sarah_story, sql

pytestmark = pytest.mark.integration

NEW_TABLES = [
    "deletion_requests",
    "deletion_items",
    "deletion_manifests",
    "deletion_tombstones",
    "deletion_suppressions",
    "legal_holds",
    "corrections",
    "attribute_corrections",
    "entity_lineage",
    "consolidation_runs",
    "ontology_simulations",
    "workspace_settings",
]
DENIED = (IntegrityError, DBAPIError, ProgrammingError)


async def run(api, t, statement: str, **params) -> None:
    async with tenant_session(api.engine, ctx_of(t)) as c:
        await c.execute(text(statement), {"ws": t.ws, **params})


async def new_request(
    api, t, kind: str, target: uuid.UUID | None, items: list[tuple[str, str, str, str]], *, purging: bool = True
) -> uuid.UUID:
    """A deletion request walked through the lifecycle the way the service does: frozen -> items -> planned -> purging. `items` = (store, kind, id, action)."""
    rid = uuid.uuid4()
    async with tenant_session(api.engine, ctx_of(t)) as c:
        await c.execute(
            text(
                "INSERT INTO deletion_requests (id, workspace_id, kind, target_id, reason_code, status) VALUES (:id, :ws, :k, :tg, 'test', 'frozen')"
            ),
            {"id": rid, "ws": t.ws, "k": kind, "tg": target},
        )
        for store, ikind, iid, action in items:
            await c.execute(
                text(
                    "INSERT INTO deletion_items (request_id, workspace_id, store, item_kind, item_id, action) VALUES (:r, :ws, :s, :k, :i, :a)"
                ),
                {"r": rid, "ws": t.ws, "s": store, "k": ikind, "i": iid, "a": action},
            )
        await c.execute(text("UPDATE deletion_requests SET status = 'planned' WHERE id = :r"), {"r": rid})
        if purging:
            await c.execute(text("UPDATE deletion_requests SET status = 'purging' WHERE id = :r"), {"r": rid})
    return rid


async def purge_as(api, t, rid: uuid.UUID | None, statement: str, **params):
    async with tenant_session(api.engine, ctx_of(t)) as c:
        if rid is not None:
            await c.execute(text("SELECT set_config('app.purge_request_id', :r, true)"), {"r": str(rid)})
        return await c.execute(text(statement), {"ws": t.ws, **params})


async def test_up_down_up_and_the_guards():
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
    await asyncio.to_thread(migrate.upgrade, SCRATCH_URL, "0007")
    engine = create_async_engine(SCRATCH_URL)
    ws, cmd, ent_a, ent_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    try:
        async with engine.begin() as c:  # state as 0007 left it: a merged pair (the lineage backfill reads it)
            await c.execute(
                text("INSERT INTO workspaces (id, slug, display_name) VALUES (:w, :s, 's')"), {"w": ws, "s": f"p8-{ws.hex[:6]}"}
            )
            await c.execute(
                text("INSERT INTO principals (id, workspace_id, kind, handle, display_name) VALUES (:p, :w, 'user', 'admin', 'admin')"),
                {"p": cmd, "w": ws},
            )
            ver = uuid.uuid4()
            await c.execute(text("INSERT INTO ontology_versions (id, workspace_id) VALUES (:v, :w)"), {"v": ver, "w": ws})
            await c.execute(
                text(
                    "INSERT INTO commands (id, workspace_id, idempotency_key, request_hash, command_type, actor_principal_id, authority_class, classification, "
                    "ontology_version_id, payload, status, http_status) VALUES (:c, :w, 'k', 'h', 'entity.create', :p, 'admin_asserted', 'internal', :v, '{}', 'applied', 201)"
                ),
                {"c": cmd, "w": ws, "p": cmd, "v": ver},
            )
            for e in (ent_a, ent_b):
                await c.execute(
                    text(
                        "INSERT INTO entities (id, workspace_id, type_key, display_name, ontology_version_id, created_by_command_id, last_command_id) "
                        "VALUES (:e, :w, 'Person', 'x', :v, :c, :c)"
                    ),
                    {"e": e, "w": ws, "v": ver, "c": cmd},
                )
            await c.execute(
                text("UPDATE entities SET status = 'merged', merged_into_id = :b, merged_at = now() WHERE id = :a"),
                {"a": ent_a, "b": ent_b},
            )
        await asyncio.to_thread(migrate.upgrade, SCRATCH_URL, "head")
        async with engine.begin() as c:
            lineage = (await c.execute(text("SELECT entity_id, related_id, relation FROM entity_lineage"))).all()
            assert [(r.entity_id, r.related_id, r.relation) for r in lineage] == [
                (ent_a, ent_b, "merged_into")
            ]  # backfilled from the P4 merge
            rid = uuid.uuid4()
            await c.execute(
                text(
                    "INSERT INTO deletion_requests (id, workspace_id, kind, target_id, reason_code, status) VALUES (:r, :w, 'subject', :t, 'x', 'frozen')"
                ),
                {"r": rid, "w": ws, "t": ent_b},
            )
        with pytest.raises(Exception, match="not finished"):  # an unfinished request: its frozen items would be orphaned
            await asyncio.to_thread(migrate.downgrade, SCRATCH_URL, "0007")
        async with engine.begin() as c:
            await c.execute(text("UPDATE deletion_requests SET status = 'cancelled'"))
            await c.execute(
                text(
                    "INSERT INTO commands (id, workspace_id, idempotency_key, request_hash, command_type, actor_principal_id, authority_class, classification, "
                    "ontology_version_id, payload, status, http_status) SELECT gen_random_uuid(), :w, 'k2', 'h', 'correction.fact_reject', :p, 'admin_asserted', "
                    "'internal', ontology_version_id, '{}', 'applied', 200 FROM commands LIMIT 1"
                ),
                {"w": ws, "p": cmd},
            )
        with pytest.raises(Exception, match="correction"):  # a correction-made row cannot be named by the old schema
            await asyncio.to_thread(migrate.downgrade, SCRATCH_URL, "0007")
        await asyncio.to_thread(lambda: migrate.downgrade(SCRATCH_URL, "0007", force_drop_governance=True))
        async with engine.begin() as c:
            assert (await c.execute(text("SELECT to_regclass('deletion_requests') IS NULL"))).scalar_one() is True
            assert (
                await c.execute(text("SELECT count(*) FROM commands WHERE command_type LIKE 'correction.%'"))
            ).scalar_one() == 0  # the governance command is gone; the rows it touched are not
            assert (await c.execute(text("SELECT count(*) FROM entities"))).scalar_one() == 2  # evidence is never dropped by a downgrade
        await asyncio.to_thread(migrate.upgrade, SCRATCH_URL, "head")
    finally:
        await engine.dispose()
        await asyncio.to_thread(drop)


async def test_the_new_tables_have_forced_rls_and_a_tenant_policy(app_engine):
    async with system_session(app_engine) as c:
        rows = (
            await c.execute(
                text(
                    "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) AS policies "
                    "FROM pg_class c WHERE c.relname = ANY(:t)"
                ),
                {"t": NEW_TABLES},
            )
        ).all()
    assert {r.relname for r in rows} == set(NEW_TABLES)
    assert all(r.relrowsecurity and r.relforcerowsecurity and r.policies >= 1 for r in rows)


async def test_every_row_level_table_is_either_gated_or_deliberately_not(app_engine):
    """A table added later without a decision fails here: either it has the purge trigger (or one of the three guards that ask the gate) or it is on the list of
    tables whose rows are derived, replaceable or removed by their own owner."""
    async with system_session(app_engine) as c:
        tables = {
            r[0]
            for r in (
                await c.execute(
                    text(
                        "SELECT relname FROM pg_class WHERE relnamespace = 'public'::regnamespace AND relkind = 'r' AND relrowsecurity ORDER BY 1"
                    )
                )
            ).all()
        }
        gated = {
            r[0]
            for r in (
                await c.execute(
                    text(
                        "SELECT tgrelid::regclass::text FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid "
                        "WHERE NOT tgisinternal AND p.proname IN ('purge_guard','interactions_guard','job_attempts_guard','ontology_versions_guard','ontology_activations_immutable')"
                    )
                )
            ).all()
        }
    not_gated = {
        # P1: identity and access (a revocation, not a deletion; removed with the workspace)
        "workspaces", "principals", "role_assignments", "principal_scopes", "api_keys", "agent_usage_daily",
        # append-only by a trigger of its own, never deleted
        "audit_log",
        # operational records with their own lifecycle
        "model_calls", "workspace_ontology", "idempotency_records",
        # derived: re-embedded and replaced by their jobs
        "memory_embeddings",
        # P8: the deletion machinery itself (a request is a record; items and manifests are removed by the service; the guards above protect their state)
        *NEW_TABLES,
        # P9: configuration and run records with their own lifecycle (the OIDC mapping and identities, the active graph namespace, reindex and reconcile runs); removed with the workspace
        "oidc_group_mappings", "oidc_identities", "workspace_graph_namespaces", "reindex_runs", "reconcile_runs",
    }  # fmt: skip
    assert tables - gated - not_gated == set(), "an immutable table has no purge guard"


@pytest.mark.parametrize(
    ("table", "where"),
    [
        ("entities", ""),
        ("facts", ""),
        ("commands", ""),
        ("fact_evidence", ""),
        ("outbox_jobs", ""),
        ("interactions", ""),
        ("audit_log", ""),
    ],
)
async def test_delete_without_a_plan_is_refused(api, tenant, table, where):
    await sarah_story(tenant)
    await tenant.req(
        "POST",
        "/v1/interactions",
        "app",
        json={"source_system": "chat", "source_ref": "m1", "kind": "message", "content": "hello", "occurred_at": "2026-10-10T09:00:00Z"},
    )
    present = (await sql(api, tenant, f"SELECT count(*) AS n FROM {table} WHERE workspace_id = :ws"))[0].n  # noqa: S608
    assert present > 0, f"the fixture left {table} empty"
    with pytest.raises(DENIED):
        await run(api, tenant, f"DELETE FROM {table} WHERE workspace_id = :ws")  # noqa: S608
    assert (await sql(api, tenant, f"SELECT count(*) AS n FROM {table} WHERE workspace_id = :ws"))[0].n == present  # noqa: S608


async def test_a_purging_request_deletes_exactly_what_it_lists(api, tenant):
    ids = await sarah_story(tenant)
    topic = uuid.UUID(ids["topic"])
    other = uuid.UUID(ids["acme"])
    rid = await new_request(api, tenant, "subject", topic, [("postgres", "entities", str(topic), "delete")])
    with pytest.raises(DENIED):  # an unlisted row of the same table
        await purge_as(api, tenant, rid, "DELETE FROM entities WHERE workspace_id = :ws AND id = :id", id=other)
    with pytest.raises(DENIED):  # the listed row with the wrong action (a redaction was not planned, a deletion was)
        await purge_as(api, tenant, rid, "DELETE FROM fact_evidence WHERE workspace_id = :ws")
    res = await purge_as(api, tenant, rid, "DELETE FROM entities WHERE workspace_id = :ws AND id = :id", id=topic)
    assert res.rowcount == 1
    assert (await sql(api, tenant, "SELECT count(*) AS n FROM entities WHERE workspace_id = :ws AND id = :id", id=other))[0].n == 1


async def test_the_action_must_match(api, tenant):
    ids = await sarah_story(tenant)
    topic = uuid.UUID(ids["topic"])
    rid = await new_request(
        api, tenant, "subject", topic, [("postgres", "entities", str(topic), "redact")]
    )  # planned as a redaction, not a deletion
    with pytest.raises(DENIED):
        await purge_as(api, tenant, rid, "DELETE FROM entities WHERE workspace_id = :ws AND id = :id", id=topic)


async def test_a_request_that_is_not_purging_unlocks_nothing(api, tenant):
    ids = await sarah_story(tenant)
    topic = uuid.UUID(ids["topic"])
    rid = await new_request(api, tenant, "subject", topic, [("postgres", "entities", str(topic), "delete")], purging=False)
    with pytest.raises(DENIED):
        await purge_as(api, tenant, rid, "DELETE FROM entities WHERE workspace_id = :ws AND id = :id", id=topic)
    with pytest.raises(DENIED):  # and a request id that does not exist
        await purge_as(api, tenant, uuid.uuid4(), "DELETE FROM entities WHERE workspace_id = :ws AND id = :id", id=topic)
    with pytest.raises(DENIED):  # nor does a setting that is not a uuid
        async with tenant_session(api.engine, ctx_of(tenant)) as c:
            await c.execute(text("SELECT set_config('app.purge_request_id', 'not-a-uuid', true)"))
            await c.execute(text("DELETE FROM entities WHERE workspace_id = :ws AND id = :id"), {"ws": tenant.ws, "id": topic})


async def test_another_workspaces_request_cannot_unlock_this_workspaces_rows(api, tenant, bare_tenant):
    ids = await sarah_story(tenant)
    topic = uuid.UUID(ids["topic"])
    # B's request is purging and lists the very id of A's entity; the gate compares the WORKSPACE of the row with the workspace of the request
    rid = await new_request(api, bare_tenant, "subject", topic, [("postgres", "entities", str(topic), "delete")])
    async with tenant_session(api.engine, ctx_of(tenant)) as c:
        await c.execute(text("SELECT set_config('app.purge_request_id', :r, true)"), {"r": str(rid)})
        with pytest.raises(DENIED):
            await c.execute(text("DELETE FROM entities WHERE workspace_id = :ws AND id = :id"), {"ws": tenant.ws, "id": topic})


async def test_audit_log_refuses_always(api, tenant):
    await sarah_story(tenant)
    rid = await new_request(api, tenant, "workspace", None, [])  # even a workspace deletion cannot rewrite the audit trail
    with pytest.raises(DENIED):
        await purge_as(api, tenant, rid, "DELETE FROM audit_log WHERE workspace_id = :ws")
    with pytest.raises(DENIED):
        await purge_as(api, tenant, rid, "UPDATE audit_log SET action = 'x' WHERE workspace_id = :ws")


async def test_redaction_is_gated_and_leaves_the_row(api, tenant):
    ids = await sarah_story(tenant)
    cmd = (await sql(api, tenant, "SELECT id FROM commands WHERE workspace_id = :ws ORDER BY created_at LIMIT 1"))[0].id
    redacted = json.dumps({"redacted": True, "hash": "0" * 64})
    with pytest.raises(DENIED):  # not without a plan
        await run(api, tenant, "UPDATE commands SET payload = CAST(:p AS jsonb) WHERE workspace_id = :ws AND id = :id", p=redacted, id=cmd)
    rid = await new_request(api, tenant, "subject", uuid.UUID(ids["sarah"]), [("postgres", "commands", str(cmd), "redact")])
    with pytest.raises(DENIED):  # a plan for a redaction is not a licence to write something else into the payload
        await purge_as(
            api, tenant, rid, 'UPDATE commands SET payload = CAST(\'{"name": "x"}\' AS jsonb) WHERE workspace_id = :ws AND id = :id', id=cmd
        )
    await purge_as(
        api, tenant, rid, "UPDATE commands SET payload = CAST(:p AS jsonb) WHERE workspace_id = :ws AND id = :id", p=redacted, id=cmd
    )
    row = (await sql(api, tenant, "SELECT payload FROM commands WHERE workspace_id = :ws AND id = :id", id=cmd))[0]
    assert row.payload == {"redacted": True, "hash": "0" * 64}
    with pytest.raises(
        DENIED
    ):  # the source reference (it can be the subject's own id in the source system) is only ever replaced by the marker
        await run(api, tenant, "UPDATE commands SET source_ref = '[redacted]' WHERE workspace_id = :ws AND id = :id", id=cmd)
    with pytest.raises(DENIED):
        await purge_as(api, tenant, rid, "UPDATE commands SET source_ref = 'anything else' WHERE workspace_id = :ws AND id = :id", id=cmd)
    await purge_as(api, tenant, rid, "UPDATE commands SET source_ref = '[redacted]' WHERE workspace_id = :ws AND id = :id", id=cmd)
    with pytest.raises(DENIED):  # and the ledger row itself is never deleted for a subject
        await purge_as(api, tenant, rid, "DELETE FROM commands WHERE workspace_id = :ws AND id = :id", id=cmd)


async def test_a_workspace_request_opens_only_its_own_workspace(api, tenant, bare_tenant):
    ids = await sarah_story(tenant)
    rid = await new_request(api, bare_tenant, "workspace", None, [])
    async with tenant_session(api.engine, ctx_of(tenant)) as c:  # purging, kind workspace, but for another tenant
        await c.execute(text("SELECT set_config('app.purge_request_id', :r, true)"), {"r": str(rid)})
        with pytest.raises(DENIED):
            await c.execute(text("DELETE FROM facts WHERE workspace_id = :ws"), {"ws": tenant.ws})
    assert (await sql(api, tenant, "SELECT count(*) AS n FROM entities WHERE workspace_id = :ws AND id = :id", id=uuid.UUID(ids["sarah"])))[
        0
    ].n == 1


async def test_the_lifecycle_moves_forward_only(api, tenant):
    rid = await new_request(api, tenant, "subject", uuid.uuid4(), [], purging=False)
    with pytest.raises(DENIED):  # frozen -> completed skips the purge
        await run(api, tenant, "UPDATE deletion_requests SET status = 'completed' WHERE id = :r", r=rid)
    with pytest.raises(DENIED):  # the target never changes
        await run(api, tenant, "UPDATE deletion_requests SET target_id = gen_random_uuid() WHERE id = :r", r=rid)
    with pytest.raises(DENIED):  # a request is a record
        await run(api, tenant, "DELETE FROM deletion_requests WHERE id = :r", r=rid)
    with pytest.raises(DENIED):  # the plan is fixed before the purge starts
        purging = await new_request(api, tenant, "subject", uuid.uuid4(), [])
        await run(
            api,
            tenant,
            "INSERT INTO deletion_items (request_id, workspace_id, store, item_kind, item_id, action) VALUES (:r, :ws, 'postgres', 'entities', 'x', 'delete')",
            r=purging,
        )


async def test_tombstones_and_suppressions_are_append_only(api, tenant):
    rid = await new_request(api, tenant, "subject", uuid.uuid4(), [], purging=False)
    e = uuid.uuid4()
    await run(
        api, tenant, "INSERT INTO deletion_tombstones (workspace_id, kind, id, request_id) VALUES (:ws, 'entity', :e, :r)", e=e, r=rid
    )
    with pytest.raises(DENIED):
        await run(api, tenant, "DELETE FROM deletion_tombstones WHERE workspace_id = :ws")
    with pytest.raises(DENIED):
        await run(api, tenant, "UPDATE deletion_tombstones SET kind = 'fact' WHERE workspace_id = :ws")
    await run(
        api,
        tenant,
        "INSERT INTO deletion_suppressions (workspace_id, kind, value_hmac, request_id) VALUES (:ws, 'external_id', :h, :r)",
        h="a" * 64,
        r=rid,
    )
    await run(
        api, tenant, "UPDATE deletion_suppressions SET lifted_at = now(), lifted_by = gen_random_uuid() WHERE workspace_id = :ws"
    )  # lifting is the one change
    with pytest.raises(DENIED):
        await run(api, tenant, "UPDATE deletion_suppressions SET value_hmac = :h WHERE workspace_id = :ws", h="b" * 64)


async def test_the_platform_receipt_is_reachable_only_through_ops(api, tenant, bare_tenant):
    rid = uuid.uuid4()
    receipt = {"counts": {"entities": 3}}
    async with tenant_session(api.engine, ctx_of(tenant)) as c:
        with pytest.raises(DENIED):
            await c.execute(text("SELECT * FROM platform_deletion_receipts"))
    async with tenant_session(api.engine, ctx_of(tenant)) as c:
        await c.execute(
            text("SELECT ops.write_platform_receipt(:r, :ws, 'workspace', CAST(:j AS jsonb))"),
            {"r": rid, "ws": tenant.ws, "j": json.dumps(receipt)},
        )
    async with tenant_session(api.engine, ctx_of(tenant)) as c:
        assert (await c.execute(text("SELECT ops.read_platform_receipt(:r, :ws)"), {"r": rid, "ws": tenant.ws})).scalar_one() == receipt
    async with tenant_session(api.engine, ctx_of(bare_tenant)) as c:  # the request id of another workspace answers nothing
        assert (await c.execute(text("SELECT ops.read_platform_receipt(:r, :ws)"), {"r": rid, "ws": bare_tenant.ws})).scalar_one() is None


async def test_a_tenant_sees_only_its_own_governance_rows_and_a_reused_connection_forgets_the_last_tenant(api, tenant, bare_tenant):
    a, b = tenant, bare_tenant
    for t in (a, b):
        await new_request(api, t, "subject", uuid.uuid4(), [], purging=False)
        await run(api, t, "INSERT INTO workspace_settings (workspace_id, version, settings) VALUES (:ws, 1, '{}')")
    for _ in range(4):  # a pool of three connections, alternating tenants: no row crosses
        for t, other in ((a, b), (b, a)):
            for table in ("deletion_requests", "workspace_settings"):
                seen = await sql(api, t, f"SELECT workspace_id FROM {table}")  # NO workspace predicate on purpose: RLS alone  # noqa: S608
                assert {r.workspace_id for r in seen} == {t.ws}
                assert (await sql(api, t, f"SELECT count(*) AS n FROM {table} WHERE workspace_id = :o", o=other.ws))[0].n == 0  # noqa: S608
    async with system_session(api.engine) as c:  # no workspace set: nothing at all (fail closed)
        assert (await c.execute(text("SELECT count(*) FROM deletion_requests"))).scalar_one() == 0
    async with tenant_session(api.engine, ctx_of(a)) as c:  # and a row cannot be written into another tenant
        with pytest.raises(DENIED):
            await c.execute(text("INSERT INTO workspace_settings (workspace_id, version, settings) VALUES (:o, 1, '{}')"), {"o": b.ws})


async def test_workspace_purge_rows_needs_a_purging_workspace_request(api, tenant):
    ids = await sarah_story(tenant)
    with pytest.raises(DENIED):  # a subject request is not a workspace deletion
        rid = await new_request(api, tenant, "subject", uuid.UUID(ids["topic"]), [("postgres", "entities", ids["topic"], "delete")])
        await purge_as(api, tenant, None, "SELECT ops.purge_workspace_rows(:r)", r=rid)
    with pytest.raises(DENIED):  # nor is an unknown id
        await purge_as(api, tenant, None, "SELECT ops.purge_workspace_rows(:r)", r=uuid.uuid4())
    assert (await sql(api, tenant, "SELECT count(*) AS n FROM entities WHERE workspace_id = :ws"))[0].n >= 4


async def test_retention_candidates_and_egress_denials_name_only_counts_and_classes(api, tenant):
    await run(
        api,
        tenant,
        "INSERT INTO workspace_settings (workspace_id, version, settings) VALUES (:ws, 1, CAST(:s AS jsonb))",
        s=json.dumps({"retention": {"model_calls_days": 0}, "egress": {"denied_classifications": ["restricted"]}}),
    )
    async with tenant_session(api.engine, ctx_of(tenant)) as c:
        denials = (await c.execute(text("SELECT ops.workspace_egress_denials(:ws)"), {"ws": tenant.ws})).scalar_one()
        cols = (await c.execute(text("SELECT * FROM ops.retention_candidates() LIMIT 0"))).keys()
    assert list(denials) == ["restricted"]
    assert set(cols) == {"workspace_id", "retention_class", "n"}  # no row ids, no content
