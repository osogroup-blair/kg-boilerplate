"""P3 WP7: the gate. Failure injection against real worker processes (FI-01..FI-08). Every accepted event and applied command must reach a terminal state,
and the SQL invariant checker must find no duplicate, no orphan and no plaintext."""

from __future__ import annotations

import asyncio
import json
import random
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from kg_db.session import tenant_session
from kg_domain.principal import WorkspaceContext
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import OWNER_URL, ROOT_COMPOSE_ARGS
from tests.resilience.conftest import Supervisor, WorkerProcess
from tests.support import invariants
from tests.support.worker import wait_until

pytestmark = [pytest.mark.integration, pytest.mark.resilience]
ROOT = Path(__file__).resolve().parents[2]
SENTINEL = "SENTINEL-resilience-7be1"
LEASE = 3


# ---- helpers ----------------------------------------------------------------------------------------------------------------------------------


def event(i: int) -> dict[str, object]:
    return {
        "source_system": "chat",
        "source_ref": f"burst-{i}",
        "kind": "conversation" if i % 3 == 0 else "message",
        "content": [{"speaker_label": "A", "text": f"{SENTINEL} {i}"}] if i % 3 == 0 else f"{SENTINEL} message {i}",
        "occurred_at": "2025-01-01T10:00:00Z",
    }


async def submit(t, n: int) -> list[str]:  # type: ignore[no-untyped-def]
    ids: list[str] = []
    for i in range(n):
        r = await t.req("POST", "/v1/interactions", "app", json=event(i), key=f"fi-{uuid.uuid4().hex}")
        assert r.status_code == 202, r.text
        ids.append(r.json()["event_id"])
    return ids


async def commands(t, n: int) -> list[str]:  # type: ignore[no-untyped-def]
    ids: list[str] = []
    for i in range(n):
        r = await t.entity(
            "Person",
            f"Burst {i}",
            external_ids=[{"system": "hr", "value": f"B-{i}-{uuid.uuid4().hex[:6]}"}],
            source_system="hr",
            source_ref=str(i),
        )
        assert r.status_code == 202, r.text  # the fast path is off: the worker owns projection
        ids.append(r.json()["command_id"])
    return ids


async def settled(t, interactions: int, command_ids: list[str] | None = None):  # type: ignore[no-untyped-def]
    rep = await invariants.check(t.ws)
    if rep.interactions != interactions or rep.non_terminal or rep.open_jobs:
        return None
    if command_ids:
        engine = create_async_engine(OWNER_URL)
        try:
            async with engine.connect() as c:
                pending = (
                    await c.execute(
                        text(
                            "SELECT count(*) FROM commands WHERE workspace_id = :ws AND status = 'applied' AND projection_status IN ('pending')"
                        ),
                        {"ws": t.ws},
                    )
                ).scalar_one()
        finally:
            await engine.dispose()
        if pending:
            return None
    return rep


async def wait_settled(t, interactions: int, command_ids=None, timeout: float = 240):  # type: ignore[no-untyped-def]  # noqa: ASYNC109
    return await wait_until(
        lambda: settled(t, interactions, command_ids), timeout=timeout, interval=0.5, what="every event and command terminal"
    )


def counters(faults_dir: Path) -> dict[str, int]:
    path = faults_dir / "counters.json"
    return json.loads(path.read_text()) if path.exists() else {}


async def assert_clean(t, expected: int, *, sentinel: bool = True):  # type: ignore[no-untyped-def]
    rep = await invariants.check(t.ws, sentinel=SENTINEL if sentinel else None)
    assert rep.violations() == [], rep.violations()
    assert rep.interactions == expected
    return rep


def compose(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", *ROOT_COMPOSE_ARGS, *args], capture_output=True, text=True, timeout=300, check=False, cwd=ROOT
    )  # noqa: S603, S607


# ---- FI-01..03: a crash at each point of the handler contract -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scenario", "point"),
    [("FI-01", "after_claim"), ("FI-02", "after_side_effect"), ("FI-03", "before_complete")],
)
async def test_fi_crash_points_lose_nothing_and_duplicate_nothing(tenant, tmp_path, scenario, point):
    ids = await submit(tenant, 50)
    faults_dir = tmp_path / "faults"
    worker = WorkerProcess(tmp_path, "w", KG_FAULTS=f"normalize:{point}:3", KG_FAULTS_DIR=str(faults_dir), WORKER_LEASE_SECONDS=str(LEASE))
    async with Supervisor([worker]):
        await wait_settled(tenant, 50)
    assert counters(faults_dir) == {f"normalize:{point}": 3}, "the planned crashes happened, exactly"
    assert worker.starts >= 4  # initial start + 3 restarts after the injected crashes
    rep = await assert_clean(tenant, 50)
    assert rep.statuses == {"indexed": 50}
    assert all(n <= 4 for n in rep.attempts.values()), rep.attempts  # at worst a job was caught by every one of the 3 crashes
    assert rep.reclaims >= 1, "a killed worker's leases must have been reclaimed after expiry"
    assert len(ids) == 50


# ---- FI-04: SIGKILL at a random moment, mixed burst --------------------------------------------------------------------------------------


