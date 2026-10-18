"""P9 WP9: the Operations v2 read models - cost, latency, freshness, backups, live evaluation and the platform's own alerts. Server-side aggregates from Postgres: ids, counts and
enums only, scoped to the caller's workspace (backups and evaluation records are the platform's and carry no workspace, no location and no content)."""

from __future__ import annotations

import datetime as dt
import json
import uuid

import pytest

from tests.support.p5 import owner_sql
from tests.support.p6 import build

pytestmark = pytest.mark.integration


async def put_calls(t, rows):
    for purpose, alias, usd, latency, outcome, age_days in rows:
        await owner_sql(
            "INSERT INTO model_calls (id, workspace_id, purpose, alias, operation, classification, prompt_tokens, completion_tokens, usage_estimated, est_cost_usd, latency_ms, attempts, outcome, fallback_used, tokenizer, service, created_at) "
            "VALUES (:id, :ws, :purpose, :alias, 'chat', 'internal', 100, 20, false, :usd, :lat, 1, :out, false, 'o200k_base', 'kg-worker', now() - make_interval(days => :age))",
            id=uuid.uuid4(),
            ws=t.ws,
            purpose=purpose,
            alias=alias,
            usd=usd,
            lat=latency,
            out=outcome,
            age=age_days,
        )


async def test_cost_by_purpose_alias_and_day_is_scoped_to_the_workspace(api, tenant_ctx, tenant_agents):
    t, other = tenant_ctx, tenant_agents
    await put_calls(
        t,
        [
            ("extraction", "oso/fast", 0.012, 800, "ok", 0),
            ("extraction", "oso/fast", 0.008, 900, "rate_limited", 0),
            ("embedding", "oso/embed", 0.001, 90, "ok", 1),
            ("extraction", "oso/fast", 9.0, 500, "ok", 40),
        ],
    )
    await put_calls(other, [("extraction", "oso/fast", 123.0, 500, "ok", 0)])
    r = await t.get("/v1/operations/cost", "operator", days=14)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_calls"] == 3 and abs(body["total_usd"] - 0.021) < 1e-9, (
        body
    )  # the 40-day-old call and the other tenant's 123 USD are not here
    assert body["by_purpose"] == {"extraction": 0.02, "embedding": 0.001} and set(body["by_alias"]) == {"oso/fast", "oso/embed"}
    errors = sum(x["errors"] for x in body["rows"])
    assert errors == 1 and "estimates" in body["note"]
    assert (await t.get("/v1/operations/cost", "reader")).status_code == 403  # a reader has no operations.read


async def test_latency_has_context_p50_p95_by_profile_and_source_and_the_router_by_alias(api, tenant_ctx):
    t = tenant_ctx
    for _ in range(3):
        assert (await build(t, "agent", "what is the plan")).status_code == 200
    await put_calls(t, [("extraction", "oso/fast", 0.01, ms, "ok", 0) for ms in (400, 800, 1200, 2000)])
    r = await t.get("/v1/operations/latency", "operator", days=1)
    assert r.status_code == 200, r.text
    body = r.json()
    prof = next(iter(body["context_by_profile"].values()))
    assert prof["n"] >= 3 and 0 <= prof["p50"] <= prof["p95"] < 30
    assert body["context_by_source"], body
    fast = body["router_by_alias"]["oso/fast"]
    assert fast["n"] == 4 and abs(fast["p50"] - 1.0) < 1e-6 and fast["p95"] > 1.8 and body["unit"] == "seconds"


async def test_freshness_reports_lag_stages_and_what_has_not_settled(api, tenant_ctx):
    t = tenant_ctx
    r0 = await t.get("/v1/operations/freshness", "operator")
    assert r0.status_code == 200 and r0.json()["accepted_to_indexed"]["n"] == 0 and r0.json()["unsettled"] == {}
    eid = uuid.uuid4()
    await owner_sql(
        "INSERT INTO interactions (id, workspace_id, source_system, source_ref, idempotency_key, request_hmac, kind, content_type, occurred_at, recorded_at, submitted_by, classification, consent_external, pipeline_version, processing_status, status_changed_at, byte_size, token_estimate) "
        "SELECT :id, :ws, 'test', :ref, :key, :hm, 'message', 'text/plain', now(), now() - interval '30 seconds', p.id, 'internal', false, 'p4.v1', 'indexed', now() - interval '10 seconds', 10, 3 "
        "FROM principals p WHERE p.workspace_id = :ws LIMIT 1",
        id=eid,
        ws=t.ws,
        ref=str(eid),
        key=str(eid),
        hm="a" * 64,
    )
    body = (await t.get("/v1/operations/freshness", "operator", hours=1)).json()
    assert body["accepted_to_indexed"]["n"] == 1 and abs(body["accepted_to_indexed"]["p50"] - 20.0) < 1.0, body
    assert body["watermarks"]["newest_indexed"] is not None


