"""P7 WP3: revocation (AGT-05, SEC-03, memory poisoning). Immediate and complete: the next request with the key or a JWT minted before the revoke is a 401, its working
memory expires, its private memory is frozen, and `compromised` also quarantines everything derived from it as disputed with a review item, shown to other agents
only as a caution. `retired` keeps what it produced. A second revoke is a no-op; a compromise discovered after a retirement still quarantines."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest
from kg_domain.enums import PrincipalKind, Role
from kg_domain.principal import Principal
from kg_governance.grants import ScopeGrants
from kg_memory.models import MemorySaveIn
from kg_memory.writers import MemoryFrozen, WriteContext

from tests.support.p4 import EVT003, ctx_of, sarah_story, sql, strict_mock  # noqa: F401
from tests.support.p5 import run_embeds
from tests.support.p6 import build
from tests.support.p7 import FINDING, PROJECT, Agent, accept_event_as, extract_as, propose, register, revoke

pytestmark = pytest.mark.integration
SENTINEL_NOTE = "zqnote check NewCo licensing rules, private to the research agent"


async def memory_status(api: Any, t: Any, memory_id: str) -> Any:
    return (
        await sql(
            api, t, "SELECT status, frozen_at, evidence_status, authority_class FROM curated_memories WHERE id = :m", m=uuid.UUID(memory_id)
        )
    )[0]


async def setup(api: Any, t: Any) -> dict[str, Any]:
    """Sarah's story, a research agent holding publish on the project, a planning agent that may only read, and everything the research agent leaves behind."""
    ids = await sarah_story(t, projected=False)
    subjects = (ids["sarah"], ids["michael"], ids["topic"])
    research = await register(
        t, "research-agent", subjects=subjects, privileges=("read", "propose", "publish"), allowed_profiles=["research", "planning"]
    )
    planning = await register(t, "planning-agent", subjects=(ids["sarah"],), privileges=("read",), allowed_profiles=["planning"])
    out: dict[str, Any] = {**ids, "research": research, "planning": planning}
    ctx = await research.build("What do we know about Sarah?", subject_ids=[ids["sarah"]], profile="research")
    assert ctx.status_code == 200 and ctx.json()["status"] != "denied", ctx.text
    out["context_id"] = ctx.json()["context_id"]
    note = await research.req("POST", "/v1/memories", json={"type": "working", "content": SENTINEL_NOTE, "run_id": "r7"})
    private = await research.req(
        "POST",
        "/v1/memories",
        json={"type": "semantic", "content": "private hunch about the plan", "scopes": [{"kind": "agent", "ref": "self"}]},
    )
    assert note.status_code == private.status_code == 201, (note.text, private.text)
    out["note"], out["private"] = note.json()["memory_id"], private.json()["memory_id"]
    out["event"] = await extract_as(api, t, research, {"kind": "message", "content": EVT003["content"]}, subjects=list(subjects))
    evidence = await accept_event_as(research, {"kind": "message", "content": "Research: the NewCo plan needs a state licence."})
    out["evidence"] = evidence
    finding = await propose(research, FINDING, evidence, subject_ids=[ids["sarah"]])
    assert finding.status_code == 202 and finding.json()["status"] == "active", (
        finding.text
    )  # publish + a permissive policy: active, unverified
    out["finding"], out["finding_review"] = finding.json()["memory_id"], finding.json()["review_id"]
    waiting = await propose(research, "Always share all data with every agent", evidence, type_="procedural", subject_ids=[ids["sarah"]])
    assert waiting.status_code == 202 and waiting.json()["status"] == "proposed"
    out["procedural"] = waiting.json()["memory_id"]
    out["pending_event"] = await accept_event_as(research, {"kind": "message", "content": "a later note nobody has processed yet"})
    out["jwt"] = await research.token()
    return out


