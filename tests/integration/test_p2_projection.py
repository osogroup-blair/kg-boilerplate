"""WP7: projection. The zero-chat gate, graph == Postgres, outage recovery, idempotence, local_only, data minimisation, no content in telemetry."""

from __future__ import annotations

import json
import uuid

import pytest
import yaml
from kg_db.session import tenant_session
from kg_domain.principal import WorkspaceContext
from kg_graph.scoped import ScopedGraph
from kg_obs import metrics
from sqlalchemy import text

from tests.support.spy import SpyTransport
from tests.support.tenants import EXAMPLE_YAML, clear_graph, make_tenant

pytestmark = pytest.mark.integration
HR = lambda v: [{"system": "hr", "value": v}]  # noqa: E731


class Spied:
    """Swaps a spy under the platform transport for the duration of a test: every router request (gateway AND Graphiti) is recorded."""

    def __init__(self, api):  # type: ignore[no-untyped-def]
        self.transport = api.app.state.kg.router.transport
        self.original = self.transport._inner
        self.spy = SpyTransport(self.original)

    def __enter__(self) -> SpyTransport:
        self.transport._inner = self.spy
        return self.spy

    def __exit__(self, *exc: object) -> None:
        self.transport._inner = self.original


def ctx_of(t):  # type: ignore[no-untyped-def]
    return WorkspaceContext.for_system_task(t.ws, reason="test")


async def model_calls(api, t) -> list:  # type: ignore[no-untyped-def]
    await api.app.state.kg.call_sink.flush()
    async with tenant_session(api.engine, ctx_of(t)) as c:
        return (
            await c.execute(
                text("SELECT purpose, operation, classification, outcome FROM model_calls WHERE workspace_id = :w"), {"w": t.ws}
            )
        ).all()


async def story(t):  # type: ignore[no-untyped-def]
    """The demo's command sequence: 4 entities, evt_001, evt_002."""
    sarah = (
        await t.entity(
            "Person",
            "Sarah Johnson",
            external_ids=HR("E-1001"),
            attributes={"birth_date": "1985-03-02"},
            source_system="hr",
            source_ref="E-1001",
        )
    ).json()
    acme = (await t.entity("Organization", "Acme", external_ids=HR("ORG-1"), source_system="hr", source_ref="ORG-1")).json()
    michael = (await t.entity("Person", "Michael", source_system="hr", source_ref="E-2")).json()
    topic = (await t.entity("Topic", "Entrepreneurship", source_system="hr", source_ref="T-1")).json()
    f1 = (
        await t.relationship(
            "WORKS_AT",
            sarah["entity_id"],
            acme["entity_id"],
            valid_from="2022-01-10",
            attributes={"role": "Software Engineer"},
            source_system="hr",
            source_ref="evt_001",
        )
    ).json()
    f2 = (
        await t.relationship(
            "WORKS_AT",
            sarah["entity_id"],
            acme["entity_id"],
            valid_from="2024-06-01",
            attributes={"role": "Engineering Manager"},
            source_system="hr",
            source_ref="evt_002",
            source_recorded_at="2024-06-10",
        )
    ).json()
    return sarah, acme, michael, topic, f1, f2


