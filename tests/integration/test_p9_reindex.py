"""P9 WP8 (Flow F): the graph rebuilt in a separate namespace and cut over atomically. Reads never see the building namespace; the build catches up with Postgres; the cutover is one
exchange plus one pointer flip, seen by other replicas at once; rollback is the same exchange the other way; a deletion clears every inactive copy; isolation holds."""

from __future__ import annotations

import asyncio
import datetime as dt
import time
import uuid

import pytest
from kg_domain.principal import WorkspaceContext
from kg_graph.group import GroupId

from tests.support.p4 import ctx_of, sarah_story, sql, strict_mock  # noqa: F401
from tests.support.p8 import deletion_world, raw, request_deletion, run_deletion
from tests.support.p9 import replicas
from tests.support.worker import running_worker, wait_until

pytestmark = pytest.mark.integration


async def active_version(api, t) -> str:
    return str((await sql(api, t, "SELECT active_version_id FROM workspace_ontology WHERE workspace_id = :ws"))[0].active_version_id)


async def start(api, t, mode="project", confirm=True, **extra):
    vid = await active_version(api, t)
    return await t.req("POST", f"/v1/ontologies/versions/{vid}/reindex", "admin", json={"mode": mode, "confirm": confirm, **extra})


async def wait_status(api, t, run_id: str, *statuses: str, timeout: float = 120.0) -> dict:  # noqa: ASYNC109
    async def done():
        r = (await t.get(f"/v1/reindex-runs/{run_id}", "admin")).json()
        return r if r["status"] in statuses else None

    return await wait_until(done, timeout=timeout, what=f"the reindex to reach {statuses}")


async def episode_in_active_group(api, t, ids) -> str:
    """A recorded episode that mentions Sarah, in the live graph (what an extraction leaves behind), so the build has something to copy."""
    graph = api.app.state.kg.graph.scoped(ctx_of(t))
    ep = str(uuid.uuid4())
    await graph.ensure_episode_node(
        uuid=ep,
        name="evt",
        body="Sarah mentioned Acme.",
        source="message",
        source_description="chat",
        valid_at=dt.datetime.now(dt.UTC),
        created_at=dt.datetime.now(dt.UTC),
    )
    await graph.run_cypher(
        "MATCH (e:Episodic {group_id: $group_id, uuid: $e}), (n:Entity {group_id: $group_id, uuid: $n}) MERGE (e)-[m:MENTIONS {uuid: $m}]->(n) SET m.group_id = $group_id, m.created_at = e.created_at",
        e=ep,
        n=ids["sarah"],
        m=str(uuid.uuid4()),
    )
    return ep


async def test_preview_names_what_would_be_built_and_what_it_would_cost(api, tenant_governance):
    t = tenant_governance
    ids = await sarah_story(t)
    r = await start(api, t, confirm=False)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "preview" and body["namespace"] == f"{t.group_id}-r1" and body["previous_namespace"] == t.group_id
    assert body["entities"] >= 4 and body["facts"] >= 2 and body["estimated_cost_usd"] >= 0
    assert (await sql(api, t, "SELECT count(*) AS n FROM reindex_runs WHERE workspace_id = :ws"))[0].n == 0  # a preview records nothing
    # only an admin; a cost ceiling is honoured
    for who in ("operator", "reviewer", "app", "agent", "reader"):
        assert (await t.req("POST", f"/v1/ontologies/versions/{await active_version(api, t)}/reindex", who, json={})).status_code == 403, (
            who
        )
    assert (await start(api, t, max_cost_usd=0.0, mode="reextract")).status_code in (202, 409)
    del ids


