"""P9 WP3: `kg reconcile`. Each drift class is MADE (in Neo4j, the outbox, the memory store, the deletion records), found by a scan that names ids only, repaired by the safe repair
(or left alone when only a person or a cost confirmation can settle it), and shown gone by a second scan."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from kg_db.session import system_session, tenant_session
from kg_modelgw.egress import egress
from sqlalchemy import text

from tests.support.p4 import HR, ctx_of, sarah_story, sql  # noqa: F401
from tests.support.p5 import owner_sql, save
from tests.support.p8 import deletion_world, request_deletion, run_deletion
from tests.support.worker import running_worker, wait_until

pytestmark = pytest.mark.integration


def reconcile(api):
    return api.app.state.kg.reconcile


async def scan(api, t, scope="all", **kw):
    return await reconcile(api).run(ctx_of(t), scope=scope, **kw)


async def test_a_clean_workspace_has_no_drift(api, tenant_governance):
    t = tenant_governance
    await sarah_story(t)
    r = await scan(api, t, "graph")
    assert r.status == "completed" and r.drift == {} and r.errors == [], r.as_dict()
    rows = await sql(api, t, "SELECT status, mode, trigger, drift FROM reconcile_runs WHERE workspace_id = :ws")
    assert [(x.status, x.mode, x.trigger, x.drift) for x in rows] == [("completed", "check", "manual", {})]


async def test_graph_drift_is_found_by_class_with_ids_only_and_repaired_from_postgres(api, tenant_governance):
    t = tenant_governance
    ids = await sarah_story(t)
    graph = api.app.state.kg.graph.scoped(ctx_of(t))
    await graph.delete_node(ids["michael"])  # missing
    await graph.run_cypher("MATCH (n:Entity {group_id: $group_id, uuid: $u}) SET n.name = 'Tampered'", u=ids["topic"])  # mismatch
    stray = str(uuid.uuid4())
    with egress(
        classification="internal", purpose="graph_projection", workspace_id=str(t.ws), correlation_id=str(uuid.uuid4()), service="test"
    ):
        await graph.upsert_entity_node(
            uuid=stray, name="Stray", type_key="Person", attributes={}, created_at=dt.datetime.now(dt.UTC)
        )  # orphan
    ghost = str(uuid.uuid4())
    await graph.ensure_episode_node(
        uuid=ghost,
        name="ghost",
        body="x",
        source="message",
        source_description="t",
        valid_at=dt.datetime.now(dt.UTC),
        created_at=dt.datetime.now(dt.UTC),
    )  # an episode that belongs to nothing
    found = await scan(api, t, "graph")
    assert (
        found.drift["graph.entity.missing"] == 1 and found.drift["graph.entity.mismatch"] == 1 and found.drift["graph.entity.orphan"] == 1
    )
    assert found.drift["graph.episode.orphan"] == 1 and found.samples["graph.episode.orphan"] == [ghost]
    assert found.samples["graph.entity.missing"] == [ids["michael"]]
    assert "Tampered" not in str(found.as_dict()) and "Stray" not in str(found.as_dict())  # ids, never content
    fixed = await scan(api, t, "graph", repair=True)
    assert fixed.mode == "repair" and fixed.drift == {}, fixed.as_dict()
    assert (
        fixed.repaired["graph.entity.missing"] == 1
        and fixed.repaired["graph.entity.orphan"] == 1
        and fixed.repaired["graph.episode.orphan"] == 1
    )
    assert (await api.app.state.kg.knowledge.projector.reconcile(ctx_of(t))).drift == []
    audit = await sql(api, t, "SELECT metadata FROM audit_log WHERE workspace_id = :ws AND action = 'reconcile.repaired'")
    assert {a.metadata["reconcile_class"] for a in audit} >= {"graph.entity.missing", "graph.episode.orphan"}
    assert (await scan(api, t, "graph")).drift == {}


async def test_a_missing_memory_vector_is_requeued_and_a_real_worker_finishes_it(api, tenant_governance):
    t = tenant_governance
    r = await save(
        t,
        "admin",
        {
            "type": "semantic",
            "content": "Sarah prefers decision comparisons that show risks and trade-offs.",
            "purpose": "history",
            "source_ref": "reconcile-test",
        },
    )
    assert r.status_code == 201, r.text
    mid = r.json()["memory_id"]
    # the job is lost (what the outbox is built to prevent, which is why the scan checks)
    await owner_sql(
        "UPDATE outbox_jobs SET status = 'cancelled' WHERE workspace_id = :ws AND stage = 'memory_embed' AND status IN ('pending','leased')",
        ws=t.ws,
    )
    found = await scan(api, t, "memory")
    assert found.drift == {"memory.embedding_missing": 1} and found.samples["memory.embedding_missing"] == [mid]
    fixed = await scan(api, t, "memory", repair=True)
    assert fixed.repaired == {"memory.embedding_missing": 1}
    async with running_worker(api, stages=("memory_embed",), concurrency=2):

        async def done() -> bool:
            return (await sql(api, t, "SELECT count(*) AS n FROM memory_embeddings WHERE workspace_id = :ws"))[0].n == 1

        await wait_until(done, timeout=60.0, what="the memory to be embedded")
    assert (await scan(api, t, "memory")).drift == {}


async def test_ledger_drift_a_job_for_a_subject_that_is_gone(api, tenant_governance):
    t = tenant_governance
    from kg_jobs.outbox import enqueue

    async with tenant_session(api.engine, ctx_of(t)) as conn:
        await enqueue(
            conn,
            workspace_id=t.ws,
            stage="graph_projection",
            subject_type="command",
            subject_id=uuid.uuid4(),
            dedupe_key=f"test:{uuid.uuid4()}",
            payload={"command_id": str(uuid.uuid4()), "generation": 0},
            pipeline_version="p4.v1",
            ontology_version_id=None,
        )
    found = await scan(api, t, "ledger")
    assert found.drift == {"ledger.job_missing_subject": 1}
    fixed = await scan(api, t, "ledger", repair=True)
    assert fixed.repaired == {"ledger.job_missing_subject": 1} and fixed.drift == {}


async def test_a_deletion_that_left_residue_opens_an_exception_instead_of_editing(api, tenant_governance):
    t = tenant_governance
    from kg_jobs.outbox import enqueue

    w = await deletion_world(api, t)
    r = await request_deletion(t, "subject", w["zeb"], "app")
    done = await run_deletion(api, t, r.json()["request_id"])
    assert done["status"] in ("completed", "completed_with_exceptions")
    assert (await scan(api, t, "deletions")).drift == {}  # a faithful purge is clean
    async with tenant_session(api.engine, ctx_of(t)) as conn:  # residue: a job that still names the deleted subject
        await enqueue(
            conn,
            workspace_id=t.ws,
            stage="graph_projection",
            subject_type="command",
            subject_id=uuid.UUID(w["zeb"]),
            dedupe_key=f"residue:{uuid.uuid4()}",
            payload={"command_id": str(uuid.uuid4()), "generation": 0},
            pipeline_version="p4.v1",
            ontology_version_id=None,
        )
    found = await scan(api, t, "deletions")
    assert found.drift == {"deletions.residual": 1} and found.samples["deletions.residual"] == [r.json()["request_id"]]
    fixed = await scan(api, t, "deletions", repair=True)
    assert fixed.drift == {"deletions.residual": 1}  # not repaired: a person decides
    exc = await sql(
        api,
        t,
        "SELECT detail FROM review_items WHERE workspace_id = :ws AND item_type = 'deletion_exception' AND detail ->> 'location' = 'reconcile:id_residual'",
    )
    assert len(exc) == 1
    await scan(api, t, "deletions", repair=True)  # a repeat opens no second review
    assert (
        len(
            await sql(
                api,
                t,
                "SELECT 1 FROM review_items WHERE workspace_id = :ws AND item_type = 'deletion_exception' AND detail ->> 'location' = 'reconcile:id_residual'",
            )
        )
        == 1
    )


async def test_a_repair_that_would_spend_model_money_waits_for_confirmation(api, tenant_governance):
    t = tenant_governance
    ids = await sarah_story(t)
    # a recorded episode that is not in the graph: the interaction row, its graph_ingest stage and a graph_episodes row, but no episode node
    from tests.support.p5 import accept_event

    iid = await accept_event(t, {"kind": "message", "content": "Sarah mentioned Acme."}, subjects=[ids["sarah"]])
    await owner_sql(
        "INSERT INTO graph_episodes (workspace_id, interaction_id, generation, episode_uuid, ontology_version_id, compiled_hash) "
        "SELECT :ws, :i, 0, :e, active_version_id, 'test' FROM workspace_ontology WHERE workspace_id = :ws",
        ws=t.ws,
        i=uuid.UUID(iid),
        e=str(uuid.uuid4()),
    )
    found = await scan(api, t, "graph")
    assert found.drift.get("graph.episode.missing") == 1 and found.samples["graph.episode.missing"] == [iid]
    held = await scan(api, t, "graph", repair=True)  # no allow_reingest: nothing is spent
    assert held.needs_confirmation == {"graph.episode.missing": 1} and held.reingest_preview is not None
    assert held.reingest_preview["interactions"] == 1 and "estimated_usd" in held.reingest_preview
    assert held.drift.get("graph.episode.missing") == 1 and not held.repaired
    jobs = await sql(
        api, t, "SELECT count(*) AS n FROM outbox_jobs WHERE workspace_id = :ws AND stage = 'graph_ingest' AND status = 'pending'"
    )
    allowed = await scan(api, t, "graph", repair=True, allow_reingest=True)
    assert allowed.reingest_preview is not None
    after = await sql(
        api, t, "SELECT count(*) AS n FROM outbox_jobs WHERE workspace_id = :ws AND stage = 'graph_ingest' AND status = 'pending'"
    )
    assert after[0].n == jobs[0].n + 1  # a fresh graph_ingest job exists only now


async def test_the_nightly_claim_hands_one_workspace_to_one_worker_at_a_time(api, tenant):
    t = tenant
    await owner_sql(
        "INSERT INTO reconcile_runs (id, workspace_id, scope, mode, trigger, status, finished_at) "
        "SELECT gen_random_uuid(), id, 'all', 'check', 'scheduled', 'completed', now() FROM workspaces WHERE id <> :ws",
        ws=t.ws,
    )
    try:
        async with system_session(api.engine) as conn:
            first = (await conn.execute(text("SELECT * FROM ops.claim_reconcile_scan(24)"))).first()
        assert first is not None and first.ws_id == t.ws
        async with system_session(api.engine) as conn:
            assert (await conn.execute(text("SELECT * FROM ops.claim_reconcile_scan(24)"))).first() is None  # the window is claimed
        result = await reconcile(api).run(ctx_of(t), scope="ledger", trigger="scheduled", run_id=first.run)
        assert result.status == "completed"
        rows = await sql(api, t, "SELECT status, trigger FROM reconcile_runs WHERE workspace_id = :ws")
        assert [(x.status, x.trigger) for x in rows] == [("completed", "scheduled")]
    finally:
        await owner_sql("DELETE FROM reconcile_runs WHERE trigger = 'scheduled'")