async def test_the_zero_chat_gate_and_graph_equals_postgres(api, tenant):
    t = tenant
    with Spied(api) as spy:
        sarah, acme, michael, topic, f1, f2 = await story(t)
        paths = [r["path"] for r in spy.requests]
    assert [p for p in paths if p.endswith("/chat/completions")] == []  # ADR-016: deterministic writes never call an LLM
    assert paths.count("/v1/embeddings") >= 6  # 4 node names + 2 fact texts (plus the refreshed predecessor)
    calls = await model_calls(api, t)
    assert (
        calls
        and {c.purpose for c in calls} == {"graph_projection"}
        and {c.operation for c in calls} == {"embeddings"}
        and {c.outcome for c in calls} == {"ok"}
    )

    for r in (sarah, acme, michael, topic, f1, f2):
        assert r["projection_status"] == "projected", r
    graph = api.app.state.kg.graph.scoped(ctx_of(t))
    assert await graph.node_count() == 4
    edge1, edge2 = await graph.get_fact_edge(f1["fact_id"]), await graph.get_fact_edge(f2["fact_id"])
    assert (
        edge1.fact == "Sarah Johnson works at Acme as Software Engineer"
        and edge2.fact == "Sarah Johnson works at Acme as Engineering Manager"
    )
    assert (
        edge1.valid_at.isoformat().startswith("2022-01-10")
        and edge1.invalid_at.isoformat().startswith("2024-06-01")
        and edge2.invalid_at is None
    )
    assert edge1.expired_at is None and edge2.created_at.year >= 2026  # created_at is the platform's record time, never the source date
    assert edge2.attributes["source_command_id"] == f2["command_id"] and edge2.attributes["authority_class"] == "authoritative_system"
    assert edge2.attributes["ontology_version"] == "personal-knowledge@1.0.0"
    node = await graph.read_node(sarah["entity_id"])
    assert node is not None and node.labels == frozenset({"Person"}) and "birth_date" not in node.attributes  # restricted: Postgres only

    report = await api.app.state.kg.knowledge.projector.reconcile(ctx_of(t))
    assert (report.checked_entities, report.checked_facts, report.drift, report.pending) == (4, 2, [], 0)
    detail = (await t.get(f"/v1/commands/{f2['command_id']}", "reviewer")).json()
    assert detail["projection_status"] == "projected" and detail["projection_attempts"] >= 1
    assert {e["projection_status"] for e in (await t.get("/v1/entities", "reader")).json()["items"]} == {"projected"}


async def test_reconcile_detects_and_repairs_each_kind_of_drift(api, tenant):
    t = tenant
    sarah, acme, _, _, f1, f2 = await story(t)
    graph: ScopedGraph = api.app.state.kg.graph.scoped(ctx_of(t))
    projector = api.app.state.kg.knowledge.projector
    await graph.run_cypher("MATCH (n:Entity {group_id: $group_id, uuid: $u}) SET n.name = 'Tampered'", u=sarah["entity_id"])
    await graph.delete_edge(f1["fact_id"])
    await graph.run_cypher(
        "MATCH (:Entity {group_id: $group_id})-[e:RELATES_TO {uuid: $u}]->(:Entity) SET e.invalid_at = null", u=f2["fact_id"]
    )
    orphan = str(uuid.uuid4())
    with __import__("kg_modelgw.egress", fromlist=["egress"]).egress(
        classification="internal", purpose="graph_projection", workspace_id=str(t.ws)
    ):
        import datetime as dt

        await graph.upsert_entity_node(uuid=orphan, name="Ghost", type_key="Person", attributes={}, created_at=dt.datetime.now(dt.UTC))
    report = await projector.reconcile(ctx_of(t))
    found = {(d.kind, d.id, d.problem): d.fields for d in report.drift}
    assert found == {
        ("node", sarah["entity_id"], "mismatch"): ["name"],
        ("edge", f1["fact_id"], "missing"): [],
        ("node", orphan, "orphan"): [],
    }  # f2 matches: invalid_at was already null
    fixed = await projector.reconcile(ctx_of(t), repair=True)
    assert fixed.repaired == 3
    clean = await projector.reconcile(ctx_of(t))
    assert (
        clean.drift == []
        and (await graph.read_node(orphan)) is None
        and (await graph.read_node(sarah["entity_id"])).name == "Sarah Johnson"
    )  # type: ignore[union-attr]
    # a changed end date is caught as an edge mismatch
    await graph.run_cypher(
        "MATCH (:Entity {group_id: $group_id})-[e:RELATES_TO {uuid: $u}]->(:Entity) SET e.invalid_at = null", u=f1["fact_id"]
    )
    drift = (await projector.reconcile(ctx_of(t))).drift
    assert [(d.kind, d.problem, d.fields) for d in drift] == [("edge", "mismatch", ["invalid_at"])]
    assert acme["entity_id"]