async def test_the_build_is_invisible_to_readers_catches_up_and_cuts_over_atomically(api, tenant_governance):
    t = tenant_governance
    ids = await sarah_story(t)
    ep = await episode_in_active_group(api, t, ids)
    ctx = ctx_of(t)
    graph = api.app.state.kg.graph
    r = await start(api, t)
    assert r.status_code == 202, r.text
    run = r.json()
    ns = run["namespace"]
    assert ns == f"{t.group_id}-r1" and run["status"] == "building"
    assert (await start(api, t)).status_code == 409  # one rebuild at a time
    async with running_worker(api, stages=("graph_reindex",), concurrency=1):
        ready = await wait_status(api, t, run["id"], "ready", "failed")
    assert ready["status"] == "ready", ready
    report = ready["report"]
    assert report["ok"] and report["built"]["nodes"] == report["active"]["nodes"] and report["built"]["edges"] == report["active"]["edges"]
    assert report["built"]["episodes"] == 1 and report["built"]["mentions"] == 1
    # readers still see the old namespace, plainly; the built one is parked
    assert graph.namespaces.active(ctx) == t.group_id
    assert (await graph.scoped(ctx).read_node(ids["sarah"])) is not None
    parked = await graph.reindexer(ctx).parked_stats(ns)
    assert parked["nodes"] == parked["parked"] > 0
    assert (await t.get(f"/v1/entities/{ids['sarah']}", "admin")).status_code == 200
    # Postgres moves on while the build waits: an update, a new entity
    rev = (await t.get(f"/v1/entities/{ids['topic']}", "admin")).json()["revision"]
    upd = await t.req(
        "PATCH",
        f"/v1/entities/{ids['topic']}",
        "app",
        json={"name": "Entrepreneurship and founding"},
        headers={"if-match": f'W/"rev-{rev}"'},
    )
    assert upd.status_code in (200, 201, 202), upd.text
    fresh = (await t.entity("Topic", "Late arrival", source_system="hr", source_ref="late-1")).json()["entity_id"]
    # the cutover: a last catch-up under the graph lock, one exchange, one flip
    async with replicas(api, 1) as (other,):
        await asyncio.wait_for(other.state.invalidator.connected.wait(), 5)
        assert other.state.graph.namespaces.active(ctx) == t.group_id
        started = time.monotonic()
        act = await t.req("POST", f"/v1/graph/namespaces/{ns}/activate", "admin", json={"reason_code": "ontology_change"})
        assert act.status_code == 200, act.text
        assert act.json()["action"] == "cutover" and act.json()["run"]["status"] == "activated"
        while other.state.graph.namespaces.active(ctx) != ns:  # the other replica learns from NOTIFY, well inside a second
            assert time.monotonic() - started < 2.0, "the other replica did not see the cutover"
            await asyncio.sleep(0.02)
    assert graph.namespaces.active(ctx) == ns
    live = graph.scoped(ctx)
    node = await live.read_node(ids["sarah"])
    assert node is not None and (await live.read_node(fresh)) is not None
    assert (await live.read_node(ids["topic"])).name == "Entrepreneurship and founding"
    assert (await api.app.state.kg.knowledge.projector.reconcile(ctx)).drift == []  # the live graph equals Postgres
    # the old namespace is retired and parked: not a single plain uuid of it is visible
    assert (await graph.reindexer(ctx).parked_stats(t.group_id))["parked"] == (await graph.reindexer(ctx).counts(t.group_id)).nodes
    states = {n["namespace"]: n["status"] for n in (await t.get("/v1/graph/namespaces", "admin")).json()}
    assert states == {ns: "active", t.group_id: "retired"}
    # ledger command, audit, and everything still reads
    assert len(await sql(api, t, "SELECT 1 FROM commands WHERE workspace_id = :ws AND command_type = 'graph.namespace_activate'")) == 1
    assert len(await sql(api, t, "SELECT 1 FROM audit_log WHERE workspace_id = :ws AND action = 'reindex.cutover'")) == 1
    hood = await t.get(f"/v1/entities/{ids['sarah']}/neighbourhood", "admin")
    assert hood.status_code in (200, 404)
    # new work lands in the new namespace
    newer = (await t.entity("Topic", "After cutover", source_system="hr", source_ref="post-1")).json()["entity_id"]
    assert (await live.read_node(newer)) is not None
    assert await graph.namespace_of(ctx, t.group_id).read_node(newer) is None
    assert ep in await graph.reindexer(ctx).episode_uuids(ns)

    # rollback: the same exchange the other way; Postgres is authoritative so the older graph is brought up to date
    back = await t.req("POST", f"/v1/graph/namespaces/{t.group_id}/activate", "admin", json={})
    assert back.status_code == 200 and back.json()["action"] == "rollback", back.text
    assert graph.namespaces.active(ctx) == t.group_id
    assert (await graph.scoped(ctx).read_node(ids["sarah"])) is not None
    assert (await graph.scoped(ctx).read_node(newer)) is not None  # projected by the repair after the rollback
    assert (await api.app.state.kg.knowledge.projector.reconcile(ctx)).drift == []
    runs = {r["id"]: r["status"] for r in (await t.get("/v1/reindex-runs", "admin")).json()}
    assert runs[run["id"]] == "rolled_back"
    assert len(await sql(api, t, "SELECT 1 FROM commands WHERE workspace_id = :ws AND command_type = 'graph.namespace_rollback'")) == 1


