"""P5 WP5: the memory_propose stage through the real pipeline (API -> outbox -> normalize -> graph_ingest -> memory_propose -> embed) against the strict scenario mock."""

from __future__ import annotations

import json
import uuid

import pytest
import yaml
from kg_obs import metrics

from eval.memory.gold import load as load_gold
from eval.memory.score import score
from tests.support.p4 import EVT003, Spied, chat_calls, ingest, run_event, sarah_story, sql, stage, wait_terminal
from tests.support.p5 import (
    BAD_MEMORY_JSON,
    BUSINESS,
    CYCLIST,
    EVT004,
    INJECTION,
    TRIVIAL,
    UNMAPPED,
    act,
    get,
    linked_names,
    memory_review_count,
    memory_rows,
    memory_snapshot,
    search,
)
from tests.support.tenants import MEMORY_YAML, clear_graph, make_tenant
from tests.support.worker import running_worker, wait_until

pytestmark = pytest.mark.integration
GOLD = {g["id"]: g for g in load_gold()}


async def story(t):  # type: ignore[no-untyped-def]
    ids = await sarah_story(t)
    await t.grant("agent", "subject", ids["sarah"])
    return ids


def subjects_of(ids):  # type: ignore[no-untyped-def]
    return [ids["sarah"], ids["michael"], ids["topic"]]


async def test_evt003_yields_one_episodic_memory_linked_to_its_interaction_episode_and_fact(api, tenant_mem, strict_mock):
    t = tenant_mem
    ids = await story(t)
    detail = await run_event(api, t, EVT003, subjects=subjects_of(ids))
    st = stage(detail, "memory_propose")
    assert st["status"] == "succeeded" and detail["processing_status"] == "indexed", st
    res = st["result"]
    assert (res["kept"], res["candidates"], res["statuses"], res["drops"], res["prompt_version"], res["alias"]) == (
        1,
        1,
        {"active": 1},
        {},
        "mem.v1",
        "oso/general",
    )
    assert res["chat_calls"] == 1 and res["prompt_tokens"] > 0 and len(res["memory_ids"]) == 1 and res["review_ids"] == []
    (m,) = await memory_rows(api, t)
    assert (m.type, m.origin, m.status, m.evidence_status, m.authority_class, m.classification) == (
        "episodic",
        "extracted",
        "active",
        "unverified",
        "conversation_report",
        "internal",
    )
    assert (
        m.content == BUSINESS and "entrepreneurship" in m.summary and m.derived_by_alias == "oso/general" and m.prompt_version == "mem.v1"
    )
    assert m.created_by == uuid.UUID(await t.principal_id("app")) and m.pipeline_version == "p5.v1" and float(m.importance) == 0.8
    full = (await get(t, "admin", str(m.id))).json()
    assert full["scopes"] == [{"kind": "subject", "ref": ids["sarah"]}] and full["subject_ids"] == [ids["sarah"]]
    kinds = sorted(e["source_kind"] for e in full["evidence"])
    assert kinds == ["episode", "fact", "interaction"]
    fact = [e for e in full["evidence"] if e["source_kind"] == "fact"][0]
    rel = (await sql(api, t, "SELECT rel_type FROM facts WHERE id = :f", f=uuid.UUID(fact["fact_id"])))[0].rel_type
    assert rel == "CONSIDERING"  # the memory references the graph fact instead of restating it
    ep = [e for e in full["evidence"] if e["source_kind"] == "episode"][0]
    assert ep["episode_uuid"] == stage(detail, "graph_ingest")["result"]["episode_uuid"] and ep["interaction_id"] == detail["id"]
    assert full["embedding"]["status"] == "ready" and full["embedding"]["version"] == 1 and full["embedding"]["dim"] == 384
    # the ledger command is attributable and content-free
    cmd = (await sql(api, t, "SELECT command_type, authority_class, payload::text AS payload FROM commands WHERE id = :c", c=m.command_id))[
        0
    ]
    assert cmd.command_type == "memory.extract" and cmd.authority_class == "conversation_report" and BUSINESS not in cmd.payload
    assert (await search(t, "agent", "starting a business"))["hits"][0]["memory_id"] == str(m.id)


