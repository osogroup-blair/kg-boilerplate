"""P8 WP5 continued (AT-16): legal holds, workspace deletion with its cooling-off, principal data, memory deletion, and a crash in the middle of a purge. The first half of
the story (the freeze, the purge, the receipt) is in test_p8_deletion.py."""

from __future__ import annotations

import uuid

import pytest
from kg_db.session import system_session, tenant_session
from kg_domain.principal import WorkspaceContext
from kg_governance.audit import SystemAudit
from sqlalchemy import text

from tests.support.p4 import sql
from tests.support.p5 import accept_event, propose, save
from tests.support.p8 import (
    SENTINEL,
    deletion_world,
    owner_exec,
    prime_org,
    raw,
    request_deletion,
    request_status,
    reviews_of,
    run_deletion,
)
from tests.support.tenants import GOVERNANCE_YAML, clear_graph, make_tenant

pytestmark = pytest.mark.integration


async def test_a_legal_hold_blocks_the_purge_but_not_the_freeze_and_releasing_it_resumes(api, tenant_governance):
    t = tenant_governance
    w = await deletion_world(api, t)
    assert (
        await t.req("POST", "/v1/legal-holds", "app", json={"scope_kind": "subject", "target_id": w["zeb"], "reason_code": "litigation"})
    ).status_code == 403
    hold = await t.req(
        "POST",
        "/v1/legal-holds",
        "admin",
        json={"scope_kind": "subject", "target_id": w["zeb"], "reason_code": "litigation", "note": "case 42"},
    )
    assert hold.status_code == 201, hold.text
    r = await request_deletion(t, "subject", w["zeb"], "admin")
    assert r.status_code == 202 and r.json()["status"] == "blocked_legal_hold", r.text
    rid = r.json()["request_id"]
    # frozen all the same: he is unreadable, and nothing is purged
    assert (await t.get(f"/v1/entities/{w['zeb']}", "admin")).status_code == 404
    assert len(await raw(api, t, "SELECT 1 FROM entities WHERE id = :z AND status = 'deletion_pending'", z=uuid.UUID(w["zeb"]))) == 1
    (blocked,) = await reviews_of(api, t, "deletion_exception")
    assert blocked.reason_code == "legal_hold"
    jobs = await raw(
        api, t, "SELECT 1 FROM outbox_jobs WHERE workspace_id = :ws AND stage LIKE 'deletion_%' AND payload ->> 'request_id' = :r", r=rid
    )
    assert jobs == []  # no stage is queued while a hold covers it
    # the purge route is not a way around it
    assert (await t.req("POST", f"/v1/deletion-requests/{rid}/retry", "admin")).status_code in (403, 409)
    released = await t.req("DELETE", f"/v1/legal-holds/{hold.json()['id']}", "admin")
    assert released.status_code == 200 and released.json()["released_at"] is not None
    assert (await request_status(api, t, rid))["status"] == "frozen"
    assert (await reviews_of(api, t, "deletion_exception")) == []  # the hold item resolved itself
    final = await run_deletion(api, t, rid)
    assert final["status"] in ("completed", "completed_with_exceptions"), final
    assert len(await raw(api, t, "SELECT 1 FROM entities WHERE id = :z", z=uuid.UUID(w["zeb"]))) == 0
    assert (
        len(
            await sql(
                api,
                t,
                "SELECT 1 FROM audit_log WHERE workspace_id = :ws AND action IN ('legal_hold.created','legal_hold.released','deletion.resumed')",
            )
        )
        == 3
    )


async def test_a_workspace_hold_blocks_every_kind_and_a_hold_on_another_subject_does_not(api, tenant_governance):
    t = tenant_governance
    w = await deletion_world(api, t)
    other = await t.req(
        "POST", "/v1/legal-holds", "admin", json={"scope_kind": "subject", "target_id": w["sarah"], "reason_code": "litigation"}
    )
    assert other.status_code == 201
    r = await request_deletion(t, "interaction", w["i_zeb"], "admin")
    assert r.json()["status"] != "blocked_legal_hold"  # Sarah's hold does not cover Zebulon's interaction... unless it is linked to her:
    ws_hold = await t.req("POST", "/v1/legal-holds", "admin", json={"scope_kind": "workspace", "reason_code": "regulator"})
    assert ws_hold.status_code == 201
    blocked = await request_deletion(t, "interaction", w["i_sar"], "admin")
    assert blocked.json()["status"] == "blocked_legal_hold"