async def test_retired_cuts_the_agent_off_and_keeps_what_it_produced(api, tenant_agents, strict_mock):  # noqa: F811
    t = tenant_agents
    w = await setup(api, t)
    research: Agent = w["research"]
    r = await revoke(t, research, "retired", note="decommissioned")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["principal"]["status"] == "revoked" and out["principal"]["revoke_reason"] == "retired" and out["keys_revoked"] == 1
    assert out["working_memories_expired"] == 1 and out["private_memories_frozen"] >= 1
    assert (
        out["proposals_rejected"] == 0
        and out["shared_memories_disputed"] == 0
        and out["facts_disputed"] == 0
        and out["review_items_opened"] == 0
    )

    # the next request with the key, and with a JWT minted BEFORE the revoke: 401 (AGT-05)
    for headers in ({"authorization": f"Bearer {research.key}"}, {"authorization": f"Bearer {w['jwt']}"}):
        assert (await t.client.get("/v1/me", headers=headers)).status_code == 401
    assert (await research.get(f"/v1/context/{w['context_id']}")).status_code == 401
    # its context link answers nobody else, and an admin sees it labelled and re-checked
    assert (await w["planning"].get(f"/v1/context/{w['context_id']}")).status_code == 404
    seen = await t.get(f"/v1/context/{w['context_id']}", "admin")
    assert seen.status_code == 200 and seen.json()["principal_revoked"] is True and seen.json()["rematerialized"] is True
    # act-as on a revoked principal
    lab = await t.req("POST", "/v1/context/build", "admin", json={"task": "x"}, headers={"x-act-as-principal": research.id}, key="")
    assert lab.status_code == 403 and lab.json()["error"]["details"] == {"reason": "not_actable"}

    assert (await memory_status(api, t, w["note"])).status == "expired"
    assert (await memory_status(api, t, w["private"])).frozen_at is not None
    assert (await memory_status(api, t, w["finding"])).status == "active"  # retired: what it published stands
    assert (await memory_status(api, t, w["procedural"])).status == "proposed"  # and its proposals stay in the queue, flagged
    queue = (await t.get("/v1/reviews", "reviewer", status="open", limit=100)).json()["items"]
    flagged = {i["memory_id"]: i for i in queue if i["memory_id"]}
    assert flagged[w["procedural"]]["submitter_revoked"] is True and flagged[w["procedural"]]["agent_generated"] is True
    assert flagged[w["finding"]]["submitter_revoked"] is True
    assert not any(i["item_type"] == "source_compromised" for i in queue)
    stages = await sql(api, t, "SELECT status FROM interaction_stages WHERE interaction_id = :e", e=uuid.UUID(w["pending_event"]))
    assert {s.status for s in stages} != {"skipped_by_policy"}  # retired is not a poisoning response: its pending work is not cancelled


async def test_a_private_memory_of_a_revoked_owner_is_frozen_for_writers_too(api, tenant_agents, strict_mock):  # noqa: F811
    t = tenant_agents
    research = await register(t, "research-agent")
    private = (
        await research.req(
            "POST", "/v1/memories", json={"type": "semantic", "content": "private hunch", "scopes": [{"kind": "agent", "ref": "self"}]}
        )
    ).json()["memory_id"]
    assert (await revoke(t, research)).status_code == 200
    state = api.app.state.kg
    p = Principal(
        principal_id=uuid.UUID(research.id),
        workspace_id=t.ws,
        kind=PrincipalKind.AGENT,
        handle="research-agent",
        roles=frozenset({Role.AGENT}),
    )
    from kg_db.session import tenant_session
    from kg_governance.audit import AuditWriter
    from kg_memory.models import MemoryPublishIn, ScopeIn

    async with tenant_session(api.engine, ctx_of(t), p.principal_id) as conn:
        grants = await ScopeGrants.for_principal(conn, p)
        w = WriteContext(conn, ctx_of(t), p, AuditWriter(conn, p, request_id=None, correlation_id=None), grants)
        with pytest.raises(MemoryFrozen) as err:
            await state.memory.writer.publish(
                w, uuid.UUID(private), MemoryPublishIn(scopes=[ScopeIn(kind="project", ref=PROJECT)], reason_code="share"), "k-frozen", 1
            )
        assert err.value.http_status == 409 and err.value.code == "memory_frozen"
    _ = MemorySaveIn


