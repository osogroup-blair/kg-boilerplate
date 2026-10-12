"""P4 WP6: evidence, neighbourhood, search, claims and the remaining decision paths through the real API."""

from __future__ import annotations

import uuid

import pytest
import yaml
from kg_obs import metrics

from tests.support.p4 import CYCLIST, EVT003, NEWCO, add_mock_rules, ctx_of, decide, reviews, run_event, sarah_story, sql
from tests.support.tenants import EXAMPLE_YAML, clear_graph, make_tenant

pytestmark = pytest.mark.integration


async def world(api, t):  # type: ignore[no-untyped-def]
    ids = await sarah_story(t)
    await run_event(api, t, EVT003, subjects=[ids["sarah"], ids["michael"], ids["topic"]])
    await run_event(api, t, NEWCO, subjects=[ids["sarah"]])
    return ids


async def test_the_evidence_endpoint_answers_why_for_command_and_extracted_facts(api, tenant, strict_mock):
    t = tenant
    ids = await world(api, t)
    em = (await t.get(f"/v1/relationships/{ids['em']}/evidence", "reader")).json()
    assert [(e["kind"], e["source_system"], e["source_ref"]) for e in em] == [("command", "hr", "evt_002")]
    (considering,) = await sql(api, t, "SELECT id::text AS id FROM facts WHERE workspace_id = :ws AND rel_type = 'CONSIDERING'")
    ev = (await t.get(f"/v1/relationships/{considering.id}/evidence", "reader")).json()
    assert (
        len(ev) == 1
        and ev[0]["kind"] == "episode"
        and ev[0]["interaction_id"]
        and ev[0]["episode_uuid"]
        and ev[0]["source_system"] == "chat"
    )
    assert "content" not in ev[0]  # the content is never part of an evidence listing
    assert (await t.get(f"/v1/relationships/{uuid.uuid4()}/evidence", "reader")).status_code == 404


async def test_every_fact_the_api_returns_carries_a_status_an_evidence_status_and_its_evidence(api, tenant, strict_mock):
    """KGR-05: no endpoint returns a fact without saying where it came from or that it is unverified."""
    t = tenant
    ids = await world(api, t)
    detail = (await t.get(f"/v1/entities/{ids['sarah']}", "reader", include="history,proposed,disputed,rejected")).json()
    rels = (await t.get(f"/v1/entities/{ids['sarah']}/relationships", "reader", include="history,proposed")).json()
    single = (await t.get(f"/v1/relationships/{ids['em']}", "reader")).json()
    facts = [*detail["relationships"], *rels, single]
    assert {f["status"] for f in facts} >= {"active", "proposed"}
    for f in facts:
        assert f["evidence_status"] in ("verified", "unverified") and isinstance(f["evidence"], list) and f["authority_class"]
        assert f["evidence"] or f["evidence_status"] == "unverified", f["id"]  # a fact with no evidence must say it is unverified
        if f["source_type"] == "extracted":
            assert f["evidence_status"] == "unverified" and f["evidence"][0]["kind"] == "episode" and f["fact_text"]
    review = (await t.get("/v1/reviews", "reviewer")).json()["items"][0]
    for f in (await t.get(f"/v1/reviews/{review['id']}", "reviewer")).json()["facts"]:
        assert "evidence_status" in f and "evidence" in f