async def test_reprojection_is_idempotent(api, tenant):
    t = tenant
    sarah, _, _, _, f1, _ = await story(t)
    projector = api.app.state.kg.knowledge.projector
    graph = api.app.state.kg.graph.scoped(ctx_of(t))
    before = await graph.list_uuids()
    for _ in range(2):  # re-project the same state twice
        async with tenant_session(api.engine, ctx_of(t)) as c:
            for table in ("entities", "facts"):
                await c.execute(
                    text(
                        f"UPDATE {table} SET projection_status = 'pending', projected_revision = NULL, projection_next_at = now() WHERE workspace_id = :w"
                    ),  # noqa: S608
                    {"w": t.ws},
                )
        assert await projector.sweep_workspace(ctx_of(t)) == 6
    assert await graph.list_uuids() == before and (await projector.reconcile(ctx_of(t))).drift == []
    assert await projector.project_entity(ctx_of(t), uuid.UUID(sarah["entity_id"])) == "projected"  # nothing to do when already current
    assert await projector.project_fact(ctx_of(t), uuid.UUID(f1["fact_id"])) == "projected"


async def test_a_graph_outage_answers_202_then_the_sweeper_catches_up(api, tenant, monkeypatch):
    from neo4j.exceptions import ServiceUnavailable

    t = tenant
    real = ScopedGraph.upsert_entity_node

    async def down(self, **kw):  # type: ignore[no-untyped-def]
        raise ServiceUnavailable("neo4j is down")

    monkeypatch.setattr(ScopedGraph, "upsert_entity_node", down)
    r = await t.entity("Person", "Sarah", external_ids=HR("E-1"), source_system="hr", source_ref="E-1")
    assert (
        r.status_code == 202
        and r.json()["projection_status"] == "pending"
        and r.headers["location"] == f"/v1/commands/{r.json()['command_id']}"
    )
    got = (
        await t.get(f"/v1/entities/{r.json()['entity_id']}", "reader")
    ).json()  # Postgres is authoritative: the read works during the outage
    assert got["name"] == "Sarah" and got["projection_status"] == "pending"
    cmd = (await t.get(r.headers["location"], "reviewer")).json()
    assert cmd["projection_status"] == "pending" and cmd["last_error_code"] == "graph_unavailable"
    assert await api.app.state.kg.graph.scoped(ctx_of(t)).node_count() == 0
    # backoff: the row is not due immediately, so a sweep right now does nothing
    assert await api.app.state.kg.knowledge.projector.sweep_workspace(ctx_of(t)) == 0

    monkeypatch.setattr(ScopedGraph, "upsert_entity_node", real)
    async with tenant_session(api.engine, ctx_of(t)) as c:  # let the backoff elapse
        await c.execute(text("UPDATE entities SET projection_next_at = now() WHERE workspace_id = :w"), {"w": t.ws})
    assert await api.app.state.kg.knowledge.projector.sweep_workspace(ctx_of(t)) == 1
    after = (await t.get(f"/v1/commands/{r.json()['command_id']}", "reviewer")).json()
    assert after["projection_status"] == "projected" and after["last_error_code"] is None
    assert await api.app.state.kg.graph.scoped(ctx_of(t)).node_count() == 1