async def test_a_resumed_build_writes_nothing_twice_and_a_cancelled_one_leaves_no_trace(api, tenant_governance):
    t = tenant_governance
    ids = await sarah_story(t)
    r = await start(api, t)
    run = r.json()
    ns = run["namespace"]
    ctx = ctx_of(t)
    svc = api.app.state.kg.reindex
    # a first pass writes everything; a second (a resume after a crash, FI-19) finds nothing left to do
    wctx = WorkspaceContext.for_system_task(t.ws, reason="test")
    first = await svc._sync(wctx, uuid.UUID(run["id"]), ns, t.group_id, first=True)
    assert first >= 6
    second = await svc._sync(wctx, uuid.UUID(run["id"]), ns, t.group_id, first=False)
    assert second == 0
    # an entity removed from Postgres' eligible set leaves the build on the next pass (a frozen subject)
    await raw(api, t, "UPDATE entities SET status = 'deleted' WHERE workspace_id = :ws AND id = :e", e=uuid.UUID(ids["topic"]))
    assert await svc._sync(wctx, uuid.UUID(run["id"]), ns, t.group_id, first=False) >= 1
    nodes, _ = await api.app.state.kg.graph.reindexer(ctx).inventory(ns)
    assert ids["topic"] not in nodes
    assert (
        await t.req("POST", f"/v1/graph/namespaces/{ns}/activate", "admin", json={})
    ).status_code == 409  # not ready: a build in progress cannot be activated
    cancelled = await t.req("POST", f"/v1/reindex-runs/{run['id']}/cancel", "admin")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert (await api.app.state.kg.graph.reindexer(ctx).counts(ns)).as_dict() == {"nodes": 0, "edges": 0, "episodes": 0, "mentions": 0}
    assert (await start(api, t)).status_code == 202  # a new run may start, with the next number
    await raw(api, t, "UPDATE entities SET status = 'active' WHERE workspace_id = :ws AND id = :e", e=uuid.UUID(ids["topic"]))


async def test_a_namespace_can_never_name_another_workspaces_graph(api, tenant, tenant_governance):
    mine, theirs = ctx_of(tenant), ctx_of(tenant_governance)
    assert GroupId.for_namespace(mine, f"{tenant.group_id}-r3").value.endswith("-r3")
    for bad in (
        tenant_governance.group_id,
        f"{tenant_governance.group_id}-r1",
        f"{tenant.group_id}-sim-abc",
        f"{tenant.group_id}-r",
        "ws-evil",
        "",
    ):
        with pytest.raises(ValueError):
            GroupId.for_namespace(mine, bad)
    assert GroupId.for_namespace(theirs, f"{tenant_governance.group_id}-r1")
    # and the API refuses what does not exist in the caller's workspace
    assert (await tenant.req("POST", f"/v1/graph/namespaces/{tenant_governance.group_id}-r1/activate", "admin", json={})).status_code == 404