async def test_the_neighbourhood_is_capped_filtered_and_labelled(api, tenant, strict_mock):
    t = tenant
    ids = await world(api, t)
    g = (await t.get(f"/v1/entities/{ids['sarah']}/graph", "reader")).json()
    assert g["center_id"] == ids["sarah"] and g["truncated"] is False
    names = {n["name"]: n for n in g["nodes"]}
    assert (
        set(names) == {"Sarah Johnson", "Acme", "Michael", "Entrepreneurship"}
        and names["Sarah Johnson"]["hop"] == 0
        and names["Acme"]["hop"] == 1
    )
    kinds = {e["rel_type"]: e for e in g["edges"]}
    assert kinds["WORKS_AT"]["status"] == "active" and kinds["WORKS_AT"]["evidence_status"] == "verified"
    assert kinds["CONSIDERING"]["evidence_status"] == "unverified" and kinds["KNOWS"]["evidence_status"] == "unverified"
    assert len([e for e in g["edges"] if e["rel_type"] == "WORKS_AT"]) == 1  # the ended Software Engineer role is history, not current
    proposed = (await t.get(f"/v1/entities/{ids['sarah']}/graph", "reader", include="proposed")).json()
    assert {n["name"] for n in proposed["nodes"]} == {"Sarah Johnson", "Acme", "Michael", "Entrepreneurship", "NewCo"}
    assert "proposed" in {e["status"] for e in proposed["edges"]}
    hist = (await t.get(f"/v1/entities/{ids['sarah']}/graph", "reader", include="historical")).json()
    assert len([e for e in hist["edges"] if e["rel_type"] == "WORKS_AT"]) == 2
    in_2023 = (await t.get(f"/v1/entities/{ids['sarah']}/graph", "reader", as_of="2023-01-01")).json()
    assert [e["valid_from"][:10] for e in in_2023["edges"] if e["rel_type"] == "WORKS_AT"] == ["2022-01-10"]
    only_orgs = (await t.get(f"/v1/entities/{ids['sarah']}/graph", "reader", types="Organization")).json()
    assert {n["type"] for n in only_orgs["nodes"]} == {"Person", "Organization"}
    two = (await t.get(f"/v1/entities/{ids['michael']}/graph", "reader", hops=2)).json()
    assert "Acme" in {n["name"] for n in two["nodes"]} and {n["hop"] for n in two["nodes"]} == {0, 1, 2}
    small = (await t.get(f"/v1/entities/{ids['sarah']}/graph", "reader", limit=2)).json()
    assert len(small["nodes"]) == 2 and small["truncated"] is True
    assert (
        await t.get(f"/v1/entities/{ids['sarah']}/graph", "reader", hops=3)
    ).status_code == 422  # caps are the server's, not the client's
    assert (await t.get(f"/v1/entities/{ids['sarah']}/graph", "reader", limit=101)).status_code == 422
    assert (await t.get(f"/v1/entities/{uuid.uuid4()}/graph", "reader")).status_code == 404


async def test_mentions_and_attribute_claims_are_listed_for_an_entity(api, tenant, strict_mock):
    d = yaml.safe_load(EXAMPLE_YAML)
    d["entity_types"]["Person"]["attributes"]["nickname"] = {"type": "string", "max_length": 50}
    t = await make_tenant(api, ontology=yaml.safe_dump(d))
    try:
        ids = await sarah_story(t)
        before = metrics.extraction_reverts_total.labels(kind="attributes")._value.get()
        await add_mock_rules(
            [{"when": {"schema_name": "Person", "contains": ["considering entrepreneurship"]}, "response": {"nickname": "Sally"}}]
        )
        detail = await run_event(api, t, EVT003, subjects=[ids["sarah"], ids["michael"], ids["topic"]])
        mentions = (await t.get(f"/v1/entities/{ids['sarah']}/mentions", "reader")).json()
        assert [(m["interaction_id"], m["resolution"], m["status"]) for m in mentions] == [(detail["id"], "subject_ref", "accepted")]
        claims = (await t.get(f"/v1/entities/{ids['sarah']}/attribute-claims", "reader")).json()
        assert [(c["attribute_key"], c["value"], c["status"]) for c in claims if c["value"] == "Sally"][:1] == [
            ("nickname", "Sally", "proposed")
        ]
        # a claim never changes the entity, and the graph node does not keep what the model wrote on it
        assert "nickname" not in (await t.get(f"/v1/entities/{ids['sarah']}", "admin")).json()["attributes"]
        node = await api.app.state.kg.graph.scoped(ctx_of(t)).read_node(ids["sarah"])
        assert node is not None and "nickname" not in node.attributes
        assert metrics.extraction_reverts_total.labels(kind="attributes")._value.get() > before
        assert (await api.app.state.kg.knowledge.projector.reconcile(ctx_of(t))).drift == []
    finally:
        await clear_graph(api, t.group_id)


