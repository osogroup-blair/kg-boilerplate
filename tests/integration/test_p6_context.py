"""P6 WP2: the context engine over the real stack (Postgres, Neo4j, the mock router, the real worker for the world's construction).

One world per test module run would be faster, but tests share nothing: each builds Sarah in a fresh tenant (the construction is the slow part, ~30 s)."""

from __future__ import annotations

import pytest

from tests.support.p4 import strict_mock  # noqa: F401
from tests.support.p5 import CYCLIST_TEXT, GUIDELINE, PREFERENCE
from tests.support.p6 import FINDING, build, ids_in, lines, ok_build, sarah_world, texts

pytestmark = pytest.mark.integration


async def test_the_planning_task_returns_what_the_blueprint_says_at_600_tokens(api, tenant_ctx, strict_mock):  # noqa: F811
    t = tenant_ctx
    ids = await sarah_world(api, t)
    out = await ok_build(t, "agent", subject_ids=[ids["sarah"]], profile="planning", max_tokens=600, project="newco-plan")
    text = texts(out)
    assert out["status"] == "complete", out["unavailable_sources"]
    assert out["budgets"]["used_tokens"] <= 600 and out["budgets"]["max_tokens"] == 600
    assert out["budgets"]["tokenizer_id"].startswith("tiktoken:max(o200k_base,cl100k_base)")
    # current verified fact: Engineering Manager at Acme, with its evidence
    assert any("Engineering Manager" in ln and "Acme" in ln and "E:evt_002" in ln for ln in lines(out)), text
    assert not any("Software Engineer" in ln for ln in lines(out))  # the ended role is not stated as current (RET-03)
    # the unverified aspiration, the episodic memory and the caution about NewCo
    assert any("considering" in ln.lower() for ln in lines(out)), text
    assert ids["business"] in ids_in(out)
    assert any("Unverified claim under review" in ln and "NewCo" in ln for ln in lines(out)), text
    # guidance, the preference and the project finding
    assert ids["guide"] in {i["id"] for i in out["instructions"]}
    assert ids["pref"] in ids_in(out) and ids["finding"] in ids_in(out)
    # the unknown, and what is NOT there
    assert "No verified financial runway for Sarah Johnson" in text
    assert ids["cycling"] not in ids_in(out) and ids["note"] not in ids_in(out)
    assert CYCLIST_TEXT not in text and "private to the research agent" not in text
    assert text.startswith("[CONTEXT DATA") and text.endswith("[END CONTEXT DATA]")
    assert FINDING in text or "state licence" in text.lower()
    assert GUIDELINE[:20] in text and PREFERENCE[:20] in text


async def test_a_smaller_budget_keeps_the_required_tier_and_too_small_is_insufficient_budget(api, tenant_ctx, strict_mock):  # noqa: F811
    t = tenant_ctx
    ids = await sarah_world(api, t)
    base = dict(subject_ids=[ids["sarah"]], profile="planning", project="newco-plan")
    big = await ok_build(t, "agent", max_tokens=1200, **base)
    small = await ok_build(t, "agent", max_tokens=big["budgets"]["required_tokens"] + 10, **base)
    assert small["status"] == "complete" and small["budgets"]["used_tokens"] <= small["budgets"]["max_tokens"]
    assert len(small["facts"]) + len(small["memories"]) < len(big["facts"]) + len(big["memories"])
    assert {i["handle"] for i in small["unknowns"]} == {i["handle"] for i in big["unknowns"]}  # required items survive
    assert {i["handle"] for i in small["conflicts"]} == {i["handle"] for i in big["conflicts"]}
    tiny = await ok_build(t, "agent", max_tokens=64, **base)
    assert tiny["status"] == "insufficient_budget" and tiny["facts"] == [] and tiny["budgets"]["required_tokens"] > 64
    assert tiny["budgets"]["used_tokens"] <= 64
    again = await ok_build(t, "agent", max_tokens=tiny["budgets"]["required_tokens"], **base)
    assert again["status"] == "complete"  # asking for exactly what it said works
    assert (
        await build(t, "agent", subject_ids=[ids["sarah"]], profile="planning", max_tokens=1201)
    ).status_code == 422  # a request can only narrow


# ---- AT-06 / AT-07 / RET-02 / RET-03 / RET-05 / AT-09 / CTX-04 / CTX-06 ------------------------------------------------------------------------------