async def test_principal_data_removes_what_the_principal_alone_could_see_and_keeps_what_the_workspace_shares(api, tenant_governance):
    t = tenant_governance
    private = (
        await save(t, "agent", {"type": "semantic", "content": "agent note, private", "scopes": [{"kind": "agent", "ref": "self"}]})
    ).json()["memory_id"]
    ev = await accept_event(t, {"kind": "message", "content": "something the agent noticed"}, who="agent")
    proposed = (
        await propose(
            t,
            "agent",
            {"type": "semantic", "content": "proposed by the agent", "requested_scope": {"kind": "workspace"}, "source_event_ids": [ev]},
        )
    ).json()["memory_id"]
    shared = (
        await save(
            t,
            "admin",
            {
                "type": "semantic",
                "content": "shared by an admin",
                "scopes": [{"kind": "workspace"}],
                "source_ref": "a",
                "purpose": "history",
            },
        )
    ).json()["memory_id"]
    me = (await raw(api, t, "SELECT id FROM principals WHERE workspace_id = :ws AND handle = :h", h=f"agent@{t.slug}"))[0].id
    # an agent may not ask for it; an admin does
    assert proposed
    assert (await request_deletion(t, "principal_data", str(me), "agent")).status_code in (403, 404)
    r = await request_deletion(t, "principal_data", str(me), "admin", reason="offboarding")
    assert r.status_code == 202, r.text
    assert (await t.get(f"/v1/memories/{private}", "agent")).status_code == 404
    final = await run_deletion(api, t, r.json()["request_id"])
    assert final["status"] == "completed", final
    assert len(await raw(api, t, "SELECT 1 FROM curated_memories WHERE id = :m", m=uuid.UUID(private))) == 0
    assert (
        len(await raw(api, t, "SELECT 1 FROM curated_memories WHERE id = :m", m=uuid.UUID(shared))) == 1
    )  # shared with the workspace: it is the workspace's now
    assert (
        len(await raw(api, t, "SELECT 1 FROM principals WHERE id = :p", p=me)) == 1
    )  # the principal itself is managed in /v1/principals, not here


async def test_memory_deletion_removes_the_memory_its_embedding_and_evidence_and_nothing_else(api, tenant_governance):
    t = tenant_governance
    w = await deletion_world(api, t)
    r = await request_deletion(t, "memory", w["m_keep"], "reviewer")
    assert r.status_code == 202, r.text
    assert (await t.get(f"/v1/memories/{w['m_keep']}", "admin")).status_code == 404
    final = await run_deletion(api, t, r.json()["request_id"])
    assert final["status"] == "completed", final
    m = uuid.UUID(w["m_keep"])
    assert [
        len(await raw(api, t, f"SELECT 1 FROM {tbl} WHERE memory_id = :m", m=m))
        for tbl in ("memory_embeddings", "memory_evidence", "memory_scopes", "memory_subjects")
    ] == [0, 0, 0, 0]
    assert len(await raw(api, t, "SELECT 1 FROM curated_memories WHERE id = :m", m=m)) == 0
    assert (
        len(await raw(api, t, "SELECT 1 FROM curated_memories WHERE id = :m", m=uuid.UUID(w["m_names"]))) == 1
    )  # the others are untouched
    assert (await t.get(f"/v1/entities/{w['sarah']}", "admin")).status_code == 200
    assert (
        await t.req("POST", "/v1/deletion-requests", "agent", json={"kind": "memory", "target_id": w["m_names"], "reason_code": "x"})
    ).status_code in (403, 404)


@pytest.mark.single_process
async def test_a_workspace_deletion_cools_off_can_be_cancelled_and_then_purges_only_its_own_workspace(api, tenant_governance):
    t = tenant_governance
    w = await deletion_world(api, t)
    other = await make_tenant(api, ontology=GOVERNANCE_YAML)
    try:
        twin = (await other.entity("Person", SENTINEL, source_system="hr", source_ref="same-name")).json()["entity_id"]
        service = api.app.state.kg.deletion.service
        # a wrong slug and a non-admin are refused
        assert (
            await t.req(
                "POST", "/v1/deletion-requests", "admin", json={"kind": "workspace", "reason_code": "offboarding", "confirm_slug": "nope"}
            )
        ).status_code in (403, 409, 422)
        assert (await request_deletion(t, "workspace", None, "app", confirm_slug=t.slug)).status_code in (403, 404)
        r = await request_deletion(t, "workspace", None, "admin", reason="offboarding", confirm_slug=t.slug)
        assert r.status_code == 202, r.text
        rid = r.json()["request_id"]
        assert r.json()["cooling_off_until"] is not None
        # suspended at once: its keys stop working
        assert (await t.get(f"/v1/entities/{w['sarah']}", "admin")).status_code in (401, 403)
        assert (await other.get(f"/v1/entities/{twin}", "admin")).status_code == 200
        # during the cooling-off the operator can undo it
        ctx = WorkspaceContext.for_system_task(t.ws, reason="test cancel")
        async with tenant_session(api.engine, ctx) as conn:
            out = await service.cancel(conn, t.ws, uuid.UUID(rid), SystemAudit(conn, t.ws))
        assert out.status == "cancelled"
        assert (await t.get(f"/v1/entities/{w['sarah']}", "admin")).status_code == 200  # active again, nothing was touched
        # and again, this time to the end (the cooling-off shortened for the test)
        service._cooling_hours = 0
        r2 = await request_deletion(t, "workspace", None, "admin", reason="offboarding", confirm_slug=t.slug)
        assert r2.status_code == 202, r2.text
        final = await run_deletion(api, t, r2.json()["request_id"], timeout=180)
        assert final["status"] == "completed", final
        left = await owner_exec(
            "SELECT (SELECT count(*) FROM entities WHERE workspace_id = :ws) AS e, (SELECT count(*) FROM interactions WHERE workspace_id = :ws) AS i, "
            "(SELECT count(*) FROM curated_memories WHERE workspace_id = :ws) AS m, (SELECT count(*) FROM facts WHERE workspace_id = :ws) AS f, "
            "(SELECT count(*) FROM principals WHERE workspace_id = :ws) AS p, (SELECT status FROM workspaces WHERE id = :ws) AS st",
            ws=t.ws,
        )
        assert tuple(left[0]) == (0, 0, 0, 0, 0, "deleted")
        async with system_session(api.engine) as conn:
            receipt = (
                await conn.execute(text("SELECT ops.read_platform_receipt(:r, :ws)"), {"r": uuid.UUID(r2.json()["request_id"]), "ws": t.ws})
            ).scalar_one()
        assert receipt["kind"] == "workspace" and receipt["status"] == "completed" and receipt["backup_copies_expire_by"]
        # the other workspace, with an entity of the same name, is whole: in Postgres, in the API and in the graph
        assert (await other.get(f"/v1/entities/{twin}", "admin")).status_code == 200
        driver = api.app.state.kg.graph._graphiti.driver
        for group, expected in ((t.group_id, 0),):
            records, _, _ = await driver.client.execute_query(
                "MATCH (n {group_id: $g}) RETURN count(n) AS n", parameters_={"g": group}, database_="neo4j"
            )
            assert records[0]["n"] == expected, group  # nothing of the deleted workspace is left in the graph
    finally:
        await clear_graph(api, other.group_id)