async def test_graph_search_returns_current_facts_with_status_and_evidence_and_respects_valid_time(api, tenant, strict_mock):
    """KGR-03/04: 'where does Sarah work' answers Acme as of now and the earlier role as of 2023; the proposed NewCo claim is never an answer."""
    t = tenant
    ids = await world(api, t)
    now = (await t.req("POST", "/v1/graph/search", "reviewer", json={"query": "Sarah employer", "limit": 10})).json()
    works = [f for f in now["facts"] if f["rel_type"] == "WORKS_AT"]
    assert [f["id"] for f in works] == [ids["em"]] and works[0]["evidence_status"] == "verified" and works[0]["evidence"]
    assert all(f["status"] == "active" for f in now["facts"]) and now["unreconciled_hits"] == 0
    assert "NewCo" not in {f["target"]["name"] for f in now["facts"]}
    then = (
        await t.req("POST", "/v1/graph/search", "reviewer", json={"query": "Sarah employer", "as_of": "2023-01-01", "limit": 10})
    ).json()
    assert [f["id"] for f in then["facts"] if f["rel_type"] == "WORKS_AT"] == [ids["swe"]]
    centred = (await t.req("POST", "/v1/graph/search", "reviewer", json={"query": "works", "center_entity_id": ids["sarah"]})).json()
    assert all(f["source"]["id"] == ids["sarah"] or f["target"]["id"] == ids["sarah"] for f in centred["facts"])
    assert (
        await t.req("POST", "/v1/graph/search", "reviewer", json={"query": "x", "center_entity_id": str(uuid.uuid4())})
    ).status_code == 404
    assert (await t.req("POST", "/v1/graph/search", "reviewer", json={"query": "x", "limit": 26})).status_code == 422
    for who in ("app", "agent", "reader"):
        assert (await t.req("POST", "/v1/graph/search", who, json={"query": "Sarah"})).status_code == 403


async def test_a_graph_hit_postgres_does_not_hold_yet_is_dropped_and_counted(api, tenant):
    t = tenant
    ids = await sarah_story(t)
    graph = api.app.state.kg.graph.scoped(ctx_of(t))
    import datetime as dt

    from kg_modelgw.egress import egress

    ghost = str(uuid.uuid4())
    with egress(classification="internal", purpose="graph_projection", workspace_id=str(t.ws), correlation_id="t", service="kg-api"):
        await graph.upsert_fact_edge(
            uuid=ghost,
            source_uuid=ids["sarah"],
            target_uuid=ids["acme"],
            name="WORKS_AT",
            fact="Sarah Johnson works at Acme ghost",
            valid_at=None,
            invalid_at=None,
            created_at=dt.datetime.now(dt.UTC),
            expired_at=None,
            attributes={},
        )
    before = metrics.graph_search_unreconciled_hits_total._value.get()
    out = (await t.req("POST", "/v1/graph/search", "reviewer", json={"query": "ghost Sarah Acme"})).json()
    assert ghost not in [f["id"] for f in out["facts"]] and out["unreconciled_hits"] >= 1
    assert metrics.graph_search_unreconciled_hits_total._value.get() >= before + 1