async def test_at06_the_aspiration_memory_is_included_with_its_evidential_status_and_sources(api, tenant_ctx, strict_mock):  # noqa: F811
    t = tenant_ctx
    ids = await sarah_world(api, t)
    out = await ok_build(t, "agent", "Is Sarah thinking about entrepreneurship?", subject_ids=[ids["sarah"]], profile="planning")
    (mem,) = [m for m in out["memories"] if m["id"] == ids["business"]]
    assert mem["evidence_status"] == "unverified" and "(unverified)" in mem["text"] and mem["memory_type"] == "episodic"
    cites = {c["handle"]: c for c in out["citations"]}
    assert set(mem["citations"]) <= set(cites) and mem["handle"] in cites  # every handle resolves
    kinds = {cites[h]["kind"] for h in mem["citations"]}
    assert {"memory", "interaction"} <= kinds or {"memory", "episode"} <= kinds  # its own pointer and the event behind it


async def test_at07_an_unrelated_task_leaves_the_career_memories_out(api, tenant_ctx, strict_mock):  # noqa: F811
    t = tenant_ctx
    ids = await sarah_world(api, t)
    out = await ok_build(t, "agent", "Which cycling club is Sarah in?", subject_ids=[ids["sarah"]], profile="quick_answer")
    assert ids["cycling"] in ids_in(out)
    assert ids["business"] not in ids_in(out) and ids["pref"] not in ids_in(out) and ids["guide"] not in ids_in(out)
    assert "software business" not in texts(out) and "Engineering Manager" not in texts(out)  # no career facts for a cycling question
    assert "NewCo" not in texts(out)  # and no conflict about the employer either: it is not material to this task
    assert out["status"] == "complete"
    # the same subject asked about work brings the career back
    work = await ok_build(t, "agent", "Where does Sarah work", subject_ids=[ids["sarah"]], profile="quick_answer")
    assert "Acme" in texts(work) and "Engineering Manager" in texts(work) and "Unverified claim under review" in texts(work)


async def test_ret02_entrepreneurship_finds_the_decision_memory_and_not_the_cycling_ride(api, tenant_ctx, strict_mock):  # noqa: F811
    t = tenant_ctx
    ids = await sarah_world(api, t)
    out = await ok_build(t, "agent", subject_ids=[ids["sarah"]], profile="planning", project="newco-plan")
    assert ids["business"] in ids_in(out) and ids["cycling"] not in ids_in(out)


async def test_ret03_the_ended_role_is_never_current_and_as_of_brings_it_back_labelled(api, tenant_ctx, strict_mock):  # noqa: F811
    t = tenant_ctx
    ids = await sarah_world(api, t)
    now = await ok_build(t, "agent", "Where does Sarah work", subject_ids=[ids["sarah"]], profile="quick_answer")
    assert "Engineering Manager" in texts(now) and "Software Engineer" not in texts(now)
    assert {f["id"] for f in now["facts"]} >= {ids["em"]} and ids["swe"] not in ids_in(now)
    then = await ok_build(t, "agent", "Where does Sarah work", subject_ids=[ids["sarah"]], profile="quick_answer", as_of="2023-01-01")
    assert "Software Engineer" in texts(then) and "Engineering Manager" not in texts(then)
    assert "VERIFIED FACTS AS OF 2023-01-01" in texts(then) and "Records as they stood on 2023-01-01" in texts(then)
    assert ids["swe"] in ids_in(then) and ids["em"] not in ids_in(then)
    assert "NewCo" not in texts(then)  # a claim made in 2026 is not part of the picture on 2023-01-01
    # record time: before the manager role was recorded the system did not know it (KGR-04)
    early = await ok_build(t, "agent", "Where does Sarah work", subject_ids=[ids["sarah"]], profile="quick_answer", known_at="2020-01-01")
    assert early["facts"] == [] and early["status"] in ("insufficient_evidence", "partial")


async def test_history_is_listed_only_when_the_profile_names_it_and_the_task_asks(api, tenant_ctx, strict_mock):  # noqa: F811
    t = tenant_ctx
    ids = await sarah_world(api, t)
    out = await ok_build(t, "agent", "What was Sarah's previous role at Acme?", subject_ids=[ids["sarah"]], profile="research")
    assert "# HISTORY (no longer current)" in texts(out) and "Software Engineer" in texts(out)
    first = texts(out).index("Engineering Manager") if "Engineering Manager" in texts(out) else -1
    assert first < texts(out).index("Software Engineer")  # historical items sit BELOW the current ones (CTX-04)
    plain = await ok_build(t, "agent", "Where does Sarah work", subject_ids=[ids["sarah"]], profile="research")
    assert "Software Engineer" not in texts(plain)