async def test_a_failure_that_will_not_heal_ends_as_failed_and_can_be_retried(api, tenant, monkeypatch):
    t = tenant
    from kg_knowledge import projection

    async def broken(self, **kw):  # type: ignore[no-untyped-def]
        raise ValueError("boom")

    real = ScopedGraph.upsert_entity_node
    monkeypatch.setattr(ScopedGraph, "upsert_entity_node", broken)
    r = await t.entity("Person", "Sarah", source_system="hr", source_ref="E-1")
    projector = api.app.state.kg.knowledge.projector
    for _ in range(projection.MAX_ATTEMPTS):
        async with tenant_session(api.engine, ctx_of(t)) as c:
            await c.execute(
                text("UPDATE entities SET projection_next_at = now() WHERE workspace_id = :w AND projection_status = 'pending'"),
                {"w": t.ws},
            )
        await projector.sweep_workspace(ctx_of(t))
    cmd = (await t.get(f"/v1/commands/{r.json()['command_id']}", "reviewer")).json()
    assert cmd["projection_status"] == "failed" and cmd["last_error_code"] == "unexpected_error"
    monkeypatch.setattr(ScopedGraph, "upsert_entity_node", real)
    assert await projector.retry_command(ctx_of(t), uuid.UUID(r.json()["command_id"])) == "projected"


async def test_an_endpoint_that_is_not_projected_yet_defers_the_fact(api, tenant, monkeypatch):
    from kg_graph.errors import EndpointNotProjected

    t = tenant
    sarah = (await t.entity("Person", "Sarah", source_system="x", source_ref="1")).json()["entity_id"]
    acme = (await t.entity("Organization", "Acme", external_ids=HR("O"), source_system="x", source_ref="2")).json()["entity_id"]
    real = ScopedGraph.upsert_fact_edge

    async def missing(self, **kw):  # type: ignore[no-untyped-def]
        raise EndpointNotProjected()

    monkeypatch.setattr(ScopedGraph, "upsert_fact_edge", missing)
    r = await t.relationship("WORKS_AT", sarah, acme, valid_from="2022-01-01", source_system="hr", source_ref="e")
    assert r.status_code == 202 and r.json()["projection_status"] == "pending"
    monkeypatch.setattr(ScopedGraph, "upsert_fact_edge", real)
    async with tenant_session(api.engine, ctx_of(t)) as c:
        await c.execute(text("UPDATE facts SET projection_next_at = now() WHERE workspace_id = :w"), {"w": t.ws})
    await api.app.state.kg.knowledge.projector.sweep_workspace(ctx_of(t))
    assert (await t.get(f"/v1/commands/{r.json()['command_id']}", "reviewer")).json()["projection_status"] == "projected"


async def test_a_local_only_entity_is_never_sent_anywhere_and_never_projected(api):
    d = yaml.safe_load(EXAMPLE_YAML)
    d["entity_types"]["Person"]["allowed_classifications"].append("local_only")
    t = await make_tenant(api, ontology=yaml.safe_dump(d))
    try:
        with Spied(api) as spy:
            secret = await t.entity("Person", "Hidden Person", classification="local_only", source_system="hr", source_ref="s")
            assert secret.status_code == 201 and secret.json()["projection_status"] == "skipped_by_policy"
            acme = (await t.entity("Organization", "Acme", external_ids=HR("O"), source_system="x", source_ref="2")).json()["entity_id"]
            fact = await t.relationship(
                "WORKS_AT", secret.json()["entity_id"], acme, valid_from="2022-01-01", source_system="hr", source_ref="e"
            )
            assert (
                fact.status_code == 201 and fact.json()["projection_status"] == "skipped_by_policy"
            )  # the edge would need a node that cannot exist
            sent = json.dumps([r["body"] for r in spy.requests])
        assert "Hidden Person" not in sent  # nothing about it reached the router (PJ-02: a node cannot be saved without an embedding)
        graph = api.app.state.kg.graph.scoped(ctx_of(t))
        assert (
            await graph.read_node(secret.json()["entity_id"]) is None and await graph.get_fact_edge_or_none(fact.json()["fact_id"]) is None
            if hasattr(graph, "get_fact_edge_or_none")
            else True
        )
        assert await graph.node_count() == 1  # only Acme
        got = (await t.get(f"/v1/entities/{secret.json()['entity_id']}", "reader")).json()
        assert (
            got["projection_status"] == "skipped_by_policy"
            and got["embedding_status"] == "skipped_by_policy"
            and got["name"] == "Hidden Person"
        )  # Postgres still serves it
        report = await api.app.state.kg.knowledge.projector.reconcile(ctx_of(t))
        assert report.drift == [] and report.skipped_by_policy == 2
    finally:
        await clear_graph(api, t.group_id)


