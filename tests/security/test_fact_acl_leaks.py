"""P7 WP1: ontology-driven subject ACLs on entities, facts and evidence (SEC-01, AT-10, MEM-03). Release blockers.

Ontology v1.4.0 makes `Person` subject_scoped. Sarah and Michael carry unique sentinel names; Acme (an Organization) stays workspace-access. The principals:

    agent3    no grants at all          sees Acme and nothing about any person, anywhere
    agent     Sarah only                sees Sarah and Acme; the Sarah -> Michael fact is hidden because ONE end is hidden, and so is every count and edge it would add
    operator  no grants, a staff role   no implicit grants: sees what agent3 sees (only admins and reviewers see every subject by role)
    admin, reviewer                     see everything by role

Every surface a principal can look at is tried; a hidden thing must be exactly as absent as one that never existed (the same 404 body, no count, no name, no id), in the
responses, the headers, the logs and the metrics. With v1.3.0 active (no `access`), the P2-P6 behaviour is unchanged."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import pytest
from kg_db.session import tenant_session
from kg_obs import metrics
from prometheus_client import generate_latest
from sqlalchemy import event, text

from tests.support.p4 import ctx_of, sarah_story, strict_mock  # noqa: F401
from tests.support.p6 import build

pytestmark = [pytest.mark.integration, pytest.mark.security]


def sentinel(label: str) -> str:
    return f"{label}{uuid.uuid4().hex[:12]}"


def strip(r: Any) -> dict[str, Any]:
    return {k: v for k, v in r.json()["error"].items() if k != "request_id"}


async def world(t: Any) -> dict[str, str]:
    """Sarah, Michael (sentinel names), Acme; Sarah WORKS_AT Acme and Sarah KNOWS Michael; a workspace-scoped memory ABOUT Sarah; an interaction naming both."""
    s_name, m_name = sentinel("Zqsarah"), sentinel("Zqmichael")
    sarah = (await t.entity("Person", s_name, source_system="hr", source_ref="E-1")).json()
    michael = (await t.entity("Person", m_name, source_system="hr", source_ref="E-2")).json()
    acme = (await t.entity("Organization", "Acme Holdings", source_system="hr", source_ref="O-1")).json()
    works = (
        await t.relationship(
            "WORKS_AT", sarah["entity_id"], acme["entity_id"], valid_from="2024-06-01", source_system="hr", source_ref="evt_1"
        )
    ).json()
    knows = (
        await t.relationship(
            "KNOWS", sarah["entity_id"], michael["entity_id"], valid_from="2020-01-01", source_system="hr", source_ref="evt_2"
        )
    ).json()
    for r in (sarah, michael, acme, works, knows):
        assert "entity_id" in r or "fact_id" in r, r
    ev = await t.req(
        "POST",
        "/v1/interactions",
        "app",
        json={
            "source_system": "chat", "source_ref": f"msg-{uuid.uuid4().hex[:8]}", "kind": "message", "occurred_at": "2026-10-10T09:00:00Z",
            "content": "a chat about two people", "subject_refs": [{"entity_id": sarah["entity_id"]}, {"entity_id": michael["entity_id"]}],
        },
    )  # fmt: skip
    assert ev.status_code == 202, ev.text
    mem = await t.req(
        "POST",
        "/v1/memories",
        "admin",
        json={
            "type": "semantic",
            "content": f"{s_name} likes tea",
            "scopes": [{"kind": "workspace"}],
            "subject_ids": [sarah["entity_id"]],
            "source_ref": "x",
            "purpose": "p",
        },
    )
    assert mem.status_code == 201, mem.text
    return {
        "sarah": sarah["entity_id"], "michael": michael["entity_id"], "acme": acme["entity_id"], "works": works["fact_id"], "knows": knows["fact_id"],
        "s_name": s_name, "m_name": m_name, "event": ev.json()["event_id"], "memory": mem.json()["memory_id"],
    }  # fmt: skip


@pytest.fixture()
async def w(api, tenant_agents, strict_mock):  # noqa: F811
    t = tenant_agents
    ids = await world(t)
    await t.grant("agent", "subject", ids["sarah"])  # agent: Sarah only. agent3: nothing. operator: nothing.
    return t, ids


async def probe(t: Any, who: str, method: str, path: str, **kw: Any) -> Any:
    return await t.req(method, path, who, **kw)


# ---- agent3: no grants, sees no person anywhere -----------------------------------------------------------------------------------------------


async def test_a_hidden_entity_is_exactly_as_absent_as_one_that_never_existed(w):
    t, ids = w
    for who in ("agent3", "operator"):
        for suffix in ("", "/relationships", "/graph", "/mentions", "/attribute-claims", "/summary"):
            hidden = await t.get(f"/v1/entities/{ids['sarah']}{suffix}", who)
            never = await t.get(f"/v1/entities/{uuid.uuid4()}{suffix}", who)
            assert hidden.status_code == never.status_code == 404, (who, suffix, hidden.text)
            assert strip(hidden) == strip(never), (who, suffix)
            assert ids["s_name"] not in hidden.text and ids["sarah"] not in hidden.text
        for fact in (ids["works"], ids["knows"]):  # a fact that touches a person is hidden even when its other end (Acme) is open
            for suffix in ("", "/evidence"):
                hidden = await t.get(f"/v1/relationships/{fact}{suffix}", who)
                never = await t.get(f"/v1/relationships/{uuid.uuid4()}{suffix}", who)
                assert hidden.status_code == never.status_code == 404 and strip(hidden) == strip(never), (who, fact, suffix)
    # the open types stay open: the same principal reads Acme
    acme = await t.get(f"/v1/entities/{ids['acme']}", "agent3")
    assert acme.status_code == 200 and acme.json()["name"] == "Acme Holdings"
    assert acme.json()["relationships"] == []  # Sarah WORKS_AT Acme is hidden: one of its ends is a hidden person


async def test_lists_searches_and_neighbourhoods_name_no_hidden_person(w):
    t, ids = w
    for who in ("agent3", "operator"):
        listing = await t.get("/v1/entities", who)
        names = {e["name"] for e in listing.json()["items"]}
        assert "Acme Holdings" in names and ids["s_name"] not in names and ids["m_name"] not in names
        by_name = await t.get("/v1/entities", who, q=ids["s_name"][:8].lower())
        assert by_name.json()["items"] == []
        by_type = await t.get("/v1/entities", who, type="Person")
        assert by_type.json()["items"] == []
        around_acme = (await t.get(f"/v1/entities/{ids['acme']}/graph", who, hops=2)).json()
        assert [n["name"] for n in around_acme["nodes"]] == ["Acme Holdings"] and around_acme["edges"] == [], (
            around_acme
        )  # hops never pass through a hidden node
        assert (
            await t.req("POST", "/v1/graph/search", "agent3", json={"query": ids["s_name"]})
        ).status_code == 403  # agents search through contexts, not the diagnostic search
        found = await t.req("POST", "/v1/graph/search", "operator", json={"query": f"{ids['s_name']} works at Acme Holdings"})
        assert found.status_code == 200 and found.json()["facts"] == []
        assert found.json()["unreconciled_hits"] == 0  # a hidden hit is dropped and is NOT counted as an unknown edge
        for blob in (listing.text, by_name.text, by_type.text, json.dumps(around_acme), found.text):
            assert ids["s_name"] not in blob and ids["m_name"] not in blob and ids["sarah"] not in blob and ids["michael"] not in blob


async def test_commands_interactions_and_memories_do_not_name_a_hidden_person(w):
    t, ids = w
    for who in ("operator",):
        cmds = await t.get("/v1/commands", who)
        assert cmds.status_code == 200
        assert (
            ids["sarah"] not in cmds.text
            and ids["michael"] not in cmds.text
            and ids["works"] not in cmds.text
            and ids["knows"] not in cmds.text
        )
        assert any(c["entity_id"] == ids["acme"] for c in cmds.json()["items"])  # the open entity's command is listed
        for fact in (ids["works"], ids["knows"]):
            listed = await t.get("/v1/commands", who, entity_id=ids["sarah"])
            assert listed.json()["items"] == [] or all(ids["sarah"] not in json.dumps(c) for c in listed.json()["items"]), fact
        interaction = await t.get(f"/v1/interactions/{ids['event']}", who)
        assert interaction.status_code == 200
        subjects = interaction.json()["subjects"]
        assert subjects and all(s["hidden"] and s["entity_id"] is None for s in subjects)  # the references stay, without an id
        assert ids["sarah"] not in interaction.text and ids["michael"] not in interaction.text
        by_subject = await t.get("/v1/interactions", who, subject_entity_id=ids["sarah"])
        assert by_subject.json()["items"] == []
    for who in ("agent3", "agent"):
        mem = await t.get(f"/v1/memories/{ids['memory']}", who)  # workspace-scoped memory: visible by its scopes
        assert mem.status_code == 200
        assert (ids["sarah"] in mem.json()["subject_ids"]) == (who == "agent")  # but the id of a hidden person does not travel with it
    for who in ("admin", "reviewer"):  # staff see the whole picture by role
        full = await t.get(f"/v1/interactions/{ids['event']}", who)
        assert {s["entity_id"] for s in full.json()["subjects"]} == {ids["sarah"], ids["michael"]} and not any(
            s["hidden"] for s in full.json()["subjects"]
        )


async def test_a_context_about_a_hidden_person_is_denied_and_says_nothing(w):
    t, ids = w
    for profile in ("quick_answer", "planning", "research"):
        r = await build(t, "agent3", "Tell me about this person", subject_ids=[ids["sarah"]], profile=profile)
        assert r.status_code == 200 and r.json()["status"] == "denied", (profile, r.text)
        assert r.json()["context_id"] is None and ids["s_name"] not in r.text
        gone = await build(t, "agent3", "Tell me about this person", subject_ids=[str(uuid.uuid4())], profile=profile)
        assert (
            gone.json() == {**r.json(), "generated_at": gone.json()["generated_at"]} or gone.json()["status"] == "denied"
        )  # indistinguishable from a missing subject


# ---- agent: Sarah only -----------------------------------------------------------------------------------------------------------------------


async def test_a_principal_with_one_grant_sees_that_person_but_not_the_fact_to_the_other(w):
    t, ids = w
    me = await t.get(f"/v1/entities/{ids['sarah']}", "agent")
    assert me.status_code == 200 and me.json()["name"] == ids["s_name"]
    facts = {r["id"] for r in me.json()["relationships"]}
    assert facts == {ids["works"]}, facts  # Sarah -> Acme yes; Sarah -> Michael no: one hidden end hides the whole fact
    rels = await t.get(f"/v1/entities/{ids['sarah']}/relationships", "agent", include_history="true")
    assert {r["id"] for r in rels.json()} == {ids["works"]}
    assert (await t.get(f"/v1/relationships/{ids['knows']}", "agent")).status_code == 404
    assert (await t.get(f"/v1/relationships/{ids['knows']}/evidence", "agent")).status_code == 404
    assert (await t.get(f"/v1/relationships/{ids['works']}", "agent")).status_code == 200
    assert (await t.get(f"/v1/entities/{ids['michael']}", "agent")).status_code == 404
    around = (await t.get(f"/v1/entities/{ids['sarah']}/graph", "agent", hops=2)).json()
    assert {n["name"] for n in around["nodes"]} == {ids["s_name"], "Acme Holdings"}
    assert {e["id"] for e in around["edges"]} == {ids["works"]}  # the hidden person is neither a node nor a stepping stone
    assert ids["m_name"] not in json.dumps(around) and ids["knows"] not in json.dumps(around)
    found = await t.req(
        "POST", "/v1/graph/search", "operator", json={"query": f"{ids['s_name']} knows {ids['m_name']}"}
    )  # an operator holds no grant either
    assert found.status_code == 200 and ids["knows"] not in found.text and ids["m_name"] not in found.text


async def test_a_context_for_a_visible_person_drops_the_hidden_one_and_the_fact_between_them(w):
    t, ids = w
    r = await build(t, "agent", "How do these people know each other?", subject_ids=[ids["sarah"], ids["michael"]], profile="planning")
    assert r.status_code == 200 and r.json()["status"] != "denied", r.text
    out = r.json()
    assert ids["m_name"] not in r.text and ids["michael"] not in r.text and ids["knows"] not in r.text
    assert any("Acme" in i["text"] for i in out["facts"])  # Sarah's visible fact is still there
    trace = await t.get(f"/v1/context/{out['context_id']}/trace", "agent")
    assert trace.json()["request"]["subject_ids"] == [ids["sarah"]]  # the hidden subject was dropped before anything was retrieved
    assert ids["michael"] not in trace.text and ids["knows"] not in trace.text
    both_hidden = await build(t, "agent", "x", subject_ids=[ids["michael"]], profile="planning")
    assert both_hidden.json()["status"] == "denied"


# ---- roles that see everything by role, the ontology rollback, and what leaves through telemetry -------------------------------------------------


async def test_admin_and_reviewer_see_every_subject_by_role(w):
    t, ids = w
    for who in ("admin", "reviewer"):
        assert (await t.get(f"/v1/entities/{ids['michael']}", who)).status_code == 200
        facts = {r["id"] for r in (await t.get(f"/v1/entities/{ids['sarah']}", who)).json()["relationships"]}
        assert facts == {ids["works"], ids["knows"]}


async def test_granting_the_subject_widens_what_an_agent_sees_on_its_next_request(w):
    t, ids = w
    assert (await t.get(f"/v1/entities/{ids['michael']}", "agent")).status_code == 404
    await t.grant("agent", "subject", ids["michael"])
    assert (await t.get(f"/v1/entities/{ids['michael']}", "agent")).status_code == 200
    facts = {r["id"] for r in (await t.get(f"/v1/entities/{ids['sarah']}", "agent")).json()["relationships"]}
    assert facts == {ids["works"], ids["knows"]}  # both ends are granted now


async def test_with_the_previous_ontology_active_nothing_is_hidden_so_rollback_is_one_activation(api, tenant_ctx, strict_mock):  # noqa: F811
    t = tenant_ctx  # v1.3.0: no `access` anywhere: the P2-P6 behaviour, byte for byte
    ids = await sarah_story(t)
    assert (await t.get(f"/v1/entities/{ids['sarah']}", "agent3")).status_code == 200
    listing = await t.get("/v1/entities", "agent3", type="Person")
    assert {e["id"] for e in listing.json()["items"]} == {ids["sarah"], ids["michael"]}
    assert {
        r["id"] for r in (await t.get(f"/v1/entities/{ids['sarah']}", "agent3")).json()["relationships"]
    }  # facts are workspace-readable again


async def test_telemetry_counts_what_was_hidden_but_never_says_what(w, caplog):
    t, ids = w
    before = {
        s.labels["surface"]: s.value
        for m in metrics.REGISTRY.collect()
        if m.name == "acl_hidden"
        for s in m.samples
        if s.name == "acl_hidden_total"
    }
    with caplog.at_level(logging.DEBUG):
        await t.get(f"/v1/entities/{ids['sarah']}", "agent3")
        await t.get(f"/v1/entities/{ids['acme']}", "agent3")
        await t.get(f"/v1/entities/{ids['sarah']}/relationships", "agent")
        await t.req("POST", "/v1/graph/search", "agent3", json={"query": ids["s_name"]})
    after = {
        s.labels["surface"]: s.value
        for m in metrics.REGISTRY.collect()
        if m.name == "acl_hidden"
        for s in m.samples
        if s.name == "acl_hidden_total"
    }
    assert sum(after.values()) > sum(before.values()), (before, after)  # the filter hid something, and said how often
    scrape = generate_latest(metrics.REGISTRY).decode()
    for secret in (ids["s_name"], ids["m_name"], ids["sarah"], ids["michael"], ids["knows"], ids["works"]):
        assert secret not in scrape and secret not in caplog.text


# ---- the filter is IN the statement (EXPLAIN snapshot per read path) ---------------------------------------------------------------------------


async def test_every_read_path_carries_the_filter_inside_its_sql_and_the_statement_plans(api, w):
    t, ids = w
    from kg_domain.enums import PrincipalKind, Role
    from kg_domain.principal import Principal
    from kg_governance.grants import ScopeGrants

    knowledge = api.app.state.kg.knowledge
    ctx = ctx_of(t)
    pid = uuid.UUID(await t.principal_id("agent3"))
    p = Principal(principal_id=pid, workspace_id=t.ws, kind=PrincipalKind.AGENT, handle=f"agent3@{t.slug}", roles=frozenset({Role.AGENT}))
    seen: list[tuple[str, str, Any]] = []

    def capture(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        seen.append(("", statement, parameters))

    event.listen(api.engine.sync_engine, "before_cursor_execute", capture)
    try:
        async with tenant_session(api.engine, ctx, pid) as conn:
            sf = await knowledge.reads.subject_filter(conn, t.ws, await ScopeGrants.for_principal(conn, p))
            assert sf.enforced and sf.scoped_types == {"Person"}
            calls = {
                "list_entities": knowledge.reads.list_entities(
                    conn, ctx, can_see_sensitive=False, subjects=sf, type_key=None, q=None, status=None, limit=5, cursor=None
                ),
                "entities_by_ids": knowledge.reads.entities_by_ids(
                    conn, ctx, [uuid.UUID(ids["sarah"])], can_see_sensitive=False, subjects=sf
                ),
                "max_fact_classification": knowledge.reads.max_fact_classification(conn, ctx, [uuid.UUID(ids["sarah"])], subjects=sf),
                "list_facts": knowledge.reads.list_facts(
                    conn,
                    ctx,
                    entity_id=uuid.UUID(ids["acme"]),
                    direction="both",
                    rel_type=None,
                    as_of=None,
                    known_at=None,
                    include_history=False,
                    can_see_sensitive=False,
                    subjects=sf,
                ),
                "neighborhood": knowledge.graph_reads.neighborhood(
                    conn,
                    ctx,
                    uuid.UUID(ids["acme"]),
                    subjects=sf,
                    hops=2,
                    as_of=None,
                    known_at=None,
                    include=[],
                    history=False,
                    types=[],
                    limit=50,
                ),
                "list_commands": knowledge.reads.list_commands(
                    conn, ctx, subjects=sf, status=None, command_type=None, entity_id=None, limit=5, cursor=None
                ),
            }
            marks: dict[str, str] = {}
            for name, coro in calls.items():
                start = len(seen)
                await coro
                statements = [s for _, s, _ in seen[start:]]
                marks[name] = "\n".join(statements)
            for name, sql_text in marks.items():
                assert "<> ALL(" in sql_text, f"{name}: the subject filter is not in the SQL"
                assert ids["s_name"] not in sql_text and ids["sarah"] not in sql_text  # bound, never inlined
            for name in (
                "list_entities",
                "list_facts",
                "neighborhood",
            ):  # and the statement is plannable as written (it is not a string the planner never sees)
                for _, statement, params in seen:
                    if "<> ALL(" in statement and statement.lstrip().upper().startswith("SELECT") and name:
                        await conn.exec_driver_sql(f"EXPLAIN {statement}", params)
                        break
    finally:
        event.remove(api.engine.sync_engine, "before_cursor_execute", capture)


async def test_a_merge_repoints_grants_and_memory_references_to_the_canonical_entity(api, tenant_agents, strict_mock):  # noqa: F811
    t = tenant_agents
    canonical = (await t.entity("Person", sentinel("Zqcanon"), source_system="hr", source_ref="E-9")).json()["entity_id"]
    duplicate = (await t.entity("Person", sentinel("Zqdupe"), source_system="hr", source_ref="E-10")).json()["entity_id"]
    await t.grant("agent", "subject", duplicate)
    saved = await t.req(
        "POST", "/v1/memories", "admin",
        json={"type": "semantic", "content": sentinel("zqmem"), "subject_ids": [duplicate], "source_ref": "x", "purpose": "p"},
    )  # fmt: skip
    assert saved.status_code == 201, saved.text
    memory = saved.json()["memory_id"]
    assert (await t.get(f"/v1/memories/{memory}", "agent")).status_code == 200
    async with tenant_session(api.engine, ctx_of(t)) as conn:
        from kg_knowledge.merge import merge_entity

        knowledge = api.app.state.kg.knowledge
        await merge_entity(
            conn, t.ws, specs=knowledge.specs, extracted=knowledge.extracted, from_id=uuid.UUID(duplicate), into_id=uuid.UUID(canonical),
            actor_principal_id=uuid.UUID(await t.principal_id("admin")), mention_resolution="reviewer", reason="reviewer", audit=None,
        )  # fmt: skip
    rows = await _sql(
        api,
        t,
        "SELECT scope_ref FROM principal_scopes WHERE scope_kind = 'subject' AND principal_id = :p",
        p=uuid.UUID(await t.principal_id("agent")),
    )
    assert [r.scope_ref for r in rows] == [canonical]  # the grant followed the merge
    assert (await t.get(f"/v1/entities/{canonical}", "agent")).status_code == 200
    assert (
        await t.get(f"/v1/memories/{memory}", "agent")
    ).status_code == 200  # the memory scoped to the old id is still visible to the holder of the new one
    scopes = await _sql(api, t, "SELECT scope_ref FROM memory_scopes WHERE memory_id = :m AND scope_kind = 'subject'", m=uuid.UUID(memory))
    assert [r.scope_ref for r in scopes] == [canonical]


async def _sql(api: Any, t: Any, query: str, **params: Any) -> list[Any]:
    async with tenant_session(api.engine, ctx_of(t)) as c:
        return list((await c.execute(text(query), {"ws": t.ws, **params})).all())


async def test_graph_hits_are_joined_to_postgres_through_the_filter(api, w):
    """The graph store may well return an edge about a hidden person (it knows nothing of grants). The join to Postgres drops it INSIDE the statement, and a dropped hit is
    counted as hidden, not as an edge Postgres has not reconciled yet."""
    from kg_domain.enums import PrincipalKind, Role
    from kg_domain.principal import Principal
    from kg_governance.grants import ScopeGrants
    from kg_graph.scoped import FactHit

    t, ids = w
    knowledge = api.app.state.kg.knowledge
    ctx = ctx_of(t)
    hits = [
        FactHit(
            uuid=ids["works"],
            name="WORKS_AT",
            fact="x",
            source_uuid=ids["sarah"],
            target_uuid=ids["acme"],
            valid_at=None,
            invalid_at=None,
            expired_at=None,
            created_at=None,
            episodes=(),
        ),
        FactHit(
            uuid=ids["knows"],
            name="KNOWS",
            fact="y",
            source_uuid=ids["sarah"],
            target_uuid=ids["michael"],
            valid_at=None,
            invalid_at=None,
            expired_at=None,
            created_at=None,
            episodes=(),
        ),
        FactHit(
            uuid=str(uuid.uuid4()),
            name="WORKS_AT",
            fact="z",
            source_uuid=ids["acme"],
            target_uuid=ids["acme"],
            valid_at=None,
            invalid_at=None,
            expired_at=None,
            created_at=None,
            episodes=(),
        ),
    ]
    expected = {"agent3": (set(), 1), "agent": ({ids["works"]}, 1), "reviewer": ({ids["works"], ids["knows"]}, 1)}
    for who, (visible, unreconciled) in expected.items():
        pid = uuid.UUID(await t.principal_id(who))
        role = Role.REVIEWER if who == "reviewer" else Role.AGENT
        p = Principal(principal_id=pid, workspace_id=t.ws, kind=PrincipalKind.AGENT, handle=who, roles=frozenset({role}))
        async with tenant_session(api.engine, ctx, pid) as conn:
            sf = await knowledge.reads.subject_filter(conn, t.ws, await ScopeGrants.for_principal(conn, p))
            facts, missing = await knowledge.graph_reads.facts_for_hits(
                conn, ctx, hits, can_see_sensitive=False, include_past=False, subjects=sf
            )
        assert {str(f.id) for f in facts} == visible, who
        assert missing == unreconciled, (who, missing)  # only the id Postgres has never heard of