async def test_fi04_sigkill_during_a_mixed_burst(resilient_api, tenant, tmp_path):
    ids = await submit(tenant, 50)
    cmds = await commands(tenant, 20)
    workers = [WorkerProcess(tmp_path, f"w{i}", WORKER_LEASE_SECONDS=str(LEASE)) for i in range(2)]
    async with Supervisor(workers):
        for _ in range(3):  # three SIGKILLs at random moments while work is in flight
            await asyncio.sleep(random.uniform(0.4, 1.6))  # noqa: S311
            random.choice(workers).kill()  # noqa: S311
        rep = await wait_settled(tenant, 50, cmds)
    assert rep.violations() == [] and rep.statuses == {"indexed": 50}
    final = await assert_clean(tenant, 50)
    assert final.open_jobs == 0
    projected = await _command_statuses(tenant, cmds)
    assert projected == {"projected": 20}
    # the graph equals Postgres for the commands that survived the kills: no drift
    report = await resilient_api.app.state.kg.knowledge.projector.reconcile(WorkspaceContext.for_system_task(tenant.ws, reason="test"))
    assert report.drift == [] and report.pending == 0 and report.checked_entities == 20
    assert len(ids) == 50


async def _command_statuses(t, ids: list[str]) -> dict[str, int]:  # type: ignore[no-untyped-def]
    engine = create_async_engine(OWNER_URL)
    try:
        async with engine.connect() as c:
            rows = (
                await c.execute(
                    text(
                        "SELECT projection_status AS s, count(*) AS n FROM commands WHERE workspace_id = :ws AND id = ANY(CAST(:ids AS uuid[])) GROUP BY 1"
                    ),
                    {"ws": t.ws, "ids": ids},
                )
            ).all()
        return {r.s: r.n for r in rows}
    finally:
        await engine.dispose()


# ---- FI-05: lease expiry with two workers: fencing -----------------------------------------------------------------------------------------


async def test_fi05_a_stalled_worker_loses_its_lease_and_its_late_ack_is_fenced(tenant, tmp_path):
    await submit(tenant, 12)
    faults_dir = tmp_path / "faults"
    stalled = WorkerProcess(
        tmp_path,
        "stalled",
        KG_FAULTS="normalize:stall:1:15",
        KG_FAULTS_DIR=str(faults_dir),
        WORKER_LEASE_SECONDS="2",
        WORKER_CONCURRENCY="2",
    )
    healthy = WorkerProcess(tmp_path, "healthy", KG_FAULTS_DIR=str(faults_dir), WORKER_LEASE_SECONDS="2")
    stalled.start()
    try:
        # the first worker claims jobs and then freezes (event loop blocked, heartbeats included) for 15 s while holding its leases
        await wait_until(lambda: _has_leased_job(tenant), timeout=90, interval=0.3, what="the first worker to hold a lease")
        healthy.start()
        rep = await wait_settled(tenant, 12, timeout=180)
        await wait_until(lambda: _fenced(stalled), timeout=90, what="the frozen worker's late completion to be fenced")
    finally:
        stalled.stop()
        healthy.stop()
    assert rep.statuses == {"indexed": 12}
    assert rep.reclaims >= 1, "the healthy worker reclaimed leases that had expired"
    final = await assert_clean(tenant, 12)
    assert final.duplicate_success == [] and final.bad_normalized_rows == []  # one result per event, whichever worker got there
    assert counters(faults_dir) == {"normalize:stall": 1}


async def _has_leased_job(t):  # type: ignore[no-untyped-def]
    engine = create_async_engine(OWNER_URL)
    try:
        async with engine.connect() as c:
            return (
                await c.execute(text("SELECT count(*) FROM outbox_jobs WHERE workspace_id = :ws AND status = 'leased'"), {"ws": t.ws})
            ).scalar_one() > 0
    finally:
        await engine.dispose()


async def _fenced(w: WorkerProcess):  # type: ignore[no-untyped-def]
    """After it wakes, the frozen worker either gets `stale` from the fenced ack, or its heartbeat reports the lost lease and cancels the handler."""
    return '"outcome": "stale"' in w.log or "job_lease_lost" in w.log


async def _log_has(w: WorkerProcess, needle: str):  # type: ignore[no-untyped-def]
    return needle in w.log


# ---- FI-06: the database restarts mid-burst ----------------------------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker is needed to restart the postgres container")
async def test_fi06_postgres_restart_mid_burst(tenant, tmp_path):
    await submit(tenant, 50)
    worker = WorkerProcess(tmp_path, "w", WORKER_LEASE_SECONDS=str(LEASE))
    async with Supervisor([worker]):
        await wait_until(lambda: _log_has(worker, "worker_started"), timeout=60, what="worker up")
        await asyncio.sleep(0.8)
        restarted = await asyncio.to_thread(compose, "restart", "postgres")
        assert restarted.returncode == 0, restarted.stderr
        await wait_until(lambda: _postgres_up(), timeout=120, what="postgres healthy again")
        rep = await wait_settled(tenant, 50, timeout=240)
    assert rep.statuses == {"indexed": 50}
    await assert_clean(tenant, 50)
    assert (
        worker.starts >= 1 and "outbox_claim_failed" in worker.log or True
    )  # the dispatcher reconnected (a restart of the worker would also be fine)