async def test_ret05_provenance_unknowns_and_conflicts_are_present_and_correct(api, tenant_ctx, strict_mock):  # noqa: F811
    t = tenant_ctx
    ids = await sarah_world(api, t)
    out = await ok_build(t, "agent", subject_ids=[ids["sarah"]], profile="planning", project="newco-plan")
    assert [u["text"] for u in out["unknowns"]] == ["No verified financial runway for Sarah Johnson"]
    (conflict,) = out["conflicts"]
    assert conflict["citations"][0].startswith("F:") and any(h.startswith("R:") for h in conflict["citations"])
    cites = {c["handle"]: c for c in out["citations"]}
    fact_cite = cites[conflict["citations"][0]]
    assert fact_cite["kind"] == "fact" and fact_cite["id"] == ids["newco_fact"]
    review = next(cites[h] for h in conflict["citations"] if h.startswith("R:"))
    assert review["kind"] == "review" and review["url"] == f"/v1/reviews/{review['id']}"
    em = next(f for f in out["facts"] if f["id"] == ids["em"])
    assert any(
        cites[h]["source_ref"] == "evt_002"
        for h in em["citations"]
        if h in cites and cites[h]["kind"] in ("command", "episode", "interaction")
    )
    assert all(cites[h]["id"] and cites[h]["url"] for h in cites if cites[h]["kind"] != "external")
    # the claim is never stated as current anywhere
    assert not any("works at NewCo" in ln and "Unverified claim" not in ln for ln in lines(out))


async def test_at09_nothing_known_is_unknown_not_invented(api, tenant_ctx, strict_mock):  # noqa: F811
    t = tenant_ctx
    ids = await sarah_world(api, t)
    ghost = (await t.entity("Person", "Priya Raman")).json()["entity_id"]  # exists, but nothing is known about her
    out = await ok_build(t, "agent", "Where does Priya work", subject_ids=[ghost], profile="planning")
    assert out["status"] == "insufficient_evidence" and out["facts"] == [] and out["memories"] == [] and out["instructions"] == []
    assert {u["text"] for u in out["unknowns"]} >= {
        "No verified current employer for Priya Raman",
        "No verified financial runway for Priya Raman",
    }
    assert any("No authorized records" in u["text"] for u in out["unknowns"])
    assert not any(ln for ln in lines(out) if "Priya" in ln and "No verified" not in ln)
    _ = ids


async def test_ctx04_a_superseded_memory_is_absent_by_default(api, tenant_ctx, strict_mock):  # noqa: F811
    from tests.support.p5 import act, run_embeds

    t = tenant_ctx
    ids = await sarah_world(api, t)
    r = await act(
        t, "admin", ids["pref"], "supersede", {"content": PREFERENCE + " And a clear recommendation.", "reason_code": "clarified"}
    )
    assert r.status_code == 201, r.text
    await run_embeds(api, t)
    out = await ok_build(t, "agent", subject_ids=[ids["sarah"]], profile="planning", project="newco-plan")
    assert ids["pref"] not in ids_in(out) and r.json()["memory_id"] in ids_in(out)
    assert "And a clear recommendation." in texts(out)  # the old text is a prefix of the new, so look for what only the new one says


async def test_ctx06_the_same_request_gives_the_same_package(api, tenant_ctx, strict_mock):  # noqa: F811
    t = tenant_ctx
    ids = await sarah_world(api, t)
    runs = [
        await ok_build(t, "agent", subject_ids=[ids["sarah"]], profile="planning", project="newco-plan", max_tokens=600) for _ in range(3)
    ]
    assert len({r["context_text"] for r in runs}) == 1
    assert (
        len({tuple(i["handle"] for k in ("facts", "memories", "instructions", "unknowns", "conflicts") for i in r[k]) for r in runs}) == 1
    )
    assert len({r["context_id"] for r in runs}) == 3  # each build is its own trace
    assert runs[0]["versions"]["ranker"] == "ctx-rank.v1"


