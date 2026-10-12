"""P4 WP6: the review queue through the real API: every decision path, the ledger/audit trail, evidence reads, merge semantics (KGR-01/02/06)."""

from __future__ import annotations

import json
import uuid

import pytest

from tests.support.p4 import CYCLIST, NEWCO, ctx_of, decide, reviews, run_event, sarah_story, sql

pytestmark = pytest.mark.integration


async def cyclist_world(api, t):  # type: ignore[no-untyped-def]
    ids = await sarah_story(t)
    detail = await run_event(api, t, CYCLIST)
    (item,) = await reviews(t, type="identity_match")
    (fork,) = await sql(api, t, "SELECT id::text AS id FROM entities WHERE workspace_id = :ws AND status = 'candidate'")
    return ids, detail, item, fork.id


async def newco_world(api, t):  # type: ignore[no-untyped-def]
    ids = await sarah_story(t)
    detail = await run_event(api, t, NEWCO, subjects=[ids["sarah"]])
    (item,) = await reviews(t, type="contradiction")
    return ids, detail, item


async def test_different_entity_keeps_two_sarahs_and_releases_the_held_facts(api, tenant, strict_mock):
    t = tenant
    ids, detail, item, fork = await cyclist_world(api, t)
    assert item["status"] == "open" and item["subject_entity_id"] == ids["sarah"] and item["candidate_entity_id"] == fork
    got = await t.get(f"/v1/reviews/{item['id']}", "reviewer")
    d = got.json()
    assert d["allowed_decisions"] == ["same_entity", "different_entity", "needs_more_evidence"]
    assert {e["id"] for e in d["entities"]} == {ids["sarah"], fork} and d["candidates"][0]["entity_id"] == ids["sarah"]
    assert [i["interaction_id"] for i in d["interactions"]] == [detail["id"]]

    r = await decide(
        t, item["id"], "different_entity", rationale="different_real_world_entity", text_="She lives in Lyon. SENTINEL-RATIONALE"
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert (
        out["status"] == "decided"
        and out["decision"] == "different_entity"
        and out["command_id"]
        and out["projection_status"] == "projected"
    )
    two = await sql(api, t, "SELECT status FROM entities WHERE workspace_id = :ws AND display_name = 'Sarah Johnson'")
    assert sorted(x.status for x in two) == ["active", "active"]
    (nm,) = await sql(
        api, t, "SELECT entity_a::text AS a, entity_b::text AS b, reason_code FROM identity_non_matches WHERE workspace_id = :ws"
    )
    assert {nm.a, nm.b} == {ids["sarah"], fork} and nm.reason_code == "different_real_world_entity"
    (fact,) = await sql(api, t, "SELECT status, decision_code FROM facts WHERE workspace_id = :ws AND source_type = 'extracted'")
    assert fact.status == "active"  # the hold ended: nothing conflicts
    # the decision is a ledger command, audited, and the rationale TEXT is stored on the item only
    (cmd,) = await sql(
        api,
        t,
        "SELECT command_type, status, actor_principal_id::text AS a, payload FROM commands WHERE workspace_id = :ws AND command_type = 'review.decide'",
    )
    assert cmd.status == "applied" and "SENTINEL-RATIONALE" not in json.dumps(cmd.payload)
    audit = await sql(
        api,
        t,
        "SELECT action, metadata::text AS m FROM audit_log WHERE workspace_id = :ws AND action IN ('review.created','review.decided')",
    )
    assert {a.action for a in audit} == {"review.created", "review.decided"} and all("SENTINEL-RATIONALE" not in a.m for a in audit)
    decided = (await t.get(f"/v1/reviews/{item['id']}", "reviewer")).json()
    assert (
        decided["status"] == "decided" and decided["rationale_text"].endswith("SENTINEL-RATIONALE") and decided["allowed_decisions"] == []
    )
    assert (await api.app.state.kg.knowledge.projector.reconcile(ctx_of(t))).drift == []
    assert [i["status"] for i in await reviews(t, status="open")] == []


async def test_same_entity_merges_with_lineage_and_the_old_id_still_resolves(api, tenant, strict_mock):
    """KGR-01: the canonical id is stable; the merged id answers 200 with merged_into_id; aliases, facts and mentions follow; the graph is rebuilt with the same uuids."""
    t = tenant
    ids, detail, item, fork = await cyclist_world(api, t)
    (fact_before,) = await sql(api, t, "SELECT id::text AS id FROM facts WHERE workspace_id = :ws AND source_type = 'extracted'")
    r = await decide(t, item["id"], "same_entity", rationale="same_real_world_entity")
    assert r.status_code == 200, r.text
    merged = (await t.get(f"/v1/entities/{fork}", "reader")).json()
    assert merged["status"] == "merged" and merged["merged_into_id"] == ids["sarah"]
    sarah = (await t.get(f"/v1/entities/{ids['sarah']}", "admin")).json()
    assert sarah["status"] == "active" and sarah["id"] == ids["sarah"]
    fact = next(f for f in sarah["relationships"] if f["id"] == fact_before.id)
    assert fact["source"]["id"] == ids["sarah"] and fact["status"] in ("active", "proposed")
    mentions = (await t.get(f"/v1/entities/{ids['sarah']}/mentions", "reader")).json()
    assert [m["interaction_id"] for m in mentions] == [detail["id"]]
    graph = api.app.state.kg.graph.scoped(ctx_of(t))
    assert await graph.read_node(fork) is None  # the merged node is gone
    edge = await graph.read_edge(fact_before.id)
    assert edge is not None and edge.source_uuid == ids["sarah"]  # the same uuid, the new endpoint
    assert (await api.app.state.kg.knowledge.projector.reconcile(ctx_of(t))).drift == []
    listed = (await t.get("/v1/entities", "reader", type="Person")).json()["items"]
    assert fork not in [e["id"] for e in listed]  # a merged entity is not a directory entry


async def test_decision_preconditions_idempotency_and_roles(api, tenant, strict_mock):
    t = tenant
    ids, detail, item, fork = await cyclist_world(api, t)
    path = f"/v1/reviews/{item['id']}/decision"
    body = {"decision": "different_entity", "rationale_code": "other"}
    assert (await t.req("POST", path, "reviewer", json=body)).status_code == 428  # If-Match is required
    assert (await t.req("POST", path, "reviewer", json=body, headers={"if-match": 'W/"rev-99"'})).status_code == 412
    assert (
        await t.req(
            "POST", path, "reviewer", json={"decision": "keep_existing", "rationale_code": "other"}, headers={"if-match": 'W/"rev-1"'}
        )
    ).status_code == 422
    assert (
        await t.req("POST", path, "reviewer", json={**body, "rationale_code": "free text"}, headers={"if-match": 'W/"rev-1"'})
    ).status_code == 422
    for who in ("app", "agent", "reader", "operator"):
        assert (await t.req("POST", path, who, json=body, headers={"if-match": 'W/"rev-1"'})).status_code == 403
    key = f"decide-{uuid.uuid4()}"
    first = await decide(t, item["id"], "different_entity", key=key)
    again = await decide(t, item["id"], "different_entity", key=key, revision='W/"rev-1"')
    assert first.status_code == again.status_code == 200 and again.headers["idempotent-replay"] == "true" and again.json() == first.json()
    assert (
        await decide(t, item["id"], "different_entity", key=key, rationale="duplicate_report", revision='W/"rev-1"')
    ).status_code == 409  # same key, different request
    late = await decide(t, item["id"], "same_entity", revision='W/"rev-2"')
    assert late.status_code == 409 and late.json()["error"]["code"] == "already_decided"
    assert (await sql(api, t, "SELECT count(*) AS n FROM commands WHERE workspace_id = :ws AND command_type = 'review.decide'"))[0].n == 1


async def test_needs_more_evidence_keeps_the_item_open_and_pushes_the_due_date(api, tenant, strict_mock):
    t = tenant
    ids, detail, item, fork = await cyclist_world(api, t)
    r = await decide(t, item["id"], "needs_more_evidence", rationale="insufficient_evidence")
    assert r.status_code == 200 and r.json()["status"] == "open" and r.json()["revision"] == 2
    again = (await t.get(f"/v1/reviews/{item['id']}", "reviewer")).json()
    assert again["status"] == "open" and again["revision"] == 2 and again["decision"] is None
    assert [e.status for e in await sql(api, t, "SELECT status FROM entities WHERE workspace_id = :ws AND id = :e", e=uuid.UUID(fork))] == [
        "candidate"
    ]


async def test_keep_existing_rejects_the_claim_and_acme_stays(api, tenant, strict_mock):
    t = tenant
    ids, detail, item = await newco_world(api, t)
    d = (await t.get(f"/v1/reviews/{item['id']}", "reviewer")).json()
    assert d["allowed_decisions"] == ["keep_existing", "mark_disputed", "accept_claim"]
    assert [f["id"] for f in d["facts"]][0] == ids["em"] and d["facts"][1]["status"] == "proposed"  # existing first, then the claim
    r = await decide(t, item["id"], "keep_existing", rationale="verified_with_source")
    assert r.status_code == 200, r.text
    claim = d["facts"][1]["id"]
    (row,) = await sql(api, t, "SELECT status, decision_code FROM facts WHERE workspace_id = :ws AND id = :id", id=uuid.UUID(claim))
    assert (row.status, row.decision_code) == ("rejected", "keep_existing")
    assert (await t.get(f"/v1/relationships/{ids['em']}", "reader")).json()["status"] == "active"
    assert (await api.app.state.kg.knowledge.projector.reconcile(ctx_of(t))).drift == []


async def test_mark_disputed_flags_both_facts_and_hides_them_from_the_current_view(api, tenant, strict_mock):
    """KGR-06."""
    t = tenant
    ids, detail, item = await newco_world(api, t)
    claim = (await t.get(f"/v1/reviews/{item['id']}", "reviewer")).json()["facts"][1]["id"]
    assert (await decide(t, item["id"], "mark_disputed", rationale="insufficient_evidence")).status_code == 200
    got = await sql(
        api,
        t,
        "SELECT id::text AS id, status FROM facts WHERE workspace_id = :ws AND id IN (:a, :b)",
        a=uuid.UUID(ids["em"]),
        b=uuid.UUID(claim),
    )
    assert {g.id: g.status for g in got} == {ids["em"]: "disputed", claim: "disputed"}
    current = (await t.get(f"/v1/entities/{ids['sarah']}", "reader")).json()["relationships"]
    assert ids["em"] not in [f["id"] for f in current]  # a disputed fact is not presented as current...
    shown = (await t.get(f"/v1/entities/{ids['sarah']}", "reader", include="disputed")).json()["relationships"]
    assert {f["id"] for f in shown if f["status"] == "disputed"} == {ids["em"], claim}  # ...but it is never hidden when asked for
    graph = api.app.state.kg.graph.scoped(ctx_of(t))
    assert (await graph.read_edge(ids["em"])).expired_at is not None  # type: ignore[union-attr]  (the standard search filter excludes it)
    assert (await api.app.state.kg.knowledge.projector.reconcile(ctx_of(t))).drift == []


async def test_accepting_a_claim_that_would_displace_a_verified_fact_is_a_correction(api, tenant, strict_mock):
    t = tenant
    ids, detail, item = await newco_world(api, t)
    r = await decide(t, item["id"], "accept_claim", rationale="verified_with_source")
    assert r.status_code == 409 and r.json()["error"]["code"] == "requires_correction"
    assert (await t.get(f"/v1/reviews/{item['id']}", "reviewer")).json()["status"] == "open"  # nothing was written
    assert (await t.get(f"/v1/relationships/{ids['em']}", "reader")).json()["status"] == "active"


async def test_a_graphiti_invalidation_with_no_competing_fact_can_be_acknowledged_but_not_corrected_yet(api, tenant, strict_mock):
    t = tenant
    ids = await sarah_story(t)
    # a review of this type is opened by the reconciler when a verified fact is touched with no claim to blame; create it the way the reconciler does
    ex = api.app.state.kg.knowledge.extracted
    from kg_db.session import tenant_session

    async with tenant_session(api.engine, ctx_of(t)) as conn:
        review_id, created = await ex.open_review(
            conn,
            t.ws,
            item_type="graphiti_invalidation",
            rule_kind="conflict_policy",
            reason_code="graphiti_invalidation",
            fact_id=uuid.UUID(ids["em"]),
        )
        assert created
        again, created_again = await ex.open_review(
            conn,
            t.ws,
            item_type="graphiti_invalidation",
            rule_kind="conflict_policy",
            reason_code="graphiti_invalidation",
            fact_id=uuid.UUID(ids["em"]),
        )
        assert (again, created_again) == (review_id, False)  # a replay never opens a second item for the same thing
    rid = str(review_id)
    corr = await decide(t, rid, "open_correction", rationale="incorrect_extraction")
    assert corr.status_code == 409 and corr.json()["error"]["code"] == "not_available"
    ack = await decide(t, rid, "acknowledge", rationale="no_action_needed")
    assert ack.status_code == 200 and ack.json()["status"] == "decided"


async def test_a_reviewer_reads_only_the_evidence_of_the_item_and_every_read_is_audited(api, tenant, strict_mock):
    t = tenant
    ids, detail, item, fork = await cyclist_world(api, t)
    other = await run_event(
        api,
        t,
        {
            "kind": "message",
            "content": "Sarah Johnson: I'm considering entrepreneurship, and Michael, who I've known for years, keeps encouraging me.",
        },
        subjects=[ids["sarah"], ids["michael"], ids["topic"]],
    )
    path = f"/v1/reviews/{item['id']}/evidence/{detail['id']}"
    r = await t.get(path, "reviewer")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert "cycling club" in r.json()["content"] and r.json()["form"] == "normalized"
    assert (
        await t.get(f"/v1/reviews/{item['id']}/evidence/{other['id']}", "reviewer")
    ).status_code == 404  # an interaction the item does not rest on
    assert (await t.get(f"/v1/reviews/{item['id']}/evidence/{uuid.uuid4()}", "reviewer")).status_code == 404
    for who in ("app", "agent", "reader", "operator"):
        assert (await t.get(path, who)).status_code == 403
    reads = await sql(api, t, "SELECT metadata::text AS m FROM audit_log WHERE workspace_id = :ws AND action = 'review.evidence_read'")
    assert len(reads) == 1 and detail["id"] in reads[0].m and "cycling" not in reads[0].m
    # the broad content read is still admin-only
    assert (await t.get(f"/v1/interactions/{detail['id']}/content", "reviewer", form="normalized")).status_code == 403
