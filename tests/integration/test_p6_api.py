"""P6 WP5: the API contract, authorisation, the Context Lab's act-as, the rate limit (ADM-04/05, AGT-03 early, AT-10, AT-11, S7)."""

from __future__ import annotations

import json
import uuid

import pytest

from tests.support.p4 import strict_mock  # noqa: F401
from tests.support.p6 import build, ids_in, ok_build, owner_rows, sarah_world, texts

pytestmark = pytest.mark.integration


def strip(body: dict) -> dict:  # type: ignore[type-arg]
    """A `denied` body without the one field that legitimately differs (the clock)."""
    return {k: v for k, v in body.items() if k != "generated_at"}


async def test_the_body_cannot_name_the_workspace_or_the_identity(api, tenant_ctx):
    t = tenant_ctx
    for field in ("workspace_id", "group_id", "agent_id", "principal_id"):
        r = await build(t, "agent", "anything", **{field: str(uuid.uuid4())})
        assert r.status_code == 422 and r.json()["error"]["details"]["fields"] == [field], field
    for bad in ({"task": ""}, {"task": "   "}, {"task": "x" * 2001}, {"max_tokens": 63}, {"max_items": 0}, {"profile": ""}):
        assert (await t.req("POST", "/v1/context/build", "agent", json={"task": "t", **bad}, key="")).status_code == 422, bad


async def test_who_may_build(api, tenant_ctx):
    t = tenant_ctx
    for who, code in (("agent", 200), ("app", 200), ("reviewer", 200), ("admin", 200), ("reader", 403), ("operator", 403)):
        assert (await build(t, who, "anything")).status_code == code, who
    anon = await t.client.post("/v1/context/build", json={"task": "x"})
    assert anon.status_code == 401


async def test_at11_a_foreign_or_unknown_subject_is_denied_with_nothing_else(api, tenant_ctx, bare_tenant, strict_mock):  # noqa: F811
    t, other = tenant_ctx, bare_tenant
    ids = await sarah_world(api, t)
    foreign = await build(other, "agent", "Help Sarah assess starting a business", subject_ids=[ids["sarah"]], profile="planning")
    random = await build(other, "agent", "Help Sarah assess starting a business", subject_ids=[str(uuid.uuid4())], profile="planning")
    assert foreign.status_code == random.status_code == 200
    a, b = foreign.json(), random.json()
    assert a["status"] == "denied" and strip(a) == strip(b)
    assert (
        a["context_id"] is None
        and a["context_text"] == ""
        and a["facts"] == a["memories"] == a["unknowns"] == a["conflicts"] == []
        and a["budgets"] is None
    )
    assert a["source_watermarks"] == {} and a["unavailable_sources"] == [] and a["excluded_source_counts"] == {}
    assert ids["sarah"] not in foreign.text and "Sarah" not in foreign.text
    # nothing was stored for it, in either workspace
    assert not await owner_rows("SELECT 1 FROM context_traces WHERE workspace_id = :ws", ws=other.ws)
    # a mix: the invisible subject is dropped silently, the visible one is served
    mixed = await ok_build(t, "agent", "Where does Sarah work", subject_ids=[str(uuid.uuid4()), ids["sarah"]], profile="quick_answer")
    assert mixed["status"] == "complete" and "Acme" in texts(mixed)


async def test_at10_a_peer_agents_private_notes_never_reach_the_planning_agent(api, tenant_ctx, strict_mock):  # noqa: F811
    t = tenant_ctx
    ids = await sarah_world(api, t)
    for profile in ("planning", "personal_assistant", "research", "review", "quick_answer"):
        out = await ok_build(
            t,
            "agent",
            "check NewCo licensing rules for the plan private research agent",
            subject_ids=[ids["sarah"]],
            profile=profile,
            project="newco-plan",
            run_id="r1",
        )
        blob = json.dumps(out)
        assert ids["note"] not in blob and "private to the research agent" not in blob, profile
        tr = (await t.get(f"/v1/context/{out['context_id']}/trace", "agent")).json()
        assert ids["note"] not in json.dumps(tr), profile
    own = await ok_build(
        t, "agent2", "check NewCo licensing rules for the plan", subject_ids=[ids["sarah"]], profile="personal_assistant", run_id="r1"
    )
    assert ids["note"] in ids_in(own) or any(
        "private to the research agent" in m["text"] for m in own["memories"]
    )  # its own note, in its own run, is its own


async def test_s7_an_agent_with_no_grants_gets_the_facts_and_none_of_the_subjects_memories(api, tenant_ctx, strict_mock):  # noqa: F811
    t = tenant_ctx
    ids = await sarah_world(api, t)
    out = await ok_build(
        t, "agent3", "Help Sarah assess starting a business", subject_ids=[ids["sarah"]], profile="planning", project="newco-plan"
    )
    assert "Engineering Manager" in texts(out)  # facts are workspace-readable until P7 puts subject ACLs on them
    assert out["memories"] == [] and out["instructions"] == []
    assert not {ids["business"], ids["pref"], ids["guide"], ids["finding"], ids["cycling"], ids["note"]} & ids_in(out)