async def test_approving_a_queued_command_applies_it_once_under_the_reviewers_name_and_rejecting_leaves_it_queued(api):
    d = yaml.safe_load(EXAMPLE_YAML)
    d["relationship_types"]["WORKS_AT"]["rules"].append({"kind": "queue_if", "params": {"when": "disallowed_endpoint"}})
    t = await make_tenant(api, ontology=yaml.safe_dump(d))
    try:
        ids = await sarah_story(t)
        queued = await t.relationship("WORKS_AT", ids["sarah"], ids["topic"], valid_from="2025-01-01", source_system="hr", source_ref="x")
        assert queued.status_code == 202 and queued.json()["status"] == "needs_review"
        other = await t.relationship("WORKS_AT", ids["michael"], ids["topic"], valid_from="2025-01-01", source_system="hr", source_ref="y")
        first, second = await reviews(t, type="policy_violation"), None
        by_cmd = {i["command_id"]: i for i in first}
        approve, reject = by_cmd[queued.json()["command_id"]], by_cmd[other.json()["command_id"]]
        detail = (await t.get(f"/v1/reviews/{approve['id']}", "reviewer")).json()
        assert detail["violations"][0]["code"] == "disallowed_endpoint" and detail["allowed_decisions"] == ["approve", "reject"]
        r = await decide(t, reject["id"], "reject", rationale="incorrect_extraction")
        assert r.status_code == 200 and r.json()["applied_command_id"] is None
        assert (await t.get(f"/v1/commands/{other.json()['command_id']}", "reviewer")).json()["status"] == "needs_review"
        r = await decide(t, approve["id"], "approve", rationale="policy_exception")
        assert r.status_code == 200, r.text
        applied = r.json()["applied_command_id"]
        assert applied and r.json()["fact_ids"]
        cmd = (await t.get(f"/v1/commands/{applied}", "reviewer")).json()
        assert cmd["status"] == "applied" and cmd["fact_id"] == r.json()["fact_ids"][0]
        facts = (await t.get(f"/v1/entities/{ids['sarah']}", "reader")).json()["relationships"]
        assert any(f["target"]["name"] == "Entrepreneurship" and f["status"] == "active" for f in facts)
        assert second is None
    finally:
        await clear_graph(api, t.group_id)


async def test_two_equal_authority_extractions_that_cannot_both_hold_are_disputed_and_flagged(api):
    """KGR-06: with a cardinality rule, a second conversation about the same person's employer disputes BOTH facts and asks a reviewer."""
    d = yaml.safe_load(EXAMPLE_YAML)
    d["relationship_types"]["WORKS_AT"]["rules"].append({"kind": "max_active_per_source", "params": {"n": 1, "match": ["source"]}})
    t = await make_tenant(api, ontology=yaml.safe_dump(d))
    from tests.support.p4 import Spied  # noqa: F401  (kept for symmetry with the other P4 suites)

    try:
        ids = await sarah_story(t)
        # the verified Acme role is in the way of any extraction: end it (as the hr system would) so two conversations can compete
        await t.req("POST", f"/v1/relationships/{ids['em']}/end", "app", json={"valid_to": "2025-01-01"})
        for name, text_ in (("Orbit", "Michael: I work at Orbit now"), ("Zenith", "Michael: I work at Zenith now")):
            await add_mock_rules(
                [
                    {
                        "when": {"schema_name": "ExtractedEntities", "contains": [text_]},
                        "response": {
                            "extracted_entities": [
                                {"name": "Michael", "entity_type_id": 2, "episode_indices": [0]},
                                {"name": name, "entity_type_id": 1, "episode_indices": [0]},
                            ]
                        },
                    },
                    {
                        "when": {"schema_name": "ExtractedEdges", "contains": [text_]},
                        "response": {
                            "edges": [
                                {
                                    "source_entity_name": "Michael",
                                    "target_entity_name": name,
                                    "relation_type": "WORKS_AT",
                                    "fact": f"Michael works at {name}",
                                    "valid_at": "2026-01-01T00:00:00Z",
                                    "invalid_at": None,
                                    "episode_indices": [0],
                                }
                            ]
                        },
                    },
                    {
                        "when": {"schema_name": "EdgeDuplicate", "contains": [f"<new fact> Michael works at {name}"]},
                        "response": {"duplicate_facts": [], "contradicted_facts": []},
                    },
                    {
                        "when": {"schema_name": "EdgeTimestamps", "contains": [f"<fact> Michael works at {name}"]},
                        "response": {"valid_at": "2026-01-01T00:00:00Z", "invalid_at": None},
                    },
                    {"when": {"schema_name": "WORKS_AT", "contains": [f"Michael works at {name}"]}, "response": {"role": None}},
                ]
            )
        await run_event(api, t, {"kind": "message", "content": "Michael: I work at Orbit now"}, subjects=[ids["michael"]])
        await run_event(api, t, {"kind": "message", "content": "Michael: I work at Zenith now"}, subjects=[ids["michael"]])
        got = {
            r.target: r
            for r in await sql(
                api,
                t,
                "SELECT t.display_name AS target, f.status, f.decision_code FROM facts f JOIN entities t ON t.id = f.target_entity_id WHERE f.workspace_id = :ws AND f.source_type = 'extracted' AND f.rel_type = 'WORKS_AT'",
            )
        }
        assert {k: v.status for k, v in got.items()} == {"Orbit": "disputed", "Zenith": "disputed"}
        (item,) = await reviews(t, type="contradiction")
        assert item["reason_code"] == "equal_authority_conflict"
        r = await decide(t, item["id"], "keep_existing", rationale="verified_with_source")
        assert r.status_code == 200
        after = {
            r.target: r.status
            for r in await sql(
                api,
                t,
                "SELECT t.display_name AS target, f.status FROM facts f JOIN entities t ON t.id = f.target_entity_id WHERE f.workspace_id = :ws AND f.source_type = 'extracted' AND f.rel_type = 'WORKS_AT'",
            )
        }
        assert sorted(after.values()) == ["active", "rejected"]  # the dispute ended with the existing side standing
    finally:
        await clear_graph(api, t.group_id)