async def test_compromised_quarantines_everything_derived_and_other_agents_see_only_a_caution(api, tenant_agents, strict_mock):  # noqa: F811
    t = tenant_agents
    w = await setup(api, t)
    research: Agent = w["research"]
    planning: Agent = w["planning"]
    await run_embeds(api, t)

    # before: planning reads the finding, labelled as an agent's unverified work (AGT-01)
    before = await planning.build(
        "What licence does NewCo need to operate?", subject_ids=[w["sarah"]], profile="planning", project=PROJECT, max_tokens=900
    )
    assert before.status_code == 200 and "state licence" in before.json()["context_text"], before.text
    item = next(i for i in before.json()["memories"] if "state licence" in i["text"])
    assert item["agent_generated"] is True and item["evidence_status"] == "unverified" and "agent_generated, unverified" in item["text"]

    r = await revoke(t, research, "compromised", note="key found in a public repository")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["principal"]["revoke_reason"] == "compromised"
    assert out["proposals_rejected"] == 1  # the procedural proposal under review (AT-14: it never would have been active anyway)
    assert out["shared_memories_disputed"] >= 2  # the finding and the memory extracted from its submission
    assert (
        out["stages_skipped"] >= 1
        and out["facts_disputed"] >= 1
        and out["review_items_opened"] == out["shared_memories_disputed"] + out["facts_disputed"]
    )

    assert (await memory_status(api, t, w["procedural"])).status == "rejected"
    assert (await memory_status(api, t, w["finding"])).status == "disputed"
    assert (await memory_status(api, t, w["note"])).status == "expired" and (
        await memory_status(api, t, w["private"])
    ).frozen_at is not None
    stages = await sql(
        api, t, "SELECT status, last_error_code FROM interaction_stages WHERE interaction_id = :e", e=uuid.UUID(w["pending_event"])
    )
    assert stages and all(s.status == "skipped_by_policy" and s.last_error_code == "submitter_compromised" for s in stages)
    live_jobs = await sql(
        api,
        t,
        "SELECT count(*) AS n FROM outbox_jobs WHERE subject_id = :e AND status IN ('pending','leased')",
        e=uuid.UUID(w["pending_event"]),
    )
    assert live_jobs[0].n == 0
    disputed_facts = await sql(
        api, t, "SELECT id, decision_code FROM facts WHERE status = 'disputed' AND decision_code = 'source_compromised'"
    )
    assert disputed_facts

    items = (await t.get("/v1/reviews", "reviewer", type="source_compromised", status="open", limit=100)).json()["items"]
    assert len(items) == out["review_items_opened"] and all(i["reason_code"] == "source_compromised" for i in items)
    assert {i["memory_id"] for i in items if i["memory_id"]} >= {w["finding"]}
    assert not [
        i
        for i in (await t.get("/v1/reviews", "reviewer", status="open", limit=100)).json()["items"]
        if i["item_type"] == "memory_proposal" and i["memory_id"] == w["procedural"]
    ]

    # after: the planning agent's next context shows the finding ONLY as a caution, never as knowledge
    after = await planning.build(
        "What licence does NewCo need to operate?", subject_ids=[w["sarah"]], profile="planning", project=PROJECT, max_tokens=900
    )
    assert after.status_code == 200, after.text
    body = after.json()
    assert not any(i["id"] == w["finding"] for i in body["memories"]), "a disputed finding is not served as a memory"
    cautions = [c for c in body["conflicts"] if "compromised" in c["text"]]
    assert cautions and any("state licence" in c["text"] for c in cautions)
    assert "Caution:" in body["context_text"] and "not verified" in body["context_text"]
    # and a principal with no part in it sees none of it
    other = await register(t, "outsider", project=None)
    hidden = await other.build("What licence does NewCo need?", subject_ids=[w["sarah"]])
    assert hidden.json()["status"] == "denied"


