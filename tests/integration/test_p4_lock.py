"""P4 WP4: the per-workspace graph writer lock (Q4) and the audited re-pin replay (Q10)."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from kg_knowledge.graphlock import GraphBusy

from tests.conftest import OWNER_URL
from tests.support.p4 import EVT003, ctx_of, ingest, sarah_story, sql, stage, wait_terminal
from tests.support.projection import project_due
from tests.support.tenants import EXAMPLE_YAML, clear_graph, make_tenant
from tests.support.worker import running_worker, wait_until

pytestmark = pytest.mark.integration


async def test_a_second_writer_gets_graph_busy_and_the_lock_is_released_afterwards(api, tenant):
    lock = api.app.state.kg.knowledge.lock
    async with lock.hold(tenant.ws):
        with pytest.raises(GraphBusy):
            async with lock.hold(tenant.ws, wait_seconds=0):
                pytest.fail("two writers held the same workspace's lock")
        async with lock.hold(uuid.uuid4(), wait_seconds=0):  # another workspace is not blocked
            pass
    async with lock.hold(tenant.ws, wait_seconds=0):  # released: free again
        pass


async def test_a_command_while_an_extraction_holds_the_lock_answers_202_and_the_job_projects_it(api, tenant):
    """FI-10 (in process): the fast path never waits long for the lock; the outbox job does the projection once the other writer is done."""
    t = tenant
    lock = api.app.state.kg.knowledge.lock
    async with lock.hold(t.ws):
        r = await t.entity("Person", "Sarah Johnson", source_system="hr", source_ref="E-1")
        assert r.status_code == 202 and r.json()["projection_status"] == "pending"
        assert await api.app.state.kg.graph.scoped(ctx_of(t)).node_count() == 0  # nothing was written while the lock was held
    async with running_worker(api, stages=("graph_projection",)):
        await wait_until(lambda: _projected(t, r.json()["command_id"]), what="the projection job")
    assert await api.app.state.kg.graph.scoped(ctx_of(t)).node_count() == 1


async def _projected(t, command_id):  # type: ignore[no-untyped-def]
    cmd = (await t.get(f"/v1/commands/{command_id}", "reviewer")).json()
    return cmd if cmd["projection_status"] == "projected" else None


async def test_concurrent_commands_and_an_extraction_in_one_workspace_never_corrupt_the_graph(api, tenant, strict_mock):
    t = tenant
    ids = await sarah_story(t)
    async with running_worker(api, concurrency=3):
        event = await ingest(t, EVT003, subjects=[ids["sarah"], ids["michael"], ids["topic"]])
        made = await asyncio.gather(
            *[t.entity("Person", f"Colleague {n}", source_system="hr", source_ref=f"E-{n}") for n in range(4)]
        )  # four command requests race the extraction for the same workspace's graph
        assert all(r.status_code in (201, 202) for r in made)
        await wait_terminal(t, event)

        async def settled() -> bool:
            rows = await sql(api, t, "SELECT count(*) AS n FROM outbox_jobs WHERE workspace_id = :ws AND status IN ('pending','leased')")
            return rows[0].n == 0

        await wait_until(settled, timeout=120.0, what="the outbox to drain")
    await project_due(api, t.ws)
    report = await api.app.state.kg.knowledge.projector.reconcile(ctx_of(t))
    assert report.drift == [] and report.pending == 0, (report.drift, report.pending)
    nodes, _ = await api.app.state.kg.graph.scoped(ctx_of(t)).list_uuids()
    assert len(nodes) == 8  # 4 from the story + 4 colleagues; the extraction added no node and duplicated none


async def test_replay_with_repin_processes_an_event_accepted_before_the_stage_existed(api, tenant, strict_mock):
    """Q10: a p3.v1 event has graph_ingest `skipped_disabled`. An audited replay with --repin moves THAT stage to the current pipeline and ontology."""
    from sqlalchemy.ext.asyncio import create_async_engine

    t = tenant
    ids = await sarah_story(t)
    async with running_worker(api, concurrency=2):
        event = await ingest(t, EVT003, subjects=[ids["sarah"], ids["michael"], ids["topic"]])
        await wait_terminal(t, event)
    owner = create_async_engine(OWNER_URL)
    try:
        async with owner.begin() as c:  # make the event look like one accepted under p3.v1: the stage was never run
            await c.execute(text_("DELETE FROM entity_mentions WHERE interaction_id = :i"), {"i": uuid.UUID(event)})
            await c.execute(text_("DELETE FROM fact_evidence WHERE interaction_id = :i"), {"i": uuid.UUID(event)})
            await c.execute(text_("DELETE FROM facts WHERE source_interaction_id = :i"), {"i": uuid.UUID(event)})
            await c.execute(text_("DELETE FROM graph_episodes WHERE interaction_id = :i"), {"i": uuid.UUID(event)})
            await c.execute(
                text_(
                    "UPDATE interaction_stages SET status = 'skipped_disabled', pipeline_version = 'p3.v1', generation = 0, result = '{}'::jsonb WHERE interaction_id = :i AND stage = 'graph_ingest'"
                ),
                {"i": uuid.UUID(event)},
            )
    finally:
        await owner.dispose()
    plain = await t.req(
        "POST", f"/v1/interactions/{event}/replay", "operator", json={"stage": "graph_ingest", "reason_code": "operator_request"}
    )
    assert plain.status_code == 422  # without --repin a disabled stage cannot be replayed
    async with running_worker(api, concurrency=2):
        r = await t.req(
            "POST",
            f"/v1/interactions/{event}/replay",
            "operator",
            json={"stage": "graph_ingest", "reason_code": "operator_request", "repin": True},
        )
        assert r.status_code == 202, r.text
        assert r.json()["replayed"][0]["generation"] == 1
        detail = await wait_until(lambda: _indexed(t, event), what="the re-pinned event to index")
    gi = stage(detail, "graph_ingest")
    assert gi["status"] == "succeeded" and gi["result"]["facts"] == {"active": 2}
    audit = await sql(
        api,
        t,
        "SELECT action, metadata::text AS m FROM audit_log WHERE workspace_id = :ws AND action IN ('interaction.repinned','interaction.replayed') ORDER BY created_at",
    )
    assert [a.action for a in audit] == ["interaction.repinned", "interaction.replayed"]
    (pin,) = await sql(
        api,
        t,
        "SELECT pipeline_version, ontology_version_id::text AS o FROM interaction_stages WHERE interaction_id = :i AND stage = 'graph_ingest'",
        i=uuid.UUID(event),
    )
    active = (await t.get("/v1/ontologies/active")).json()["version"]["id"]
    assert (pin.pipeline_version, pin.o) == ("p4.v1", active)
    assert (await t.req("POST", f"/v1/interactions/{event}/replay", "reader", json={"reason_code": "x", "repin": True})).status_code == 403


async def _indexed(t, event):  # type: ignore[no-untyped-def]
    d = (await t.get(f"/v1/interactions/{event}", "admin")).json()
    return d if d["processing_status"] == "indexed" and stage(d, "graph_ingest")["status"] == "succeeded" else None


async def test_an_event_accepted_without_an_ontology_is_skipped_and_can_be_repinned_once_one_is_active(api, strict_mock):
    t = await make_tenant(api, ontology=None)
    try:
        async with running_worker(api, concurrency=2):
            event = await ingest(
                t,
                {
                    "kind": "message",
                    "content": "Sarah Johnson: I'm considering entrepreneurship, and Michael, who I've known for years, keeps encouraging me.",
                },
            )
            first = await wait_terminal(t, event)
        gi = stage(first, "graph_ingest")
        assert (
            gi["status"] == "skipped_by_policy"
            and gi["last_error_code"] == "no_active_ontology"
            and first["processing_status"] == "indexed"
        )
        assert (
            await t.req("POST", f"/v1/interactions/{event}/replay", "operator", json={"reason_code": "operator_request", "repin": True})
        ).status_code == 409  # still none
        from kg_cli.seed import publish_ontology

        assert await publish_ontology(api.engine, EXAMPLE_YAML, slug=t.slug)
        async with running_worker(api, concurrency=2):
            r = await t.req("POST", f"/v1/interactions/{event}/replay", "operator", json={"reason_code": "operator_request", "repin": True})
            assert r.status_code == 202, r.text
            detail = await wait_until(lambda: _indexed(t, event), what="the repinned event")
        assert stage(detail, "graph_ingest")["generation"] == 1
    finally:
        await clear_graph(api, t.group_id)


def text_(sql_: str):  # type: ignore[no-untyped-def]
    from sqlalchemy import text

    return text(sql_)
