"""P9 gate: FI-15 … FI-23, the part of the failure matrix that the earlier phases left open (the model router, the embedding alias, latency, a Postgres restart inside a
deletion, a Neo4j restart or a crash inside a reindex, a full graph disk, a killed API replica, clock skew, a router that answers nonsense).

Where the real failure cannot be produced on a development machine the test says so: FI-20 injects the error a full disk makes Neo4j raise (a `DatabaseError` on write) rather
than filling a volume, and FI-22 skews the HOST clock of the test process (the database keeps its own); both prove the platform's handling, not the operating system's."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import time
import uuid
from pathlib import Path

import httpx
import pytest
from kg_db.session import tenant_session

from tests.resilience.conftest import Supervisor, WorkerProcess
from tests.resilience.test_failure_injection import LEASE, _make_due, _postgres_up, assert_clean, compose, counters
from tests.support import invariants
from tests.support.p4 import EVT003, MOCK, ctx_of, ingest, sarah_story, sql, stage, strict_mock, wait_terminal  # noqa: F401
from tests.support.p8 import deletion_world, raw, request_deletion, request_status
from tests.support.p9 import replicas
from tests.support.worker import running_worker, wait_until

pytestmark = [pytest.mark.integration, pytest.mark.resilience]


async def set_faults(faults: list[dict]) -> None:
    async with httpx.AsyncClient() as c:
        await c.delete(f"{MOCK}/__mock/requests")  # also clears the queue
        if faults:
            assert (await c.post(f"{MOCK}/__mock/faults", json={"faults": faults})).status_code == 200


@pytest.fixture(autouse=True)
async def _clean_faults():
    yield
    await set_faults([])


# ---- FI-15: the router is down for everything, during a mixed burst ----------------------------------------------------------------------------------


async def test_fi15_router_down_during_a_mixed_burst_degrades_visibly_and_completes_after_recovery(api, tenant_mem, strict_mock):  # noqa: F811
    t = tenant_mem
    ids = await sarah_story(t)
    await set_faults([{"code": "provider_unavailable", "count": 100000, "endpoint": "any"}])
    async with running_worker(api, concurrency=2):
        event = await ingest(t, EVT003, subjects=[ids["sarah"], ids["michael"], ids["topic"]])  # accepting an event needs no model
        saved = await t.req(
            "POST",
            "/v1/memories",
            "admin",
            json={"type": "semantic", "content": "Sarah prefers side by side comparisons.", "purpose": "history", "source_ref": "fi15"},
        )
        assert saved.status_code == 201, saved.text  # a memory is accepted; its vector waits (FI-16 looks at that)

        async def backing_off() -> bool:
            d = (await t.get(f"/v1/interactions/{event}", "admin")).json()
            gi = next(s for s in d["stages"] if s["stage"] == "graph_ingest")
            return gi["attempts"] >= 1 and gi["status"] in ("pending", "running", "failed")

        await wait_until(backing_off, timeout=120.0, interval=0.5, what="model stages visibly backing off")
        d = (await t.get(f"/v1/interactions/{event}", "admin")).json()
        assert (
            d["processing_status"] not in ("indexed",)
            and next(s for s in d["stages"] if s["stage"] == "normalize")["status"] == "succeeded"
        )  # nothing is lost, nothing pretends
        # a context build in the middle of the outage: partial, with the missing sources named; or refused when the caller will not take a partial package
        partial = await t.req("POST", "/v1/context/build", "agent", json={"task": "What does Sarah prefer?"}, key="")
        assert partial.status_code == 200 and partial.json()["status"] in ("partial", "complete", "insufficient_evidence"), partial.text
        strict = await t.req("POST", "/v1/context/build", "agent", json={"task": "What does Sarah prefer?", "allow_partial": False}, key="")
        assert strict.status_code in (200, 503), strict.text
        if partial.json()["status"] == "partial":
            assert strict.status_code == 503 and strict.json()["error"]["code"] == "context_incomplete"
        # recovery: the router answers again; the work that waited completes
        await set_faults([])
        await _make_due(t)
        detail = await wait_terminal(t, event, timeout=240.0)

        async def quiet() -> bool:
            return (
                await sql(
                    api,
                    t,
                    "SELECT count(*) AS n FROM outbox_jobs WHERE workspace_id = :ws AND status IN ('pending','leased') AND available_at <= now()",
                )
            )[0].n == 0

        await _make_due(t)
        await wait_until(quiet, timeout=240.0, interval=0.5, what="the outbox to drain after the router came back")
    assert stage(detail, "graph_ingest")["status"] == "succeeded"
    rep = await invariants.check(t.ws)
    assert rep.violations() == []


# ---- FI-16: the embedding alias is down, chat is up -----------------------------------------------------------------------------------------------


async def test_fi16_embeddings_down_memories_stay_pending_search_says_lexical_and_it_recovers(api, tenant_mem):
    t = tenant_mem
    await set_faults([{"code": "provider_unavailable", "count": 100000, "endpoint": "embeddings"}])
    async with running_worker(api, stages=("memory_embed",), concurrency=2):
        r = await t.req(
            "POST",
            "/v1/memories",
            "admin",
            json={
                "type": "semantic",
                "content": "Michael keeps encouraging Sarah to start something of her own.",
                "purpose": "history",
                "source_ref": "fi16",
            },
        )
        assert r.status_code == 201, r.text
        mid = r.json()["memory_id"]

        async def retrying() -> bool:
            rows = await sql(
                api,
                t,
                "SELECT attempts, status FROM outbox_jobs WHERE workspace_id = :ws AND stage = 'memory_embed' ORDER BY created_at DESC LIMIT 1",
            )
            return bool(rows and rows[0].attempts >= 1)

        await wait_until(retrying, timeout=60.0, interval=0.3, what="the embed job to be retried")
        row = (await sql(api, t, "SELECT embedding_status FROM curated_memories WHERE workspace_id = :ws AND id = :m", m=uuid.UUID(mid)))[0]
        assert row.embedding_status == "pending"  # visible, not lost
        found = await t.req("POST", "/v1/memories/search", "admin", json={"query": "Michael encouraging Sarah"})
        assert found.status_code == 200 and mid in [h["memory_id"] for h in found.json()["hits"]]  # lexical still finds it
        assert found.json()["degraded"] == ["vector_unavailable"]  # and says that it is lexical only
        await set_faults([])
        await _make_due(t)

        async def embedded() -> bool:
            return (
                await sql(api, t, "SELECT embedding_status FROM curated_memories WHERE workspace_id = :ws AND id = :m", m=uuid.UUID(mid))
            )[0].embedding_status == "ready"

        await wait_until(embedded, timeout=120.0, interval=0.5, what="the memory to be embedded after recovery")
    again = await t.req("POST", "/v1/memories/search", "admin", json={"query": "Michael encouraging Sarah"})
    assert again.json()["degraded"] == []


# ---- FI-17: the router is slow (10x), timeouts hold -----------------------------------------------------------------------------------------------


async def test_fi17_a_slow_router_does_not_hold_the_context_build_or_exhaust_connections(api, tenant_mem):
    t = tenant_mem
    await sarah_story(t)
    await t.req(
        "POST",
        "/v1/memories",
        "admin",
        json={"type": "semantic", "content": "Sarah prefers side by side comparisons.", "purpose": "history", "source_ref": "fi17"},
    )
    await set_faults([{"code": "slow", "count": 100000, "endpoint": "any", "delay_ms": 6000}])
    timeout_ms = api.app.state.kg.settings.context_source_timeout_ms

    async def one() -> tuple[int, float, str | None]:
        started = time.monotonic()
        r = await t.req("POST", "/v1/context/build", "agent", json={"task": "What does Sarah prefer?"}, key="")
        return r.status_code, time.monotonic() - started, r.json().get("status")

    results = await asyncio.gather(*[one() for _ in range(8)])
    envelope = timeout_ms / 1000.0 * 3 + 2.0
    ok = [r for r in results if r[0] == 200]
    refused = [r for r in results if r[0] == 429]
    assert len(ok) + len(refused) == len(results) and len(ok) >= 6, (
        results
    )  # the workspace's concurrency cap answers the surplus at once (429), the rest build
    assert all(elapsed < envelope for _, elapsed, _ in results), (
        results,
        envelope,
    )  # per-source timeouts hold, whatever the router does; a refusal is immediate
    assert {s for _, _, s in ok} <= {"partial", "complete"}
    # nothing is left hanging: connections are back, and a normal request is prompt once the router is fast again
    await set_faults([])
    started = time.monotonic()
    assert (await t.get("/v1/me", "admin")).status_code == 200 and time.monotonic() - started < 3.0
    busy = (
        await sql(
            api, t, "SELECT count(*) AS n FROM pg_stat_activity WHERE usename = 'kg_app' AND state = 'active' AND pid <> pg_backend_pid()"
        )
    )[0].n
    assert busy < 10, f"{busy} connections still active after the slow burst"


# ---- FI-18: Postgres restarts while a deletion is being purged ------------------------------------------------------------------------------------


async def test_fi18_a_postgres_restart_inside_a_deletion_resumes_without_purging_twice(api, tenant_governance, tmp_path: Path):
    t = tenant_governance
    w = await deletion_world(api, t)
    r = await request_deletion(t, "subject", w["zeb"], "app")
    assert r.status_code == 202, r.text
    rid = r.json()["request_id"]
    worker = WorkerProcess(tmp_path, "w", WORKER_LEASE_SECONDS=str(LEASE))
    async with Supervisor([worker]):
        await asyncio.sleep(0.3)
        restarted = await asyncio.to_thread(compose, "restart", "postgres")
        assert restarted.returncode == 0, restarted.stderr
        await wait_until(lambda: _postgres_up(), timeout=120, what="postgres healthy again")

        async def done():
            try:
                s = await request_status(api, t, rid)
            except Exception:
                return None  # the pool is reconnecting
            return s if s["status"] in ("completed", "completed_with_exceptions", "failed") else None

        final = await wait_until(done, timeout=240.0, interval=0.5, what="the deletion to finish after the restart")
    assert final["status"] in ("completed", "completed_with_exceptions"), final
    assert (await sql(api, t, "SELECT count(*) AS n FROM entities WHERE workspace_id = :ws AND id = :e", e=uuid.UUID(w["zeb"])))[0].n == 0
    journal = api.app.state.kg.deletion.journal
    assert sum(1 for line in journal._lines() if line["request_id"] == rid) == 1  # one journal line: nothing was completed twice
    tombs = await sql(
        api, t, "SELECT count(*) AS n FROM deletion_tombstones WHERE workspace_id = :ws AND request_id = :r", r=uuid.UUID(rid)
    )
    assert tombs[0].n >= 4
    journal.verify()


# ---- FI-19: Neo4j restarts, and a worker crashes, inside a reindex build ------------------------------------------------------------------------------


async def test_fi19_a_crash_and_a_neo4j_restart_inside_a_reindex_leave_the_live_namespace_alone_and_the_build_resumes(
    api, tenant_governance, tmp_path: Path
):
    t = tenant_governance
    ctx = ctx_of(t)
    for i in range(40):
        r = await t.entity("Topic", f"Topic {i}", source_system="hr", source_ref=f"fi19-{i}")
        assert r.status_code in (200, 201), r.text
    vid = str((await sql(api, t, "SELECT active_version_id FROM workspace_ontology WHERE workspace_id = :ws"))[0].active_version_id)
    graph = api.app.state.kg.graph
    live_before = (await graph.reindexer(ctx).counts(t.group_id)).nodes
    started = await t.req("POST", f"/v1/ontologies/versions/{vid}/reindex", "admin", json={"confirm": True})
    assert started.status_code == 202, started.text
    run = started.json()
    faults_dir = tmp_path / "faults"
    worker = WorkerProcess(
        tmp_path, "w", KG_FAULTS="graph_reindex:after_side_effect:1", KG_FAULTS_DIR=str(faults_dir), WORKER_LEASE_SECONDS=str(LEASE)
    )
    async with Supervisor([worker]):

        async def writing() -> bool:
            return (await t.get(f"/v1/reindex-runs/{run['id']}", "admin")).json()["progress"].get("entities_done", 0) > 0

        await wait_until(writing, timeout=120, interval=0.2, what="the build to start writing")
        restarted = await asyncio.to_thread(compose, "restart", "neo4j")
        assert restarted.returncode == 0, restarted.stderr

        async def finished():
            s = (await t.get(f"/v1/reindex-runs/{run['id']}", "admin")).json()
            return s if s["status"] in ("ready", "failed") else None

        final = await wait_until(finished, timeout=420.0, interval=1.0, what="the build to finish after a crash and a Neo4j restart")
    assert final["status"] == "ready", final
    assert final["report"]["ok"] and final["report"]["built"]["nodes"] == final["report"]["active"]["nodes"]
    assert (
        counters(faults_dir).get("graph_reindex:after_side_effect") == 1
    )  # the planned crash happened, once; the resumed attempt had little left to write
    assert (
        graph.namespaces.active(ctx) == t.group_id and (await graph.reindexer(ctx).counts(t.group_id)).nodes == live_before
    )  # the live namespace was never touched
    assert (await t.get("/v1/entities", "admin", q="Topic 7")).status_code == 200
    await t.req("POST", f"/v1/reindex-runs/{run['id']}/cancel", "admin")


# ---- FI-20: the graph disk is full -----------------------------------------------------------------------------------------------------------------


async def test_fi20_a_full_graph_disk_fails_graph_stages_retryably_and_everything_recovers(api, tenant_governance, monkeypatch):
    from kg_graph.scoped import ScopedGraph
    from neo4j.exceptions import DatabaseError

    t = tenant_governance
    real = ScopedGraph.upsert_entity_node
    broken = {"on": True}

    async def full_disk(self, **kw):
        if broken["on"]:
            raise DatabaseError(
                "No space left on device"
            )  # what Neo4j raises when it cannot write (the real volume is not filled by this test)
        return await real(self, **kw)

    monkeypatch.setattr(ScopedGraph, "upsert_entity_node", full_disk)
    projector = api.app.state.kg.knowledge.projector
    created = await t.req(
        "POST",
        "/v1/entities",
        "app",
        json={"type": "Topic", "name": "Disk full", "source_system": "hr", "source_ref": "fi20"},
        key=f"fi20-{uuid.uuid4()}",
    )
    assert created.status_code in (201, 202), created.text
    eid = created.json()["entity_id"]
    status = await projector.project_entity(ctx_of(t), uuid.UUID(eid))
    row = (
        await sql(
            api,
            t,
            "SELECT projection_status, last_error_code, projection_attempts FROM entities WHERE workspace_id = :ws AND id = :e",
            e=uuid.UUID(eid),
        )
    )[0]
    assert (
        status in ("pending", "failed") and row.last_error_code == "graph_unavailable" and row.projection_attempts >= 1
    )  # named, retryable, counted
    broken["on"] = False
    assert await projector.project_entity(ctx_of(t), uuid.UUID(eid)) == "projected"
    assert (await projector.reconcile(ctx_of(t))).drift == []


# ---- FI-21: an API replica dies mid-request -----------------------------------------------------------------------------------------------------------


async def test_fi21_a_killed_replica_and_an_idempotent_retry_create_exactly_one_entity(api, tenant):
    t = tenant
    async with replicas(api, 2) as (a, b):
        for i, delay in enumerate((0.0, 0.002, 0.01, 0.03, 0.08, 0.2)):
            key = f"fi21-{uuid.uuid4()}"
            name = f"Killed {i} {uuid.uuid4().hex[:6]}"
            body = {"type": "Topic", "name": name, "source_system": "hr", "source_ref": f"k{i}"}
            headers = {**t.h("app"), "idempotency-key": key}
            doomed = asyncio.create_task(a.client.post("/v1/entities", headers=headers, json=body))
            await asyncio.sleep(delay)
            doomed.cancel()  # the replica "dies": its request is torn down wherever it was
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await doomed
            retried = await b.client.post(
                "/v1/entities", headers=headers, json=body
            )  # the client retries on the other replica with the same key
            assert retried.status_code in (200, 201, 202), retried.text
            again = await a.client.post("/v1/entities", headers=headers, json=body)
            assert again.json()["entity_id"] == retried.json()["entity_id"]
            n = (await sql(api, t, "SELECT count(*) AS n FROM entities WHERE workspace_id = :ws AND display_name = :n", n=name))[0].n
            assert n == 1, f"{n} entities named {name!r} after a kill at {delay}s"
            cmds = (await sql(api, t, "SELECT count(*) AS n FROM commands WHERE workspace_id = :ws AND idempotency_key = :k", k=key))[0].n
            assert cmds == 1


# ---- FI-22: clock skew -----------------------------------------------------------------------------------------------------------------------------


async def test_fi22_a_skewed_host_clock_changes_no_lease_no_expiry_and_no_quota_window(api, tenant, monkeypatch):
    from kg_governance.limiter import SharedLimiter

    t = tenant
    real_time, real_mono = time.time, time.monotonic
    key = f"skew:{uuid.uuid4()}"
    lim = SharedLimiter(api.engine)
    first = await lim.hit(key, window_seconds=60, limit=1)
    # an API key that expires in five minutes by the DATABASE clock
    await raw(
        api,
        t,
        "UPDATE api_keys SET expires_at = now() + interval '5 minutes' WHERE workspace_id = :ws AND principal_id = (SELECT id FROM principals WHERE workspace_id = :ws AND handle = :h)",
        h=f"app@{t.slug}",
    )
    monkeypatch.setattr(time, "time", lambda: real_time() + 600)  # the host clock runs ten minutes fast
    monkeypatch.setattr(time, "monotonic", lambda: real_mono() + 600)
    second = await lim.hit(key, window_seconds=60, limit=1)
    assert (
        second.hits == first.hits + 1 and not second.admitted and 1 <= second.retry_after <= 60
    )  # the window and the retry-after are the database's
    # a lease is judged by the database too: claim a job under the skewed clock and look at what the database recorded
    from kg_jobs.client import OpsClient
    from kg_jobs.outbox import enqueue

    async with tenant_session(api.engine, ctx_of(t)) as conn:
        await enqueue(
            conn,
            workspace_id=t.ws,
            stage="graph_projection",
            subject_type="command",
            subject_id=uuid.uuid4(),
            dedupe_key=f"skew:{uuid.uuid4()}",
            payload={"command_id": str(uuid.uuid4()), "generation": 0},
            pipeline_version="p4.v1",
        )
    claimed = await OpsClient(api.engine).claim(f"skew-{uuid.uuid4().hex[:6]}", ["graph_projection"], 50, 30)
    mine = [j for j in claimed if j.workspace_id == t.ws]
    assert mine, "the test's own job was claimed"
    for job in mine:
        lease = (await sql(api, t, "SELECT lease_expires_at - now() AS left FROM outbox_jobs WHERE id = :j", j=job.job_id))[0].left
        assert dt.timedelta(seconds=20) < lease <= dt.timedelta(seconds=31)  # 30 s from the DATABASE's now, not 10 minutes from the host's
    # the keys the platform issued earlier still authenticate: expiry is `key_expired` from the database, never the host's comparison
    assert (await t.get("/v1/me", "app")).status_code == 200


# ---- FI-23: the router answers nonsense or an error code the platform does not know -----------------------------------------------------------------


@pytest.mark.parametrize("fault", [{"code": "malformed", "endpoint": "chat"}, {"code": "an_error_code_nobody_mapped", "endpoint": "chat"}])
async def test_fi23_a_router_that_answers_nonsense_is_never_retried_for_ever(api, tenant_mem, fault):
    t = tenant_mem
    ids = await sarah_story(t)
    await set_faults([{**fault, "count": 100000}])
    event = await ingest(t, EVT003, subjects=[ids["sarah"], ids["michael"], ids["topic"]])
    async with running_worker(
        api, stages=("normalize", "graph_ingest", "graph_projection", "memory_propose", "memory_embed"), concurrency=2
    ):

        async def terminal():
            await _make_due(t)
            d = (await t.get(f"/v1/interactions/{event}", "admin")).json()
            return d if d["processing_status"] in ("indexed", "partial", "failed") else None

        detail = await wait_until(terminal, timeout=240.0, interval=1.0, what="the stage to stop retrying")
    gi = stage(detail, "graph_ingest")
    assert gi["status"] in ("dead", "failed", "invalid") and gi["attempts"] <= 5, gi  # terminal after a bounded number of attempts
    jobs = await sql(api, t, "SELECT status, attempts, max_attempts FROM outbox_jobs WHERE workspace_id = :ws AND stage = 'graph_ingest'")
    assert jobs and all(j.attempts <= j.max_attempts for j in jobs)
    dead = (await t.get("/v1/operations/jobs", "operator", status="dead")).json()["items"]
    assert any(j["subject_id"] == event for j in dead), "a dead letter the operator can see and replay"
    await assert_clean(t, 1, sentinel=False)
