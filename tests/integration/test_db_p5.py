"""P5 WP1: migration 0005. The cycle and its guard, RLS on every memory table (and a pooled connection reused across tenants), column grants (content is immutable),
the generated tsvector, the vector dimension CHECK, the dedupe index, the shape CHECKs, and the plan of the scoped search (the scope predicate is IN the scan)."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from kg_db import migrate
from kg_db.session import system_session, tenant_session
from kg_domain.enums import PrincipalKind, Role
from kg_domain.principal import Principal
from kg_memory.models import SearchIn
from kg_memory.reads import Viewer
from kg_memory.scopes import ScopeGrants
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError, ProgrammingError

from tests.conftest import SCRATCH_URL, require
from tests.support.p4 import ctx_of, sarah_story, sql
from tests.support.p5 import accept_event, body, memory_rows, run_embeds, save, sub

pytestmark = pytest.mark.integration
TABLES = ["curated_memories", "memory_scopes", "memory_subjects", "memory_evidence", "memory_lineage", "memory_embeddings"]


async def test_up_down_up_with_the_memory_guard():
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
    ws, ver, principal, cmd, mem = (uuid.uuid4() for _ in range(5))
    try:
        async with engine.begin() as c:
            await c.execute(text("INSERT INTO workspaces (id, slug, display_name) VALUES (:w, :s, 's')"), {"w": ws, "s": f"m-{ws.hex[:6]}"})
            await c.execute(
                text("INSERT INTO principals (id, workspace_id, kind, handle, display_name) VALUES (:p, :w, 'agent', 'a', 'a')"),
                {"p": principal, "w": ws},
            )
            await c.execute(
                text(
                    "INSERT INTO commands (id, workspace_id, idempotency_key, request_hash, command_type, actor_principal_id, authority_class, classification, payload, status, http_status) "
                    "VALUES (:c, :w, 'k', 'h', 'memory.save', :p, 'agent_generated', 'internal', '{}', 'applied', 201)"
                ),
                {"c": cmd, "w": ws, "p": principal},
            )
            await c.execute(
                text(
                    "INSERT INTO curated_memories (id, workspace_id, type, origin, content, summary, status, authority_class, evidence_status, classification, created_by, command_id, "
                    "pipeline_version, candidate_hmac) VALUES (:m, :w, 'semantic', 'explicit', 'c', 's', 'active', 'user_asserted', 'verified', 'internal', :p, :c, 'p5.v1', :h)"
                ),
                {"m": mem, "w": ws, "p": principal, "c": cmd, "h": "a" * 64},
            )
        with pytest.raises(Exception, match="curated memories"):  # the guard refuses to throw memories away silently
            await asyncio.to_thread(migrate.downgrade, SCRATCH_URL, "0004")
        async with engine.begin() as c:
            assert (await c.execute(text("SELECT count(*) FROM curated_memories"))).scalar_one() == 1
        await asyncio.to_thread(
            lambda: migrate.downgrade(
                SCRATCH_URL, "0004", force_drop_memories=True, force_forget_privileges=True, force_drop_governance=True
            )
        )
        async with engine.begin() as c:
            assert (await c.execute(text("SELECT to_regclass('curated_memories') IS NULL"))).scalar_one() is True
            assert (await c.execute(text("SELECT count(*) FROM commands WHERE command_type LIKE 'memory.%'"))).scalar_one() == 0
            assert (
                await c.execute(
                    text(
                        "SELECT is_nullable FROM information_schema.columns WHERE table_name = 'commands' AND column_name = 'ontology_version_id'"
                    )
                )
            ).scalar_one() == "NO"
        await asyncio.to_thread(migrate.upgrade, SCRATCH_URL, "head")
        _ = ver
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


async def test_every_memory_table_has_forced_rls_and_a_tenant_policy(app_engine):
    async with system_session(app_engine) as c:
        rows = (
            await c.execute(
                text(
                    "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity, (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) AS policies FROM pg_class c WHERE c.relname = ANY(:t)"
                ),
                {"t": TABLES},
            )
        ).all()
    assert {r.relname for r in rows} == set(TABLES) and all(r.relrowsecurity and r.relforcerowsecurity and r.policies >= 1 for r in rows)


async def test_a_tenant_sees_only_its_own_memory_and_a_reused_pooled_connection_forgets_the_last_tenant(api, tenant_mem, bare_tenant):
    a, b = tenant_mem, bare_tenant
    await sarah_story(a)
    for t in (a, b):
        assert (
            await save(t, "admin", body(content=f"fact of {t.slug}", scopes=[{"kind": "workspace"}], source_ref="x", purpose="p"))
        ).status_code == 201
    for _ in range(4):  # a pool of three connections, alternating tenants: no row crosses
        for t, other in ((a, b), (b, a)):
            seen = await sql(api, t, "SELECT content FROM curated_memories")  # NO workspace predicate on purpose: RLS alone
            assert [r.content for r in seen] == [f"fact of {t.slug}"]
            for table in TABLES:
                n = await sql(api, t, f"SELECT count(*) AS n FROM {table} WHERE workspace_id = :other", other=other.ws)  # noqa: S608
                assert n[0].n == 0, table
    async with system_session(api.engine) as c:  # no workspace set: nothing at all (fail closed)
        for table in TABLES:
            assert (await c.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one() == 0, table  # noqa: S608
    async with tenant_session(api.engine, ctx_of(a)) as c:  # and a row cannot be written into another tenant
        with pytest.raises((IntegrityError, DBAPIError, ProgrammingError)):
            await c.execute(
                text(
                    "INSERT INTO memory_scopes (memory_id, workspace_id, scope_kind, scope_ref) VALUES (gen_random_uuid(), :o, 'workspace', '*')"
                ),
                {"o": b.ws},
            )


async def test_content_is_immutable_for_the_application_role_and_only_lifecycle_columns_move(api, tenant_mem):
    t = tenant_mem
    ids = await sarah_story(t)
    mid = (await save(t, "admin", body(subject_ids=[ids["sarah"]], source_ref="x", purpose="p"))).json()["memory_id"]
    m = uuid.UUID(mid)

    async def attempt(statement: str) -> bool:
        async with tenant_session(api.engine, ctx_of(t)) as c:
            try:
                await c.execute(text(statement), {"ws": t.ws, "m": m})
                return True
            except (IntegrityError, DBAPIError, ProgrammingError):
                return False

    for col, value in (
        ("content", "'changed'"),
        ("summary", "'changed'"),
        ("type", "'procedural'"),
        ("created_by", "gen_random_uuid()"),
        ("classification", "'public'"),
        ("authority_class", "'admin_asserted'"),
        (
            "evidence_status",
            "'unverified'",
        ),  # P7: the only legal move is unverified -> verified, by a named reviewer, on an agent finding (a trigger)
        ("candidate_hmac", "repeat('b', 64)"),
        ("recorded_at", "now()"),
        ("assertion", "NULL"),
    ):
        assert not await attempt(f"UPDATE curated_memories SET {col} = {value} WHERE workspace_id = :ws AND id = :m"), col
    for col, value in (
        ("status", "'disputed'"),
        ("revision", "revision + 1"),
        ("last_confirmed_at", "now()"),
        ("embedding_status", "'ready'"),
        ("expires_at", "now() + interval '1 day'"),
        ("effective_until", "now() + interval '1 day'"),
    ):
        assert await attempt(f"UPDATE curated_memories SET {col} = {value} WHERE workspace_id = :ws AND id = :m"), col
    assert not await attempt(
        "DELETE FROM curated_memories WHERE workspace_id = :ws AND id = :m"
    )  # memories are retired, never deleted, by the application role
    assert not await attempt(
        "UPDATE memory_scopes SET scope_ref = 'x' WHERE workspace_id = :ws AND memory_id = :m"
    )  # scopes are fixed at birth
    assert not await attempt("DELETE FROM memory_evidence WHERE workspace_id = :ws AND memory_id = :m")
    async with tenant_session(
        api.engine, ctx_of(t)
    ) as c:  # behind the P8 purge gate: refused when there is a row, a no-op when there is none
        try:
            assert (
                await c.execute(text("DELETE FROM memory_lineage WHERE workspace_id = :ws AND memory_id = :m"), {"ws": t.ws, "m": m})
            ).rowcount == 0
        except (IntegrityError, DBAPIError, ProgrammingError):
            pass


async def test_the_generated_tsvector_indexes_summary_and_content(api, tenant_mem):
    t = tenant_mem
    ids = await sarah_story(t)
    mid = (
        await save(
            t,
            "admin",
            body(
                content="Sarah plans the quarterly offsite",
                summary="offsite planning",
                subject_ids=[ids["sarah"]],
                source_ref="x",
                purpose="p",
            ),
        )
    ).json()["memory_id"]
    rows = await sql(
        api, t, "SELECT id FROM curated_memories WHERE workspace_id = :ws AND tsv @@ to_tsquery('simple', 'quarterly & offsite')"
    )
    assert [str(r.id) for r in rows] == [mid]
    assert [
        str(r.id)
        for r in await sql(api, t, "SELECT id FROM curated_memories WHERE workspace_id = :ws AND tsv @@ to_tsquery('simple', 'planning')")
    ] == [mid]  # the summary too
    assert await sql(api, t, "SELECT id FROM curated_memories WHERE workspace_id = :ws AND tsv @@ to_tsquery('simple', 'unrelated')") == []
    idx = await sql(
        api, t, "SELECT indexdef FROM pg_indexes WHERE tablename = 'curated_memories' AND indexname = 'curated_memories_tsv_idx'"
    )
    assert "gin" in idx[0].indexdef.lower()


async def test_vectors_are_384_dimensional_versioned_and_carry_their_model(api, tenant_mem):
    t = tenant_mem
    ids = await sarah_story(t)
    mid = uuid.UUID((await save(t, "admin", body(subject_ids=[ids["sarah"]], source_ref="x", purpose="p"))).json()["memory_id"])
    await run_embeds(api, t)
    (e,) = await sql(
        api,
        t,
        "SELECT embedding_version, embedding_model_id, embedding_dim, vector_dims(embedding) AS d FROM memory_embeddings WHERE memory_id = :m",
        m=mid,
    )
    assert (e.embedding_version, e.embedding_model_id, e.embedding_dim, e.d) == (1, "intfloat/multilingual-e5-small", 384, 384)

    async def refused(vector: str, dim: int, version: int = 2) -> None:
        async with tenant_session(api.engine, ctx_of(t)) as c:
            with pytest.raises((IntegrityError, DBAPIError, ProgrammingError)):
                await c.execute(
                    text(
                        "INSERT INTO memory_embeddings (memory_id, embedding_version, workspace_id, embedding, embedding_model_id, embedding_dim) VALUES (:m, :v, :ws, CAST(:e AS vector), 'x', :d)"
                    ),
                    {"m": mid, "v": version, "ws": t.ws, "e": vector, "d": dim},
                )

    await refused("[1,2,3]", 3)  # the column is vector(384)
    await refused("[" + ",".join(["0.1"] * 384) + "]", 128)  # the stored dimension must equal the vector's
    # a second version coexists with the first (PV-04 path A): reads pin ONE
    async with tenant_session(api.engine, ctx_of(t)) as c:
        await c.execute(
            text(
                "INSERT INTO memory_embeddings (memory_id, embedding_version, workspace_id, embedding, embedding_model_id, embedding_dim) VALUES (:m, 2, :ws, CAST(:e AS vector), 'next', 384)"
            ),
            {"m": mid, "ws": t.ws, "e": "[" + ",".join(["0.1"] * 384) + "]"},
        )
    assert sorted(
        r.embedding_version for r in await sql(api, t, "SELECT embedding_version FROM memory_embeddings WHERE memory_id = :m", m=mid)
    ) == [1, 2]
    assert (
        await sql(
            api,
            t,
            "SELECT count(*) AS n FROM pg_indexes WHERE tablename = 'memory_embeddings' AND indexdef ILIKE '%hnsw%' OR indexdef ILIKE '%ivfflat%'",
        )
    )[0].n == 0  # no ANN index (ADR-019)


async def test_the_shape_constraints_keep_memories_honest(api, tenant_mem):
    t = tenant_mem
    ids = await sarah_story(t)
    base = await memory_rows(api, t)
    assert base == []
    mid = uuid.UUID((await save(t, "admin", body(subject_ids=[ids["sarah"]], source_ref="x", purpose="p"))).json()["memory_id"])
    (row,) = await memory_rows(api, t)

    async def refused(statement: str, **params) -> None:  # type: ignore[no-untyped-def]
        async with tenant_session(api.engine, ctx_of(t)) as c:
            with pytest.raises((IntegrityError, DBAPIError, ProgrammingError)):
                await c.execute(text(statement), {"ws": t.ws, "p": row.created_by, "c": row.command_id, **params})

    insert = (
        "INSERT INTO curated_memories (id, workspace_id, type, origin, content, summary, status, authority_class, evidence_status, classification, created_by, command_id, "
        "pipeline_version, candidate_hmac, {extra}) VALUES (gen_random_uuid(), :ws, {type}, 'explicit', 'c', 's', 'active', 'user_asserted', 'verified', 'internal', :p, :c, 'p5.v1', "
        "repeat(:h, 64), {vals})"
    )
    await refused(insert.format(extra="run_id", type="'semantic'", vals="'r1'"), h="1")  # a run id belongs to working memory only
    await refused(insert.format(extra="importance", type="'working'", vals="0.5"), h="2")  # working memory has a deadline
    await refused(insert.format(extra="confidence", type="'semantic'", vals="1.5"), h="3")
    await refused(insert.format(extra="effective_until", type="'semantic'", vals="now() - interval '10 days'"), h="4")
    await refused(insert.format(extra="importance", type="'semantic'", vals="2"), h="5")
    await refused(insert.format(extra="importance", type="'galaxy'", vals="0.5"), h="6")
    await refused(insert.format(extra="importance", type="'semantic'", vals="0.5").replace("'user_asserted'", "'god_mode'"), h="7")
    await refused(
        "INSERT INTO memory_scopes (memory_id, workspace_id, scope_kind, scope_ref) VALUES (:m, :ws, 'workspace', 'not-star')", m=mid
    )
    await refused("INSERT INTO memory_scopes (memory_id, workspace_id, scope_kind, scope_ref) VALUES (:m, :ws, 'galaxy', 'x')", m=mid)
    await refused(
        "INSERT INTO memory_evidence (id, memory_id, workspace_id, source_kind) VALUES (gen_random_uuid(), :m, :ws, 'fact')", m=mid
    )  # a fact row without a fact
    await refused(
        "INSERT INTO memory_evidence (id, memory_id, workspace_id, source_kind, interaction_id) VALUES (gen_random_uuid(), :m, :ws, 'episode', gen_random_uuid())",
        m=mid,
    )
    await refused(
        "INSERT INTO memory_lineage (memory_id, related_id, workspace_id, relation) VALUES (:m, :m, :ws, 'replaces')", m=mid
    )  # not its own predecessor
    await refused(
        "INSERT INTO memory_subjects (memory_id, entity_id, workspace_id) VALUES (:m, gen_random_uuid(), :ws)", m=mid
    )  # an entity of this workspace
    # review items for memory name a memory, and there is one open item per memory and type
    await refused(
        "INSERT INTO review_items (id, workspace_id, item_type, rule_kind, reason_code) VALUES (gen_random_uuid(), :ws, 'memory_proposal', 'x', 'x')"
    )
    item = "INSERT INTO review_items (id, workspace_id, item_type, rule_kind, reason_code, memory_id) VALUES (gen_random_uuid(), :ws, 'memory_proposal', 'x', 'x', :m)"
    async with tenant_session(api.engine, ctx_of(t)) as c:
        await c.execute(text(item), {"ws": t.ws, "m": mid})
    await refused(item, m=mid)


async def test_the_dedupe_index_allows_one_live_memory_per_keyed_hash_and_frees_it_when_it_retires(api, tenant_mem):
    t = tenant_mem
    ids = await sarah_story(t)
    mid = uuid.UUID((await save(t, "admin", body(subject_ids=[ids["sarah"]], source_ref="x", purpose="p"))).json()["memory_id"])
    (row,) = await memory_rows(api, t)
    clone = (
        "INSERT INTO curated_memories (id, workspace_id, type, origin, content, summary, status, authority_class, evidence_status, classification, created_by, command_id, "
        "pipeline_version, candidate_hmac) VALUES (gen_random_uuid(), :ws, 'semantic', 'explicit', 'c', 's', :st, 'user_asserted', 'verified', 'internal', :p, :c, 'p5.v1', :h)"
    )
    params = {"ws": t.ws, "p": row.created_by, "c": row.command_id, "h": row.candidate_hmac}
    async with tenant_session(api.engine, ctx_of(t)) as c:
        with pytest.raises(IntegrityError):
            await c.execute(text(clone), {**params, "st": "proposed"})
    async with tenant_session(api.engine, ctx_of(t)) as c:
        await c.execute(
            text("UPDATE curated_memories SET status = 'superseded' WHERE workspace_id = :ws AND id = :m"), {"ws": t.ws, "m": mid}
        )
        await c.execute(text(clone), {**params, "st": "active"})  # the retired row no longer holds the slot
        await c.execute(text(clone.replace("repeat", "repeat")), {**params, "st": "rejected", "h": "f" * 64})


async def test_grants_commands_and_the_stats_function(api, tenant_mem):
    t = tenant_mem
    # principal_scopes: one row per grant, and the classification kind exists
    await t.grant("agent", "classification", "restricted")
    await t.grant("agent", "classification", "restricted")  # idempotent
    rows = await sql(
        api, t, "SELECT scope_kind, scope_ref FROM principal_scopes WHERE workspace_id = :ws AND scope_kind = 'classification'"
    )
    assert [(r.scope_kind, r.scope_ref) for r in rows] == [("classification", "restricted")]
    # a memory command may have no ontology version; any other command may not
    async with tenant_session(api.engine, ctx_of(t)) as c:
        pid = (await c.execute(text("SELECT id FROM principals WHERE workspace_id = :ws LIMIT 1"), {"ws": t.ws})).scalar_one()
        ins = (
            "INSERT INTO commands (id, workspace_id, idempotency_key, request_hash, command_type, actor_principal_id, authority_class, classification, payload, status, http_status) "
            "VALUES (gen_random_uuid(), :ws, :k, 'h', :ct, :p, 'agent_generated', 'internal', '{}', 'applied', 200)"
        )
        await c.execute(text(ins), {"ws": t.ws, "k": f"k-{uuid.uuid4()}", "ct": "memory.save", "p": pid})
        with pytest.raises((IntegrityError, DBAPIError)):
            async with c.begin_nested():
                await c.execute(text(ins), {"ws": t.ws, "k": f"k-{uuid.uuid4()}", "ct": "entity.create", "p": pid})
    # aggregates only: the cross-workspace stats function returns types, statuses and counts, never ids
    async with system_session(api.engine) as c:
        cols = (await c.execute(text("SELECT * FROM ops.memory_stats()"))).keys()
    assert list(cols) == ["type", "status", "embedding_status", "memories"]


async def test_the_scope_predicate_is_in_the_scan_of_the_search_statement_not_a_filter_after_it(api, tenant_mem):
    """EXPLAIN snapshot (WP4): the visibility test sits inside the `candidates` CTE, over memory_scopes, before any ranking."""
    t = tenant_mem
    ids = await sarah_story(t)
    await t.grant("agent2", "subject", ids["sarah"])
    for i in range(3):
        await save(t, "admin", body(content=f"shared ledger fact {i}", subject_ids=[ids["sarah"]], source_ref="x", purpose="p"))
    principal = Principal(
        principal_id=uuid.UUID(await t.principal_id("agent2")),
        workspace_id=t.ws,
        kind=PrincipalKind.AGENT,
        handle="agent2",
        roles=frozenset({Role.AGENT}),
    )
    search = api.app.state.kg.memory.search
    async with tenant_session(api.engine, ctx_of(t)) as c:
        grants = await ScopeGrants.for_principal(c, principal)
        sql_text, params = search.build(Viewer(principal, grants), SearchIn(query="shared ledger"), [0.1] * 384)
        plan = (await c.execute(text("EXPLAIN (FORMAT JSON) " + sql_text), params)).scalar_one()
    flat = json.dumps(plan)

    def nodes(node):  # type: ignore[no-untyped-def]
        yield node
        for child in node.get("Plans", []):
            yield from nodes(child)

    root = plan[0]["Plan"]
    all_nodes = list(nodes(root))
    candidates = next(n for n in all_nodes if n.get("CTE Name") == "candidates" or n.get("Subplan Name") == "CTE candidates")
    inside = json.dumps(candidates)
    assert "memory_scopes" in inside and "curated_memories" in inside  # the authorisation reads memory_scopes INSIDE the candidates scan
    ranking = [n for n in all_nodes if n.get("Node Type") in ("WindowAgg",)]
    assert ranking, "a ranking step exists"
    for w in ranking:  # and every ranking step sits ABOVE a node that already depends on the candidates CTE
        assert "memory_scopes" not in json.dumps({k: v for k, v in w.items() if k != "Plans"})
    assert "Filter" in flat
    # lex and vec are merged with a FULL JOIN (hash or merge, never a nested loop): a LEFT JOIN of each onto `candidates` was planned as a nested loop
    # on a row estimate of 6 against 50 000 actual when a term matched most of the workspace (2.5 billion comparisons)
    assert any(n.get("Join Type") == "Full" and n.get("Node Type") in ("Hash Join", "Merge Join") for n in all_nodes), (
        "lex/vec are merged by a full join"
    )
    _ = accept_event, sub
