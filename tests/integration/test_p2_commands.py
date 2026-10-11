"""WP6: deterministic commands and reads. KGR-01..05 (command subset), ONT-04, idempotency, redaction, concurrency, isolation."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import uuid

import pytest
import yaml
from kg_db.session import tenant_session
from kg_domain.principal import WorkspaceContext
from sqlalchemy import text

from tests.support.tenants import EXAMPLE_YAML, clear_graph, make_tenant

pytestmark = pytest.mark.integration
HR = lambda v: [{"system": "hr", "value": v}]  # noqa: E731


async def sarah_and_acme(t):  # type: ignore[no-untyped-def]
    sarah = await t.entity(
        "Person",
        "Sarah Johnson",
        external_ids=HR("E-1001"),
        attributes={"birth_date": "1985-03-02"},
        source_system="hr",
        source_ref="E-1001",
    )
    acme = await t.entity("Organization", "Acme", external_ids=HR("ORG-1"), source_system="hr", source_ref="ORG-1")
    assert sarah.status_code == acme.status_code == 201, (sarah.text, acme.text)
    return sarah.json()["entity_id"], acme.json()["entity_id"]


async def evt(t, sarah, acme, ref, start, role, **body):  # type: ignore[no-untyped-def]
    return await t.relationship(
        "WORKS_AT", sarah, acme, valid_from=start, attributes={"role": role}, source_system="hr", source_ref=ref, **body
    )


async def db_now(api) -> dt.datetime:  # type: ignore[no-untyped-def]
    async with tenant_session(api.engine, WorkspaceContext.for_system_task(uuid.uuid4(), reason="clock")) as c:
        return (await c.execute(text("SELECT now()"))).scalar_one()  # type: ignore[no-any-return]


def instant(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---- the Sarah story (evt_001 / evt_002) -------------------------------------------------------------------------------------------------------


async def test_kgr03_04_sarah_evt_001_and_evt_002(api, tenant):
    t = tenant
    sarah, acme = await sarah_and_acme(t)
    e1 = await evt(t, sarah, acme, "evt_001", "2022-01-10", "Software Engineer")
    assert e1.status_code == 201 and e1.json()["status"] == "applied" and e1.json()["ended_fact_ids"] == []
    e2 = await evt(t, sarah, acme, "evt_002", "2024-06-01", "Engineering Manager", source_recorded_at="2024-06-10")
    assert e2.status_code == 201
    assert e2.json()["ended_fact_ids"] == [e1.json()["fact_id"]]  # succession ended the predecessor, it did not supersede it

    first = (await t.get(f"/v1/relationships/{e1.json()['fact_id']}", "reader")).json()
    second = (await t.get(f"/v1/relationships/{e2.json()['fact_id']}", "reader")).json()
    assert (
        first["valid_from"].startswith("2022-01-10")
        and first["valid_to"].startswith("2024-06-01")
        and first["status"] == "active"
        and first["phase"] == "historical"
    )
    assert second["valid_to"] is None and second["phase"] == "current" and second["status"] == "active"
    assert first["succeeded_by_fact_id"] == second["id"] and first["superseded_at"] is None  # succession is not supersession (ADR-023)
    assert second["source_recorded_at"].startswith("2024-06-10") and first["source_recorded_at"] is None
    assert abs((await db_now(api)) - instant(second["recorded_at"])) < dt.timedelta(
        minutes=2
    )  # recorded_at is the platform's own time, not 2024-06-10

    current = (await t.get(f"/v1/entities/{sarah}", "agent")).json()["relationships"]
    assert [(r["attributes"]["role"], r["target"]["name"]) for r in current] == [("Engineering Manager", "Acme")]
    past = (await t.get(f"/v1/entities/{sarah}", "agent", as_of="2023-01-01")).json()["relationships"]
    assert [r["attributes"]["role"] for r in past] == ["Software Engineer"]
    assert (await t.get(f"/v1/entities/{sarah}", "agent", as_of="2021-01-01")).json()["relationships"] == []
    history = (await t.get(f"/v1/entities/{sarah}", "agent", include="history")).json()["relationships"]
    assert [r["attributes"]["role"] for r in history] == ["Software Engineer", "Engineering Manager"]
    assert (await t.get(f"/v1/entities/{sarah}", "agent", known_at="2020-01-01")).json()["relationships"] == []  # nothing was recorded yet
    assert len((await t.get(f"/v1/entities/{sarah}", "agent", include="history", known_at="2020-01-01")).json()["relationships"]) == 0
    out = (await t.get(f"/v1/entities/{sarah}/relationships", "agent", direction="out", type="WORKS_AT", include_history=True)).json()
    assert (
        len(out) == 2
        and (await t.get(f"/v1/entities/{acme}/relationships", "agent", direction="in")).json()[0]["source"]["name"] == "Sarah Johnson"
    )
    assert (await t.get(f"/v1/entities/{acme}/relationships", "agent", direction="out")).json() == []


async def test_kgr05_every_fact_names_its_command_and_source(tenant):
    t = tenant
    sarah, acme = await sarah_and_acme(t)
    r = await evt(t, sarah, acme, "evt_001", "2022-01-10", "Software Engineer")
    fact = (await t.get(f"/v1/relationships/{r.json()['fact_id']}", "reviewer")).json()
    assert fact["source_command_id"] == r.json()["command_id"] and (fact["source_system"], fact["source_ref"]) == ("hr", "evt_001")
    assert (
        fact["authority_class"] == "authoritative_system"
        and fact["source_type"] == "command"
        and fact["ontology_version_id"] == r.json()["ontology_version_id"]
    )
    cmd = (await t.get(f"/v1/commands/{fact['source_command_id']}", "reviewer")).json()
    assert cmd["command_type"] == "relationship.assert" and cmd["status"] == "applied" and cmd["fact_id"] == fact["id"]
    stages = {s["stage"]: s["outcome"] for s in cmd["decision"]}
    assert (
        stages["schema_validation"] == "pass"
        and stages["temporal_rules"] == "pass"
        and stages["mutation_authorization"] == "pass"
        and stages["decision"] == "accept"
    )
    assert "Software Engineer" not in json.dumps(cmd) and "Sarah" not in json.dumps(cmd)  # the trace holds codes and paths, never values


async def test_kgr01_the_same_external_id_returns_the_existing_entity(tenant):
    t = tenant
    first = await t.entity("Person", "Sarah Johnson", external_ids=HR("E-1001"))
    again = await t.entity(
        "Person", "Sarah J. (renamed)", external_ids=HR("E-1001")
    )  # a different name does not matter: the identifier does
    assert first.status_code == 201 and again.status_code == 200
    assert again.json()["matched_existing"] is True and again.json()["entity_id"] == first.json()["entity_id"]
    assert again.json()["command_id"] != first.json()["command_id"]
    got = (await t.get(f"/v1/entities/{first.json()['entity_id']}", "reader")).json()
    assert (
        got["name"] == "Sarah Johnson" and got["revision"] == 1 and got["external_ids"] == [{"system": "hr", "value": "E-1001"}]
    )  # a create is never an upsert


async def test_kgr02_names_never_merge_entities(tenant):
    t = tenant
    a, b = await t.entity("Person", "Sarah Johnson"), await t.entity("Person", "Sarah Johnson")
    assert a.status_code == b.status_code == 201 and a.json()["entity_id"] != b.json()["entity_id"]
    found = (await t.get("/v1/entities", "reader", q="sarah johnson", type="Person")).json()["items"]
    assert len(found) == 2
    assert (await t.get("/v1/entities", "reader", q="%")).json()["items"] == []  # LIKE wildcards in the query are literals


async def test_conflicting_external_ids_are_a_rule_violation_not_a_guess(tenant):
    t = tenant
    a = (await t.entity("Person", "A", external_ids=HR("E-1"))).json()["entity_id"]
    b = (await t.entity("Person", "B", external_ids=HR("E-2"))).json()["entity_id"]
    both = await t.entity("Person", "C", external_ids=[*HR("E-1"), *HR("E-2")])
    assert both.status_code == 422 and both.json()["error"]["details"]["violations"][0]["code"] == "ambiguous_identity"
    assert a != b
    unknown_system = await t.entity("Person", "D", external_ids=[{"system": "crm", "value": "1"}])
    assert unknown_system.json()["error"]["details"]["violations"][0]["code"] == "unknown_external_id_system"


# ---- ONT-04 -------------------------------------------------------------------------------------------------------------------------------


async def test_ont04_a_disallowed_edge_is_rejected_without_echoing_values(tenant):
    t = tenant
    sarah, _ = await sarah_and_acme(t)
    topic = (await t.entity("Topic", "Entrepreneurship")).json()["entity_id"]
    r = await t.relationship("WORKS_AT", sarah, topic, valid_from="2025-01-01", source_system="hr", source_ref="x")
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "rule_violation" and err["details"]["violations"] == [
        {"rule_kind": "endpoint_types", "rule_id": None, "code": "disallowed_endpoint", "path": "target"}
    ]
    assert "Entrepreneurship" not in r.text and "Sarah" not in r.text
    ledger = (await t.get(f"/v1/commands/{err['details']['command_id']}", "reviewer")).json()  # the refusal is recorded too
    assert (
        ledger["status"] == "rejected"
        and ledger["http_status"] == 422
        and ledger["projection_status"] == "not_applicable"
        and ledger["fact_id"] is None
    )
    assert (await t.get(f"/v1/entities/{sarah}", "reader", include="history")).json()["relationships"] == []


async def test_ont04_a_queue_rule_turns_the_rejection_into_a_review_item_and_writes_nothing(api):
    d = yaml.safe_load(EXAMPLE_YAML)
    d["relationship_types"]["WORKS_AT"]["rules"].append({"kind": "queue_if", "params": {"when": "disallowed_endpoint"}})
    t = await make_tenant(api, ontology=yaml.safe_dump(d))
    try:
        sarah, _ = await sarah_and_acme(t)
        topic = (await t.entity("Topic", "Entrepreneurship")).json()["entity_id"]
        r = await t.relationship("WORKS_AT", sarah, topic, valid_from="2025-01-01", source_system="hr", source_ref="x")
        assert r.status_code == 202 and r.json()["status"] == "needs_review" and r.json()["review_item_id"] and r.json()["fact_id"] is None
        assert r.headers["location"] == f"/v1/commands/{r.json()['command_id']}"
        assert (await t.get(f"/v1/entities/{sarah}", "reader", include="history")).json()["relationships"] == []
        async with tenant_session(api.engine, WorkspaceContext.for_system_task(t.ws, reason="test")) as c:
            rows = (
                await c.execute(
                    text("SELECT item_type, status, rule_kind, reason_code FROM review_items WHERE workspace_id = :w"), {"w": t.ws}
                )
            ).all()
            facts = (await c.execute(text("SELECT count(*) FROM facts WHERE workspace_id = :w"), {"w": t.ws})).scalar_one()
        assert [tuple(r) for r in rows] == [("policy_violation", "open", "endpoint_types", "disallowed_endpoint")] and facts == 0
        queued = (await t.get("/v1/commands?status=needs_review", "reviewer")).json()["items"]
        assert [c["id"] for c in queued] == [r.json()["command_id"]]
        # a replay answers the same 202, still with one review item
        again = await t.relationship("WORKS_AT", sarah, topic, valid_from="2025-01-01", source_system="hr", source_ref="x", key=None)
        assert again.status_code == 202 and again.json()["command_id"] != r.json()["command_id"]
    finally:
        await clear_graph(api, t.group_id)


# ---- idempotency ----------------------------------------------------------------------------------------------------------------------------


async def test_the_same_key_replays_and_the_same_key_with_another_body_conflicts(tenant):
    t = tenant
    body = {"type": "Person", "name": "Michael", "external_ids": HR("E-77")}
    first = await t.req("POST", "/v1/entities", "app", json=body, key="create-michael")
    replay = await t.req("POST", "/v1/entities", "app", json=body, key="create-michael")
    assert first.status_code == 201 and replay.status_code == 201 and replay.headers["idempotent-replay"] == "true"
    assert {k: v for k, v in replay.json().items() if k != "projection_status"} == {
        k: v for k, v in first.json().items() if k != "projection_status"
    }
    clash = await t.req("POST", "/v1/entities", "app", json={**body, "name": "Mike"}, key="create-michael")
    assert clash.status_code == 409 and clash.json()["error"]["code"] == "idempotency_mismatch"
    assert (
        len((await t.get("/v1/entities", "reader")).json()["items"]) == 1
        and len((await t.get("/v1/commands", "reviewer")).json()["items"]) == 1
    )
    nokey = await t.req("POST", "/v1/entities", "app", json=body, key="")
    assert nokey.status_code == 400 and nokey.json()["error"]["code"] == "idempotency_key_required"


async def test_a_rejected_command_replays_as_the_same_rejection(tenant):
    t = tenant
    sarah, _ = await sarah_and_acme(t)
    topic = (await t.entity("Topic", "Topic")).json()["entity_id"]
    one = await t.relationship("WORKS_AT", sarah, topic, valid_from="2025-01-01", source_system="hr", source_ref="x", key="same")
    two = await t.relationship("WORKS_AT", sarah, topic, valid_from="2025-01-01", source_system="hr", source_ref="x", key="same")
    assert one.status_code == two.status_code == 422 and two.headers["idempotent-replay"] == "true"
    assert one.json()["error"]["details"] == two.json()["error"]["details"]


# ---- rules in the pipeline ------------------------------------------------------------------------------------------------------------------


async def test_validation_errors_are_reported_by_path_and_never_echo_values(tenant):
    t = tenant
    r = await t.entity("Person", "Sarah", attributes={"birth_date": "not-a-date", "shoe_size": 9, "name": "x"}, classification="local_only")
    assert r.status_code == 422
    v = {(x["code"], x["path"]) for x in r.json()["error"]["details"]["violations"]}
    assert (
        ("wrong_type", "attributes.birth_date") in v
        and ("unknown_attribute", "attributes.shoe_size") in v
        and ("attribute_is_node_name", "attributes.name") in v
    )
    assert "not-a-date" not in r.text and 'shoe_size": 9' not in r.text
    assert (await t.entity("Spaceship", "X")).json()["error"]["details"]["violations"][0]["code"] == "unknown_entity_type"
    assert (await t.entity("Person", "")).status_code == 422
    classification = await t.entity("Person", "Sarah", classification="local_only")
    assert classification.json()["error"]["details"]["violations"][0]["code"] == "classification_not_allowed"


async def test_temporal_rules_preserve_valid_time_and_order(tenant):
    t = tenant
    sarah, acme = await sarah_and_acme(t)
    missing = await t.relationship("WORKS_AT", sarah, acme, source_system="hr", source_ref="x")
    assert missing.json()["error"]["details"]["violations"][0]["code"] == "valid_from_required"  # never defaulted to now
    unknown = await t.relationship("WORKS_AT", sarah, acme, valid_from_unknown=True, source_system="hr", source_ref="x")
    assert unknown.status_code == 201
    assert (await t.get(f"/v1/relationships/{unknown.json()['fact_id']}", "reader")).json()["valid_from_unknown"] is True
    no_source = await t.relationship("WORKS_AT", sarah, acme, valid_from="2020-01-01")
    assert {v["code"] for v in no_source.json()["error"]["details"]["violations"]} == {"source_required"}
    late = await evt(t, sarah, acme, "evt_2", "2024-06-01", "Engineering Manager")
    assert late.status_code == 201
    early = await evt(t, sarah, acme, "evt_1", "2022-01-10", "Software Engineer")
    assert early.status_code == 422 and early.json()["error"]["details"]["violations"][0]["code"] == "out_of_order_succession"
    wedge = await evt(
        t, sarah, acme, "evt_0", "2018-01-10", "Intern", valid_to="2019-01-01"
    )  # inside the unknown-start fact that now ends in 2024
    assert wedge.json()["error"]["details"]["violations"][0]["code"] == "succession_conflict"
    bad_interval = await evt(t, sarah, acme, "evt_3", "2025-01-10", "X", valid_to="2024-01-01")
    assert bad_interval.json()["error"]["details"]["violations"][0]["code"] == "invalid_interval"


async def test_relationship_end_closes_an_open_fact_once(tenant):
    t = tenant
    sarah, acme = await sarah_and_acme(t)
    fact = (await evt(t, sarah, acme, "evt_1", "2022-01-10", "Software Engineer")).json()["fact_id"]
    r = await t.req(
        "POST", f"/v1/relationships/{fact}/end", "app", json={"valid_to": "2025-03-31", "source_system": "hr", "source_ref": "evt_9"}
    )
    assert r.status_code == 201 and r.json()["command_type"] == "relationship.end" and r.json()["ended_fact_ids"] == [fact]
    after = (await t.get(f"/v1/relationships/{fact}", "reader")).json()
    assert after["valid_to"].startswith("2025-03-31") and after["status"] == "active" and after["revision"] == 2
    twice = await t.req(
        "POST", f"/v1/relationships/{fact}/end", "app", json={"valid_to": "2025-04-30", "source_system": "hr", "source_ref": "evt_10"}
    )
    assert twice.status_code == 422 and twice.json()["error"]["details"]["violations"][0]["code"] == "already_ended"
    before = await t.req(
        "POST",
        f"/v1/relationships/{(await evt(t, sarah, acme, 'e2', '2026-01-01', 'X')).json()['fact_id']}/end",
        "app",
        json={"valid_to": "2025-01-01", "source_system": "hr", "source_ref": "r"},
    )
    assert before.json()["error"]["details"]["violations"][0]["code"] == "invalid_interval"
    assert (await t.req("POST", f"/v1/relationships/{uuid.uuid4()}/end", "app", json={"valid_to": "2025-01-01"})).status_code == 404
    assert (await t.get("/v1/entities/" + sarah, "reader", as_of="2025-06-01")).json()[
        "relationships"
    ] == []  # ended in March; the next fact starts in 2026


async def test_entity_update_is_optimistic_and_respects_editability(api):
    d = yaml.safe_load(EXAMPLE_YAML)
    d["entity_types"]["Person"]["attributes"]["badge"] = {"type": "string", "editable": False}
    d["entity_types"]["Person"]["attributes"]["nickname"] = {"type": "string", "max_length": 5}
    t = await make_tenant(api, ontology=yaml.safe_dump(d))
    try:
        sarah = (await t.entity("Person", "Sarah", attributes={"badge": "B-1"}, external_ids=HR("E-1"))).json()["entity_id"]
        etag = (await t.get(f"/v1/entities/{sarah}", "app")).headers["etag"]
        assert etag == 'W/"rev-1"'
        none = await t.req("PATCH", f"/v1/entities/{sarah}", "app", json={"attributes": {"nickname": "Sas"}})
        assert none.status_code == 428 and none.json()["error"]["code"] == "precondition_required"
        stale = await t.req(
            "PATCH", f"/v1/entities/{sarah}", "app", json={"attributes": {"nickname": "Sas"}}, headers={"If-Match": 'W/"rev-9"'}
        )
        assert stale.status_code == 412
        ok = await t.req(
            "PATCH",
            f"/v1/entities/{sarah}",
            "app",
            json={"attributes": {"nickname": "Sas"}, "add_aliases": ["S. Johnson"], "add_external_ids": HR("E-1-b")},
            headers={"If-Match": etag},
        )
        assert ok.status_code == 201 and ok.json()["command_type"] == "entity.update"
        got = (await t.get(f"/v1/entities/{sarah}", "app")).json()
        assert (
            got["attributes"] == {"badge": "B-1", "nickname": "Sas"}
            and got["aliases"] == ["S. Johnson"]
            and got["revision"] == 2
            and len(got["external_ids"]) == 2
        )
        fixed = await t.req(
            "PATCH", f"/v1/entities/{sarah}", "app", json={"attributes": {"badge": "B-2"}}, headers={"If-Match": 'W/"rev-2"'}
        )
        assert fixed.status_code == 422 and fixed.json()["error"]["details"]["violations"][0]["code"] == "attribute_not_editable"
        taken = (await t.entity("Person", "Michael", external_ids=HR("E-2"))).json()["entity_id"]
        steal = await t.req(
            "PATCH", f"/v1/entities/{taken}", "app", json={"add_external_ids": HR("E-1")}, headers={"If-Match": 'W/"rev-1"'}
        )
        assert steal.status_code == 422 and steal.json()["error"]["details"]["violations"][0]["code"] == "external_id_in_use"
        cleared = await t.req(
            "PATCH",
            f"/v1/entities/{sarah}",
            "app",
            json={"attributes": {"nickname": None}, "name": "Sarah J"},
            headers={"If-Match": 'W/"rev-2"'},
        )
        assert cleared.status_code == 201
        after = (await t.get(f"/v1/entities/{sarah}", "app")).json()
        assert after["attributes"] == {"badge": "B-1"} and after["name"] == "Sarah J" and after["revision"] == 3
        assert (await t.req("PATCH", f"/v1/entities/{uuid.uuid4()}", "app", json={}, headers={"If-Match": 'W/"rev-1"'})).status_code == 404
    finally:
        await clear_graph(api, t.group_id)


async def test_a_workspace_without_an_active_ontology_refuses_commands(bare_tenant):
    r = await bare_tenant.entity("Person", "Sarah")
    assert r.status_code == 409 and r.json()["error"]["code"] == "no_active_ontology"


async def test_authority_comes_from_the_role_and_can_only_be_lowered(tenant):
    t = tenant
    by_app = (await t.entity("Person", "A", source_system="hr", source_ref="1")).json()["command_id"]
    by_admin = (await t.entity("Person", "B", who="admin")).json()["command_id"]
    lowered = await t.entity("Person", "C", authority="user_asserted")
    raised = await t.entity("Person", "D", who="admin", authority="authoritative_system")
    assert raised.status_code == 422 and raised.json()["error"]["details"]["fields"] == ["authority"]
    cmds = {c["id"]: c["authority_class"] for c in (await t.get("/v1/commands", "reviewer")).json()["items"]}
    assert (
        cmds[by_app] == "authoritative_system"
        and cmds[by_admin] == "admin_asserted"
        and cmds[lowered.json()["command_id"]] == "user_asserted"
    )


async def test_min_authority_rejects_a_lowered_request(api):
    d = yaml.safe_load(EXAMPLE_YAML)
    d["entity_types"]["Person"]["rules"] = [{"kind": "min_authority", "params": {"level": "admin_asserted"}}]
    t = await make_tenant(api, ontology=yaml.safe_dump(d))
    try:
        assert (await t.entity("Person", "A")).status_code == 201
        low = await t.entity("Person", "B", authority="user_asserted")
        assert low.status_code == 422 and low.json()["error"]["details"]["violations"][0]["code"] == "insufficient_authority"
    finally:
        await clear_graph(api, t.group_id)


# ---- roles, redaction, isolation ----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("who", ["agent", "reader", "reviewer", "operator"])
async def test_only_admins_and_apps_write_commands(tenant, who):
    t = tenant
    sarah, acme = await sarah_and_acme(t)
    assert (await t.entity("Person", "X", who=who)).status_code == 403
    assert (await t.relationship("WORKS_AT", sarah, acme, who=who, valid_from="2020-01-01")).status_code == 403
    assert (await t.req("PATCH", f"/v1/entities/{sarah}", who, json={}, headers={"If-Match": 'W/"rev-1"'})).status_code == 403
    assert (await t.req("POST", f"/v1/relationships/{uuid.uuid4()}/end", who, json={"valid_to": "2020-01-01"})).status_code == 403
    assert (await t.get(f"/v1/entities/{sarah}", who)).status_code == 200 and (await t.get("/v1/entities", who)).status_code == 200


@pytest.mark.parametrize("who", ["agent", "reader"])
async def test_agents_and_readers_cannot_read_the_command_ledger(tenant, who):
    assert (await tenant.get("/v1/commands", who)).status_code == 403


async def test_sensitive_attributes_are_redacted_for_everyone_but_admins(api, tenant):
    t = tenant
    sarah, _ = await sarah_and_acme(t)
    as_app = (await t.get(f"/v1/entities/{sarah}", "app")).json()
    as_admin = (await t.get(f"/v1/entities/{sarah}", "admin")).json()
    assert as_app["attributes"]["birth_date"] == {"redacted": True} and as_admin["attributes"]["birth_date"] == "1985-03-02"
    listed = (await t.get("/v1/entities", "reviewer", type="Person")).json()["items"][0]
    assert listed["attributes"]["birth_date"] == {"redacted": True}
    assert "1985-03-02" not in json.dumps((await t.get("/v1/commands", "reviewer")).json())
    async with tenant_session(
        api.engine, WorkspaceContext.for_system_task(t.ws, reason="test")
    ) as c:  # even the ledger keeps the value out of the stored payload
        payloads = [r[0] for r in (await c.execute(text("SELECT payload::text FROM commands WHERE workspace_id = :w"), {"w": t.ws})).all()]
    assert payloads and not any("1985-03-02" in p for p in payloads) and any("[redacted]" in p for p in payloads)


async def test_cross_workspace_ids_are_indistinguishable_from_random_ones(api, tenant):
    t = tenant
    sarah, acme = await sarah_and_acme(t)
    fact = (await evt(t, sarah, acme, "evt_1", "2022-01-10", "Software Engineer")).json()
    other = await make_tenant(api)
    try:
        mine = {"entity": sarah, "fact": fact["fact_id"], "command": fact["command_id"]}
        for path in ("/v1/entities/{}", "/v1/entities/{}/relationships", "/v1/relationships/{}", "/v1/commands/{}"):
            ident = mine["entity" if "entities" in path else "fact" if "relationships" in path else "command"]
            foreign = await other.get(path.format(ident), "admin")
            random = await other.get(path.format(uuid.uuid4()), "admin")
            assert foreign.status_code == random.status_code == 404, path
            assert {k: v for k, v in foreign.json()["error"].items() if k != "request_id"} == {
                k: v for k, v in random.json()["error"].items() if k != "request_id"
            }
        assert (await other.req("PATCH", f"/v1/entities/{sarah}", "admin", json={}, headers={"If-Match": 'W/"rev-1"'})).status_code == 404
        assert (
            await other.req("POST", f"/v1/relationships/{fact['fact_id']}/end", "admin", json={"valid_to": "2025-01-01"})
        ).status_code == 404
        local = (await other.entity("Person", "Local", who="admin")).json()["entity_id"]
        foreign_ref = await other.relationship(
            "KNOWS", local, sarah, who="admin", valid_from="2020-01-01", source_system="x", source_ref="y"
        )
        random_ref = await other.relationship(
            "KNOWS", local, str(uuid.uuid4()), who="admin", valid_from="2020-01-01", source_system="x", source_ref="y"
        )
        assert foreign_ref.status_code == random_ref.status_code == 422
        assert {k: v for k, v in foreign_ref.json()["error"].items() if k != "request_id"} == {
            k: v for k, v in random_ref.json()["error"].items() if k != "request_id"
        }
        assert foreign_ref.json()["error"]["code"] == "unknown_entity"
        by_ext = await other.relationship(
            "KNOWS",
            local,
            {"type": "Person", "external_id": {"system": "hr", "value": "E-1001"}},
            who="admin",
            valid_from="2020-01-01",
            source_system="x",
            source_ref="y",
        )
        assert by_ext.json()["error"]["code"] == "unknown_entity"  # the same identifier exists in the other workspace: still not found here
        assert (await other.get("/v1/entities", "admin")).json()["items"][0]["name"] == "Local" and len(
            (await other.get("/v1/entities", "admin")).json()["items"]
        ) == 1
        assert (await other.get("/v1/commands", "admin")).json()["items"][0]["id"] != fact["command_id"]
    finally:
        await clear_graph(api, other.group_id)


# ---- concurrency --------------------------------------------------------------------------------------------------------------------------


async def test_concurrent_successions_on_one_person_leave_a_consistent_chain(tenant):
    t = tenant
    sarah, acme = await sarah_and_acme(t)
    starts = [f"20{y:02d}-03-01" for y in (15, 16, 17, 18, 19, 20, 21, 22)]
    results = await asyncio.gather(*(evt(t, sarah, acme, f"evt_{s}", s, f"Role {s}") for s in starts))
    codes = [r.status_code for r in results]
    assert set(codes) <= {201, 202, 422} and set(codes) & {201, 202}, (
        codes
    )  # an out-of-order loser is a clean 422, never a 500 (202: projection still pending)
    chain = (await t.get(f"/v1/entities/{sarah}", "admin", include="history")).json()["relationships"]
    assert len(chain) == codes.count(201) + codes.count(202)
    spans = sorted(
        (instant(f["valid_from"]), instant(f["valid_to"]) if f["valid_to"] else dt.datetime.max.replace(tzinfo=dt.UTC)) for f in chain
    )
    assert sum(1 for f in chain if f["valid_to"] is None) == 1  # exactly one open fact
    assert all(a_end <= b_start for (_, a_end), (b_start, _) in zip(spans, spans[1:], strict=False))  # no overlaps
    assert all(f["status"] == "active" for f in chain)


async def test_parallel_creates_with_one_external_id_make_one_entity(tenant):
    t = tenant
    rs = await asyncio.gather(*(t.entity("Person", f"Sarah {i}", external_ids=HR("E-RACE")) for i in range(6)))
    assert sorted(r.status_code for r in rs) == [200] * 5 + [201]
    assert len({r.json()["entity_id"] for r in rs}) == 1
    assert len((await t.get("/v1/entities", "admin")).json()["items"]) == 1


# ---- listing --------------------------------------------------------------------------------------------------------------------------------


async def test_directory_and_ledger_pagination(tenant):
    t = tenant
    for i in range(5):
        assert (await t.entity("Person" if i % 2 else "Organization", f"Name {i}", external_ids=HR(f"X-{i}"))).status_code == 201
    page1 = (await t.get("/v1/entities", "reader", limit=2)).json()
    page2 = (await t.get("/v1/entities", "reader", limit=2, cursor=page1["next_cursor"])).json()
    page3 = (await t.get("/v1/entities", "reader", limit=2, cursor=page2["next_cursor"])).json()
    names = [e["name"] for p in (page1, page2, page3) for e in p["items"]]
    assert names == [f"Name {i}" for i in range(5)] and page3["next_cursor"] is None
    assert len((await t.get("/v1/entities", "reader", type="Organization")).json()["items"]) == 3
    assert (await t.get("/v1/entities", "reader", cursor="garbage")).status_code == 422
    cmds1 = (await t.get("/v1/commands", "reviewer", limit=2)).json()
    cmds2 = (await t.get("/v1/commands", "reviewer", limit=10, cursor=cmds1["next_cursor"])).json()
    assert len(cmds1["items"]) == 2 and len(cmds2["items"]) == 3
    only = (await t.get("/v1/commands", "reviewer", entity_id=page1["items"][0]["id"])).json()["items"]
    assert len(only) == 1 and only[0]["entity_id"] == page1["items"][0]["id"]
    assert (await t.get("/v1/commands", "reviewer", status="rejected")).json()["items"] == []
