"""P8 WP2/WP3: corrections and the identity split over the real stack (API -> Postgres -> outbox -> graph), AT-13, ADM-03 groundwork and the preview.

Every correction is checked three ways: the answer (status, headers), Postgres (the row, the correction record, the audit entry) and the graph (the edge or node agrees)."""

from __future__ import annotations

import uuid

import pytest

from tests.support.p4 import BOARD, HR, run_event, sarah_story, sql, strict_mock  # noqa: F401
from tests.support.p5 import save
from tests.support.p6 import ok_build, texts
from tests.support.p8 import (
    audit_rows,
    correct_entity,
    correct_fact,
    correct_memory,
    edge_of,
    entity_rev,
    fact_rev,
    node_of,
    prime_org,
    reconcile_clean,
    reviews_of,
)

pytestmark = pytest.mark.integration


async def acme_labs_story(t):  # type: ignore[no-untyped-def]
    """AT-13's world: HR (a system of record) wrongly says Sarah works at "Acme Labs"; Acme is the real employer."""
    sarah = (await t.entity("Person", "Sarah Johnson", external_ids=HR("E-1001"), source_system="hr", source_ref="E-1001")).json()[
        "entity_id"
    ]
    acme = await prime_org(t, "Acme", "ORG-1")
    labs = await prime_org(t, "Acme Labs", "ORG-2")
    for who in ("agent", "agent2"):
        await t.grant(who, "subject", sarah)
    bad = (
        await t.relationship(
            "WORKS_AT",
            sarah,
            labs,
            valid_from="2022-01-10",
            attributes={"role": "Software Engineer"},
            source_system="hr",
            source_ref="evt_001",
        )
    ).json()
    assert bad["projection_status"] == "projected", bad
    return {"sarah": sarah, "acme": acme, "labs": labs, "bad": bad["fact_id"]}