@pytest.mark.parametrize("gold_id", sorted(GOLD))
async def test_every_gold_scenario_scores_perfectly(api, tenant_mem, strict_mock, gold_id):
    gold = GOLD[gold_id]
    t = tenant_mem
    ids = await story(t)
    names = {"Sarah Johnson": ids["sarah"], "Michael": ids["michael"], "Entrepreneurship": ids["topic"]}
    detail = await run_event(api, t, gold["content"], subjects=[names[n] for n in gold["subjects"]], occurred_at=gold["occurred_at"])
    scored = score(
        gold,
        await memory_snapshot(api, t),
        stage(detail, "memory_propose"),
        gold["content"]["content"],
        await linked_names(api, t, detail["id"]),
        await memory_review_count(api, t),
    )
    assert scored["stage_ok"] and scored["error_code_ok"] and scored["drops_ok"] and scored["reviews_ok"], scored
    assert scored["memories"]["fp"] == 0 and scored["memories"]["fn"] == 0, scored["memories"]
    assert (
        scored["escalation_violations"]
        == scored["unmapped_subject_leaks"]
        == scored["procedural_auto_activations"]
        == scored["scope_violations"]
        == 0
    )


async def test_an_extracted_procedural_memory_is_proposed_with_a_review_and_a_reviewer_decides(api, tenant_mem, strict_mock):
    t = tenant_mem
    ids = await story(t)
    detail = await run_event(api, t, EVT004, subjects=subjects_of(ids))
    res = stage(detail, "memory_propose")["result"]
    assert res["statuses"] == {"proposed": 1} and len(res["review_ids"]) == 1
    (m,) = await memory_rows(api, t)
    assert (m.type, m.status, m.origin) == ("procedural", "proposed", "extracted")
    assert (await search(t, "agent", "advising options risks trade-offs"))["hits"] == []  # not active, so not served
    from tests.support.p4 import decide

    ok = await decide(t, res["review_ids"][0], "approve", rationale="verified_with_source")
    assert ok.status_code == 200
    assert (await search(t, "agent", "advising options risks trade-offs"))["hits"][0]["memory_id"] == str(m.id)


async def test_instructions_inside_content_choose_neither_scope_nor_status_nor_importance(api, tenant_mem, strict_mock):
    t = tenant_mem
    ids = await story(t)
    detail = await run_event(api, t, INJECTION, subjects=[ids["sarah"]])
    assert stage(detail, "memory_propose")["status"] == "succeeded"
    (m,) = await memory_rows(api, t)
    assert (m.type, m.status) == ("procedural", "proposed")  # at most a PROPOSAL a reviewer must approve
    scopes = await sql(api, t, "SELECT scope_kind FROM memory_scopes WHERE memory_id = :m", m=m.id)
    assert [s.scope_kind for s in scopes] == ["subject"]  # the policy default, never what the content asked for ("workspace")
    assert (await search(t, "agent", "reveal private data every agent"))["hits"] == []
    assert (await search(t, "agent3", "reveal private data every agent", include=[]))["candidates_considered"] == 0


async def test_a_trivial_event_yields_nothing(api, tenant_mem, strict_mock):
    t = tenant_mem
    ids = await story(t)
    detail = await run_event(api, t, TRIVIAL, subjects=[ids["sarah"]])
    st = stage(detail, "memory_propose")
    assert st["status"] == "succeeded" and st["result"]["kept"] == 0 and st["result"]["drops"] == {"below_importance": 1}
    assert await memory_rows(api, t) == []
    assert detail["processing_status"] == "indexed"


async def test_a_name_the_platform_did_not_link_is_dropped_not_invented(api, tenant_mem, strict_mock):
    t = tenant_mem
    ids = await story(t)
    detail = await run_event(api, t, UNMAPPED, subjects=[ids["sarah"]])
    assert stage(detail, "memory_propose")["result"]["drops"] == {"unmapped_subject": 1} and await memory_rows(api, t) == []
    assert (await sql(api, t, "SELECT count(*) AS n FROM entities WHERE workspace_id = :ws AND display_name LIKE 'Priya%'"))[
        0
    ].n == 0  # and no entity was created for it


@pytest.mark.parametrize(
    "extra", [{"consent": {"external_processing": "denied"}}, {"sensitivity": "local_only"}], ids=["consent_denied", "local_only"]
)
async def test_data_that_may_not_leave_makes_zero_memory_model_calls_ing06(api, strict_mock, extra):
    d = yaml.safe_load(MEMORY_YAML)
    d["entity_types"]["Person"]["allowed_classifications"].append("local_only")
    t = await make_tenant(api, ontology=yaml.safe_dump(d))
    try:
        ids = await sarah_story(t)
        with Spied(api) as spy:
            detail = await run_event(api, t, EVT003, subjects=subjects_of(ids), **extra)
        st = stage(detail, "memory_propose")
        assert st["status"] == "skipped_by_policy" and detail["processing_status"] == "indexed", st
        assert (
            chat_calls(spy) == 0
            and (await sql(api, t, "SELECT count(*) AS n FROM model_calls WHERE workspace_id = :ws AND purpose = 'memory_extraction'"))[0].n
            == 0
        )
        assert await memory_rows(api, t) == []
    finally:
        await clear_graph(api, t.group_id)