async def test_a_workspace_wide_view_for_the_curators_and_the_app(api, tenant_ctx, strict_mock):  # noqa: F811
    t = tenant_ctx
    ids = await sarah_world(api, t)
    for who in ("app", "reviewer", "admin"):
        out = await ok_build(t, who, "Help Sarah assess starting a business", subject_ids=[ids["sarah"]], profile="planning")
        assert ids["business"] in ids_in(out) and ids["note"] not in ids_in(out), (
            who
        )  # no one reaches another agent's private note by building a context


# ---- the Context Lab's act-as (ADM-04, ADM-05, ADR-035) -------------------------------------------------------------------------------------------


async def act_as(t, who: str, target: str, task: str = "Help Sarah assess starting a business", **body):  # type: ignore[no-untyped-def]
    return await t.req("POST", "/v1/context/build", who, json={"task": task, **body}, key="", headers={"x-act-as-principal": target})


async def test_adm04_the_lab_builds_exactly_what_the_agent_would_get(api, tenant_ctx, strict_mock):  # noqa: F811
    t = tenant_ctx
    ids = await sarah_world(api, t)
    body = {"subject_ids": [ids["sarah"]], "profile": "planning", "max_tokens": 600, "project": "newco-plan"}
    direct = await ok_build(t, "agent", **body)
    via = await act_as(t, "admin", await t.principal_id("agent"), **body)
    assert via.status_code == 200, via.text
    lab = via.json()
    assert lab["context_text"] == direct["context_text"] and [i["handle"] for i in lab["facts"]] == [i["handle"] for i in direct["facts"]]
    assert lab["agent_id"] == await t.principal_id("agent")  # built AS the agent
    tr = (await t.get(f"/v1/context/{lab['context_id']}/trace", "admin")).json()
    assert tr["principal_id"] == await t.principal_id("agent") and tr["act_as_by"] == await t.principal_id("admin")
    audit = (await t.get("/v1/audit", "admin", action="context.lab_build")).json()["items"]
    assert audit and audit[0]["resource_id"] == lab["context_id"] and audit[0]["actor_principal_id"] == await t.principal_id("admin")
    assert audit[0]["metadata"]["target_principal_id"] == await t.principal_id("agent") and audit[0]["metadata"]["profile"] == "planning"
    assert "Engineering" not in json.dumps(audit)  # metadata only
    # as agent3 (no grants) the Lab shows what agent3 would get, not what the admin could see
    thin = (await act_as(t, "admin", await t.principal_id("agent3"), **body)).json()
    assert thin["memories"] == [] and thin["instructions"] == [] and ids["business"] not in ids_in(thin)
    # the agent itself (no context.lab) cannot use the header
    assert (await act_as(t, "agent", await t.principal_id("agent2"), **body)).status_code == 403
    # a non-admin sending the header naming ITSELF is simply a normal build
    assert (await act_as(t, "agent", await t.principal_id("agent"), **body)).status_code == 200


async def test_adm05_the_lab_cannot_be_used_to_widen_or_cross_a_boundary(api, tenant_ctx, bare_tenant, strict_mock):  # noqa: F811
    t, other = tenant_ctx, bare_tenant
    ids = await sarah_world(api, t)
    body = {"subject_ids": [ids["sarah"]], "profile": "planning"}
    for target in ("reviewer", "operator", "reader"):  # users are not actable
        r = await act_as(t, "admin", await t.principal_id(target), **body)
        assert r.status_code == 403, target
    own = await act_as(t, "admin", await t.principal_id("admin"), **body)  # naming yourself is an ordinary build: no act-as, no lab audit
    assert own.status_code == 200 and own.json()["agent_id"] == await t.principal_id("admin")
    assert not (await t.get("/v1/audit", "admin", action="context.lab_build")).json()["items"]
    # another workspace's agent is the same 404 as an id that never existed
    foreign = await act_as(t, "admin", await other.principal_id("agent"), **body)
    ghost = await act_as(t, "admin", str(uuid.uuid4()), **body)
    assert foreign.status_code == ghost.status_code == 404 and foreign.json()["error"]["code"] == ghost.json()["error"]["code"]
    # an inactive agent is not actable
    await owner_rows(
        "UPDATE principals SET status = 'revoked', revoked_at = now() WHERE id = :id", id=uuid.UUID(await t.principal_id("agent2"))
    )
    assert (await act_as(t, "admin", await t.principal_id("agent2"), **body)).status_code == 403
    # an admin of ANOTHER workspace cannot act as this workspace's agent (the id is invisible to it)
    assert (await act_as(other, "admin", await t.principal_id("agent"), **body)).status_code == 404
    # the header means nothing on any other route
    r = await t.req("GET", "/v1/context/profiles", "agent", key="", headers={"x-act-as-principal": await t.principal_id("admin")})
    assert r.status_code == 200
    with_header = await t.req("GET", "/v1/memories", "agent", key="", headers={"x-act-as-principal": await t.principal_id("admin")})
    without = await t.req("GET", "/v1/memories", "agent", key="")
    assert with_header.status_code == 200 and with_header.json() == without.json()  # the header changed nothing: it is the AGENT's listing
    assert (
        await t.req("POST", "/v1/context/build", "agent", json={"task": "x"}, key="", headers={"x-act-as-principal": "not-a-uuid"})
    ).status_code == 422


