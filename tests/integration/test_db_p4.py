"""P4 WP2: migration 0004. The cycle and its guard, the CHECK constraints that keep extracted rows honest, the partial unique index that stops duplicate reviews,
grants (what kg_app can and cannot touch), and the sweeper function being gone."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from kg_db import migrate
from kg_db.session import system_session, tenant_session
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError

from tests.conftest import SCRATCH_URL, require
from tests.support.p4 import CYCLIST, ctx_of, run_event, sarah_story, sql

pytestmark = pytest.mark.integration


async def test_up_down_up_with_the_extracted_data_guard():
    require(SCRATCH_URL, "postgres")
    from sqlalchemy.ext.asyncio import create_async_engine

    await asyncio.to_thread(
        lambda: migrate.downgrade(
            SCRATCH_URL,
            "base",
            force_drop_extracted=True,
            force_drop_memories=True,
            force_forget_privileges=True,
            force_drop_governance=True,
        )
    )
    await asyncio.to_thread(migrate.upgrade, SCRATCH_URL, "head")
    engine = create_async_engine(SCRATCH_URL)
    ws, ver, principal = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    interaction, entity = uuid.uuid4(), uuid.uuid4()
    try:
        async with engine.begin() as c:
            assert (
                await c.execute(
                    text(
                        "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'ops' AND p.proname = 'workspaces_with_pending_projection'"
                    )
                )
            ).scalar_one() == 0
            await c.execute(text("INSERT INTO workspaces (id, slug, display_name) VALUES (:w, :s, 's')"), {"w": ws, "s": f"m-{ws.hex[:6]}"})
            await c.execute(
                text("INSERT INTO principals (id, workspace_id, kind, handle, display_name) VALUES (:p, :w, 'agent', 'a', 'a')"),
                {"p": principal, "w": ws},
            )
            await c.execute(text("INSERT INTO ontology_versions (id, workspace_id) VALUES (:v, :w)"), {"v": ver, "w": ws})
            await c.execute(
                text(
                    "INSERT INTO interactions (id, workspace_id, source_system, source_ref, idempotency_key, request_hmac, kind, occurred_at, submitted_by, classification, "
                    "pipeline_version, byte_size) VALUES (:i, :w, 'chat', 'r', 'k', :h, 'message', now(), :p, 'internal', 'p4.v1', 1)"
                ),
                {"i": interaction, "w": ws, "p": principal, "h": "0" * 64},
            )
            await c.execute(
                text(
                    "INSERT INTO entities (id, workspace_id, type_key, display_name, origin, status, ontology_version_id) VALUES (:e, :w, 'Person', 'X', 'extracted', 'candidate', :v)"
                ),
                {"e": entity, "w": ws, "v": ver},
            )
        with pytest.raises(Exception, match="extracted"):  # the guard refuses to throw extracted data away silently
            await asyncio.to_thread(migrate.downgrade, SCRATCH_URL, "0003")
        async with engine.begin() as c:
            assert (
                await c.execute(text("SELECT count(*) FROM entities WHERE id = :e"), {"e": entity})
            ).scalar_one() == 1  # nothing was touched
        await asyncio.to_thread(
            lambda: migrate.downgrade(
                SCRATCH_URL,
                "0003",
                force_drop_extracted=True,
                force_drop_memories=True,
                force_forget_privileges=True,
                force_drop_governance=True,
            )
        )
        async with engine.begin() as c:
            assert (await c.execute(text("SELECT count(*) FROM entities WHERE id = :e"), {"e": entity})).scalar_one() == 0
            assert (
                await c.execute(
                    text(
                        "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'ops' AND p.proname = 'workspaces_with_pending_projection'"
                    )
                )
            ).scalar_one() == 1
        await asyncio.to_thread(migrate.upgrade, SCRATCH_URL, "head")
    finally:
        await engine.dispose()
        await asyncio.to_thread(
            lambda: migrate.downgrade(
                SCRATCH_URL,
                "base",
                force_drop_extracted=True,
                force_drop_memories=True,
                force_forget_privileges=True,
                force_drop_governance=True,
            )
        )


async def test_constraints_keep_extracted_rows_honest(api, tenant, strict_mock):
    t = tenant
    ids = await sarah_story(t)
    await run_event(api, t, CYCLIST)
    (fork,) = await sql(api, t, "SELECT id FROM entities WHERE workspace_id = :ws AND status = 'candidate'")
    (ver,) = await sql(api, t, "SELECT ontology_version_id AS v FROM entities WHERE workspace_id = :ws LIMIT 1")
    (inter,) = await sql(api, t, "SELECT id FROM interactions WHERE workspace_id = :ws")

    async def refused(statement: str, **params) -> None:  # type: ignore[no-untyped-def]
        async with tenant_session(api.engine, ctx_of(t)) as c:
            with pytest.raises((IntegrityError, DBAPIError, ProgrammingError)):
                await c.execute(text(statement), {"ws": t.ws, **params})

    base = {"s": uuid.UUID(ids["sarah"]), "t": uuid.UUID(ids["acme"]), "v": ver.v}
    # an extracted fact must name its interaction and carry its text; a command fact must name its command
    await refused(
        "INSERT INTO facts (id, workspace_id, rel_type, source_entity_id, target_entity_id, authority_class, source_type, ontology_version_id) "
        "VALUES (gen_random_uuid(), :ws, 'KNOWS', :s, :t, 'conversation_report', 'extracted', :v)",
        **base,
    )
    await refused(
        "INSERT INTO facts (id, workspace_id, rel_type, source_entity_id, target_entity_id, authority_class, source_type, ontology_version_id) "
        "VALUES (gen_random_uuid(), :ws, 'KNOWS', :s, :t, 'admin_asserted', 'command', :v)",
        **base,
    )
    await refused(  # an authority outside the five classes
        "INSERT INTO facts (id, workspace_id, rel_type, source_entity_id, target_entity_id, authority_class, source_type, fact_text, source_interaction_id, ontology_version_id) "
        "VALUES (gen_random_uuid(), :ws, 'KNOWS', :s, :t, 'god_mode', 'extracted', 'x', :i, :v)",
        i=inter.id,
        **base,
    )
    # a merged entity names the entity it merged into, and only a merged one does
    await refused("UPDATE entities SET status = 'merged' WHERE workspace_id = :ws AND id = :e", e=fork.id)
    await refused("UPDATE entities SET merged_into_id = :m WHERE workspace_id = :ws AND id = :e", e=fork.id, m=uuid.UUID(ids["sarah"]))
    # a non-match pair is stored ordered, once
    a, b = sorted((uuid.UUID(ids["sarah"]), uuid.UUID(ids["acme"])), key=str)
    await refused("INSERT INTO identity_non_matches (entity_a, entity_b, workspace_id, reason_code) VALUES (:a, :b, :ws, 'x')", a=b, b=a)
    # one open identity item per candidate
    await refused(
        "INSERT INTO review_items (id, workspace_id, item_type, rule_kind, reason_code, subject_entity_id, candidate_entity_id) "
        "VALUES (gen_random_uuid(), :ws, 'identity_match', 'x', 'x', :s, :c)",
        s=uuid.UUID(ids["sarah"]),
        c=fork.id,
    )
    # evidence is unique per (fact, interaction)
    (ev,) = await sql(
        api, t, "SELECT fact_id, interaction_id, episode_uuid FROM fact_evidence WHERE workspace_id = :ws AND source_kind = 'episode'"
    )
    await refused(
        "INSERT INTO fact_evidence (id, fact_id, workspace_id, source_kind, interaction_id, episode_uuid) VALUES (gen_random_uuid(), :f, :ws, 'episode', :i, 'again')",
        f=ev.fact_id,
        i=ev.interaction_id,
    )
    # a review item of an identity type must name both entities; a contradiction must name a fact
    await refused(
        "INSERT INTO review_items (id, workspace_id, item_type, rule_kind, reason_code) VALUES (gen_random_uuid(), :ws, 'identity_match', 'x', 'x')"
    )
    await refused(
        "INSERT INTO review_items (id, workspace_id, item_type, rule_kind, reason_code) VALUES (gen_random_uuid(), :ws, 'contradiction', 'x', 'x')"
    )


async def test_the_application_role_cannot_delete_or_rewrite_what_it_must_not(api, tenant, strict_mock):
    t = tenant
    await sarah_story(t)
    await run_event(api, t, CYCLIST)

    async def denied(statement: str) -> None:
        """Refused, or (a DELETE behind the P8 purge gate on a table that has no row here) a no-op: nothing ever goes."""
        async with tenant_session(api.engine, ctx_of(t)) as c:
            try:
                res = await c.execute(text(statement), {"ws": t.ws})
            except (ProgrammingError, DBAPIError):
                return
            assert statement.startswith("DELETE") and res.rowcount == 0, statement

    for table in (
        "fact_evidence",
        "entity_mentions",
        "identity_non_matches",
        "attribute_claims",
        "graph_episodes",
        "review_items",
        "facts",
        "entities",
        "commands",
    ):
        await denied(f"DELETE FROM {table} WHERE workspace_id = :ws")  # noqa: S608 (constant names)
    await denied("UPDATE fact_evidence SET episode_uuid = 'x' WHERE workspace_id = :ws")  # evidence is append-only
    await denied("UPDATE commands SET result = '{}'::jsonb WHERE workspace_id = :ws")  # the ledger is immutable
    await denied(
        "UPDATE facts SET fact_text = 'rewritten', source_interaction_id = NULL WHERE workspace_id = :ws"
    )  # provenance is not rewritable
    await denied("UPDATE entities SET origin = 'command' WHERE workspace_id = :ws")
    await denied("UPDATE graph_episodes SET episode_uuid = 'x' WHERE workspace_id = :ws")


async def test_the_review_gauges_function_returns_aggregates_without_ids(api, tenant, strict_mock):
    t = tenant
    await sarah_story(t)
    await run_event(api, t, CYCLIST)
    async with system_session(api.engine) as c:
        res = await c.execute(text("SELECT * FROM ops.review_stats()"))
        assert list(res.keys()) == ["item_type", "open_items", "oldest_age_seconds"]
        rows = res.all()
    assert any(r.item_type == "identity_match" and r.open_items >= 1 for r in rows)