async def test_the_extractive_mode_uses_the_sources_own_words(api, tenant_ctx, strict_mock):  # noqa: F811
    t = tenant_ctx
    ids = await sarah_world(api, t)
    out = await ok_build(t, "agent", subject_ids=[ids["sarah"]], profile="planning", project="newco-plan", compression="extractive")
    assert out["compression"] == "extractive" and out["versions"]["compression"] == "extractive"
    assert "Sarah discussed a possible software business with Michael; she had not decided to leave Acme." in texts(
        out
    )  # the memory's content, not its summary
    assert "(role:" not in texts(out)  # no template rewording
    assert (
        await build(t, "agent", subject_ids=[ids["sarah"]], profile="planning", compression="summarized")
    ).status_code == 422  # planning does not allow it


# ---- CTX-07 / RET-06: degradation ---------------------------------------------------------------------------------------------------------------


@pytest.mark.single_process
async def test_ctx07_a_graph_outage_is_partial_and_current_facts_still_come_from_postgres(api, tenant_ctx, strict_mock, monkeypatch):  # noqa: F811
    from neo4j.exceptions import ServiceUnavailable

    t = tenant_ctx
    ids = await sarah_world(api, t)

    async def down(*a, **k):  # type: ignore[no-untyped-def]
        raise ServiceUnavailable("neo4j is down")

    monkeypatch.setattr(api.app.state.kg.knowledge.search, "run", down)
    out = await ok_build(t, "agent", subject_ids=[ids["sarah"]], profile="planning", project="newco-plan")
    assert out["status"] == "partial" and out["unavailable_sources"] == [{"source": "graph", "reason": "unavailable"}]
    assert "Engineering Manager" in texts(out) and "Acme" in texts(out)  # Postgres is authoritative: current state does not wait for Neo4j
    assert ids["business"] in ids_in(out)
    refused = await build(t, "agent", subject_ids=[ids["sarah"]], profile="planning", allow_partial=False)
    assert refused.status_code == 503 and refused.json()["error"]["code"] == "context_incomplete"
    assert refused.json()["error"]["details"]["unavailable_sources"] == [{"source": "graph", "reason": "unavailable"}]
    assert "Engineering Manager" not in refused.text  # no package at all


@pytest.mark.single_process
async def test_a_slow_source_times_out_into_partial(api, tenant_ctx, strict_mock, monkeypatch):  # noqa: F811
    import asyncio

    t = tenant_ctx
    ids = await sarah_world(api, t)

    async def slow(*a, **k):  # type: ignore[no-untyped-def]
        await asyncio.sleep(30)

    monkeypatch.setattr(api.app.state.kg.knowledge.search, "run", slow)
    monkeypatch.setattr(api.app.state.kg.context.service._s, "context_source_timeout_ms", 300, raising=False)
    out = await ok_build(t, "agent", subject_ids=[ids["sarah"]], profile="planning")
    assert out["status"] == "partial" and {"source": "graph", "reason": "timeout"} in out["unavailable_sources"]


async def test_an_embedding_outage_is_partial_lexical_only_and_says_so(api, tenant_ctx, strict_mock):  # noqa: F811
    from tests.support.p5 import clear_faults, embedding_fault

    t = tenant_ctx
    ids = await sarah_world(api, t)
    await embedding_fault("provider_unavailable", 200)
    try:
        out = await ok_build(t, "agent", subject_ids=[ids["sarah"]], profile="planning", project="newco-plan")
        refused = await build(t, "agent", subject_ids=[ids["sarah"]], profile="planning", allow_partial=False)
    finally:
        await clear_faults()
    assert out["status"] == "partial"
    assert {u["source"] for u in out["unavailable_sources"]} == {"memory_vector", "graph"}
    # the gateway retries a failing embedding with backoff, which can outlast the per-source timeout: both are an honest "the vector half is not available"
    assert {u["reason"] for u in out["unavailable_sources"] if u["source"] == "memory_vector"} <= {"vector_unavailable", "timeout"}
    assert "Engineering Manager" in texts(out) and ids["business"] in ids_in(out)  # the lexical half and Postgres still answer
    assert refused.status_code == 503
    healthy = await ok_build(t, "agent", subject_ids=[ids["sarah"]], profile="planning", project="newco-plan")
    assert healthy["status"] == "complete"