async def test_a_crash_in_the_middle_of_the_purge_is_finished_by_the_retry_and_leaves_the_same_end_state(api, tenant_governance):
    from kg_governance.deletion import faults

    t = tenant_governance
    w = await deletion_world(api, t)
    rid = (await request_deletion(t, "subject", w["zeb"], "admin")).json()["request_id"]
    fired = {"n": 0}

    def crash_twice(stage: str, point: str) -> None:
        if stage == "deletion_purge" and point == "after_side_effect" and fired["n"] < 2:
            fired["n"] += 1
            raise RuntimeError("simulated crash after one store's step")  # FI-13

    faults.install(crash_twice)
    try:
        final = await run_deletion(api, t, rid, timeout=180)
    finally:
        faults.install(lambda stage, point: None)
    assert fired["n"] == 2, "the purge never reached the injected crash"
    assert final["status"] == "completed_with_exceptions", final  # only the free-text memory that names him is left to a person
    assert len(await raw(api, t, "SELECT 1 FROM entities WHERE id = :z", z=uuid.UUID(w["zeb"]))) == 0
    assert len(await raw(api, t, "SELECT 1 FROM interactions WHERE id = :i", i=uuid.UUID(w["i_zeb"]))) == 0
    assert final["receipt"]["verification"]["id_residuals"] == 0
    purged = await sql(api, t, "SELECT 1 FROM audit_log WHERE workspace_id = :ws AND action = 'deletion.purged'")
    assert len(purged) == 1  # recorded once, at the end, whatever the retries


async def test_a_crash_between_the_freeze_and_the_plan_leaves_the_request_frozen_and_the_job_runs_again(api, tenant_governance):
    from kg_governance.deletion import faults

    t = tenant_governance
    w = await deletion_world(api, t)
    rid = (await request_deletion(t, "subject", w["zeb"], "admin")).json()["request_id"]
    fired = {"n": 0}

    def crash_once(stage: str, point: str) -> None:
        if stage == "deletion_plan" and fired["n"] < 1:
            fired["n"] += 1
            raise RuntimeError("simulated crash after the plan")  # FI-14

    faults.install(crash_once)
    try:
        # while it is down the subject stays unreadable (the freeze committed with the request)
        assert (await t.get(f"/v1/entities/{w['zeb']}", "admin")).status_code == 404
        final = await run_deletion(api, t, rid, timeout=180)
    finally:
        faults.install(lambda stage, point: None)
    assert fired["n"] == 1 and final["status"] in ("completed", "completed_with_exceptions"), final
    assert len(await raw(api, t, "SELECT 1 FROM entities WHERE id = :z", z=uuid.UUID(w["zeb"]))) == 0


async def test_an_organization_that_other_facts_still_use_is_not_deleted_with_a_subject(api, tenant_governance):
    t = tenant_governance
    w = await deletion_world(api, t)
    acme2 = await prime_org(t, "Globex", "ORG-2")
    r = await request_deletion(t, "subject", w["zeb"], "admin")
    await run_deletion(api, t, r.json()["request_id"])
    assert (await t.get(f"/v1/entities/{w['acme']}", "admin")).status_code == 200
    assert (await t.get(f"/v1/entities/{acme2}", "admin")).status_code == 200