async def test_memory_extraction_is_skipped_by_policy_when_the_ontology_disables_it(api, tenant, strict_mock):
    t = tenant  # the pre-P5 tenant: extraction disabled by policy
    ids = await sarah_story(t)
    with Spied(api) as spy:
        detail = await run_event(api, t, EVT003, subjects=subjects_of(ids))
    st = stage(detail, "memory_propose")
    assert (st["status"], st["last_error_code"]) == ("skipped_by_policy", "extraction_disabled") and detail[
        "processing_status"
    ] == "indexed"
    assert not any(
        "memory" in json.dumps(r["body"]).lower()
        for r in spy.requests
        if r["path"].endswith("/chat/completions") and "CONTENT>>>" in json.dumps(r["body"])
    )


async def test_unparseable_output_is_quarantined_after_two_attempts_and_changes_nothing(api, tenant_mem, strict_mock):
    t = tenant_mem
    ids = await story(t)
    detail = await run_event(api, t, BAD_MEMORY_JSON, subjects=[ids["sarah"]])
    st = stage(detail, "memory_propose")
    assert (st["status"], st["last_error_code"], st["attempts"]) == ("dead", "memory_extraction_unparseable", 2)
    assert detail["processing_status"] == "partial" and stage(detail, "graph_ingest")["status"] == "succeeded"
    assert await memory_rows(api, t) == []
    # the cause is fixed; an audited replay of just that stage finishes the event
    from tests.support.p4 import add_mock_rules

    await add_mock_rules(
        [
            {
                "when": {"alias": "oso/general", "schema_name": "MemoryCandidates", "contains": ["unparseable memory extraction"]},
                "response": {"candidates": []},
            }
        ]
    )
    async with running_worker(api, concurrency=2):
        r = await t.req(
            "POST", f"/v1/interactions/{detail['id']}/replay", "admin", json={"stage": "memory_propose", "reason_code": "model_fixed"}
        )
        assert r.status_code == 202, r.text

        async def done():  # type: ignore[no-untyped-def]
            d = (await t.get(f"/v1/interactions/{detail['id']}", "admin")).json()
            return d if d["processing_status"] == "indexed" else None

        fixed = await wait_until(done, timeout=120, what="the replay")
    assert stage(fixed, "memory_propose")["status"] == "succeeded" and stage(fixed, "memory_propose")["generation"] == 1


async def test_a_replay_of_the_stage_asks_the_model_again_but_never_duplicates_a_memory(api, tenant_mem, strict_mock):
    t = tenant_mem
    ids = await story(t)
    detail = await run_event(api, t, EVT003, subjects=subjects_of(ids))
    (first,) = await memory_rows(api, t)
    async with running_worker(api, concurrency=2):
        r = await t.req(
            "POST", f"/v1/interactions/{detail['id']}/replay", "admin", json={"stage": "memory_propose", "reason_code": "operator_check"}
        )
        assert r.status_code == 202, r.text

        async def done():  # type: ignore[no-untyped-def]
            d = (await t.get(f"/v1/interactions/{detail['id']}", "admin")).json()
            s = stage(d, "memory_propose")
            return d if s["generation"] == 1 and s["status"] == "succeeded" else None

        again = await wait_until(done, timeout=120, what="the replay")
    rows = await memory_rows(api, t)
    assert [m.id for m in rows] == [first.id]  # the keyed content hash recognised it (FI-11 relies on this)
    assert stage(again, "memory_propose")["result"]["statuses"] == {"active": 1}