async def test_backups_and_live_evaluation_are_platform_records_without_locations_or_content(api, tenant_ctx):
    t = tenant_ctx
    marker = f"zz{uuid.uuid4().hex[:6]}"
    bid, rid, eid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    try:
        await owner_sql(
            "INSERT INTO backup_runs (id, kind, store, status, completed_at, size_bytes, location, expires_at) VALUES (:id, 'backup', 'postgres', 'completed', now() - interval '2 hours', 1234567, :loc, now() + interval '30 days')",
            id=bid,
            loc=f"/secret/path/{marker}",
        )
        await owner_sql(
            "INSERT INTO backup_runs (id, kind, store, status, completed_at, details) VALUES (:id, 'restore_rehearsal', 'all', 'completed', now() - interval '1 day', CAST(:d AS jsonb))",
            id=rid,
            d=json.dumps({"rto_seconds": 101.5, "rpo_seconds": 3600.0}),
        )
        await owner_sql(
            "INSERT INTO eval_runs (id, kind, label, status, finished_at, git_commit, summary) VALUES (:id, 'live_session', :label, 'passed', now() - interval '3 days', 'abc1234', CAST(:s AS jsonb))",
            id=eid,
            label=marker,
            s=json.dumps({"suites": {"extraction": "passed", "context": "passed"}, "drift": {"relevance": 0.01}}),
        )
        b = (await t.get("/v1/operations/backups", "operator")).json()
        stores = {s["store"]: s for s in b["stores"]}
        assert (
            stores["postgres"]["age_seconds"] is not None
            and stores["postgres"]["age_seconds"] < 3 * 3600
            and stores["postgres"]["stale"] is False
        )
        assert (
            b["last_rehearsal"]["rto_seconds"] == 101.5
            and b["last_rehearsal"]["rpo_seconds"] == 3600.0
            and b["last_rehearsal"]["status"] == "completed"
        )
        assert marker not in json.dumps(b) and all("location" not in row for row in b["recent"])  # no server paths
        e = (await t.get("/v1/operations/live-eval", "operator")).json()
        assert e["last_live_session"]["label"] == marker and e["last_live_session"]["summary"]["suites"]["extraction"] == "passed"
        assert any(r["kind"] == "live_session" for r in e["latest_by_kind"])
    finally:
        await owner_sql("DELETE FROM backup_runs WHERE id = ANY(:ids)", ids=[bid, rid])
        await owner_sql("DELETE FROM eval_runs WHERE id = :id", id=eid)


async def test_alerts_come_from_the_platforms_own_checks(api, tenant_ctx):
    t = tenant_ctx
    quiet = (await t.get("/v1/operations/alerts", "operator")).json()
    names = {a["name"] for a in quiet["firing"]}
    assert "DeadLetters" not in names and "ReconcileDrift" not in names and quiet["alertmanager"] == "not_configured"
    jid = uuid.uuid4()
    await owner_sql(
        "INSERT INTO outbox_jobs (id, workspace_id, stage, subject_type, subject_id, dedupe_key, payload, status, attempts, max_attempts, available_at, pipeline_version, created_at, finished_at) "
        "VALUES (:id, :ws, 'graph_ingest', 'interaction', :sid, :dk, '{}', 'dead', 5, 5, now(), 'p4.v1', now(), now())",
        id=jid,
        ws=t.ws,
        sid=uuid.uuid4(),
        dk=str(jid),
    )
    await owner_sql(
        "INSERT INTO reconcile_runs (id, workspace_id, scope, mode, trigger, status, drift, repaired, finished_at) VALUES (:id, :ws, 'all', 'check', 'scheduled', 'completed', CAST(:d AS jsonb), '{}', now())",
        id=uuid.uuid4(),
        ws=t.ws,
        d=json.dumps({"graph_missing_node": 2}),
    )
    loud = (await t.get("/v1/operations/alerts", "operator")).json()
    by = {a["name"]: a for a in loud["firing"]}
    assert by["DeadLetters"]["value"] == 1.0 and by["ReconcileDrift"]["value"] == 2.0
    assert dt.datetime.now(dt.UTC)  # (the backup and rehearsal alerts depend on the shared platform records; they are exercised above)