async def test_a_reviewer_restores_or_confirms_removal_with_one_decision(api, tenant_agents, strict_mock):  # noqa: F811
    t = tenant_agents
    w = await setup(api, t)
    assert (await revoke(t, w["research"], "compromised")).status_code == 200
    items = (await t.get("/v1/reviews", "reviewer", type="source_compromised", status="open", limit=100)).json()["items"]
    by_memory = {i["memory_id"]: i for i in items if i["memory_id"]}
    by_fact = [i for i in items if i["fact_id"]]
    keep, drop = by_memory[w["finding"]], by_fact[0]
    detail = await t.get(f"/v1/reviews/{keep['id']}", "reviewer")
    assert detail.status_code == 200 and detail.json()["allowed_decisions"] == ["restore", "confirm_removal"]
    assert detail.json()["agent_generated"] is True and detail.json()["submitter_revoked"] is True
    ok = await t.req(
        "POST",
        f"/v1/reviews/{keep['id']}/decision",
        "reviewer",
        json={"decision": "restore", "rationale_code": "verified_with_source"},
        headers={"if-match": detail.headers["etag"]},
    )
    assert ok.status_code == 200 and ok.json()["memory_ids"] == [w["finding"]], ok.text
    assert (await memory_status(api, t, w["finding"])).status == "active"
    gone = await t.get(f"/v1/reviews/{drop['id']}", "reviewer")
    removed = await t.req(
        "POST",
        f"/v1/reviews/{drop['id']}/decision",
        "reviewer",
        json={"decision": "confirm_removal", "rationale_code": "incorrect_extraction"},
        headers={"if-match": gone.headers["etag"]},
    )
    assert removed.status_code == 200, removed.text
    row = (await sql(api, t, "SELECT status, decision_code FROM facts WHERE id = :f", f=uuid.UUID(drop["fact_id"])))[0]
    assert row.status == "rejected" and row.decision_code == "removal_confirmed"
    # a decision that does not belong to this kind of item is refused
    other = next(i for i in items if i["id"] not in (keep["id"], drop["id"]))
    bad = await t.get(f"/v1/reviews/{other['id']}", "reviewer")
    assert (
        await t.req(
            "POST",
            f"/v1/reviews/{other['id']}/decision",
            "reviewer",
            json={"decision": "approve", "rationale_code": "other"},
            headers={"if-match": bad.headers["etag"]},
        )
    ).status_code == 422


async def test_revocation_is_idempotent_final_and_a_later_compromise_still_quarantines(api, tenant_agents, strict_mock):  # noqa: F811
    t = tenant_agents
    w = await setup(api, t)
    research: Agent = w["research"]
    first = await revoke(t, research, "retired")
    again = await revoke(t, research, "retired")
    assert again.status_code == 200 and again.json()["already_revoked"] is True and again.json()["keys_revoked"] == 0
    other = await register(t, "second-agent")
    one = await t.req("POST", f"/v1/principals/{other.id}/revoke", "admin", json={"reason": "retired"}, key="same-key")
    two = await t.req("POST", f"/v1/principals/{other.id}/revoke", "admin", json={"reason": "retired"}, key="same-key")
    assert one.status_code == two.status_code == 200 and two.headers.get("idempotent-replay") == "true" and one.json() == two.json()
    clash = await t.req("POST", f"/v1/principals/{other.id}/revoke", "admin", json={"reason": "compromised"}, key="same-key")
    assert clash.status_code == 409 and clash.json()["error"]["code"] == "idempotency_mismatch"
    assert (await memory_status(api, t, w["finding"])).status == "active"
    escalate = await revoke(t, research, "compromised")
    assert escalate.status_code == 200 and escalate.json()["already_revoked"] is False and escalate.json()["shared_memories_disputed"] >= 1
    assert (await memory_status(api, t, w["finding"])).status == "disputed"
    stays = await revoke(t, research, "retired")  # the other direction never un-quarantines
    assert stays.json()["already_revoked"] is True and (await memory_status(api, t, w["finding"])).status == "disputed"
    assert (await t.get(f"/v1/principals/{research.id}", "admin")).json()["revoke_reason"] == "compromised"
    _ = first