async def test_merging_entities_that_carry_different_strong_ids_is_refused(api, tenant, strict_mock):
    t = tenant
    await sarah_story(t)
    await run_event(api, t, CYCLIST)
    (item,) = await reviews(t, type="identity_match")
    cand = item["candidate_entity_id"]
    # the candidate gains a different HR id (as a trusted app would assert): they are provably different people
    from kg_db.session import tenant_session
    from sqlalchemy import text

    async with tenant_session(api.engine, ctx_of(t)) as c:
        await c.execute(
            text(
                "INSERT INTO entity_external_ids (workspace_id, entity_id, type_key, system, value) VALUES (:ws, :e, 'Person', 'hr', 'E-9999')"
            ),
            {"ws": t.ws, "e": uuid.UUID(cand)},
        )
    r = await decide(t, item["id"], "same_entity", rationale="same_real_world_entity")
    assert r.status_code == 409 and r.json()["error"]["code"] == "merge_refused"
    assert (await t.get(f"/v1/reviews/{item['id']}", "reviewer")).json()["status"] == "open"


async def test_the_command_list_includes_review_decisions_and_merges(api, tenant, strict_mock):
    """A review decision is a ledger command: listing and filtering commands must serialise its type (regression: the type enum once stopped at P2's four)."""
    t = tenant
    ids = await world(api, t)
    item = next(r for r in await reviews(t, "reviewer") if r["item_type"] == "contradiction")
    assert (await decide(t, item["id"], "keep_existing", rationale="verified_with_source")).status_code == 200
    listed = await t.get("/v1/commands", "admin", limit=100)
    assert listed.status_code == 200 and "review.decide" in {c["command_type"] for c in listed.json()["items"]}
    assert (await t.get("/v1/commands", "admin", type="review.decide")).status_code == 200
    assert ids  # the story stays alive until here


async def test_the_restore_scope_includes_rows_the_reconciler_put_back(api, tenant, strict_mock):
    """Graphiti can change a fact on a node the interaction does not mention; the stage result names the reverted rows so the restore reaches them."""
    t = tenant
    ids = await sarah_story(t)
    event = (await run_event(api, t, NEWCO, subjects=[ids["sarah"]]))["id"]
    stray_fact, stray_entity = uuid.uuid4(), uuid.uuid4()
    await sql(
        api,
        t,
        "UPDATE interaction_stages SET result = result || CAST(:r AS jsonb) WHERE workspace_id = :ws AND interaction_id = :i AND stage = 'graph_ingest' RETURNING 1 AS x",
        r=f'{{"revert_fact_ids": ["{stray_fact}"], "revert_entity_ids": ["{stray_entity}"]}}',
        i=uuid.UUID(str(event)),
    )
    entities, facts = await api.app.state.kg.knowledge.projector.interaction_scope(ctx_of(t), uuid.UUID(str(event)))
    assert stray_fact in facts and stray_entity in entities
