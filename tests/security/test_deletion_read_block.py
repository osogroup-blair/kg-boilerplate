"""P8 WP7 (SEC-05, AT-16, MEM-07): the read block between the freeze and the purge.

The purge jobs are PAUSED (no worker runs), so everything below is the state the platform is in right after a deletion request is accepted: the rows are still in Postgres,
frozen. Every read surface, as every kind of reader, must answer exactly as if the subject had never existed: the same 404, never a trace in a list, a count, a search hit, a
neighbourhood, a context or a trace. A surface that forgets the rule fails here."""

from __future__ import annotations

import uuid

import pytest

from tests.support.p4 import add_mock_rules, sql, strict_mock  # noqa: F401
from tests.support.p5 import search
from tests.support.p6 import ok_build
from tests.support.p8 import SENTINEL, deletion_world, raw, request_deletion

pytestmark = pytest.mark.security
READERS = ("admin", "reviewer", "app", "agent")


async def frozen_world(api, t):  # type: ignore[no-untyped-def]
    w = await deletion_world(api, t)
    for who in ("agent", "agent2"):
        await t.grant(who, "subject", w["zeb"])  # the agents could read him before; they must not after
    # an open review item about him, and a context built BEFORE the request that names his fact and his memory
    await raw(
        api,
        t,
        "INSERT INTO review_items (id, workspace_id, item_type, rule_kind, reason_code, subject_entity_id, candidate_entity_id) "
        "VALUES (gen_random_uuid(), :ws, 'identity_match', 'identity', 'name_match', :s, :c)",
        s=uuid.UUID(w["sarah"]),
        c=uuid.UUID(w["zeb"]),
    )
    before = await ok_build(
        t, "agent", f"What do we know about {SENTINEL} and Sarah?", subject_ids=[w["sarah"], w["zeb"]], profile="planning"
    )
    assert "Zebulon" in before["context_text"], before["context_text"]
    r = await request_deletion(t, "subject", w["zeb"], "admin")
    assert r.status_code == 202, r.text
    return w, before, r.json()["request_id"]


async def test_every_entity_and_fact_surface_answers_404_for_every_reader(api, tenant_governance):
    t = tenant_governance
    w, _, _ = await frozen_world(api, t)
    for who in READERS:
        for path in (
            f"/v1/entities/{w['zeb']}",
            f"/v1/entities/{w['zeb']}/relationships",
            f"/v1/entities/{w['zeb']}/graph",
            f"/v1/entities/{w['zeb']}/mentions",
            f"/v1/entities/{w['zeb']}/attribute-claims",
            f"/v1/entities/{w['zeb']}/summary",
            f"/v1/relationships/{w['knows']}",
            f"/v1/relationships/{w['knows']}/evidence",
            f"/v1/interactions/{w['i_zeb']}",
            f"/v1/memories/{w['m_zeb']}",
            f"/v1/memories/{w['m_from_zeb']}",
        ):
            r = await t.get(path, who)
            assert r.status_code in (403, 404), (
                who,
                path,
                r.status_code,
            )  # 403 only where the role never had the surface (an agent has no raw interaction or graph read)
            if r.status_code == 404:
                assert r.json()["error"]["code"] == "not_found", (who, path)
    gone = (await t.get(f"/v1/entities/{w['zeb']}", "admin")).json()["error"]
    missing = (await t.get(f"/v1/entities/{uuid.uuid4()}", "admin")).json()["error"]
    assert {k: v for k, v in gone.items() if k != "request_id"} == {
        k: v for k, v in missing.items() if k != "request_id"
    }  # indistinguishable from a never-existing id