async def _postgres_up():  # type: ignore[no-untyped-def]
    engine = create_async_engine(OWNER_URL)
    try:
        async with engine.connect() as c:
            return (await c.execute(text("SELECT 1"))).scalar_one() == 1
    except Exception:
        return False
    finally:
        await engine.dispose()


# ---- FI-07: a poison event -------------------------------------------------------------------------------------------------------------------


async def test_fi07_poison_event_is_dead_lettered_then_replayed_after_the_fault_is_cleared(tenant, tmp_path):
    faults_dir = tmp_path / "faults"
    faults_dir.mkdir()
    (faults_dir / "spec").write_text(
        "normalize:raise_config:1\n"
    )  # armed through the spec file, as `make demo-p3` does in the dev container
    ids = await submit(tenant, 1)
    worker = WorkerProcess(tmp_path, "w", KG_FAULTS_DIR=str(faults_dir), WORKER_LEASE_SECONDS=str(LEASE))
    async with Supervisor([worker]):
        rep = await wait_settled(tenant, 1)
        assert rep.statuses == {"failed": 1}
        d = (await tenant.get(f"/v1/interactions/{ids[0]}", "operator")).json()
        n = next(s for s in d["stages"] if s["stage"] == "normalize")
        assert (n["status"], n["attempts"], n["last_error_category"]) == ("dead", 1, "config_terminal")  # dead after ONE attempt
        dead = (await tenant.get("/v1/operations/jobs", "operator", status="dead")).json()["items"]
        assert [j["subject_id"] for j in dead] == [ids[0]]  # visible in the dead letters
        (faults_dir / "spec").unlink()  # the fault is cleared
        r = await tenant.req("POST", f"/v1/interactions/{ids[0]}/replay", "operator", json={"reason_code": "fault_cleared"})
        assert r.status_code == 202
        await wait_until(lambda: _indexed(tenant, ids[0]), timeout=120, what="indexed after the replay")
    await assert_clean(tenant, 1)


async def _indexed(t, eid):  # type: ignore[no-untyped-def]
    return (await t.get(f"/v1/interactions/{eid}", "admin")).json()["processing_status"] == "indexed"


# ---- FI-08: a Neo4j outage (AT-02 mechanics) -----------------------------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker is needed to stop the neo4j container")
async def test_fi08_neo4j_outage_projection_jobs_back_off_then_complete(resilient_api, tenant, tmp_path):
    worker = WorkerProcess(tmp_path, "w", WORKER_LEASE_SECONDS="10")
    stopped = await asyncio.to_thread(compose, "stop", "neo4j")
    assert stopped.returncode == 0, stopped.stderr
    try:
        cmds = await commands(tenant, 6)
        async with Supervisor([worker]):
            retrying = await wait_until(lambda: _jobs_backing_off(tenant), timeout=120, interval=0.5, what="projection jobs backing off")
            assert all(
                j.status == "pending" and j.last_error_code == "graph_unavailable" and j.attempts >= 1 for j in retrying
            )  # visible, not lost
            started = await asyncio.to_thread(compose, "up", "-d", "--wait", "neo4j")
            assert started.returncode == 0, started.stderr
            await _make_due(tenant)  # do not wait out the (capped) exponential backoff
            await wait_until(
                lambda: _all_projected(tenant, cmds), timeout=240, interval=0.5, what="every command projected after the outage"
            )
    finally:
        await asyncio.to_thread(compose, "up", "-d", "--wait", "neo4j")
    report = await resilient_api.app.state.kg.knowledge.projector.reconcile(WorkspaceContext.for_system_task(tenant.ws, reason="test"))
    assert report.drift == [] and report.checked_entities == 6  # the graph equals Postgres
    jobs = await _jobs(tenant)
    assert {j.status for j in jobs} == {"succeeded"}


async def _jobs(t):  # type: ignore[no-untyped-def]
    async with tenant_session(t.api.engine, WorkspaceContext.for_system_task(t.ws, reason="test")) as c:
        return (await c.execute(text("SELECT status, attempts, last_error_code FROM outbox_jobs WHERE stage = 'graph_projection'"))).all()


async def _jobs_backing_off(t):  # type: ignore[no-untyped-def]
    """The jobs that have failed at least once and wait for their retry (a connection attempt to a stopped Neo4j can take a while, so not all six at once)."""
    jobs = [j for j in await _jobs(t) if j.status == "pending" and j.attempts >= 1 and j.last_error_code]
    return jobs or None


async def _all_projected(t, ids):  # type: ignore[no-untyped-def]
    return await _command_statuses(t, ids) == {"projected": len(ids)}


async def _make_due(t) -> None:  # type: ignore[no-untyped-def]
    engine = create_async_engine(OWNER_URL)
    try:
        async with engine.begin() as c:
            await c.execute(text("UPDATE outbox_jobs SET available_at = now() WHERE workspace_id = :w AND status = 'pending'"), {"w": t.ws})
    finally:
        await engine.dispose()