async def test_a_deletion_clears_the_retired_and_the_building_namespaces_too(api, tenant_governance):
    t = tenant_governance
    w = await deletion_world(api, t)
    ctx = ctx_of(t)
    graph = api.app.state.kg.graph
    # a finished rebuild (activated) leaves the old group retired; a second one is still building
    first = (await start(api, t)).json()
    async with running_worker(api, stages=("graph_reindex",), concurrency=1):
        await wait_status(api, t, first["id"], "ready")
    assert (await t.req("POST", f"/v1/graph/namespaces/{first['namespace']}/activate", "admin", json={})).status_code == 200
    second = (await start(api, t)).json()
    assert (await graph.reindexer(ctx).counts(t.group_id)).nodes > 0  # the retired copy still holds the subject
    r = await request_deletion(t, "subject", w["zeb"], "app")
    assert r.status_code == 202, r.text
    done = await run_deletion(api, t, r.json()["request_id"])
    assert done["status"] in ("completed", "completed_with_exceptions")
    assert (await graph.reindexer(ctx).counts(t.group_id)).as_dict() == {
        "nodes": 0,
        "edges": 0,
        "episodes": 0,
        "mentions": 0,
    }  # the retired copy is gone
    assert (await graph.reindexer(ctx).counts(second["namespace"])).nodes == 0
    rows = {n["namespace"]: n for n in (await t.get("/v1/graph/namespaces", "admin")).json()}
    assert rows[t.group_id]["cleared_at"] and rows[second["namespace"]]["cleared_at"]
    assert (await t.get(f"/v1/reindex-runs/{second['id']}", "admin")).json()["error_code"] == "deletion_during_build"
    # the active namespace holds none of the subject either
    assert not await graph.scoped(ctx).read_node(w["zeb"])


async def test_reextraction_replays_every_interaction_under_the_active_ontology_after_the_cutover(api, strict_mock):  # noqa: F811
    from tests.support.p4 import EVT003, run_event, stage
    from tests.support.tenants import GOVERNANCE_YAML, clear_graph, make_tenant

    t = await make_tenant(api, ontology=GOVERNANCE_YAML)
    try:
        ids = await sarah_story(t)
        detail = await run_event(api, t, EVT003, subjects=[ids["sarah"], ids["michael"], ids["topic"]])
        assert stage(detail, "graph_ingest")["status"] == "succeeded"
        before = (await sql(api, t, "SELECT generation FROM interaction_stages WHERE workspace_id = :ws AND stage = 'graph_ingest'"))[
            0
        ].generation
        preview = (await start(api, t, mode="reextract", confirm=False)).json()
        assert preview["reextraction_interactions"] == 1 and "reextraction_cost_usd" in preview
        run = (await start(api, t, mode="reextract")).json()
        async with running_worker(api, stages=("graph_reindex",), concurrency=1):
            await wait_status(api, t, run["id"], "ready")
            assert (await t.req("POST", f"/v1/graph/namespaces/{run['namespace']}/activate", "admin", json={})).status_code == 200

            # the follow-up job is the ONLY thing that replays; once the workers run it, the interaction is queued for extraction again
            async def replayed():
                rows = await sql(
                    api, t, "SELECT generation, status FROM interaction_stages WHERE workspace_id = :ws AND stage = 'graph_ingest'"
                )
                return rows[0] if rows[0].generation > before else None

            got = await wait_until(replayed, timeout=60.0, what="the interaction to be queued for re-extraction")
            assert got.generation == before + 1
        done = (await t.get(f"/v1/reindex-runs/{run['id']}", "admin")).json()
        assert done["progress"].get("reextract_done") == 1
        assert len(await sql(api, t, "SELECT 1 FROM audit_log WHERE workspace_id = :ws AND action = 'interaction.repinned'")) >= 1
    finally:
        await clear_graph(api, t.group_id)
        await clear_graph(api, f"{t.group_id}-r1")