async def test_lists_counts_search_and_neighbourhoods_do_not_know_him(api, tenant_governance):
    t = tenant_governance
    w, _, _ = await frozen_world(api, t)
    for who in READERS:
        listed = (await t.get("/v1/entities", who, limit=100)).json()["items"]
        assert w["zeb"] not in [e["id"] for e in listed] and SENTINEL not in str(listed), who
        assert (await t.get("/v1/entities", who, q="zebulon")).json()["items"] == []
        sarah = (await t.get(f"/v1/entities/{w['sarah']}", who)).json()
        assert all(r["target"]["id"] != w["zeb"] and r["source"]["id"] != w["zeb"] for r in sarah["relationships"]), who
        assert w["knows"] not in [r["id"] for r in sarah["relationships"]]
        hist = (await t.get(f"/v1/entities/{w['sarah']}", who, include="history,proposed,disputed,rejected")).json()["relationships"]
        assert w["knows"] not in [r["id"] for r in hist], who
        assert sarah["memories"] is None or sarah["memories"] >= 0
    for who in ("admin", "reviewer"):
        g = (await t.get(f"/v1/entities/{w['sarah']}/graph", who, hops=2)).json()
        assert w["zeb"] not in str(g) and w["knows"] not in str(g), who
        found = await t.req("POST", "/v1/graph/search", who, json={"query": "Zebulon Quillfeather knows Sarah"}, key="")
        assert found.status_code == 200 and SENTINEL not in found.text and w["knows"] not in found.text, who
    # Sarah's visible memory count excludes what rested only on his interaction
    admin_count = (await t.get(f"/v1/entities/{w['sarah']}", "admin")).json()["memories"]
    assert admin_count == 2  # m_keep and m_names; m_from_zeb is frozen
    # the interaction list and the command list
    inter = (await t.get("/v1/interactions", "admin", limit=100)).json()["items"]
    assert w["i_zeb"] not in [i["id"] for i in inter] and w["i_sar"] in [i["id"] for i in inter]
    by_subject = (await t.get("/v1/interactions", "admin", subject_entity_id=w["zeb"])).json()["items"]
    assert by_subject == []
    cmds = (await t.get("/v1/commands", "admin", limit=100)).json()["items"]
    assert w["zeb"] not in [c.get("entity_id") for c in cmds] and w["knows"] not in [c.get("fact_id") for c in cmds]
    creator = await raw(
        api,
        t,
        "SELECT id FROM commands WHERE workspace_id = :ws AND command_type = 'entity.create' AND result ->> 'entity_id' = :e",
        e=w["zeb"],
    )
    assert creator and (await t.get(f"/v1/commands/{creator[0].id}", "admin")).status_code == 404


async def test_memory_surfaces_hide_what_is_frozen(api, tenant_governance):
    t = tenant_governance
    w, _, _ = await frozen_world(api, t)
    for who in ("admin", "reviewer"):
        hits = [h["memory_id"] for h in (await search(t, who, "Zebulon green tea afternoon"))["hits"]]
        assert w["m_zeb"] not in hits, who
        hits = [h["memory_id"] for h in (await search(t, who, "Sarah greeted a colleague Monday morning"))["hits"]]
        assert w["m_from_zeb"] not in hits, who
        listing = (await t.get("/v1/memories", who, limit=100)).json()["items"]
        assert w["m_zeb"] not in [m["id"] for m in listing] and w["m_from_zeb"] not in [m["id"] for m in listing], who
        for status in ("active", "proposed", "disputed", "superseded", "expired", "rejected"):
            rows = (await t.get("/v1/memories", who, status=status, limit=100)).json()["items"]
            assert not {w["m_zeb"], w["m_from_zeb"]} & {m["id"] for m in rows}, (who, status)
        assert w["m_zeb"] not in str((await t.get("/v1/memories", who, subject_id=w["zeb"])).json())
        assert (await t.get(f"/v1/memories/{w['m_zeb']}/usage", who)).status_code in (403, 404)


def only_the_exception_names_him(text: str) -> bool:
    """The one memory that merely NAMES him (another subject's, retained for a person to redact: a deletion_exception) may still be shown; nothing else about him may."""
    return all("thanked" in line for line in text.splitlines() if SENTINEL in line or "Zebulon" in line)