async def test_only_an_admin_can_ask_for_a_debug_snapshot_even_through_the_lab(api, tenant_ctx, strict_mock):  # noqa: F811
    t = tenant_ctx
    ids = await sarah_world(api, t)
    via = await act_as(t, "admin", await t.principal_id("agent"), subject_ids=[ids["sarah"]], profile="planning", debug_snapshot=True)
    assert via.status_code == 200
    snap = await t.get(f"/v1/context/{via.json()['context_id']}/debug", "admin")
    assert snap.status_code == 200 and snap.json()["context_text"] == via.json()["context_text"]
    assert (await t.get(f"/v1/context/{via.json()['context_id']}/debug", "agent")).status_code == 403


# ---- rate limit, feature flag, usage, validation ----------------------------------------------------------------------------------------------


async def test_the_rate_limit_answers_429_with_retry_after_and_counts_nothing_else(api, tenant_ctx, monkeypatch):
    from kg_context.limits import BuildLimiter

    t = tenant_ctx
    now = [1000.0]
    monkeypatch.setattr(api.app.state.kg.context, "limiter", BuildLimiter(60, 2, 8, clock=lambda: now[0]))
    assert [(await build(t, "agent", "x")).status_code for _ in range(2)] == [200, 200]
    third = await build(t, "agent", "x")
    assert third.status_code == 429 and third.json()["error"]["code"] == "rate_limited" and int(third.headers["retry-after"]) >= 1
    assert third.json()["error"]["retry_after"] == int(third.headers["retry-after"])
    assert (await build(t, "agent2", "x")).status_code == 200  # a different principal has its own bucket
    now[0] += 5.0
    assert (await build(t, "agent", "x")).status_code == 200  # the bucket refills


async def test_the_per_workspace_concurrency_cap(api, tenant_ctx, monkeypatch):
    from kg_context.errors import RateLimited
    from kg_context.limits import BuildLimiter

    limiter = BuildLimiter(600, 100, 1)
    ws, p = uuid.uuid4(), uuid.uuid4()
    with limiter.admit(p, ws):
        with pytest.raises(RateLimited):
            with limiter.admit(uuid.uuid4(), ws):
                pass
        with limiter.admit(uuid.uuid4(), uuid.uuid4()):  # another workspace is unaffected
            pass
    with limiter.admit(p, ws):  # and the slot is released
        pass
    _ = tenant_ctx, monkeypatch


async def test_the_api_can_be_switched_off(api, tenant_ctx, monkeypatch):
    t = tenant_ctx
    monkeypatch.setattr(api.app.state.kg.context.service._s, "context_api_enabled", False, raising=False)
    r = await build(t, "agent", "x")
    assert r.status_code == 503 and r.json()["error"]["code"] == "feature_disabled"


async def test_a_memorys_usage_in_contexts_is_an_admin_view(api, tenant_ctx, strict_mock):  # noqa: F811
    t = tenant_ctx
    ids = await sarah_world(api, t)
    out = await ok_build(t, "agent", subject_ids=[ids["sarah"]], profile="planning", project="newco-plan")
    u = (await t.get(f"/v1/memories/{ids['business']}/usage", "admin")).json()
    assert u["builds"] == 1 and u["recent_context_ids"] == [out["context_id"]] and u["last_used_at"]
    assert (await t.get(f"/v1/memories/{ids['cycling']}/usage", "admin")).json()["builds"] == 0
    for who in ("agent", "reviewer", "app"):
        assert (await t.get(f"/v1/memories/{ids['business']}/usage", who)).status_code == 403, who
    assert (await t.get(f"/v1/memories/{uuid.uuid4()}/usage", "admin")).status_code == 404


async def test_the_response_is_never_cached_and_carries_request_ids(api, tenant_ctx):
    t = tenant_ctx
    r = await build(t, "agent", "x")
    assert r.headers["cache-control"] == "no-store" and r.headers["x-request-id"]