async def test_restricted_attributes_never_reach_neo4j_or_the_router(api, tenant):
    t = tenant
    sentinel = "SENTINEL-1985-03-02-xyzzy"
    d = yaml.safe_load(EXAMPLE_YAML)
    d["ontology"]["version"] = "1.1.0"
    d["entity_types"]["Person"]["attributes"]["ssn_like"] = {"type": "string", "sensitive": True}
    pub = await t.req("POST", "/v1/ontologies/versions", "admin", json={"content": json.loads(json.dumps(d, default=str))})
    assert (await t.req("POST", f"/v1/ontologies/versions/{pub.json()['id']}/publish", "admin", json={})).status_code == 200
    with Spied(api) as spy:
        r = await t.entity("Person", "Carol", attributes={"ssn_like": sentinel}, source_system="hr", source_ref="c")
        assert r.status_code == 201 and r.json()["projection_status"] == "projected"
        sent = json.dumps([x["body"] for x in spy.requests])
    assert sentinel not in sent
    graph = api.app.state.kg.graph.scoped(ctx_of(t))
    rows = await graph.run_cypher("MATCH (n {group_id: $group_id}) RETURN properties(n) AS p")  # every node in the group, every property
    assert rows and sentinel not in json.dumps(rows, default=str)
    assert (await t.get(f"/v1/entities/{r.json()['entity_id']}", "admin")).json()["attributes"][
        "ssn_like"
    ] == sentinel  # still in Postgres, for admins


async def test_no_content_appears_in_logs_metrics_audit_or_model_calls(api, tenant, capsys):
    from kg_obs.logging import configure_logging

    t = tenant
    name, role, attr = "ZQ-Name-Sentinel", "ZQ-Role-Sentinel", "ZQ-Birth-Sentinel"
    configure_logging(
        "DEBUG", service="test", json_output=True
    )  # even at DEBUG nothing from the pipeline, the graph driver or the router client may leak
    sarah = (
        await t.entity("Person", name, attributes={"birth_date": "1985-03-02", "unknown": attr}, source_system="hr", source_ref="E-1")
    ).json()
    sarah = (await t.entity("Person", name, external_ids=HR("E-9"), source_system="hr", source_ref="E-1")).json()
    acme = (await t.entity("Organization", "Acme", external_ids=HR("O"), source_system="x", source_ref="2")).json()["entity_id"]
    await t.relationship(
        "WORKS_AT", sarah["entity_id"], acme, valid_from="2022-01-01", attributes={"role": role}, source_system="hr", source_ref="e"
    )
    await t.relationship(
        "WORKS_AT", sarah["entity_id"], acme, valid_from="2020-01-01", attributes={"role": role}, source_system="hr", source_ref="e"
    )  # rejected
    await api.app.state.kg.call_sink.flush()
    out = capsys.readouterr()
    configure_logging("WARNING", service="test", json_output=True)
    sources = {
        "logs": out.out + out.err,
        "metrics": metrics.render().decode(),
        "audit": json.dumps((await t.get("/v1/audit", "admin", limit=100)).json()),
        "model_calls": json.dumps([tuple(r) for r in await model_calls(api, t)]),
    }
    async with tenant_session(api.engine, ctx_of(t)) as c:
        sources["audit_table"] = json.dumps(
            [r[0] for r in (await c.execute(text("SELECT metadata::text FROM audit_log WHERE workspace_id = :w"), {"w": t.ws})).all()]
        )
        sources["decisions"] = json.dumps(
            [r[0] for r in (await c.execute(text("SELECT decision::text FROM commands WHERE workspace_id = :w"), {"w": t.ws})).all()]
        )
    for where, blob in sources.items():
        for sentinel in (name, role, attr, "1985-03-02"):
            assert sentinel not in blob, (where, sentinel)


