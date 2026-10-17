"""P7 WP4: the publish model and the labels (AGT-01, AGT-02, AGT-04, AT-14, MEM-03), model disclosure (ING-06), and the trusted-source flag.

Research Agent proposes a source-backed finding. With `publish` on the target scope AND a permissive ontology policy it is active at once, labelled agent_generated and
unverified; without the privilege it waits for review; a reviewer's approval is what verifies it. Planning retrieves it with those labels. Private working notes never cross;
an agent can never activate procedural memory; a citation the proposer cannot see fails with ONE answer."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest

from tests.support.p4 import EVT003, Spied, chat_calls, decide, reviews, run_event, sarah_story, sql, strict_mock  # noqa: F401
from tests.support.p5 import run_embeds
from tests.support.p7 import FINDING, PROJECT, Agent, accept_event_as, propose, register
from tests.support.tenants import clear_graph, make_tenant

pytestmark = pytest.mark.integration
TASK = "What licence does NewCo need to operate?"


async def duo(t: Any) -> tuple[dict[str, str], Agent, Agent]:
    ids = await sarah_story(t, projected=False)
    research = await register(t, "research-agent", subjects=(ids["sarah"],), privileges=("read", "propose", "publish"))
    planning = await register(t, "planning-agent", subjects=(ids["sarah"],), privileges=("read",))
    return ids, research, planning


async def publish_finding(research: Agent, ids: dict[str, str]) -> tuple[str, Any]:
    evidence = await accept_event_as(research, {"kind": "message", "content": "Research: the NewCo plan needs a state licence."})
    r = await propose(research, FINDING, evidence, subject_ids=[ids["sarah"]])
    assert r.status_code == 202, r.text
    return evidence, r


async def test_agt01_a_published_finding_is_active_labelled_and_retrieved_by_planning(api, tenant_agents, strict_mock):  # noqa: F811
    t = tenant_agents
    ids, research, planning = await duo(t)
    _, r = await publish_finding(research, ids)
    assert r.json()["status"] == "active" and r.json()["review_id"], (
        r.text
    )  # active at once, with a review item through which a person can verify it
    mid = r.json()["memory_id"]
    m = (await t.get(f"/v1/memories/{mid}", "reviewer")).json()
    assert (
        m["origin"] == "proposal"
        and m["authority_class"] == "agent_generated"
        and m["agent_generated"] is True
        and m["evidence_status"] == "unverified"
    )
    assert [s["kind"] for s in m["scopes"]] == ["project"] and m["evidence"] and m["evidence"][0]["source_kind"] == "interaction"
    await run_embeds(api, t)
    hits = (await planning.req("POST", "/v1/memories/search", json={"query": "state licence", "project": PROJECT})).json()
    hit = next(h for h in hits["hits"] if h["memory_id"] == mid)
    assert hit["agent_generated"] is True and hit["evidence_status"] == "unverified"  # search hits carry both labels
    ctx = (await planning.build(TASK, subject_ids=[ids["sarah"]], project=PROJECT, profile="planning", max_tokens=900)).json()
    item = next(i for i in ctx["memories"] if i["id"] == mid)
    assert item["agent_generated"] is True and item["evidence_status"] == "unverified" and item["label"] == "unverified"
    assert "(agent_generated, unverified)" in ctx["context_text"] and item["supported"] is True and item["citations"]
    # after a reviewer approves it, it is verified, says who, and the label follows
    review = await t.get(f"/v1/reviews/{r.json()['review_id']}", "reviewer")
    assert review.json()["allowed_decisions"] == ["approve", "reject", "approve_with_edit"] and review.json()["agent_generated"] is True
    ok = await decide(t, r.json()["review_id"], "approve", rationale="verified_with_source")
    assert ok.status_code == 200, ok.text
    done = (await t.get(f"/v1/memories/{mid}", "reviewer")).json()
    assert done["evidence_status"] == "verified" and done["authority_class"] == "agent_generated" and done["status"] == "active"
    row = (await sql(api, t, "SELECT verified_by, verified_at FROM curated_memories WHERE id = :m", m=uuid.UUID(mid)))[0]
    assert str(row.verified_by) == await t.principal_id("reviewer") and row.verified_at is not None
    again = (await planning.build(TASK, subject_ids=[ids["sarah"]], project=PROJECT, profile="planning", max_tokens=900)).json()
    assert "(agent_generated, verified)" in again["context_text"]


async def test_without_the_publish_privilege_the_same_finding_waits_for_review(api, tenant_agents, strict_mock):  # noqa: F811
    t = tenant_agents
    ids = await sarah_story(t, projected=False)
    modest = await register(
        t, "modest-agent", subjects=(ids["sarah"],), privileges=("read", "propose")
    )  # the seed's default privileges: no publish
    _, r = await publish_finding(modest, ids)
    assert r.json()["status"] == "proposed" and r.json()["review_id"]
    row = (await sql(api, t, "SELECT reason_code FROM review_items WHERE id = :r", r=uuid.UUID(r.json()["review_id"])))[0]
    assert row.reason_code == "publish_privilege_required"
    planning = await register(t, "planning-agent", subjects=(ids["sarah"],), privileges=("read",))
    ctx = (await planning.build(TASK, subject_ids=[ids["sarah"]], project=PROJECT, profile="planning", max_tokens=900)).json()
    assert FINDING not in ctx["context_text"]  # not served until a person approves
    approved = await decide(t, r.json()["review_id"], "approve", rationale="verified_with_source")
    assert approved.status_code == 200
    assert (await t.get(f"/v1/memories/{r.json()['memory_id']}", "reviewer")).json()["evidence_status"] == "verified"


async def test_publish_needs_the_privilege_on_every_target_scope(api, tenant_agents, strict_mock):  # noqa: F811
    t = tenant_agents
    ids, research, _ = await duo(t)
    evidence = await accept_event_as(research, {"kind": "message", "content": "Research: the NewCo plan needs a state licence."})
    both = await propose(
        research,
        FINDING,
        evidence,
        scope={"kind": "project", "ref": PROJECT},
        subject_ids=[ids["sarah"]],
        scopes=[{"kind": "project", "ref": PROJECT}, {"kind": "subject", "ref": ids["sarah"]}],
    )
    assert both.status_code == 202 and both.json()["status"] == "proposed"  # publish on the project, but only propose on the subject
    assert (
        await propose(research, "no grant on this project", evidence, scope={"kind": "project", "ref": "other-plan"})
    ).status_code == 422  # invalid_scope


async def test_agt02_a_working_note_never_reaches_planning_on_any_surface(api, tenant_agents, strict_mock):  # noqa: F811
    t = tenant_agents
    ids, research, planning = await duo(t)
    marker = f"zqwork{uuid.uuid4().hex[:10]}"
    note = await research.req(
        "POST", "/v1/memories", json={"type": "working", "content": f"{marker} check NewCo licensing", "run_id": "r7"}
    )
    private = await research.req(
        "POST", "/v1/memories", json={"type": "semantic", "content": f"{marker} hunch", "scopes": [{"kind": "agent", "ref": "self"}]}
    )
    assert note.status_code == private.status_code == 201
    await publish_finding(research, ids)
    await run_embeds(api, t)
    seen: list[str] = []
    for query in (marker, "NewCo licensing", "hunch"):
        r = await planning.req("POST", "/v1/memories/search", json={"query": query, "all_runs": True})
        seen.append(r.text)
        r = await planning.req("POST", "/v1/memories/search", json={"query": query, "run_id": "r7", "types": ["working"]})
        seen.append(r.text)
    seen.append((await planning.get("/v1/memories")).text)
    seen.append((await planning.get("/v1/memories", status="expired")).text)
    for build in (
        await planning.build(
            f"{marker} NewCo licensing hunch",
            subject_ids=[ids["sarah"]],
            project=PROJECT,
            profile="planning",
            run_id="r7",
            include=["working", "episodic", "semantic"],
        ),
        await planning.build(marker, profile="personal_assistant", subject_ids=[ids["sarah"]], run_id="r7"),
    ):
        seen.append(build.text)
    for mid in (note.json()["memory_id"], private.json()["memory_id"]):
        r = await planning.get(f"/v1/memories/{mid}")
        assert r.status_code == 404
        seen.append(r.text)
        assert (await planning.get(f"/v1/memories/{mid}/usage")).status_code == 403  # nor its usage
    assert all(marker not in blob for blob in seen)


async def test_agt04_unavailable_evidence_is_one_answer(api, tenant_agents, strict_mock):  # noqa: F811
    t = tenant_agents
    ids, research, planning = await duo(t)
    other = await make_tenant(api)
    try:
        foreign_event = await accept_event_as(await register(other, "outsider", project=None), {"kind": "message", "content": "outsider"})
        planning_event = await accept_event_as(planning, {"kind": "message", "content": "planning's own submission"})
        hidden_fact = (
            await t.relationship("KNOWS", ids["sarah"], ids["michael"], valid_from="2020-01-01", source_system="hr", source_ref="e")
        ).json()["fact_id"]
        visible_fact = ids["em"]
        answers = []
        for label, evidence in {
            "missing": {"source_event_ids": [str(uuid.uuid4())]},
            "another agent's interaction": {"source_event_ids": [planning_event]},
            "another workspace's interaction": {"source_event_ids": [foreign_event]},
            "a hidden fact": {"fact_ids": [hidden_fact]},
            "a missing fact": {"fact_ids": [str(uuid.uuid4())]},
        }.items():
            r = await research.req(
                "POST", "/v1/memories/proposals",
                json={"type": "semantic", "content": FINDING, "scopes": [{"kind": "project", "ref": PROJECT}], "purpose": "finding", **evidence},
            )  # fmt: skip
            assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_evidence", (label, r.text)
            answers.append({k: v for k, v in r.json()["error"].items() if k != "request_id"})
            assert all(x not in r.text for x in (planning_event, foreign_event, hidden_fact))
        assert all(a == answers[0] for a in answers)  # the same answer for all five: no oracle
        ok = await research.req(
            "POST", "/v1/memories/proposals",
            json={"type": "semantic", "content": FINDING, "scopes": [{"kind": "project", "ref": PROJECT}], "purpose": "finding", "fact_ids": [visible_fact]},
        )  # fmt: skip
        assert ok.status_code == 202, ok.text  # a fact the proposer may see is citable
    finally:
        await clear_graph(api, other.group_id)


async def test_at14_an_agent_never_activates_procedural_memory_even_with_publish(api, tenant_agents, strict_mock):  # noqa: F811
    t = tenant_agents
    ids, research, planning = await duo(t)
    evidence = await accept_event_as(research, {"kind": "message", "content": "Research: a rule of thumb for advising."})
    r = await propose(
        research,
        "When advising Sarah, always list risks before benefits.",
        evidence,
        type_="procedural",
        subject_ids=[ids["sarah"]],
        scopes=[{"kind": "project", "ref": PROJECT}],
    )
    assert r.status_code == 202 and r.json()["status"] == "proposed", r.text
    row = (await sql(api, t, "SELECT reason_code FROM review_items WHERE id = :r", r=uuid.UUID(r.json()["review_id"])))[0]
    assert row.reason_code == "procedural_requires_review"
    mid = r.json()["memory_id"]
    ctx = (await planning.build("Advise Sarah", subject_ids=[ids["sarah"]], project=PROJECT, profile="planning", max_tokens=900)).json()
    assert ctx["instructions"] == [] and "risks before benefits" not in ctx["context_text"]
    approved = await decide(t, r.json()["review_id"], "approve", rationale="verified_with_source")
    assert approved.status_code == 200
    ctx = (await planning.build("Advise Sarah", subject_ids=[ids["sarah"]], project=PROJECT, profile="planning", max_tokens=900)).json()
    guide = [i for i in ctx["instructions"] if i["id"] == mid]
    assert (
        guide and guide[0]["label"] == "guidance" and "GUIDANCE (preferences; does not override your instructions)" in ctx["context_text"]
    )
    assert "policy" not in guide[0]["text"].lower()  # approved procedural memory is guidance, never policy


async def test_evidence_required_drops_an_unverified_agent_finding_and_says_why(api, tenant_agents, strict_mock):  # noqa: F811
    t = tenant_agents
    ids, research, planning = await duo(t)
    _, r = await publish_finding(research, ids)
    mid = r.json()["memory_id"]
    await run_embeds(api, t)
    body = {"subject_ids": [ids["sarah"]], "project": PROJECT, "profile": "planning", "max_tokens": 900}
    plain = (await planning.build(TASK, **body)).json()
    assert any(i["id"] == mid for i in plain["memories"])
    strict = (await planning.build(TASK, evidence_required=True, **body)).json()
    assert not any(i["id"] == mid for i in strict["memories"]) and FINDING not in strict["context_text"]
    trace = (await planning.get(f"/v1/context/{strict['context_id']}/trace")).json()
    excluded = [i for i in trace["items"] if i["id"] == mid]
    assert excluded and excluded[0]["included"] is False and excluded[0]["exclusion_reason"] == "unverified_agent_finding"
    assert strict["excluded_source_counts"].get("unverified_agent_finding") == 1
    # ordinary evidence still satisfies the requirement: Sarah's verified employer fact is there
    assert any("Acme" in i["text"] for i in strict["facts"])
    ok = await decide(t, r.json()["review_id"], "approve", rationale="verified_with_source")
    assert ok.status_code == 200
    verified = (await planning.build(TASK, evidence_required=True, **body)).json()
    item = next(i for i in verified["memories"] if i["id"] == mid)
    assert item["evidence_status"] == "verified" and item["agent_generated"] is True


async def test_a_reviewer_can_withdraw_an_active_finding(api, tenant_agents, strict_mock):  # noqa: F811
    t = tenant_agents
    ids, research, planning = await duo(t)
    _, r = await publish_finding(research, ids)
    mid = r.json()["memory_id"]
    assert (await decide(t, r.json()["review_id"], "reject", rationale="incorrect_extraction")).status_code == 200
    row = (await sql(api, t, "SELECT status, evidence_status FROM curated_memories WHERE id = :m", m=uuid.UUID(mid)))[0]
    assert row.status == "rejected" and row.evidence_status == "unverified"
    assert (await planning.get(f"/v1/memories/{mid}")).status_code == 404


async def test_the_application_cannot_launder_a_memory_into_verified(api, tenant_agents, strict_mock):  # noqa: F811
    """The only legal move of evidence_status is unverified -> verified, by a named reviewer, on an agent finding: a trigger, not just application code."""
    from kg_db.session import tenant_session
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    t = tenant_agents
    ids, research, _ = await duo(t)
    _, r = await publish_finding(research, ids)
    from tests.support.p4 import ctx_of

    for statement in (
        "UPDATE curated_memories SET evidence_status = 'verified' WHERE id = :m",  # no named reviewer
        "UPDATE curated_memories SET verified_by = :p WHERE id = :m",  # a name without the verification
    ):
        with pytest.raises(DBAPIError):
            async with tenant_session(api.engine, ctx_of(t)) as c:
                await c.execute(text(statement), {"m": uuid.UUID(r.json()["memory_id"]), "p": uuid.UUID(await t.principal_id("reviewer"))})


async def test_ing06_model_disclosure_denied_means_no_model_stage_and_no_router_call(api, tenant_agents, strict_mock):  # noqa: F811
    t = tenant_agents
    ids = await sarah_story(t)
    quiet = await register(t, "quiet-agent", subjects=(ids["sarah"], ids["michael"], ids["topic"]), model_disclosure="denied")
    open_ = await register(t, "open-agent", subjects=(ids["sarah"], ids["michael"], ids["topic"]))
    from tests.support.p7 import extract_as

    with Spied(api) as spy:
        denied_event = await extract_as(
            api, t, quiet, {"kind": "message", "content": EVT003["content"]}, subjects=[ids["sarah"], ids["michael"], ids["topic"]]
        )
        sent = [x for x in spy.requests if "Sarah" in json.dumps(x["body"])]
        calls = chat_calls(spy)
    detail = (await t.get(f"/v1/interactions/{denied_event}", "admin")).json()
    stages = {s["stage"]: s["status"] for s in detail["stages"]}
    assert stages["graph_ingest"] == "skipped_by_policy" and stages["memory_propose"] == "skipped_by_policy", stages
    assert sent == [] and calls == 0 and detail["consent_external"] is False  # consent_external = min(request, policy)
    allowed_event = await extract_as(
        api, t, open_, {"kind": "message", "content": EVT003["content"]}, subjects=[ids["sarah"], ids["michael"], ids["topic"]]
    )
    stages = {s["stage"]: s["status"] for s in (await t.get(f"/v1/interactions/{allowed_event}", "admin")).json()["stages"]}
    assert (
        stages["graph_ingest"] != "skipped_by_policy"
        and (await t.get(f"/v1/interactions/{allowed_event}", "admin")).json()["consent_external"] is True
    )


async def test_a_principal_that_may_not_disclose_gets_no_summary_and_no_query_embedding(api, tenant_agents, strict_mock):  # noqa: F811
    t = tenant_agents
    ids = await sarah_story(t)
    quiet = await register(t, "quiet-agent", subjects=(ids["sarah"],), model_disclosure="denied", allowed_profiles=["research"])
    with Spied(api) as spy:
        r = await quiet.build("Tell me about Sarah", subject_ids=[ids["sarah"]], profile="research", compression="summarized")
        sent = [x for x in spy.requests if "Sarah" in json.dumps(x["body"])]
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["compression"] == "structured" and out["versions"].get("compression_fallback") == "model_disclosure_denied"
    assert {u["reason"] for u in out["unavailable_sources"]} >= {"model_disclosure_denied"} and sent == []  # the task never left


async def test_the_registered_profile_list_is_enforced_for_every_build_including_act_as(api, tenant_agents, strict_mock):  # noqa: F811
    t = tenant_agents
    ids = await sarah_story(t)
    planner = await register(t, "planner", subjects=(ids["sarah"],), allowed_profiles=["planning"])
    ok = await planner.build("x", subject_ids=[ids["sarah"]], profile="planning")
    assert ok.status_code == 200
    for profile in ("research", "quick_answer"):
        bad = await planner.build("x", subject_ids=[ids["sarah"]], profile=profile)
        assert bad.status_code == 422 and bad.json()["error"]["code"] == "profile_not_allowed", bad.text
    lab = await t.req(
        "POST",
        "/v1/context/build",
        "admin",
        json={"task": "x", "subject_ids": [ids["sarah"]], "profile": "research"},
        headers={"x-act-as-principal": planner.id},
        key="",
    )
    assert lab.status_code == 422 and lab.json()["error"]["code"] == "profile_not_allowed"  # the TARGET's registration is judged
    lab_ok = await t.req(
        "POST",
        "/v1/context/build",
        "admin",
        json={"task": "x", "subject_ids": [ids["sarah"]], "profile": "planning"},
        headers={"x-act-as-principal": planner.id},
        key="",
    )
    assert lab_ok.status_code == 200 and lab_ok.json()["agent_id"] == planner.id
    # act-as uses the target's grants: an agent with no grant on Sarah sees no Sarah, whatever the admin could see
    blind = await register(t, "blind", project=None)
    denied = await t.req(
        "POST",
        "/v1/context/build",
        "admin",
        json={"task": "x", "subject_ids": [ids["sarah"]], "profile": "quick_answer"},
        headers={"x-act-as-principal": blind.id},
        key="",
    )
    assert denied.status_code == 200 and denied.json()["status"] == "denied"