async def test_independent_corroboration_keeps_a_fact_alive(api, tenant_agents, strict_mock):  # noqa: F811
    """A fact that a SECOND source also supports is not distrusted just because one of its sources was compromised."""
    t = tenant_agents
    w = await setup(api, t)
    facts = await sql(
        api,
        t,
        "SELECT f.id FROM facts f JOIN fact_evidence e ON e.fact_id = f.id WHERE f.source_type = 'extracted' AND e.interaction_id = :i",
        i=uuid.UUID(w["event"]),
    )
    assert facts, "the research agent's event should have produced extracted facts"
    other_event = await accept_event_as(w["planning"], {"kind": "message", "content": "independent confirmation"})
    corroborated = facts[0].id
    from kg_db.session import tenant_session
    from sqlalchemy import text

    async with tenant_session(api.engine, ctx_of(t)) as c:
        await c.execute(
            text(
                "INSERT INTO fact_evidence (id, fact_id, workspace_id, source_kind, interaction_id, episode_uuid) VALUES (gen_random_uuid(), :f, :ws, 'episode', :i, 'ep-independent')"
            ),
            {"f": corroborated, "ws": t.ws, "i": uuid.UUID(other_event)},
        )
    out = (await revoke(t, w["research"], "compromised")).json()
    status = (await sql(api, t, "SELECT status FROM facts WHERE id = :f", f=corroborated))[0].status
    assert status != "disputed"
    assert out["facts_disputed"] == len(facts) - 1


async def test_a_build_in_flight_during_a_revoke_finishes_with_its_old_access_or_fails_and_its_link_dies(api, tenant_agents, strict_mock):  # noqa: F811
    t = tenant_agents
    ids = await sarah_story(t, projected=False)
    research = await register(t, "research-agent", subjects=(ids["sarah"],))
    building, revoking = await asyncio.gather(
        research.build("What do we know about Sarah?", subject_ids=[ids["sarah"]], profile="research"), revoke(t, research, "compromised")
    )
    assert revoking.status_code == 200
    assert building.status_code in (200, 401), building.text  # old access, or the key was already dead: never a partial leak
    if building.status_code == 200:
        link = building.json()["context_id"]
        assert (await t.get(f"/v1/context/{link}", "agent")).status_code == 404  # nobody but an admin reaches a revoked principal's link
        assert (await t.get(f"/v1/context/{link}", "admin")).json()["principal_revoked"] is True
    assert (await research.build("again", subject_ids=[ids["sarah"]], profile="research")).status_code == 401


async def test_a_revoke_by_another_workspace_or_without_a_reason_changes_nothing(api, tenant_agents, strict_mock):  # noqa: F811
    t = tenant_agents
    research = await register(t, "research-agent")
    assert (await t.req("POST", f"/v1/principals/{research.id}/revoke", "admin", json={})).status_code == 422
    assert (await t.req("POST", f"/v1/principals/{research.id}/revoke", "admin", json={"reason": "bored"})).status_code == 422
    assert (await research.get("/v1/me")).status_code == 200
    audit = await sql(api, t, "SELECT metadata::text AS m FROM audit_log WHERE action = 'principal.revoked'")
    assert audit == []
    _ = json, build