async def test_at13_an_admin_supersedes_a_wrong_employer_and_the_context_and_the_audit_follow(api, tenant_governance):
    t = tenant_governance
    ids = await acme_labs_story(t)
    before = await ok_build(t, "reviewer", "Where does Sarah work?", subject_ids=[ids["sarah"]], profile="quick_answer")
    assert "Acme Labs" in texts(before)  # the wrong state, as the context shows it
    preview = await t.get(f"/v1/relationships/{ids['bad']}/corrections/preview", "admin", kind="supersede")
    assert (
        preview.status_code == 200
        and preview.json()["authoritative"] is True
        and preview.json()["graph_operations"] == ["expire_edge", "create_edge"]
    )
    r = await correct_fact(
        t,
        ids["bad"],
        "supersede",
        "admin",
        reason="source_error",
        reason_text="HR typed the wrong legal entity",
        target={"entity_id": ids["acme"]},
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert (
        out["kind"] == "fact.supersede"
        and out["authoritative_override"] is True
        and out["new_target_id"]
        and out["projection_status"] == "projected"
    )
    new_fact = out["new_target_id"]
    # current: Acme only; the wrong fact is history (superseded), never deleted
    current = (await t.get(f"/v1/entities/{ids['sarah']}", "admin")).json()["relationships"]
    assert (
        [x["target"]["name"] for x in current] == ["Acme"]
        and current[0]["source_type"] == "correction"
        and current[0]["authority_class"] == "admin_asserted"
    )
    hist = (await t.get(f"/v1/entities/{ids['sarah']}", "admin", include="history")).json()["relationships"]
    old = next(x for x in hist if x["id"] == ids["bad"])
    assert old["status"] == "superseded" and old["succeeded_by_fact_id"] == new_fact
    # the context re-materialises from the new state
    after = await ok_build(t, "reviewer", "Where does Sarah work?", subject_ids=[ids["sarah"]], profile="quick_answer")
    assert "Acme Labs" not in texts(after) and "Acme" in texts(after)
    # the audit entry and the correction record are linked through the command; the authoritative override raised a notice
    (rec,) = (await t.get("/v1/corrections", "admin", target_id=ids["bad"])).json()
    assert (
        rec["kind"] == "fact.supersede" and rec["reason_code"] == "source_error" and rec["reason_text"] == "HR typed the wrong legal entity"
    )
    (aud,) = await audit_rows(api, t, "correction.applied", resource_id=ids["bad"])
    assert aud.metadata["correction_kind"] == "fact.supersede" and aud.metadata["reason_code"] == "source_error"
    assert "HR typed" not in str(aud.metadata)  # the reason TEXT never reaches the audit trail
    assert len(await audit_rows(api, t, "correction.authoritative_override", resource_id=ids["bad"])) == 1
    (notice,) = await reviews_of(api, t, "authoritative_override")
    assert str(notice.fact_id) == ids["bad"]
    # the graph agrees
    assert (await edge_of(api, t, ids["bad"])).expired_at is not None and (await edge_of(api, t, new_fact)).expired_at is None
    await reconcile_clean(api, t)
    # a reviewer sees the correction text too; a reader may not even list it
    assert (await t.get("/v1/corrections", "reader")).status_code == 403
    assert (await t.get("/v1/corrections", "operator", target_id=ids["bad"])).json()[0][
        "reason_text"
    ] is None  # ids and codes only, below reviewer


async def test_a_correction_is_idempotent_and_a_stale_revision_is_refused(api, tenant_governance):
    t = tenant_governance
    ids = await acme_labs_story(t)
    rev = await fact_rev(t, ids["bad"])
    first = await correct_fact(t, ids["bad"], "end", "admin", key="k-end", revision=rev, valid_to="2023-01-01")
    again = await correct_fact(t, ids["bad"], "end", "admin", key="k-end", revision=rev, valid_to="2023-01-01")
    assert first.status_code == again.status_code == 200 and again.headers["idempotent-replay"] == "true"
    assert first.json()["correction_id"] == again.json()["correction_id"] and first.json()["command_id"] == again.json()["command_id"]
    assert len(await sql(api, t, "SELECT id FROM corrections WHERE workspace_id = :ws")) == 1
    other = await correct_fact(t, ids["bad"], "end", "admin", key="k-end", revision=rev, valid_to="2023-02-01")  # same key, another request
    assert other.status_code == 409 and other.json()["error"]["code"] == "idempotency_mismatch"
    stale = await correct_fact(t, ids["bad"], "reject", "admin", revision=rev)  # the fact moved on since it was read
    assert stale.status_code == 412
    no_match = await t.req("POST", f"/v1/relationships/{ids['bad']}/corrections", "admin", json={"kind": "reject", "reason_code": "other"})
    assert no_match.status_code == 428


async def test_fact_end_sets_the_valid_to_on_the_edge(api, tenant_governance):
    t = tenant_governance
    ids = await acme_labs_story(t)
    r = await correct_fact(t, ids["bad"], "end", "reviewer", reason="outdated_information", valid_to="2024-01-01")
    assert r.status_code == 200 and r.json()["projection_status"] == "projected"
    fact = (await t.get(f"/v1/relationships/{ids['bad']}", "admin")).json()
    assert fact["valid_to"].startswith("2024-01-01") and fact["status"] == "active" and fact["phase"] == "historical"
    assert (await edge_of(api, t, ids["bad"])).invalid_at.isoformat().startswith("2024-01-01")
    await reconcile_clean(api, t)


async def test_agents_and_apps_cannot_correct_and_a_hidden_fact_is_the_same_404(api, tenant_governance):
    t = tenant_governance
    ids = await acme_labs_story(t)
    for who in ("agent", "app", "reader", "operator"):
        r = await correct_fact(t, ids["bad"], "reject", who, revision=1)
        assert r.status_code == 403, (who, r.text)
    assert (await correct_entity(t, ids["sarah"], "rename", "agent", name="S", revision=1)).status_code == 403
    # an admin sees everything; a fact that does not exist (or lives in another workspace) answers like a hidden one
    missing = await correct_fact(t, str(uuid.uuid4()), "reject", "admin", revision=1)
    assert missing.status_code == 404
    assert (await t.get(f"/v1/relationships/{ids['bad']}/corrections/preview", "agent", kind="reject")).status_code == 403


async def test_reject_reopens_the_predecessor_a_dependent_memory_is_disputed_and_restore_reverses_it(api, tenant_governance):
    t = tenant_governance
    sarah = (await t.entity("Person", "Sarah Johnson", external_ids=HR("E-1001"), source_system="hr", source_ref="E-1001")).json()[
        "entity_id"
    ]
    acme = await prime_org(t, "Acme", "ORG-1")
    first = (
        await t.relationship(
            "WORKS_AT", sarah, acme, valid_from="2022-01-10", attributes={"role": "Engineer"}, source_system="hr", source_ref="e1"
        )
    ).json()
    second = (
        await t.relationship(
            "WORKS_AT", sarah, acme, valid_from="2024-06-01", attributes={"role": "Manager"}, source_system="hr", source_ref="e2"
        )
    ).json()
    assert second["ended_fact_ids"] == [first["fact_id"]]  # succession: the Engineer role ended when the Manager role began
    # a memory that rests on the second fact
    prop = await t.req(
        "POST",
        "/v1/memories/proposals",
        "admin",
        json={
            "type": "semantic",
            "content": "Sarah leads the platform team as Manager.",
            "fact_ids": [second["fact_id"]],
            "purpose": "role",
        },
    )
    assert prop.status_code in (201, 202), prop.text
    mid = prop.json()["memory_id"]
    pv = (await t.get(f"/v1/relationships/{second['fact_id']}/corrections/preview", "admin", kind="reject")).json()
    assert [m["id"] for m in pv["dependent_memories"]] == [mid] and pv["predecessor_fact_ids"] == [first["fact_id"]]
    r = await correct_fact(t, second["fact_id"], "reject", "reviewer", reason="incorrect_extraction")
    assert r.status_code == 200, r.text
    assert r.json()["reopened_fact_ids"] == [first["fact_id"]] and r.json()["dependent_memory_ids"] == [mid]
    # the predecessor is current again, the rejected fact is gone from current, the memory is disputed with a review item
    current = (await t.get(f"/v1/entities/{sarah}", "admin")).json()["relationships"]
    assert [(x["id"], x["valid_to"]) for x in current] == [(first["fact_id"], None)]
    got = (await t.get(f"/v1/memories/{mid}", "admin")).json()
    assert got["status"] == "disputed"
    (item,) = await reviews_of(api, t, "memory_conflict")
    assert str(item.memory_id) == mid and item.reason_code == "source_fact_rejected"
    assert (await edge_of(api, t, second["fact_id"])).expired_at is not None and (
        await edge_of(api, t, first["fact_id"])
    ).invalid_at is None
    await reconcile_clean(api, t)
    # the reverse
    back = await correct_fact(t, second["fact_id"], "restore", "reviewer", reason="other")
    assert back.status_code == 200 and back.json()["dependent_memory_ids"] == [mid]
    assert (await t.get(f"/v1/memories/{mid}", "admin")).json()["status"] == "active"
    assert (await reviews_of(api, t, "memory_conflict")) == []  # the item closed with the dispute
    assert (await t.get(f"/v1/relationships/{second['fact_id']}", "admin")).json()["status"] == "active"
    await reconcile_clean(api, t)


async def test_rejecting_an_extracted_fact_expires_its_edge_and_a_context_link_withdraws_it(api, tenant_governance, strict_mock):  # noqa: F811
    t = tenant_governance
    ids = await sarah_story(t)
    await t.grant("agent", "subject", ids["sarah"])
    await run_event(api, t, BOARD, subjects=[ids["sarah"]], occurred_at="2026-10-09T10:00:00Z")
    (fact,) = await sql(api, t, "SELECT id::text AS id, status FROM facts WHERE workspace_id = :ws AND source_type = 'extracted'")
    # a context built before the correction names the fact
    ctx = await ok_build(t, "agent", "What boards is Sarah on?", subject_ids=[ids["sarah"]], profile="planning")
    assert fact.id in {i["id"] for i in ctx["facts"]} or "Velo Club" in ctx["context_text"]
    r = await correct_fact(t, fact.id, "reject", "reviewer", reason="incorrect_extraction")
    assert r.status_code == 200, r.text
    assert (await edge_of(api, t, fact.id)).expired_at is not None
    assert (
        "Velo Club"
        not in (await ok_build(t, "agent", "What boards is Sarah on?", subject_ids=[ids["sarah"]], profile="planning"))["context_text"]
    )
    old = await t.req(
        "GET", f"/v1/context/{ctx['context_id']}", "agent"
    )  # the link of the earlier build re-materialises from today's state
    assert old.status_code == 200 and old.json()["rematerialized"] is True and "Velo Club" not in old.json()["context_text"]
    assert old.json()["withdrawn"] >= 1
    # an already rejected fact cannot be rejected twice
    assert (await correct_fact(t, fact.id, "reject", "reviewer")).status_code == 409


async def test_rename_keeps_the_id_and_the_old_name_resolves_as_an_alias(api, tenant_governance):
    t = tenant_governance
    ids = await acme_labs_story(t)
    r = await correct_entity(t, ids["labs"], "rename", "admin", name="Acme Research", reason="source_error")
    assert r.status_code == 200 and r.json()["projection_status"] == "projected"
    e = (await t.get(f"/v1/entities/{ids['labs']}", "admin")).json()
    assert e["id"] == ids["labs"] and e["name"] == "Acme Research" and "Acme Labs" in e["aliases"] and e["revision"] == 2
    assert (await node_of(api, t, ids["labs"])).name == "Acme Research"
    edge = await edge_of(api, t, ids["bad"])
    assert "Acme Research" in edge.fact  # the fact text embeds the new name
    await reconcile_clean(api, t)


async def test_a_corrected_attribute_is_validated_hashed_in_the_lineage_and_a_sensitive_one_stays_out_of_the_graph(api, tenant_governance):
    t = tenant_governance
    ids = await acme_labs_story(t)
    bad = await correct_entity(t, ids["sarah"], "correct_attribute", "admin", attribute="not_an_attribute", value="x")
    assert bad.status_code == 422  # the schema still applies to a correction
    r = await correct_entity(
        t, ids["sarah"], "correct_attribute", "admin", attribute="birth_date", value="1990-02-03", reason="source_error"
    )
    assert r.status_code == 200, r.text
    e = (await t.get(f"/v1/entities/{ids['sarah']}", "admin")).json()
    assert e["attributes"]["birth_date"].startswith("1990-02-03")
    node = await node_of(api, t, ids["sarah"])
    assert "birth_date" not in node.attributes  # a sensitive attribute is Postgres-only
    (lin,) = await sql(
        api, t, "SELECT attribute_key, old_value_hmac, new_value_hmac, entity_revision FROM attribute_corrections WHERE workspace_id = :ws"
    )
    assert lin.attribute_key == "birth_date" and lin.old_value_hmac is None and len(lin.new_value_hmac) == 64 and lin.entity_revision == 2
    (cmd,) = await sql(api, t, "SELECT payload FROM commands WHERE workspace_id = :ws AND command_type = 'correction.attribute'")
    assert "1990" not in str(cmd.payload)  # the ledger keeps a hash of the value, not the value
    redo = await correct_entity(t, ids["sarah"], "correct_attribute", "admin", attribute="birth_date", value=None)  # null removes it
    assert redo.status_code == 200
    assert "birth_date" not in (await t.get(f"/v1/entities/{ids['sarah']}", "admin")).json()["attributes"]


async def test_the_preview_counts_what_the_correction_then_does(api, tenant_governance):
    t = tenant_governance
    ids = await acme_labs_story(t)
    mem = await t.req(
        "POST",
        "/v1/memories/proposals",
        "admin",
        json={"type": "semantic", "content": "Sarah works for Acme Labs.", "fact_ids": [ids["bad"]], "purpose": "employer"},
    )
    mid = mem.json()["memory_id"]
    pv = (await t.get(f"/v1/relationships/{ids['bad']}/corrections/preview", "admin", kind="reject")).json()
    assert pv["dependent_memory_counts"] == {"active": 1} and pv["recent_context_count"] == 0
    await ok_build(t, "reviewer", "Where does Sarah work?", subject_ids=[ids["sarah"]], profile="quick_answer")
    pv2 = (await t.get(f"/v1/relationships/{ids['bad']}/corrections/preview", "admin", kind="reject")).json()
    assert pv2["recent_context_count"] == 1  # admin-only number: the build that included this fact
    assert (await t.get(f"/v1/relationships/{ids['bad']}/corrections/preview", "reviewer", kind="reject")).json()[
        "recent_context_count"
    ] is None
    r = await correct_fact(t, ids["bad"], "reject", "admin")
    assert r.json()["dependent_memory_ids"] == [mid]  # exactly what the preview named
    for kind, ops in (
        ("rename", ["upsert_node", "refresh_edge_text"]),
        ("split", ["create_node", "move_mentions", "recreate_edges_with_new_endpoint"]),
    ):
        p = (await t.get(f"/v1/entities/{ids['sarah']}/corrections/preview", "admin", kind=kind)).json()
        assert p["graph_operations"] == ops and p["kind"] == f"entity.{kind}"


async def test_memory_corrections_supersede_reject_expire_dispute_and_leave_search(api, tenant_governance):
    t = tenant_governance
    ids = await acme_labs_story(t)
    saved = await save(
        t,
        "admin",
        {
            "type": "semantic",
            "content": "Sarah prefers morning meetings",
            "subject_ids": [ids["sarah"]],
            "source_ref": "x",
            "purpose": "scheduling",
        },
    )
    assert saved.status_code == 201, saved.text
    mid = saved.json()["memory_id"]
    pv = await t.get(f"/v1/memories/{mid}/corrections/preview", "reviewer", kind="supersede")
    assert pv.status_code == 200 and pv.json()["graph_operations"][0] == "leave_search"
    r = await correct_memory(t, mid, "supersede", "reviewer", content="Sarah prefers afternoon meetings", reason="other")
    assert r.status_code == 200, r.text
    new = r.json()["new_target_id"]
    assert (await t.get(f"/v1/memories/{mid}", "admin")).json()["status"] == "superseded" and (
        await t.get(f"/v1/memories/{new}", "admin")
    ).json()["status"] == "active"
    (rec,) = (await t.get("/v1/corrections", "admin", target_id=mid)).json()
    assert rec["kind"] == "memory.supersede" and rec["new_target_id"] == new
    rej = await correct_memory(t, new, "reject", "reviewer")
    assert rej.status_code == 200 and (await t.get(f"/v1/memories/{new}", "admin")).json()["status"] == "rejected"
    assert (await correct_memory(t, new, "reject", "reviewer")).status_code == 409  # already rejected
    denied = await t.req(
        "POST",
        f"/v1/memories/{mid}/corrections",
        "agent",
        json={"kind": "reject", "reason_code": "other"},
        headers={"if-match": 'W/"rev-1"'},
    )
    assert denied.status_code == 403  # corrections are staff's


# ---- WP3: the identity split ----------------------------------------------------------------------------------------------------------------------


async def test_a_wrong_identity_link_is_split_into_a_new_person_with_the_mention_fact_memory_and_identifier_moved(
    api,
    tenant_governance,
    strict_mock,  # noqa: F811
):
    from tests.support.p4 import S_JOHNSON

    t = tenant_governance
    ids = await sarah_story(t)
    await t.grant("agent", "subject", ids["sarah"])
    await run_event(api, t, S_JOHNSON, subjects=[ids["sarah"]])  # P4: "S. Johnson" is linked to Sarah by the subject reference
    (mention,) = await sql(
        api,
        t,
        "SELECT id::text AS id, interaction_id::text AS interaction_id FROM entity_mentions WHERE workspace_id = :ws AND entity_id = :e",
        e=uuid.UUID(ids["sarah"]),
    )
    saved = await save(
        t,
        "admin",
        {
            "type": "episodic",
            "content": "S. Johnson confirmed the meeting for Thursday",
            "subject_ids": [ids["sarah"]],
            "source_ref": "chat",
            "purpose": "meeting",
        },
    )
    mid = saved.json()["memory_id"]
    assert mid in [
        h["memory_id"] for h in (await t.req("POST", "/v1/memories/search", "agent", json={"query": "meeting Thursday"})).json()["hits"]
    ]
    ext = {"system": "hr", "value": "E-9999"}
    upd = await t.req(
        "PATCH",
        f"/v1/entities/{ids['sarah']}",
        "app",
        json={"add_external_ids": [ext]},
        headers={"if-match": f'W/"rev-{await entity_rev(t, ids["sarah"])}"'},
    )
    assert upd.status_code in (200, 201), upd.text
    swe = ids["swe"]  # the Software Engineer role at Acme: it belongs to the other S. Johnson
    pv = (await t.get(f"/v1/entities/{ids['sarah']}/corrections/preview", "admin", kind="split")).json()
    assert pv["mention_count"] == 1 and len(pv["grant_holders"]) == 1
    r = await correct_entity(
        t,
        ids["sarah"],
        "split",
        "admin",
        reason="wrong_identity",
        new_entity={"display_name": "S. Johnson", "aliases": ["S. Johnson"]},
        move={"mention_ids": [mention.id], "fact_ids": [swe], "memory_ids": [mid], "external_ids": [ext]},
    )
    assert r.status_code == 200, r.text
    out = r.json()
    new = out["new_target_id"]
    assert (
        out["kind"] == "entity.split"
        and out["moved"] == {"mentions": 1, "facts": 1, "memories": 1, "external_ids": 1}
        and out["projection_status"] == "projected"
    )
    assert out["grant_holders"] == [await t.principal_id("agent")]  # who held a grant on the ORIGINAL (the new person has none)
    # Postgres: the new entity, its lineage, the non-match pair, the moved rows
    (e,) = await sql(
        api, t, "SELECT origin, type_key, display_name, aliases FROM entities WHERE workspace_id = :ws AND id = :i", i=uuid.UUID(new)
    )
    assert e.origin == "correction" and e.type_key == "Person" and e.display_name == "S. Johnson"
    (lin,) = await sql(
        api, t, "SELECT related_id::text AS rid, relation FROM entity_lineage WHERE workspace_id = :ws AND entity_id = :i", i=uuid.UUID(new)
    )
    assert (lin.rid, lin.relation) == (ids["sarah"], "split_from")
    assert len(await sql(api, t, "SELECT 1 FROM identity_non_matches WHERE workspace_id = :ws")) == 1
    assert (
        await sql(api, t, "SELECT entity_id::text AS e FROM entity_mentions WHERE workspace_id = :ws AND id = :m", m=uuid.UUID(mention.id))
    )[0].e == new
    assert (await sql(api, t, "SELECT source_entity_id::text AS s FROM facts WHERE workspace_id = :ws AND id = :f", f=uuid.UUID(swe)))[
        0
    ].s == new
    assert (await sql(api, t, "SELECT entity_id::text AS e FROM entity_external_ids WHERE workspace_id = :ws AND value = 'E-9999'"))[
        0
    ].e == new
    assert (
        await sql(api, t, "SELECT entity_id::text AS e FROM memory_subjects WHERE workspace_id = :ws AND memory_id = :m", m=uuid.UUID(mid))
    )[0].e == new
    assert (
        await sql(
            api,
            t,
            "SELECT scope_ref FROM memory_scopes WHERE workspace_id = :ws AND memory_id = :m AND scope_kind = 'subject'",
            m=uuid.UUID(mid),
        )
    )[0].scope_ref == new
    # the graph equals Postgres: the new node, the moved edge with the SAME uuid between the new endpoints, the episode's MENTIONS on the new node
    assert (await node_of(api, t, new)).name == "S. Johnson"
    edge = await edge_of(api, t, swe)
    assert edge.source_uuid == new and edge.target_uuid == ids["acme"]
    rows = await api.app.state.kg.graph.scoped(__import__("tests.support.p4", fromlist=["ctx_of"]).ctx_of(t)).run_cypher(
        "MATCH (:Episodic {group_id: $group_id})-[:MENTIONS]->(n:Entity) RETURN n.uuid AS u"
    )
    assert [x["u"] for x in rows] == [new]
    await reconcile_clean(api, t)
    # the new person starts with no grants: the agent that could see Sarah cannot see S. Johnson, or the memory that moved with them
    assert (await t.get(f"/v1/entities/{new}", "agent")).status_code == 404 and (
        await t.get(f"/v1/entities/{new}", "admin")
    ).status_code == 200
    assert mid not in [
        h["memory_id"] for h in (await t.req("POST", "/v1/memories/search", "agent", json={"query": "meeting Thursday"})).json()["hits"]
    ]
    assert mid in [
        h["memory_id"] for h in (await t.req("POST", "/v1/memories/search", "admin", json={"query": "meeting Thursday"})).json()["hits"]
    ]
    # idempotent replay, and a stale revision is refused
    again = await t.req(
        "POST",
        f"/v1/entities/{ids['sarah']}/corrections",
        "admin",
        json={
            "kind": "split",
            "reason_code": "wrong_identity",
            "new_entity": {"display_name": "S. Johnson", "aliases": ["S. Johnson"]},
            "move": {"mention_ids": [mention.id], "fact_ids": [swe], "memory_ids": [mid], "external_ids": [ext]},
        },
        key="x",
        headers={"if-match": 'W/"rev-1"'},
    )
    assert again.status_code == 412


async def test_a_split_refuses_to_move_what_is_not_the_entitys_and_a_merged_entity_is_not_splittable(api, tenant_governance):
    t = tenant_governance
    ids = await sarah_story(t)
    r = await correct_entity(t, ids["sarah"], "split", "admin", new_entity={"display_name": "X"}, move={"fact_ids": [str(uuid.uuid4())]})
    assert r.status_code == 422  # a fact that is not Sarah's cannot be moved off her
    empty = await correct_entity(t, ids["sarah"], "split", "admin", new_entity={"display_name": "X"}, move={})
    assert empty.status_code == 422
    other = await correct_entity(
        t, ids["sarah"], "split", "admin", new_entity={"display_name": "X", "type": "Nonsense"}, move={"fact_ids": [ids["em"]]}
    )
    assert other.status_code == 422  # the type must exist in the active ontology
    assert (await sql(api, t, "SELECT count(*) AS n FROM entities WHERE workspace_id = :ws AND origin = 'correction'"))[
        0
    ].n == 0  # nothing half-done


async def test_a_preview_of_a_foreign_or_missing_record_is_the_same_404(api, tenant_governance, bare_tenant):
    t, other = tenant_governance, bare_tenant
    ids = await sarah_story(t)
    mem = (
        await t.req(
            "POST",
            "/v1/memories",
            "admin",
            json={"type": "semantic", "content": "Sarah likes tea", "subject_ids": [ids["sarah"]], "purpose": "p", "source_ref": "x"},
        )
    ).json()["memory_id"]
    for path, query in (
        (f"/v1/relationships/{ids['em']}/corrections/preview", {"kind": "reject"}),
        (f"/v1/entities/{ids['sarah']}/corrections/preview", {"kind": "rename", "name": "X"}),
        (f"/v1/memories/{mem}/corrections/preview", {"kind": "reject"}),
    ):
        foreign = await other.get(path, "admin", **query)
        ghost = await other.get(path.replace(path.split("/")[3], str(uuid.uuid4())), "admin", **query)
        assert foreign.status_code == ghost.status_code == 404, path
        strip = lambda r: {k: v for k, v in r.json()["error"].items() if k != "request_id"}  # noqa: E731
        assert strip(foreign) == strip(ghost)
        assert (await t.get(path, "agent", **query)).status_code == 403