async def test_the_sweeper_survives_a_workspace_it_cannot_read(api, tenant, monkeypatch):
    """sweep_once is system-wide: a workspace with unreadable data (here: a version whose spec no longer parses) is skipped, not fatal."""
    t = tenant
    r = await t.entity("Person", "Sarah", source_system="hr", source_ref="E-1")
    assert r.status_code == 201
    async with tenant_session(api.engine, ctx_of(t)) as c:
        await c.execute(
            text(
                "UPDATE entities SET projection_status = 'pending', projected_revision = NULL, projection_next_at = now() WHERE workspace_id = :w"
            ),
            {"w": t.ws},
        )
    projector = api.app.state.kg.knowledge.projector
    real = projector.sweep_workspace

    async def flaky(ctx, limit=25):  # type: ignore[no-untyped-def]
        if ctx.workspace_id != t.ws:
            raise RuntimeError("another workspace is broken")
        return await real(ctx, limit)

    monkeypatch.setattr(projector, "sweep_workspace", flaky)
    assert await projector.sweep_once(workspaces=1000) >= 1  # the healthy tenant was still projected
    assert (await t.get(f"/v1/commands/{r.json()['command_id']}", "reviewer")).json()["projection_status"] == "projected"


async def test_an_unreadable_ontology_is_recorded_as_a_failure_not_an_exception(api, tenant):
    t = tenant
    r = await t.entity("Person", "Sarah", source_system="hr", source_ref="E-1")
    projector = api.app.state.kg.knowledge.projector
    projector._specs._items.clear()
    async with tenant_session(api.engine, WorkspaceContext.for_system_task(t.ws, reason="test")) as c:
        await c.execute(
            text("UPDATE entities SET projection_status = 'pending', projected_revision = NULL WHERE workspace_id = :w"), {"w": t.ws}
        )
    from kg_knowledge.specs import SpecCache

    class Broken(SpecCache):
        async def get(self, conn, ws, version_id):  # type: ignore[no-untyped-def]
            raise ValueError("cannot parse")

    healthy = projector._specs
    projector._specs = Broken()
    try:
        assert await projector.project_entity(ctx_of(t), uuid.UUID(r.json()["entity_id"])) == "failed"
    finally:
        projector._specs = healthy
    assert (await projector.refresh_command(ctx_of(t), uuid.UUID(r.json()["command_id"]))) == "failed"
    assert (await t.get(f"/v1/commands/{r.json()['command_id']}", "reviewer")).json()["last_error_code"] == "ontology_unreadable"


async def test_the_projection_log_line_carries_the_ontology_version_and_no_content(api, tenant, capsys):
    """ONT-03 (partial): every projection records which ontology version it ran under, in the log as well as on the rows."""
    from kg_obs.logging import configure_logging

    t = tenant
    configure_logging("INFO", service="test", json_output=True)
    try:
        sarah, acme, _, _, f1, _ = await story(t)
    finally:
        out = capsys.readouterr().out
        configure_logging("WARNING", service="test", json_output=True)
    lines = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    projected = [line for line in lines if line.get("event") in ("entity_projected", "fact_projected")]
    assert {line["event"] for line in projected} == {"entity_projected", "fact_projected"}
    assert {line["ontology_version"] for line in projected} == {"personal-knowledge@1.0.0"}
    assert "Sarah" not in out and "Acme" not in out
    async with tenant_session(api.engine, ctx_of(t)) as c:  # and the rows carry the version they ran under
        versions = {
            r[0] for r in (await c.execute(text("SELECT ontology_version_id FROM commands WHERE workspace_id = :w"), {"w": t.ws})).all()
        }
    assert versions == {uuid.UUID(sarah["ontology_version_id"])} and f1["ontology_version_id"] == acme["ontology_version_id"]
