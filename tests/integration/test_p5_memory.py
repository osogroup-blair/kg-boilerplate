"""P5 WP2/WP4/WP6: the memory write paths, lifecycle and scoped search over the real API (MEM-01..05, AT-14, RET-02 offline)."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from tests.support.p4 import decide, reviews, sarah_story
from tests.support.p5 import (
    BUSINESS,
    CYCLIST_TEXT,
    GUIDELINE,
    PREFERENCE,
    accept_event,
    act,
    body,
    clear_faults,
    embedding_fault,
    get,
    ids_of,
    memory_rows,
    propose,
    run_embeds,
    save,
    search,
    sub,
)

pytestmark = pytest.mark.integration


async def story(t):  # type: ignore[no-untyped-def]
    """Sarah, and the grants the demo gives: the app sees every subject, the two agents share a project and see Sarah."""
    ids = await sarah_story(t, projected=False)
    for who in ("agent", "agent2"):
        await t.grant(who, "subject", ids["sarah"])
    return ids


# ---- MEM-01: explicit save and proposal are attributable --------------------------------------------------------------------------------------------


async def test_an_explicit_save_is_active_verified_and_attributable(api, tenant_mem):
    t = tenant_mem
    ids = await story(t)
    evt = await accept_event(
        t, {"kind": "message", "content": "Sarah Johnson: I prefer decisions laid out with risks."}, subjects=[ids["sarah"]]
    )
    r = await save(t, "app", body(subject_ids=[ids["sarah"]], source_interaction_id=evt, purpose="advice style"))
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["status"] == "active" and out["deduplicated"] is False and out["revision"] == 1 and r.headers["etag"] == 'W/"rev-1"'
    m = (await get(t, "app", out["memory_id"])).json()
    assert (m["type"], m["origin"], m["status"], m["evidence_status"], m["authority_class"]) == (
        "semantic",
        "explicit",
        "active",
        "verified",
        "user_asserted",
    )
    assert m["created_by"] == await t.principal_id("app") and m["purpose"] == "advice style" and m["content"] == PREFERENCE
    assert m["scopes"] == [{"kind": "subject", "ref": ids["sarah"]}] and m["subject_ids"] == [
        ids["sarah"]
    ]  # scopes default to the subjects
    assert [(e["source_kind"], e["interaction_id"]) for e in m["evidence"]] == [("interaction", evt)]
    assert m["summary"] == PREFERENCE and m["embedding"]["status"] == "pending" and m["pipeline_version"] == "p5.v1"
    assert (await t.get("/v1/audit", "admin", action="memory.saved")).json()["items"][0]["resource_id"] == out["memory_id"]


async def test_a_shared_save_without_its_source_and_purpose_is_refused(api, tenant_mem):
    t = tenant_mem
    ids = await story(t)
    for extra in ({}, {"purpose": "advice style"}, {"source_ref": "crm:123"}):
        r = await save(t, "app", body(subject_ids=[ids["sarah"]], **extra))
        assert r.status_code == 422 and r.json()["error"]["code"] == "attribution_required", extra
    ok = await save(t, "app", body(subject_ids=[ids["sarah"]], source_ref="crm:123", purpose="advice style"))
    assert ok.status_code == 201
    m = (await get(t, "app", ok.json()["memory_id"])).json()
    assert [(e["source_kind"], e["external_ref"]) for e in m["evidence"]] == [("external_ref", "crm:123")]


async def test_who_may_save_what(api, tenant_mem):
    t = tenant_mem
    ids = await story(t)
    shared = body(subject_ids=[ids["sarah"]], source_ref="x", purpose="p")
    assert (await save(t, "agent", shared)).json()["error"]["code"] == "use_proposal"  # agents propose shared memory
    for who in ("reader", "operator", "reviewer"):  # no memory.write_own
        assert (await save(t, who, shared)).status_code == 403, who
    assert (await save(t, "admin", shared)).status_code == 201
    private = await save(t, "agent", body(content="my private note", scopes=[{"kind": "agent", "ref": "self"}]))
    assert private.status_code == 201 and private.json()["status"] == "active"  # an agent's own notes need no one's approval
    assert (await save(t, "agent", body(content="no scope given"))).status_code == 201  # defaults to agent:self for an agent
    row = (await memory_rows(api, t, "content = 'no scope given'"))[0]
    assert row.evidence_status == "unverified" and row.authority_class == "agent_generated"


async def test_scopes_and_subjects_the_caller_cannot_use_are_refused_with_one_answer(api, tenant_mem):
    t = tenant_mem
    ids = await story(t)
    ghost = str(uuid.uuid4())
    answers = set()
    for scope in (sub(ghost), sub(ids["acme"]) | {}, {"kind": "project", "ref": "no-such-project"}, {"kind": "agent", "ref": ghost}):
        r = await propose(
            t,
            "agent3",
            {
                "type": "semantic",
                "content": "x y z",
                "source_event_ids": [await accept_event(t, {"kind": "message", "content": "hi"}, who="agent3")],
                "requested_scope": scope,
            },
        )
        assert r.status_code == 422, (scope, r.text)
        answers.add((r.json()["error"]["code"], r.json()["error"]["message"]))
    assert len(answers) == 1 and answers == {("invalid_scope", "A requested scope is not available to you")}
    # subjects named in subject_ids: an entity that does not exist and one the caller has no grant for answer alike
    evt = await accept_event(t, {"kind": "message", "content": "hi"}, who="agent3")
    a = await propose(
        t,
        "agent3",
        {
            "type": "semantic",
            "content": "x y z",
            "source_event_ids": [evt],
            "subject_ids": [ghost],
            "requested_scope": {"kind": "workspace"},
        },
    )
    b = await propose(
        t,
        "agent3",
        {
            "type": "semantic",
            "content": "x y z",
            "source_event_ids": [evt],
            "subject_ids": [ids["sarah"]],
            "requested_scope": {"kind": "workspace"},
        },
    )
    assert a.status_code == b.status_code == 422 and a.json()["error"]["code"] == b.json()["error"]["code"] == "unknown_entity"


async def test_a_proposal_cites_only_evidence_the_proposer_can_see_and_the_answer_never_says_which(api, tenant_mem, bare_tenant):
    t = tenant_mem
    ids = await story(t)
    mine = await accept_event(t, {"kind": "message", "content": "agent's own note"}, who="agent")
    theirs = await accept_event(t, {"kind": "message", "content": "someone else's note"}, who="app")  # in the workspace, not the agent's
    foreign = await accept_event(bare_tenant, {"kind": "message", "content": "another workspace"}, who="app")
    ghost = str(uuid.uuid4())
    answers = set()
    for evidence in (
        {"source_event_ids": [theirs]},
        {"source_event_ids": [foreign]},
        {"source_event_ids": [ghost]},
        {"fact_ids": [ghost]},
        {"memory_ids": [ghost]},
    ):
        r = await propose(t, "agent", {"type": "semantic", "content": "a finding", "requested_scope": sub(ids["sarah"]), **evidence})
        assert r.status_code == 422, (evidence, r.text)
        answers.add(r.text.replace(r.json()["error"]["request_id"], ""))
    assert len(answers) == 1 and "invalid_evidence" in answers.pop()
    ok = await propose(
        t, "agent", {"type": "semantic", "content": "a finding", "requested_scope": sub(ids["sarah"]), "source_event_ids": [mine]}
    )
    assert ok.status_code == 202 and ok.json()["status"] == "proposed"  # an agent's shared proposal waits for review


async def test_an_agent_cannot_activate_procedural_memory_and_only_a_reviewer_can_at_14(api, tenant_mem):
    t = tenant_mem
    ids = await story(t)
    evt = await accept_event(t, {"kind": "message", "content": "agent note"}, who="agent")
    r = await propose(
        t,
        "agent",
        {
            "type": "procedural",
            "content": GUIDELINE,
            "subject_ids": [ids["sarah"]],
            "requested_scope": sub(ids["sarah"]),
            "source_event_ids": [evt],
        },
    )
    assert r.status_code == 202 and r.json()["status"] == "proposed" and r.json()["review_id"]
    mid, review_id = r.json()["memory_id"], r.json()["review_id"]
    assert (await search(t, "reviewer", "advising Sarah options", include=["proposed"]))["hits"][0][
        "memory_id"
    ] == mid  # a curator can see it...
    assert (await search(t, "agent2", "advising Sarah options"))["hits"] == []  # ...a peer agent cannot: it is not active
    denied = await decide(t, review_id, "approve", who="agent", revision='W/"rev-1"')
    assert denied.status_code == 403  # an agent cannot decide any review
    (item,) = [i for i in await reviews(t) if i["id"] == review_id]
    assert item["item_type"] == "memory_proposal" and item["memory_id"] == mid and item["status"] == "open"
    ok = await decide(t, review_id, "approve", rationale="verified_with_source")
    assert ok.status_code == 200 and ok.json()["memory_ids"] == [mid] and ok.json()["projection_status"] == "not_applicable"
    m = (await get(t, "reviewer", mid)).json()
    # P7: a reviewer's approval is what verifies an AGENT's finding (who and when are recorded); its authority stays agent_generated
    assert m["status"] == "active" and m["evidence_status"] == "verified" and m["authority_class"] == "agent_generated"
    assert (await search(t, "agent2", "advising Sarah options"))["hits"][0][
        "memory_id"
    ] == mid  # approved, so visible to those who may see Sarah


async def test_the_app_proposes_a_procedural_memory_and_it_also_waits(api, tenant_mem):
    t = tenant_mem
    ids = await story(t)
    evt = await accept_event(t, {"kind": "message", "content": "Sarah Johnson: please show risks"}, subjects=[ids["sarah"]])
    r = await propose(
        t,
        "app",
        {
            "type": "procedural",
            "content": GUIDELINE,
            "subject_ids": [ids["sarah"]],
            "source_event_ids": [evt],
            "requested_scope": sub(ids["sarah"]),
        },
    )
    assert r.status_code == 202 and r.json()["status"] == "proposed"
    admin_r = await propose(
        t,
        "admin",
        {"type": "procedural", "content": "Always greet Sarah by name.", "subject_ids": [ids["sarah"]], "source_event_ids": [evt]},
    )
    assert admin_r.json()["status"] == "active"  # an administrator is the trusted writer for procedural memory


async def test_a_duplicate_adds_independent_evidence_instead_of_a_second_memory(api, tenant_mem):
    t = tenant_mem
    ids = await story(t)
    e1 = await accept_event(t, {"kind": "message", "content": "first mention"}, subjects=[ids["sarah"]])
    e2 = await accept_event(t, {"kind": "message", "content": "second mention"}, subjects=[ids["sarah"]])
    payload = {"type": "semantic", "content": PREFERENCE, "subject_ids": [ids["sarah"]], "requested_scope": sub(ids["sarah"])}
    first = await propose(t, "app", {**payload, "source_event_ids": [e1]})
    second = await propose(t, "app", {**payload, "content": "  " + PREFERENCE.upper() + " ", "source_event_ids": [e2]})
    assert first.status_code == 202 and second.status_code == 200
    assert second.json()["deduplicated"] is True and second.json()["memory_id"] == first.json()["memory_id"]
    assert len(await memory_rows(api, t)) == 1
    m = (await get(t, "app", first.json()["memory_id"])).json()
    assert sorted(e["interaction_id"] for e in m["evidence"]) == sorted([e1, e2]) and m["last_confirmed_at"] is not None
    # the same words in ANOTHER scope are another memory (and can never reveal one the caller could not write)
    third = await propose(t, "app", {**payload, "requested_scope": {"kind": "workspace"}, "source_event_ids": [e2]})
    assert third.status_code == 202 and third.json()["memory_id"] != first.json()["memory_id"]


async def test_a_higher_authority_conflict_is_proposed_with_a_conflict_review_never_a_silent_override(api, tenant_mem):
    t = tenant_mem
    ids = await story(t)
    assertion = {"subject_id": ids["sarah"], "predicate": "prefers_meetings_in"}
    held = (
        await save(
            t,
            "admin",
            body(
                content="Sarah prefers morning meetings.",
                subject_ids=[ids["sarah"]],
                source_ref="hr:1",
                purpose="scheduling",
                assertion={**assertion, "object": "morning"},
            ),
        )
    ).json()
    evt = await accept_event(t, {"kind": "message", "content": "Sarah Johnson: evenings"}, subjects=[ids["sarah"]])

    async def claim(text, obj):  # type: ignore[no-untyped-def]
        return await propose(
            t,
            "app",
            {
                "type": "semantic",
                "content": text,
                "subject_ids": [ids["sarah"]],
                "source_event_ids": [evt],
                "assertion": {**assertion, "object": obj},
            },
        )

    first = (await claim("Sarah prefers evening meetings.", "evening")).json()
    assert first["status"] == "proposed" and first["review_id"]  # a user's report does not outrank an administrator's assertion
    (item,) = [i for i in await reviews(t) if i["id"] == first["review_id"]]
    assert (
        item["item_type"] == "memory_conflict"
        and item["memory_id"] == first["memory_id"]
        and item["competing_memory_id"] == held["memory_id"]
    )
    assert (await get(t, "app", held["memory_id"])).json()["status"] == "active"  # the existing memory is untouched
    assert ids_of(await search(t, "agent", "meetings", subject_ids=[ids["sarah"]])) == [held["memory_id"]]
    # a reviewer holds user_asserted authority, so cannot supersede an admin_asserted memory
    low = await decide(t, item["id"], "supersede_existing")
    assert low.status_code == 409 and low.json()["error"]["code"] == "insufficient_authority"
    assert (await get(t, "admin", first["memory_id"])).json()["status"] == "proposed"
    won = await decide(t, item["id"], "supersede_existing", who="admin")
    assert won.status_code == 200 and sorted(won.json()["memory_ids"]) == sorted([first["memory_id"], held["memory_id"]])
    assert (await get(t, "admin", held["memory_id"])).json()["status"] == "superseded"
    new = (await get(t, "admin", first["memory_id"])).json()
    assert new["status"] == "active" and any(x["relation"] == "replaces" and x["memory_id"] == held["memory_id"] for x in new["lineage"])
    # keep_existing refuses the claim; mark_disputed disputes both
    agent_evt = await accept_event(t, {"kind": "message", "content": "agent finding"}, who="agent")

    async def agent_claim(text, obj):  # type: ignore[no-untyped-def]
        r = await propose(
            t,
            "agent",
            {
                "type": "semantic",
                "content": text,
                "subject_ids": [ids["sarah"]],
                "source_event_ids": [agent_evt],
                "assertion": {**assertion, "object": obj},
            },
        )
        assert r.status_code == 202, r.text
        return r

    equal = (
        await claim("Sarah prefers lunchtime meetings.", "lunch")
    ).json()  # equal authority is not a conflict: both are the user's word
    assert equal["status"] == "active" and equal["review_id"] is None
    second = (await agent_claim("Sarah prefers lunch-hour meetings, per a calendar scan.", "lunch-hour")).json()
    (item2,) = [i for i in await reviews(t) if i["id"] == second["review_id"]]
    assert item2["competing_memory_id"] in (first["memory_id"], equal["memory_id"])
    assert (await decide(t, item2["id"], "keep_existing")).status_code == 200
    assert (await get(t, "admin", second["memory_id"])).json()["status"] == "rejected" and (
        await get(t, "admin", first["memory_id"])
    ).json()["status"] == "active"
    third = (await agent_claim("Sarah prefers early meetings.", "early")).json()
    (item3,) = [i for i in await reviews(t) if i["id"] == third["review_id"]]
    assert (await decide(t, item3["id"], "mark_disputed")).status_code == 200
    assert {(await get(t, "admin", m)).json()["status"] for m in (third["memory_id"], item3["competing_memory_id"])} == {"disputed"}


async def test_a_reviewer_can_approve_a_proposal_with_an_edit_and_the_original_is_kept(api, tenant_mem):
    t = tenant_mem
    ids = await story(t)
    evt = await accept_event(t, {"kind": "message", "content": "agent note"}, who="agent")
    p = (
        await propose(
            t,
            "agent",
            {
                "type": "semantic",
                "content": "Sarah likes bikes a lot.",
                "subject_ids": [ids["sarah"]],
                "requested_scope": sub(ids["sarah"]),
                "source_event_ids": [evt],
            },
        )
    ).json()
    got = await t.get(f"/v1/reviews/{p['review_id']}", "reviewer")
    assert got.json()["allowed_decisions"] == ["approve", "reject", "approve_with_edit"]
    bad = await t.req(
        "POST",
        f"/v1/reviews/{p['review_id']}/decision",
        "reviewer",
        json={"decision": "approve_with_edit", "rationale_code": "other"},
        headers={"if-match": got.headers["etag"]},
    )
    assert bad.status_code == 422  # an edit needs its text
    r = await t.req(
        "POST",
        f"/v1/reviews/{p['review_id']}/decision",
        "reviewer",
        json={"decision": "approve_with_edit", "rationale_code": "other", "edited_content": "Sarah rides a road bike on weekends."},
        headers={"if-match": got.headers["etag"]},
    )
    assert r.status_code == 200, r.text
    old_id, new_id = p["memory_id"], [m for m in r.json()["memory_ids"] if m != p["memory_id"]][0]
    old, new = (await get(t, "reviewer", old_id)).json(), (await get(t, "reviewer", new_id)).json()
    assert old["status"] == "superseded" and old["content"] == "Sarah likes bikes a lot."  # the proposal is kept as written
    assert new["status"] == "active" and new["content"] == "Sarah rides a road bike on weekends." and new["evidence_status"] == "verified"
    assert any(x["relation"] == "replaces" and x["memory_id"] == old_id for x in new["lineage"]) and {
        e["source_kind"] for e in new["evidence"]
    } >= {"interaction", "memory"}
    cmds = await t.get("/v1/commands", "admin", limit=100)
    assert all(
        not c["command_type"].startswith("memory.") for c in cmds.json()["items"]
    )  # the review's own command is the review.decide one
    assert any(c["command_type"] == "review.decide" for c in cmds.json()["items"])


# ---- lifecycle ----------------------------------------------------------------------------------------------------------------------------


async def test_supersede_keeps_the_old_row_its_evidence_and_the_lineage_mem04(api, tenant_mem):
    t = tenant_mem
    ids = await story(t)
    evt = await accept_event(t, {"kind": "message", "content": "Sarah Johnson: remote please"}, subjects=[ids["sarah"]])
    old = (
        await save(
            t,
            "app",
            body(subject_ids=[ids["sarah"]], source_interaction_id=evt, purpose="work style", content="Sarah prefers remote work."),
        )
    ).json()
    r = await act(
        t,
        "reviewer",
        old["memory_id"],
        "supersede",
        {"content": "Sarah prefers remote work three days a week.", "reason_code": "clarified"},
    )
    assert r.status_code == 201, r.text
    new_id = r.json()["memory_id"]
    assert new_id != old["memory_id"]
    o = (await get(t, "reviewer", old["memory_id"])).json()
    n = (await get(t, "reviewer", new_id)).json()
    assert o["status"] == "superseded" and o["effective_until"] is not None and o["content"] == "Sarah prefers remote work."
    assert n["status"] == "active" and n["content"].endswith("a week.") and n["origin"] == "explicit" and n["evidence_status"] == "verified"
    assert {(x["relation"], x["direction"], x["memory_id"]) for x in n["lineage"]} == {("replaces", "replaces", old["memory_id"])}
    assert {(x["relation"], x["direction"], x["memory_id"]) for x in o["lineage"]} == {("replaces", "replaced_by", new_id)}
    kinds = sorted((e["source_kind"], e.get("interaction_id") or e.get("memory_id")) for e in n["evidence"])
    assert kinds == sorted([("interaction", evt), ("memory", old["memory_id"])])  # the evidence travelled by reference
    assert [e["interaction_id"] for e in o["evidence"]] == [evt]  # and the old row still has its own
    assert (await get(t, "reviewer", new_id)).json()["scopes"] == o["scopes"]


async def test_supersede_needs_curation_if_match_and_a_live_memory(api, tenant_mem):
    t = tenant_mem
    ids = await story(t)
    mid = (await save(t, "app", body(subject_ids=[ids["sarah"]], source_ref="x", purpose="p"))).json()["memory_id"]
    payload = {"content": "Sarah prefers brief comparisons.", "reason_code": "corrected"}
    assert (await act(t, "app", mid, "supersede", payload)).status_code == 403  # an app saves; it does not curate
    assert (await act(t, "agent", mid, "supersede", payload)).status_code in (403, 404)
    no_match = await write_no_match(t, "reviewer", mid, payload)
    assert no_match.status_code == 428
    stale = await act(t, "reviewer", mid, "supersede", payload, revision='W/"rev-9"')
    assert stale.status_code == 412
    ok = await act(t, "reviewer", mid, "supersede", payload)
    assert ok.status_code == 201
    again = await act(t, "reviewer", mid, "supersede", {**payload, "reason_code": "again"})
    assert again.status_code in (409, 412)  # the old one is no longer live


async def write_no_match(t, who, mid, payload):  # type: ignore[no-untyped-def]
    return await t.req("POST", f"/v1/memories/{mid}/supersede", who, json=payload)


async def test_idempotency_replays_and_refuses_a_different_request(api, tenant_mem):
    t = tenant_mem
    ids = await story(t)
    payload = body(subject_ids=[ids["sarah"]], source_ref="x", purpose="p")
    a = await save(t, "app", payload, key="k-1")
    b = await save(t, "app", payload, key="k-1")
    assert a.status_code == 201 and b.status_code == 201 and b.headers["idempotent-replay"] == "true"
    assert a.json() == b.json() and len(await memory_rows(api, t)) == 1
    other = await save(t, "app", {**payload, "content": "another"}, key="k-1")
    assert other.status_code == 409 and other.json()["error"]["code"] == "idempotency_mismatch"
    stolen = await save(t, "admin", payload, key="k-1")  # another principal reusing the key neither replays nor learns the memory id
    assert stolen.status_code == 409 and "memory_id" not in stolen.text


async def test_expire_and_dispute_are_curation_and_leave_the_memory_readable_to_curators(api, tenant_mem):
    t = tenant_mem
    ids = await story(t)
    mid = (await save(t, "app", body(subject_ids=[ids["sarah"]], source_ref="x", purpose="p"))).json()["memory_id"]
    d = await act(t, "reviewer", mid, "dispute", {"reason_code": "contradicted"})
    assert d.status_code == 200 and d.json()["status"] == "disputed" and d.json()["review_id"]
    assert (await search(t, "app", "decision comparisons"))["hits"] == []  # a disputed memory is not served
    (item,) = [i for i in await reviews(t) if i["id"] == d.json()["review_id"]]
    assert item["item_type"] == "memory_conflict" and item["memory_id"] == mid and item["competing_memory_id"] is None
    keep = await decide(t, item["id"], "keep_existing")
    assert keep.status_code == 200 and (await get(t, "reviewer", mid)).json()["status"] == "active"
    ex = await act(t, "reviewer", mid, "expire", {"reason_code": "no_longer_relevant"})
    assert ex.status_code == 200 and ex.json()["status"] == "expired"
    assert (await get(t, "reviewer", mid)).json()["status"] == "expired"
    assert (await act(t, "app", mid, "expire", {"reason_code": "x"})).status_code == 403


# ---- MEM-02: working memory ----------------------------------------------------------------------------------------------------------------------


async def test_working_memory_belongs_to_one_agent_and_one_run_mem02(api, tenant_mem):
    t = tenant_mem
    await story(t)
    r = await save(t, "agent", {"type": "working", "content": "check NewCo licensing rules for the plan", "run_id": "r1"})
    assert r.status_code == 201 and r.json()["expires_at"]
    mid = r.json()["memory_id"]
    m = (await get(t, "agent", mid)).json()
    assert m["scopes"] == [{"kind": "session", "ref": f"{await t.principal_id('agent')}/r1"}] and m["run_id"] == "r1" and m["expires_at"]
    await run_embeds(api, t)
    assert ids_of(await search(t, "agent", "NewCo licensing", run_id="r1")) == [mid]
    assert (await search(t, "agent", "NewCo licensing", run_id="r2"))["hits"] == []
    assert (await search(t, "agent", "NewCo licensing"))["hits"] == []  # no run named: no session memory
    assert ids_of(await search(t, "agent", "NewCo licensing", all_runs=True)) == [mid]
    for other in ("agent2", "agent3", "app", "reviewer", "admin", "operator", "reader"):
        assert (await search(t, other, "NewCo licensing", run_id="r1", all_runs=True))["hits"] == [], other
        assert (await get(t, other, mid)).status_code == 404, other
    # the administrator sees it only by asking, explicitly, and the read is audited
    assert (await get(t, "admin", mid)).status_code == 404
    assert (await get(t, "reviewer", mid, reveal_private="true")).status_code == 403
    revealed = await get(t, "admin", mid, reveal_private="true")
    assert revealed.status_code == 200 and revealed.json()["id"] == mid
    audit = (await t.get("/v1/audit", "admin", action="memory.private_read")).json()["items"]
    assert audit and audit[0]["resource_id"] == mid


async def test_a_working_note_expires_on_time_and_the_delayed_job_marks_it(api, tenant_mem):
    t = tenant_mem
    await story(t)
    r = await save(
        t, "agent", {"type": "working", "content": "short lived note about ledgers", "run_id": "r1", "ttl_hours": 0.0008}
    )  # ~3 s
    mid = r.json()["memory_id"]
    jobs = await owner_jobs(mid)
    assert sorted(j.stage for j in jobs) == ["memory_embed", "memory_expire"] and {j.subject_type for j in jobs} == {"memory"}
    assert [j.delayed for j in jobs if j.stage == "memory_expire"] == [True]  # the deadline is a DELAYED job: no sweeper
    assert all(set(j.payload) == {"memory_id", "generation"} for j in jobs)  # ids only (R9)
    assert ids_of(await search(t, "agent", "short lived ledgers", run_id="r1")) == [mid]  # visible until the deadline
    await asyncio.sleep(3.2)
    assert (await search(t, "agent", "short lived ledgers", run_id="r1"))[
        "hits"
    ] == []  # gone from search the moment the clock passes it, job or no job

    async def expired() -> bool:
        return (await memory_rows(api, t, "id = :i", i=uuid.UUID(mid)))[0].status == "expired"

    from tests.support.worker import running_worker, wait_until

    async with running_worker(api, stages=("memory_embed", "memory_expire")):
        await wait_until(expired, timeout=60, what="the expiry job")
    row = (await memory_rows(api, t, "id = :i", i=uuid.UUID(mid)))[0]
    assert row.status == "expired" and row.revision == 2 and row.effective_until is not None


async def owner_jobs(memory_id):  # type: ignore[no-untyped-def]
    from tests.support.p5 import owner_sql

    return await owner_sql(
        "SELECT stage, payload, subject_type, available_at > now() AS delayed FROM outbox_jobs WHERE subject_id = :m ORDER BY stage",
        m=uuid.UUID(memory_id),
    )


async def test_publishing_a_private_note_creates_a_new_proposal_and_the_original_stays_private(api, tenant_mem):
    t = tenant_mem
    ids = await story(t)
    note = (await save(t, "agent", {"type": "working", "content": "NewCo needs a state licence to operate", "run_id": "r1"})).json()
    r = await act(
        t, "agent", note["memory_id"], "publish", {"scopes": [{"kind": "project", "ref": "newco-plan"}], "reason_code": "sourced_finding"}
    )
    assert r.status_code == 202 and r.json()["status"] == "proposed" and r.json()["memory_id"] != note["memory_id"]
    pub = r.json()["memory_id"]
    m = (await get(t, "reviewer", pub)).json()
    assert m["origin"] == "published" and m["type"] == "semantic" and m["scopes"] == [{"kind": "project", "ref": "newco-plan"}]
    assert {(x["relation"], x["direction"]) for x in m["lineage"]} <= {
        ("published_from", "published_from")
    }  # the reviewer cannot see the private source
    assert m["lineage"] == []
    own = (await get(t, "agent", pub)).json()
    assert [(x["relation"], x["memory_id"]) for x in own["lineage"]] == [("published_from", note["memory_id"])]
    assert (await get(t, "agent2", note["memory_id"])).status_code == 404  # still private
    assert (await get(t, "agent", note["memory_id"])).json()["status"] == "active"
    # someone else cannot publish it, and a shared memory is not "published" again
    assert (
        await act(
            t,
            "agent2",
            note["memory_id"],
            "publish",
            {"scopes": [{"kind": "project", "ref": "newco-plan"}], "reason_code": "x"},
            revision='W/"rev-1"',
        )
    ).status_code == 404
    (item,) = [i for i in await reviews(t) if i["memory_id"] == pub]
    assert (await decide(t, item["id"], "approve")).status_code == 200
    assert ids_of(await search(t, "agent2", "state licence NewCo")) == [
        pub
    ]  # MEM-03: the approved finding is shared; the working note is not
    assert (await search(t, "agent3", "state licence NewCo"))["hits"] == []  # an agent without the project grant sees neither
    _ = ids


# ---- MEM-05 and RET-02: search ---------------------------------------------------------------------------------------------------------------


async def seed_sarah_memories(t, ids):  # type: ignore[no-untyped-def]
    business = (
        await save(
            t,
            "admin",
            {
                "type": "episodic",
                "content": BUSINESS,
                "summary": "Sarah is considering entrepreneurship: a possible software business",
                "subject_ids": [ids["sarah"]],
                "source_ref": "evt_003",
                "purpose": "career context",
            },
        )
    ).json()["memory_id"]
    cycling = (
        await save(
            t,
            "admin",
            {
                "type": "episodic",
                "content": CYCLIST_TEXT.replace("Sarah Johnson from", "Sarah from"),
                "summary": "Sarah rode 40 km with the cycling club",
                "subject_ids": [ids["sarah"]],
                "source_ref": "evt_c",
                "purpose": "leisure",
            },
        )
    ).json()["memory_id"]
    return business, cycling


async def test_an_entrepreneurship_query_ranks_the_business_memory_and_excludes_the_cycling_one_ret02(api, tenant_mem):
    t = tenant_mem
    ids = await story(t)
    business, cycling = await seed_sarah_memories(t, ids)
    await run_embeds(api, t)
    for query in ("entrepreneurship", "starting a business", "software company decision"):
        out = await search(t, "agent", query, subject_ids=[ids["sarah"]])
        assert ids_of(out)[:1] == [business], (query, out)
        assert cycling not in ids_of(out), query
    out = await search(t, "agent", "starting a business", subject_ids=[ids["sarah"]])
    top = out["hits"][0]
    assert top["matched_by"] == "both" and top["components"]["lex_rank"] == 1 and top["components"]["vec_rank"] == 1 and top["score"] > 0
    assert (
        out["ranker_version"] == "mem-rrf.v1"
        and out["embedding_coverage"] == 1.0
        and out["degraded"] == []
        and out["candidates_considered"] == 2
    )
    bike = await search(t, "agent", "cycling club ride", subject_ids=[ids["sarah"]])
    assert ids_of(bike)[0] == cycling
    # stable: the same request on the same data gives the same order and the same scores
    assert [h["score"] for h in out["hits"]] == [
        h["score"] for h in (await search(t, "agent", "starting a business", subject_ids=[ids["sarah"]]))["hits"]
    ]


async def test_search_respects_status_validity_and_history_mem05(api, tenant_mem):
    t = tenant_mem
    ids = await story(t)
    base = {"type": "semantic", "subject_ids": [ids["sarah"]], "source_ref": "x", "purpose": "p"}
    live = (await save(t, "admin", {**base, "content": "Sarah uses alpha tooling for planning."})).json()["memory_id"]
    old = (await save(t, "admin", {**base, "content": "Sarah uses beta tooling for planning."})).json()["memory_id"]
    future = (
        await save(t, "admin", {**base, "content": "Sarah will use gamma tooling for planning.", "effective_from": "2099-01-01"})
    ).json()["memory_id"]
    lapsed = (
        await save(
            t,
            "admin",
            {**base, "content": "Sarah used delta tooling for planning.", "effective_from": "2020-01-01", "effective_until": "2021-01-01"},
        )
    ).json()["memory_id"]
    expired = (await save(t, "admin", {**base, "content": "Sarah uses epsilon tooling for planning."})).json()["memory_id"]
    proposed = (
        await propose(
            t,
            "agent",
            {
                "type": "semantic",
                "content": "Sarah uses zeta tooling for planning.",
                "requested_scope": sub(ids["sarah"]),
                "source_event_ids": [await accept_event(t, {"kind": "message", "content": "n"}, who="agent")],
            },
        )
    ).json()["memory_id"]
    await act(t, "reviewer", old, "supersede", {"content": "Sarah uses eta tooling for planning.", "reason_code": "corrected"})
    await act(t, "reviewer", expired, "expire", {"reason_code": "stale"})
    out = await search(t, "agent", "tooling for planning", subject_ids=[ids["sarah"]], limit=50)
    got = set(ids_of(out))
    assert live in got and old not in got and future not in got and lapsed not in got and expired not in got and proposed not in got
    assert {h["status"] for h in out["hits"]} == {"active"} and len(got) == 2  # the live one and the replacement
    # include=proposed is curation
    assert (await t.req("POST", "/v1/memories/search", "agent", json={"query": "tooling", "include": ["proposed"]})).status_code == 403
    assert (await t.req("POST", "/v1/memories/search", "app", json={"query": "tooling", "include": ["proposed"]})).status_code == 403
    cur = await search(t, "reviewer", "tooling for planning", include=["proposed"], limit=50)
    assert proposed in ids_of(cur)
    # as_of brings the past back: the superseded memory was true then
    row = (await memory_rows(api, t, "id = :i", i=uuid.UUID(old)))[0]
    hist = await search(t, "agent", "tooling for planning", subject_ids=[ids["sarah"]], limit=50, as_of=(row.recorded_at).isoformat())
    assert old in ids_of(hist) and lapsed not in ids_of(hist) and future not in ids_of(hist)
    back = await search(t, "agent", "tooling for planning", subject_ids=[ids["sarah"]], limit=50, as_of="2020-06-01")
    assert ids_of(back) == [lapsed]  # what was valid in mid-2020


async def test_search_never_returns_what_the_caller_may_not_see_and_counts_only_what_it_may(api, tenant_mem):
    t = tenant_mem
    ids = await story(t)
    business, cycling = await seed_sarah_memories(t, ids)
    for who in ("agent3", "reader", "operator"):  # no subject grant: nothing, and the count says zero
        out = await search(t, who, "entrepreneurship business cycling", limit=50)
        assert out["hits"] == [] and out["candidates_considered"] == 0, who
        assert (await get(t, who, business)).status_code == 404
    seen = await search(t, "agent", "entrepreneurship business cycling", limit=50)
    assert seen["candidates_considered"] == 2
    a, b = await get(t, "agent3", business), await get(t, "agent3", str(uuid.uuid4()))
    assert (
        a.status_code == b.status_code == 404
        and a.json()["error"]["code"] == b.json()["error"]["code"]
        and a.json()["error"]["message"] == b.json()["error"]["message"]
    )


async def test_the_classification_ceiling_hides_restricted_memory_from_agents_without_a_grant(api, tenant_mem):
    t = tenant_mem
    ids = await story(t)
    mid = (
        await save(
            t,
            "admin",
            {
                "type": "semantic",
                "content": "Sarah's compensation band is level five.",
                "subject_ids": [ids["sarah"]],
                "source_ref": "hr",
                "purpose": "comp",
                "classification": "restricted",
            },
        )
    ).json()["memory_id"]
    assert (await search(t, "agent", "compensation band"))["hits"] == [] and (await get(t, "agent", mid)).status_code == 404
    assert ids_of(await search(t, "reviewer", "compensation band")) == [mid]  # curation roles see every classification
    await t.grant("agent", "classification", "restricted")
    assert ids_of(await search(t, "agent", "compensation band")) == [mid] and (await get(t, "agent", mid)).status_code == 200


async def test_a_router_outage_degrades_search_to_lexical_only_and_says_so_then_recovers(api, tenant_mem):
    t = tenant_mem
    ids = await story(t)
    business, _ = await seed_sarah_memories(t, ids)
    await embedding_fault(count=500)
    try:
        out = await search(t, "agent", "entrepreneurship", subject_ids=[ids["sarah"]])
        assert out["degraded"] == ["vector_unavailable"] and ids_of(out) == [business]  # the lexical half still answers, scoped
        assert out["hits"][0]["matched_by"] == "lexical" and out["embedding_coverage"] == 0.0
        from tests.support.worker import running_worker

        async with running_worker(api, stages=("memory_embed",), concurrency=1):
            await asyncio.sleep(1.5)
        assert {r.embedding_status for r in await memory_rows(api, t)} == {"pending"}  # the embed jobs are retrying, not lost
    finally:
        await clear_faults()
    await run_embeds(api, t, timeout=90)
    healthy = await search(t, "agent", "entrepreneurship", subject_ids=[ids["sarah"]])
    assert healthy["degraded"] == [] and healthy["embedding_coverage"] == 1.0 and healthy["hits"][0]["matched_by"] == "both"


async def test_a_local_only_memory_is_never_embedded_and_stays_lexical(api, tenant_mem):
    t = tenant_mem
    ids = await story(t)
    mid = (
        await save(
            t,
            "admin",
            {
                "type": "semantic",
                "content": "Sarah keeps a private journal about the venture.",
                "subject_ids": [ids["sarah"]],
                "source_ref": "x",
                "purpose": "p",
                "classification": "local_only",
            },
        )
    ).json()
    assert mid["embedding_status"] == "skipped_by_policy"
    assert await memory_rows(api, t, "embedding_status = 'skipped_by_policy'")
    jobs = await owner_jobs(mid["memory_id"])
    assert jobs == []
    assert ids_of(await search(t, "reviewer", "private journal")) == [mid["memory_id"]]


async def test_memory_commands_are_not_listed_by_the_command_api(api, tenant_mem):
    t = tenant_mem
    ids = await story(t)
    r = await save(t, "app", body(subject_ids=[ids["sarah"]], source_ref="x", purpose="p"))
    assert r.status_code == 201
    cmd = r.json()["command_id"]
    for who in ("admin", "app", "operator", "reviewer"):
        items = (await t.get("/v1/commands", who, limit=100)).json()["items"]
        assert all(not i["command_type"].startswith("memory.") for i in items), who
        assert (await t.get(f"/v1/commands/{cmd}", who)).status_code == 404
    rows = await memory_rows(api, t)
    assert rows[0].command_id == uuid.UUID(cmd)


async def test_memory_jobs_show_in_operations_and_a_dead_embed_can_be_retried(api, tenant_mem):
    from tests.support.p5 import owner_sql

    t = tenant_mem
    ids = await story(t)
    mid = (await save(t, "admin", body(subject_ids=[ids["sarah"]], source_ref="x", purpose="p"))).json()["memory_id"]
    jobs = (await t.get("/v1/operations/jobs", "operator", subject_type="memory")).json()["items"]
    assert [(j["stage"], j["subject_type"], j["subject_id"], j["status"]) for j in jobs] == [("memory_embed", "memory", mid, "pending")]
    assert (await t.get("/v1/operations/jobs", "operator", subject_type="command", limit=100)).status_code == 200
    # the embed died (say the router was down for good): the job is dead and the memory says it is lexical-only
    await owner_sql(
        "UPDATE outbox_jobs SET status = 'dead', last_error_code = 'router_unavailable', last_error_category = 'retryable', attempts = max_attempts WHERE subject_id = :m",
        m=uuid.UUID(mid),
    )
    await owner_sql("UPDATE curated_memories SET embedding_status = 'failed' WHERE id = :m", m=uuid.UUID(mid))
    assert (await get(t, "admin", mid)).json()["embedding"]["status"] == "failed"
    retry = await t.req("POST", f"/v1/operations/jobs/{jobs[0]['id']}/retry", "operator", json={"reason_code": "fault_cleared"})
    assert retry.status_code == 202, retry.text
    new = (await t.get(f"/v1/operations/jobs/{retry.json()['new_job_id']}", "operator")).json()
    assert (new["stage"], new["subject_type"], new["subject_id"], new["generation"]) == ("memory_embed", "memory", mid, 1)
    await run_embeds(api, t)  # embedding_status != pending now, so wait on the job instead
    assert (await get(t, "admin", mid)).json()["embedding"]["status"] == "ready"


async def test_a_curator_can_reject_a_proposal_and_it_never_becomes_visible(api, tenant_mem):
    t = tenant_mem
    ids = await story(t)
    evt = await accept_event(t, {"kind": "message", "content": "agent note"}, who="agent")
    p = (
        await propose(
            t,
            "agent",
            {
                "type": "semantic",
                "content": "Sarah owns a vineyard in Chile.",
                "subject_ids": [ids["sarah"]],
                "requested_scope": sub(ids["sarah"]),
                "source_event_ids": [evt],
            },
        )
    ).json()
    ok = await decide(t, p["review_id"], "reject", rationale="incorrect_extraction")
    assert ok.status_code == 200 and ok.json()["memory_ids"] == [p["memory_id"]]
    m = (await get(t, "reviewer", p["memory_id"])).json()
    assert m["status"] == "rejected" and m["content"] == "Sarah owns a vineyard in Chile."  # kept for audit
    assert (await search(t, "agent2", "vineyard Chile", include=[]))["hits"] == []
    again = await decide(t, p["review_id"], "approve")
    assert again.status_code == 409  # a decided item cannot be decided again
    # the proposer sees what became of its proposal (it may read its own), nobody else sees it
    assert (await get(t, "agent", p["memory_id"])).json()["status"] == "rejected"
    assert (await get(t, "agent2", p["memory_id"])).status_code == 404


async def test_the_entity_read_counts_only_the_memories_the_caller_may_see(api, tenant_mem):
    t = tenant_mem
    ids = await story(t)
    for i in range(2):
        await save(t, "admin", body(content=f"Sarah fact number {i}", subject_ids=[ids["sarah"]], source_ref="x", purpose="p"))
    await save(
        t,
        "agent",
        body(content="agent's private thought about Sarah", subject_ids=[ids["sarah"]], scopes=[{"kind": "agent", "ref": "self"}]),
    )
    assert (await t.get(f"/v1/entities/{ids['sarah']}", "reviewer")).json()["memories"] == 2
    assert (await t.get(f"/v1/entities/{ids['sarah']}", "agent")).json()["memories"] == 3  # the agent also counts its own private one
    assert (await t.get(f"/v1/entities/{ids['sarah']}", "agent3")).json()["memories"] == 0  # no grant: zero, not "2 you cannot see"
    assert (await t.get(f"/v1/entities/{ids['sarah']}", "agent2")).json()["memories"] == 2


async def test_the_expiry_job_is_idempotent_and_ignores_a_memory_that_is_gone_or_already_retired(api, tenant_mem):
    from kg_jobs.client import ClaimedJob
    from kg_memory.jobs import MemoryExpireJob

    t = tenant_mem
    await story(t)
    mid = (
        await save(t, "agent", {"type": "working", "content": "short lived note about ledgers", "run_id": "r9", "ttl_hours": 0.0003})
    ).json()["memory_id"]
    await asyncio.sleep(1.6)
    job = MemoryExpireJob(api.engine)

    def claimed(memory_id: str) -> ClaimedJob:
        return ClaimedJob(
            job_id=uuid.uuid4(),
            workspace_id=t.ws,
            stage="memory_expire",
            subject_type="memory",
            subject_id=uuid.UUID(memory_id),
            payload={"memory_id": memory_id, "generation": 0},
            attempts=1,
            max_attempts=6,
            lease_token=uuid.uuid4(),
            pipeline_version="p5.v1",
            ontology_version_id=None,
            correlation_id=None,
        )

    await job.handle(claimed(mid))
    first = (await memory_rows(api, t, "id = :i", i=uuid.UUID(mid)))[0]
    assert (first.status, first.revision) == ("expired", 2)
    await job.handle(claimed(mid))  # replayed (at-least-once delivery): nothing changes
    await job.handle(claimed(str(uuid.uuid4())))  # a memory that does not exist: nothing to do, not an error
    second = (await memory_rows(api, t, "id = :i", i=uuid.UUID(mid)))[0]
    assert (second.status, second.revision, second.effective_until) == ("expired", 2, first.effective_until)