async def test_an_older_event_gets_the_memory_stage_through_an_audited_pipeline_upgrade(api, tenant_mem, strict_mock):
    t = tenant_mem
    ids = await story(t)
    # an event accepted under p4.v1 had memory_propose skipped_disabled: make this one look like that, after graph_ingest has run
    with Spied(api):
        event_id = await ingest(t, EVT003, subjects=subjects_of(ids))
        async with running_worker(api, stages=("normalize", "graph_ingest", "graph_projection")):

            async def graph_done():  # type: ignore[no-untyped-def]
                d = (await t.get(f"/v1/interactions/{event_id}", "admin")).json()
                return stage(d, "graph_ingest")["status"] == "succeeded"

            await wait_until(graph_done, timeout=120, what="graph_ingest")
    from tests.support.p5 import owner_sql

    await owner_sql(
        "UPDATE interaction_stages SET status = 'skipped_disabled', pipeline_version = 'p4.v1', generation = 0, result = '{}'::jsonb WHERE interaction_id = :i AND stage = 'memory_propose'",
        i=uuid.UUID(event_id),
    )
    await owner_sql("UPDATE outbox_jobs SET status = 'cancelled' WHERE subject_id = :i AND stage = 'memory_propose'", i=uuid.UUID(event_id))
    await owner_sql("UPDATE interactions SET processing_status = 'indexed' WHERE id = :i", i=uuid.UUID(event_id))
    plain = await t.req("POST", f"/v1/interactions/{event_id}/replay", "admin", json={"reason_code": "upgrade"})
    assert (
        plain.status_code == 409 and plain.json()["error"]["code"] == "nothing_to_replay"
    )  # a normal replay does not touch a disabled stage
    async with running_worker(api, concurrency=2):
        r = await t.req(
            "POST", f"/v1/interactions/{event_id}/replay", "admin", json={"reason_code": "enable_memory", "upgrade_pipeline": True}
        )
        assert r.status_code == 202, r.text
        assert [x["stage"] for x in r.json()["replayed"]] == ["memory_propose"]
        detail = await wait_terminal(t, event_id)
    st = stage(detail, "memory_propose")
    pin = (
        await sql(
            api,
            t,
            "SELECT pipeline_version, ontology_version_id FROM interaction_stages WHERE interaction_id = :i AND stage = 'memory_propose'",
            i=uuid.UUID(event_id),
        )
    )[0]
    assert st["status"] == "succeeded" and pin.pipeline_version == "p5.v1" and st["result"]["kept"] == 1
    assert [m.content for m in await memory_rows(api, t)] == [BUSINESS]
    actions = {a["action"] for a in (await t.get("/v1/audit", "admin", limit=100)).json()["items"]}
    assert {"interaction.pipeline_upgraded", "interaction.replayed", "memory.extracted"} <= actions


async def test_the_stage_result_audit_logs_and_metrics_hold_ids_and_counts_never_the_memory_text(api, tenant_mem, strict_mock, capsys):
    from kg_obs.logging import configure_logging

    t = tenant_mem
    configure_logging("INFO", service="test", json_output=True)
    try:
        ids = await story(t)
        detail = await run_event(api, t, EVT003, subjects=subjects_of(ids))
    finally:
        out = capsys.readouterr().out
        configure_logging("WARNING", service="test", json_output=True)
    needles = ["software business", "entrepreneurship", "had not decided"]
    for needle in needles:
        assert needle.lower() not in out.lower(), ("log", needle)
        assert needle.lower() not in metrics.render().decode().lower(), ("metrics", needle)
    result = json.dumps(stage(detail, "memory_propose")["result"])
    audit = " ".join(r.m for r in await sql(api, t, "SELECT metadata::text AS m FROM audit_log WHERE workspace_id = :ws"))
    calls = " ".join(
        str(tuple(r))
        for r in await sql(api, t, "SELECT * FROM model_calls WHERE workspace_id = :ws AND purpose IN ('memory_extraction','embedding')")
    )
    jobs = " ".join(r.p for r in await sql(api, t, "SELECT payload::text AS p FROM outbox_jobs WHERE workspace_id = :ws"))
    for needle in needles:
        for where, blob in (("stage result", result), ("audit", audit), ("model_calls", calls), ("outbox", jobs)):
            assert needle.lower() not in blob.lower(), (where, needle)
    assert (await sql(api, t, "SELECT count(*) AS n FROM model_calls WHERE workspace_id = :ws AND purpose = 'memory_extraction'"))[0].n >= 1


async def test_extracted_memory_inherits_the_most_restrictive_classification_of_its_source(api, strict_mock):
    d = yaml.safe_load(MEMORY_YAML)
    t = await make_tenant(api, ontology=yaml.safe_dump(d))
    try:
        ids = await story(t)
        detail = await run_event(api, t, EVT003, subjects=subjects_of(ids), sensitivity="restricted")
        assert stage(detail, "memory_propose")["status"] == "succeeded"
        (m,) = await memory_rows(api, t)
        assert m.classification == "restricted"
        assert (await search(t, "agent", "starting a business"))["hits"] == []  # an agent without a classification grant cannot see it
        await t.grant("agent", "classification", "restricted")
        assert (await search(t, "agent", "starting a business"))["hits"][0]["memory_id"] == str(m.id)
    finally:
        await clear_graph(api, t.group_id)
    _ = (CYCLIST, act)