async def test_a_backlog_older_than_the_freshness_requirement_is_stale(api, tenant_ctx, strict_mock):  # noqa: F811
    from tests.support.p4 import ingest
    from tests.support.worker import running_worker

    t = tenant_ctx
    ids = await sarah_world(api, t)
    async with running_worker(api, stages=("normalize",), concurrency=1):  # accept an event and normalise it, leaving graph_ingest waiting
        evt = await ingest(t, {"kind": "message", "content": "Sarah Johnson: a note nobody extracted yet."}, subjects=[ids["sarah"]])
        from tests.support.worker import wait_until

        async def waiting() -> bool:
            rows = await __import__("tests.support.p6", fromlist=["q"]).q(
                api, t, "SELECT count(*) AS n FROM outbox_jobs WHERE workspace_id = :ws AND stage = 'graph_ingest' AND status = 'pending'"
            )
            return bool(rows[0].n)

        await wait_until(waiting, timeout=30, what="a waiting graph_ingest job")
    _ = evt
    fresh = await ok_build(t, "agent", subject_ids=[ids["sarah"]], profile="planning", freshness_requirement={"max_lag_seconds": 3600})
    assert fresh["status"] == "complete" and fresh["freshness"]["graph_lag_seconds"] is not None
    stale = await ok_build(t, "agent", subject_ids=[ids["sarah"]], profile="planning", freshness_requirement={"max_lag_seconds": 0})
    assert stale["status"] == "partial" and {"source": "graph", "reason": "stale"} in stale["unavailable_sources"]
    assert stale["freshness"]["stale"] is True and "graph_ingest" in stale["source_watermarks"]
    assert (
        await build(
            t, "agent", subject_ids=[ids["sarah"]], profile="planning", freshness_requirement={"max_lag_seconds": 0}, allow_partial=False
        )
    ).status_code == 503


# ---- profiles ---------------------------------------------------------------------------------------------------------------------------


async def test_the_workspaces_ontology_profile_overrides_the_builtin_and_unknown_profiles_are_422(api, tenant_ctx, strict_mock):  # noqa: F811
    t = tenant_ctx
    listed = (await t.req("GET", "/v1/context/profiles", "agent", key="")).json()
    by = {p["name"]: p for p in listed}
    assert set(by) == {"quick_answer", "planning", "research", "review", "personal_assistant"}
    assert by["planning"]["source"] == "ontology" and by["planning"]["compression"] == ["structured", "extractive"]
    assert by["research"]["source"] == "builtin" and "summarized" in by["research"]["compression"]
    bad = await build(t, "agent", profile="no_such_profile")
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "unknown_profile"
    assert (await build(t, "agent", profile="Planning!")).status_code == 422


async def test_evidence_required_drops_what_has_no_evidence_and_other_profiles_label_it(api, tenant_ctx, strict_mock):  # noqa: F811
    t = tenant_ctx
    ids = await sarah_world(api, t)
    # agent2's working note has no evidence: personal_assistant (evidence not required) shows it, labelled; planning (evidence required) never would
    run = await ok_build(
        t, "agent2", "check NewCo licensing rules for the plan", profile="personal_assistant", run_id="r1", subject_ids=[ids["sarah"]]
    )
    note = [m for m in run["memories"] if m["id"] == ids["note"]]
    assert note and note[0]["supported"] is False and "unsupported]" in next(ln for ln in lines(run) if "NewCo licensing" in ln)


async def test_a_subject_the_graph_has_not_seen_yet_does_not_make_the_graph_unavailable(api, tenant_ctx, strict_mock):  # noqa: F811
    """An entity whose projection is still pending is unknown to Neo4j: the graph search then runs without a centre instead of failing, and its own facts come from Postgres."""
    import uuid

    from tests.support.p6 import owner_rows

    t = tenant_ctx
    ids = await sarah_world(api, t)
    new = uuid.uuid4()
    await owner_rows(
        "INSERT INTO entities (id, workspace_id, type_key, display_name, ontology_version_id, created_by_command_id, last_command_id, projection_status) "
        "SELECT :id, :ws, 'Person', 'Pending Person', ontology_version_id, created_by_command_id, last_command_id, 'pending' FROM entities WHERE id = :sarah",
        id=new,
        ws=t.ws,
        sarah=uuid.UUID(ids["sarah"]),
    )
    out = await ok_build(t, "agent", "Where does Pending Person work", subject_ids=[str(new)], profile="planning")
    assert out["unavailable_sources"] == [] and out["status"] == "insufficient_evidence"
    assert "No verified current employer for Pending Person" in texts(out)
