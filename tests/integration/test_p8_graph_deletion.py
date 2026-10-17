"""P8 WP6 (AT-16): deleting a subject whose knowledge went through the REAL extraction pipeline (strict mock router) removes it from Neo4j as well: the episode, the node, every
edge made from the episode, the summaries and fact text that cite him, and nothing of any other workspace. What stays is re-projected from Postgres, so the graph equals it."""

from __future__ import annotations

import pytest

from tests.support.p4 import EVT003, ctx_of, run_event, sarah_story, sql, strict_mock  # noqa: F401
from tests.support.p8 import raw, request_deletion, run_deletion
from tests.support.tenants import GOVERNANCE_YAML, clear_graph, make_tenant

pytestmark = pytest.mark.integration


async def graph_counts(api, t, name: str) -> dict[str, int]:  # type: ignore[no-untyped-def]
    g = api.app.state.kg.graph.scoped(ctx_of(t))
    nodes = await g.run_cypher("MATCH (n:Entity {group_id: $group_id}) WHERE n.name = $n RETURN count(n) AS c", n=name)
    edges = await g.run_cypher("MATCH (:Entity {group_id: $group_id})-[e:RELATES_TO {group_id: $group_id}]->() RETURN count(e) AS c")
    eps = await g.run_cypher("MATCH (e:Episodic {group_id: $group_id}) RETURN count(e) AS c")
    return {"nodes_named": nodes[0]["c"], "edges": edges[0]["c"], "episodes": eps[0]["c"]}


async def test_deleting_a_subject_removes_his_episode_node_and_edges_from_the_graph_and_reprojects_the_rest(
    api,
    tenant_governance,
    strict_mock,  # noqa: F811
):
    t = tenant_governance
    other = await make_tenant(api, ontology=GOVERNANCE_YAML)
    try:
        ids = await sarah_story(t)
        twin = (await other.entity("Person", "Michael", source_system="hr", source_ref="E-2")).json()["entity_id"]
        detail = await run_event(api, t, EVT003, subjects=[ids["sarah"], ids["michael"], ids["topic"]])
        assert detail["processing_status"] == "indexed"
        g = api.app.state.kg.graph.scoped(ctx_of(t))
        before = await graph_counts(api, t, "Michael")
        assert before["nodes_named"] == 1 and before["episodes"] == 1 and before["edges"] >= 4  # WORKS_AT x2, KNOWS, CONSIDERING
        assert await g.read_node(ids["michael"]) is not None
        knows = (await sql(api, t, "SELECT id::text AS id FROM facts WHERE workspace_id = :ws AND rel_type = 'KNOWS'"))[0].id
        assert await g.read_edge(knows) is not None
        assert await g.scan_text(["Michael"], [])  # his name is in the graph before the purge

        r = await request_deletion(t, "subject", ids["michael"], "admin")
        assert r.status_code == 202, r.text
        final = await run_deletion(api, t, r.json()["request_id"], timeout=240)
        assert final["status"] == "completed", final
        assert final["receipt"]["stores"]["graph"]["deleted"] >= 3, final["receipt"]
        assert final["receipt"]["verification"]["graph_residuals"] == 0

        after = await graph_counts(api, t, "Michael")
        assert after["nodes_named"] == 0 and after["episodes"] == 0
        assert await g.read_node(ids["michael"]) is None and await g.read_edge(knows) is None
        assert await g.scan_text(["Michael", "Entrepreneurship encouraging"], []) == []  # no node, edge or episode text names him
        # what stays: Sarah, Acme and both employment edges, projected from Postgres (their text and times are what Postgres says)
        assert await g.read_node(ids["sarah"]) is not None and await g.read_node(ids["acme"]) is not None
        for fid in (ids["swe"], ids["em"]):
            edge = await g.read_edge(fid)
            assert edge is not None
        report = await api.app.state.kg.knowledge.projector.reconcile(ctx_of(t))
        assert report.drift == [], report.drift
        # the interaction extraction made Sarah's node summary mention him: it was cleared, not left behind
        sarah = await g.read_node(ids["sarah"])
        assert "Michael" not in str(sarah)
        # the other workspace's Michael is untouched, in Postgres and in the API
        assert (await other.get(f"/v1/entities/{twin}", "admin")).status_code == 200
        assert len(await raw(api, t, "SELECT 1 FROM entities WHERE workspace_id = :ws AND display_name = 'Michael'")) == 0
    finally:
        await clear_graph(api, other.group_id)


async def test_the_graph_purge_is_idempotent_a_second_run_finds_nothing_to_do(api, tenant_governance, strict_mock):  # noqa: F811
    t = tenant_governance
    ids = await sarah_story(t)
    detail = await run_event(api, t, EVT003, subjects=[ids["sarah"], ids["michael"], ids["topic"]])
    episode = (await sql(api, t, "SELECT episode_uuid FROM graph_episodes WHERE workspace_id = :ws"))[0].episode_uuid
    rid = (await request_deletion(t, "subject", ids["michael"], "admin")).json()["request_id"]
    await run_deletion(api, t, rid, timeout=240)
    purger = api.app.state.kg.deletion.graph
    ctx = ctx_of(t)
    again = await purger.purge(
        ctx,
        episode_uuids=[str(episode)],
        node_uuids=[ids["michael"]],
        edge_uuids=[],
        summary_nodes=[],
        reproject_nodes=[ids["sarah"]],
        reproject_edges=[ids["em"]],
    )  # a retry after a crash replays exactly this, over what the first run already removed
    assert (again.episodes, again.nodes, again.edges) == (0, 0, 0)
    assert (await api.app.state.kg.knowledge.projector.reconcile(ctx)).drift == []
    assert detail["id"]