async def test_a_new_context_excludes_him_and_an_old_link_rematerialises_without_him(api, tenant_governance):
    t = tenant_governance
    w, before, _ = await frozen_world(api, t)
    for who in ("admin", "reviewer", "agent"):
        out = await ok_build(t, who, f"What do we know about {SENTINEL} and Sarah?", subject_ids=[w["sarah"], w["zeb"]], profile="planning")
        assert out["status"] == "denied" or only_the_exception_names_him(out["context_text"]), who
        assert "green tea" not in out["context_text"] and "greeted a colleague" not in out["context_text"], who
        about_sarah = await ok_build(t, who, f"Tell me about {SENTINEL} and what Sarah did", subject_ids=[w["sarah"]], profile="planning")
        assert only_the_exception_names_him(about_sarah["context_text"]), who
        assert "greeted a colleague" not in about_sarah["context_text"], who
        assert w["m_from_zeb"] not in str(about_sarah) and w["knows"] not in str(about_sarah), who
    old = await t.req("GET", f"/v1/context/{before['context_id']}", "agent")
    assert old.status_code == 200 and old.json()["rematerialized"] is True
    assert only_the_exception_names_him(old.json()["context_text"]) and "green tea" not in old.json()["context_text"]
    assert w["knows"] not in str(old.json()) and old.json()["withdrawn"] >= 1
    trace = await t.req("GET", f"/v1/context/{before['context_id']}/trace", "admin")
    assert trace.status_code == 200
    assert w["knows"] not in str(trace.json()["items"]) and w["m_from_zeb"] not in str(trace.json()["items"])


async def test_reviews_and_corrections_cannot_reach_a_frozen_record(api, tenant_governance):
    t = tenant_governance
    w, _, _ = await frozen_world(api, t)
    items = (await t.get("/v1/reviews", "reviewer", limit=100)).json()["items"]
    assert not [i for i in items if w["zeb"] in (i.get("subject_entity_id"), i.get("candidate_entity_id"))]
    assert (
        await t.req(
            "POST",
            f"/v1/relationships/{w['knows']}/corrections",
            "admin",
            json={"kind": "reject", "reason_code": "other"},
            headers={"if-match": 'W/"rev-1"'},
        )
    ).status_code == 404
    assert (
        await t.req(
            "POST",
            f"/v1/entities/{w['zeb']}/corrections",
            "admin",
            json={"kind": "rename", "reason_code": "other", "name": "Nobody"},
            headers={"if-match": 'W/"rev-1"'},
        )
    ).status_code == 404
    assert (
        await t.req("PATCH", f"/v1/entities/{w['zeb']}", "app", json={"name": "Changed"}, headers={"if-match": 'W/"rev-1"'})
    ).status_code in (404, 422)
    fresh = await t.relationship(
        "KNOWS", w["sarah"], w["zeb"], source_system="hr", source_ref="again"
    )  # a command against a frozen endpoint is refused too
    assert fresh.status_code == 422 and fresh.json()["error"]["code"] == "unknown_entity"
    # nothing about him appears in what a count or a gauge could reveal to a tenant either
    assert len(await sql(api, t, "SELECT 1 FROM entities WHERE workspace_id = :ws AND status = 'deletion_pending'")) == 1


async def test_the_graph_is_not_a_back_door_between_the_freeze_and_the_purge(api, tenant_governance):
    t = tenant_governance
    w, _, _ = await frozen_world(api, t)
    # the graph still holds his node and edge (the purge has not run), but no read joins to them: Postgres decides
    graph = api.app.state.kg.graph.scoped(__import__("tests.support.p4", fromlist=["ctx_of"]).ctx_of(t))
    assert await graph.read_node(w["zeb"]) is not None
    for who in ("admin", "reviewer"):
        r = await t.req("POST", "/v1/graph/search", who, json={"query": "knows Zebulon", "limit": 25}, key="")
        assert r.status_code == 200 and w["knows"] not in r.text and SENTINEL not in r.text
    # the projector does not write for a frozen row (a late job cannot put it back)
    status = await api.app.state.kg.knowledge.projector.project_entity(
        __import__("tests.support.p4", fromlist=["ctx_of"]).ctx_of(t), uuid.UUID(w["zeb"])
    )
    assert status == "skipped_by_policy"
    _ = add_mock_rules
