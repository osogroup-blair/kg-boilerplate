"""WP2: migration 0003 and the ops.* outbox functions. Claiming, leases, fencing, concurrency keys, RLS, grants, immutability, backfill."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from kg_db import migrate
from kg_db.session import system_session, tenant_session
from kg_jobs.client import OpsClient
from kg_jobs.outbox import enqueue
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError

from tests.conftest import OWNER_URL, SCRATCH_URL, require
from tests.integration.test_db import ctx, make_workspace

pytestmark = pytest.mark.integration
PV = "p3.v1"


@pytest.fixture()
def stage() -> str:
    """A stage name unique to the test (letters only, per the table constraint): jobs of other tests in the shared database are never claimed."""
    import random
    import string

    return "t" + "".join(random.choices(string.ascii_lowercase, k=14))


P3_TABLES = ["interactions", "interaction_contents", "interaction_subjects", "interaction_stages", "outbox_jobs", "job_attempts"]


async def add_jobs(engine, ws: uuid.UUID, n: int, *, stage: str, key: str | None = None, prefix: str = "j") -> list[uuid.UUID]:  # type: ignore[no-untyped-def]
    ids: list[uuid.UUID] = []
    async with tenant_session(engine, ctx(ws)) as c:
        for i in range(n):
            job = await enqueue(
                c,
                workspace_id=ws,
                stage=stage,
                subject_type="interaction",
                subject_id=uuid.uuid4(),
                dedupe_key=f"{prefix}-{uuid.uuid4().hex}-{i}",
                payload={"generation": 0},
                pipeline_version=PV,
                concurrency_key=key,
            )
            assert job is not None
            ids.append(job)
    return ids


async def job_row(engine, ws: uuid.UUID, job_id: uuid.UUID):  # type: ignore[no-untyped-def]
    async with tenant_session(engine, ctx(ws)) as c:
        return (await c.execute(text("SELECT * FROM outbox_jobs WHERE id = :i"), {"i": job_id})).one()


async def expire_lease(owner_engine, job_id: uuid.UUID) -> None:  # type: ignore[no-untyped-def]
    async with owner_engine.begin() as c:
        await c.execute(text("UPDATE outbox_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :i"), {"i": job_id})


@pytest.fixture()
async def owner_engine(migrated_db):  # type: ignore[no-untyped-def]
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(OWNER_URL)
    yield engine
    await engine.dispose()


async def test_claim_is_skip_locked_every_job_claimed_exactly_once(app_engine, stage):
    ws = await make_workspace(app_engine, "claim", ["a"])
    expected = set(await add_jobs(app_engine, ws, 200, stage=stage))
    client = OpsClient(app_engine)
    claimed: list[list[uuid.UUID]] = [[] for _ in range(4)]

    async def claimer(n: int) -> None:
        while True:
            got = await client.claim(f"w{n}", [stage], 10, 30)
            if not got:
                return
            claimed[n].extend(j.job_id for j in got)

    await asyncio.gather(*(claimer(n) for n in range(4)))
    everything = [j for c in claimed for j in c]
    assert len(everything) == len(set(everything)) and set(everything) & expected == expected
    assert sum(1 for c in claimed if c) >= 2, "the work should actually be shared"


async def test_claim_returns_routing_metadata_and_counts_the_attempt(app_engine, stage):
    ws = await make_workspace(app_engine, "meta", ["a"])
    (job_id,) = await add_jobs(app_engine, ws, 1, stage=stage)
    (job,) = await OpsClient(app_engine).claim(f"w1-{stage}", [stage], 5, 30)
    assert (job.job_id, job.workspace_id, job.stage, job.attempts, job.max_attempts, job.pipeline_version) == (
        job_id,
        ws,
        stage,
        1,
        5,
        PV,
    )
    assert set(job.payload) == {"generation"} and job.reclaimed is False and job.lease_token
    row = await job_row(app_engine, ws, job_id)
    assert row.status == "leased" and row.lease_owner == f"w1-{stage}" and row.attempts == 1


async def test_only_claimable_jobs_of_the_requested_stages_are_returned(app_engine, stage):
    ws = await make_workspace(app_engine, "stages", ["a"])
    other = f"{stage}_other"
    await add_jobs(app_engine, ws, 2, stage=other)
    async with tenant_session(app_engine, ctx(ws)) as c:  # a job that is not due yet
        await enqueue(
            c,
            workspace_id=ws,
            stage=stage,
            subject_type="interaction",
            subject_id=uuid.uuid4(),
            dedupe_key="later",
            payload={},
            pipeline_version=PV,
            delay_seconds=3600,
        )
    client = OpsClient(app_engine)
    assert await client.claim("w", [stage], 10, 30) == []
    assert len(await client.claim("w", [other], 10, 30)) == 2


async def test_an_expired_lease_is_reclaimed_and_the_attempt_counted(app_engine, owner_engine, stage):
    ws = await make_workspace(app_engine, "reclaim", ["a"])
    (job_id,) = await add_jobs(app_engine, ws, 1, stage=stage)
    client = OpsClient(app_engine)
    (first,) = await client.claim("dead-worker", [stage], 1, 30)
    assert await client.claim("w2", [stage], 1, 30) == []  # a live lease is not claimable
    await expire_lease(owner_engine, job_id)
    (second,) = await client.claim("w2", [stage], 1, 30)
    assert second.reclaimed and second.attempts == 2 and second.lease_token != first.lease_token
    async with tenant_session(app_engine, ctx(ws)) as c:
        attempts = (
            await c.execute(
                text("SELECT attempt_no, outcome, worker_id FROM job_attempts WHERE job_id = :i ORDER BY attempt_no"), {"i": job_id}
            )
        ).all()
    assert [(a.attempt_no, a.outcome, a.worker_id) for a in attempts] == [(1, "lease_expired", "dead-worker"), (2, None, "w2")]


async def test_completion_is_fenced_a_stale_token_changes_nothing(app_engine, owner_engine, stage):
    ws = await make_workspace(app_engine, "fence", ["a"])
    (job_id,) = await add_jobs(app_engine, ws, 1, stage=stage)
    client = OpsClient(app_engine)
    (old,) = await client.claim("w1", [stage], 1, 30)
    await expire_lease(owner_engine, job_id)
    (new,) = await client.claim("w2", [stage], 1, 30)
    assert await client.complete(old.job_id, old.lease_token, "succeeded") == "stale"  # the first worker wakes up late
    assert (await job_row(app_engine, ws, job_id)).status == "leased"
    assert await client.complete(new.job_id, new.lease_token, "succeeded") == "ok"
    row = await job_row(app_engine, ws, job_id)
    assert row.status == "succeeded" and row.lease_token is None and row.finished_at is not None
    assert await client.complete(new.job_id, new.lease_token, "succeeded") == "stale"  # and a duplicate ack is harmless
    assert await client.complete(uuid.uuid4(), uuid.uuid4(), "succeeded") == "stale"


async def test_outcomes_retry_dead_skipped_and_budget_pause(app_engine, stage):
    ws = await make_workspace(app_engine, "outcomes", ["a"])
    ids = await add_jobs(app_engine, ws, 4, stage=stage)
    client = OpsClient(app_engine)
    jobs = {j.job_id: j for j in await client.claim("w", [stage], 10, 30)}
    retry, dead, skipped, budget = (jobs[i] for i in ids)
    await client.complete(
        retry.job_id, retry.lease_token, "retry", error_category="retryable", error_code="graph_unavailable", delay_seconds=3600
    )
    await client.complete(dead.job_id, dead.lease_token, "dead", error_category="config_terminal", error_code="bad_config")
    await client.complete(skipped.job_id, skipped.lease_token, "skipped", error_category="policy_terminal", error_code="no_consent")
    await client.complete(
        budget.job_id, budget.lease_token, "budget_pause", error_category="budget", error_code="budget_exceeded", delay_seconds=900
    )
    r, d, s, b = [await job_row(app_engine, ws, i) for i in ids]
    assert (r.status, r.attempts, r.last_error_code) == ("pending", 1, "graph_unavailable")
    assert (d.status, d.last_error_category, d.finished_at is not None) == ("dead", "config_terminal", True)
    assert (s.status, s.last_error_code) == ("skipped", "no_consent")
    assert (b.status, b.attempts) == ("pending", 0), "budget pause does not consume an attempt"
    assert await client.claim("w", [stage], 10, 30) == []  # both pending jobs wait for their backoff (database clock)
    async with tenant_session(app_engine, ctx(ws)) as c:
        outcomes = {row.job_id: row.outcome for row in (await c.execute(text("SELECT job_id, outcome FROM job_attempts"))).all()}
    assert outcomes[ids[0]] == "retry" and outcomes[ids[1]] == "dead" and outcomes[ids[3]] == "budget_pause"


async def test_heartbeat_extends_owned_leases_and_reports_lost_ones(app_engine, owner_engine, stage):
    ws = await make_workspace(app_engine, "beat", ["a"])
    ids = await add_jobs(app_engine, ws, 2, stage=stage)
    client = OpsClient(app_engine)
    mine, taken = await client.claim("w1", [stage], 2, 30)
    await expire_lease(owner_engine, taken.job_id)
    (theirs,) = await client.claim("w2", [stage], 1, 30)
    assert theirs.job_id == taken.job_id
    lost = await client.heartbeat("w1", [mine.lease_token, taken.lease_token], 120)
    assert lost == [taken.lease_token]
    row = await job_row(app_engine, ws, mine.job_id)
    assert row.lease_expires_at is not None and ids  # extended, still ours
    async with owner_engine.begin() as c:
        remaining = (
            await c.execute(text("SELECT EXTRACT(EPOCH FROM lease_expires_at - now()) FROM outbox_jobs WHERE id = :i"), {"i": mine.job_id})
        ).scalar_one()
    assert remaining > 60


async def test_release_returns_inflight_jobs_without_consuming_an_attempt(app_engine, stage):
    ws = await make_workspace(app_engine, "release", ["a"])
    (job_id,) = await add_jobs(app_engine, ws, 1, stage=stage)
    client = OpsClient(app_engine)
    await client.claim(f"w1-{stage}", [stage], 1, 30)
    assert await client.release("someone-else") == 0
    assert await client.release(f"w1-{stage}") == 1
    row = await job_row(app_engine, ws, job_id)
    assert (row.status, row.attempts, row.lease_token) == ("pending", 0, None)
    (again,) = await client.claim(f"w2-{stage}", [stage], 1, 30)
    assert again.attempts == 1 and again.reclaimed is False


async def test_concurrency_key_serialises_jobs_across_claimers(app_engine, stage):
    ws = await make_workspace(app_engine, "ckey", ["a"])
    ids = await add_jobs(app_engine, ws, 6, stage=stage, key=f"graph:{uuid.uuid4()}")
    others = await add_jobs(app_engine, ws, 3, stage=stage)  # no key: unaffected
    client = OpsClient(app_engine)
    peak = 0
    done: set[uuid.UUID] = set()

    async def claimer(n: int) -> None:
        nonlocal peak
        for _ in range(60):
            got = await client.claim(f"w{n}", [stage], 4, 30)
            async with tenant_session(app_engine, ctx(ws)) as c:
                live = (
                    await c.execute(text("SELECT count(*) FROM outbox_jobs WHERE concurrency_key IS NOT NULL AND status = 'leased'"))
                ).scalar_one()
            peak = max(peak, live)
            for j in got:
                await asyncio.sleep(0.01)
                await client.complete(j.job_id, j.lease_token, "succeeded")
                done.add(j.job_id)
            if done >= set(ids) | set(others):
                return
            await asyncio.sleep(0.02)

    await asyncio.gather(*(claimer(n) for n in range(4)))
    assert peak <= 1, "two jobs with the same concurrency key were leased at once"
    assert done >= set(ids) | set(others)


async def test_backlog_stats_aggregates_without_any_identifier(app_engine, stage):
    ws = await make_workspace(app_engine, "stats", ["a"])
    await add_jobs(app_engine, ws, 3, stage=stage)
    async with system_session(app_engine) as c:
        result = await c.execute(text("SELECT * FROM ops.backlog_stats()"))
        assert set(result.keys()) == {"stage", "status", "jobs", "oldest_age_seconds"}
        rows = {(r.stage, r.status): r for r in result.all()}
    assert rows[(stage, "pending")].jobs == 3 and rows[(stage, "pending")].oldest_age_seconds is not None


async def test_enqueue_is_idempotent_on_the_dedupe_key(app_engine, stage):
    ws = await make_workspace(app_engine, "dedupe", ["a"])
    async with tenant_session(app_engine, ctx(ws)) as c:
        args = {
            "workspace_id": ws,
            "stage": "normalize",
            "subject_type": "interaction",
            "subject_id": uuid.uuid4(),
            "dedupe_key": "k",
            "payload": {},
            "pipeline_version": PV,
        }
        assert await enqueue(c, **args) is not None
        assert await enqueue(c, **args) is None
        assert (await c.execute(text("SELECT count(*) FROM outbox_jobs"))).scalar_one() == 1


async def test_cancel_job_is_scoped_to_the_callers_workspace(app_engine, stage):
    mine = await make_workspace(app_engine, "cancel-a", ["a"])
    other = await make_workspace(app_engine, "cancel-b", ["b"])
    (job_id,) = await add_jobs(app_engine, mine, 1, stage=stage)
    async with tenant_session(app_engine, ctx(other)) as c:
        assert (await c.execute(text("SELECT * FROM ops.cancel_job(:j)"), {"j": job_id})).first() is None  # another tenant cannot cancel it
    async with tenant_session(app_engine, ctx(mine)) as c:
        row = (await c.execute(text("SELECT * FROM ops.cancel_job(:j)"), {"j": job_id})).one()
        assert row.previous_status == "pending"
    assert (await job_row(app_engine, mine, job_id)).status == "cancelled"
    async with system_session(app_engine) as c:  # and with no tenant at all
        assert (await c.execute(text("SELECT * FROM ops.cancel_job(:j)"), {"j": job_id})).first() is None


# ---- grants, immutability, RLS --------------------------------------------------------------------------------------------------------------


async def test_kg_app_cannot_update_or_delete_outbox_rows_directly(app_engine, stage):
    ws = await make_workspace(app_engine, "grants", ["a"])
    (job_id,) = await add_jobs(app_engine, ws, 1, stage=stage)
    for sql in (
        "UPDATE outbox_jobs SET status = 'succeeded'",
        "UPDATE outbox_jobs SET attempts = 0",
        "DELETE FROM outbox_jobs",  # P8: refused by the purge gate (the row exists)
        "UPDATE job_attempts SET outcome = 'x'",
        "INSERT INTO job_attempts (id, job_id, workspace_id, attempt_no, worker_id, started_at) SELECT gen_random_uuid(), id, workspace_id, 1, 'w', now() FROM outbox_jobs",
    ):
        with pytest.raises((ProgrammingError, DBAPIError)):
            async with tenant_session(app_engine, ctx(ws)) as c:
                await c.execute(text(sql))
    async with tenant_session(
        app_engine, ctx(ws)
    ) as c:  # P8 granted DELETE behind the gate: nothing to delete is no error, and no row goes
        assert (await c.execute(text("DELETE FROM job_attempts"))).rowcount == 0
    assert (await job_row(app_engine, ws, job_id)).status == "pending"


async def make_interaction(engine, ws: uuid.UUID) -> uuid.UUID:  # type: ignore[no-untyped-def]
    iid = uuid.uuid4()
    async with tenant_session(engine, ctx(ws)) as c:
        principal = (await c.execute(text("SELECT id FROM principals LIMIT 1"))).scalar_one()
        await c.execute(
            text(
                "INSERT INTO interactions (id, workspace_id, source_system, source_ref, idempotency_key, request_hmac, kind, occurred_at, submitted_by, "
                "classification, pipeline_version, byte_size) VALUES (:i, :w, 'chat', 'r', 'k', :h, 'message', now(), :p, 'internal', :pv, 5)"
            ),
            {"i": iid, "w": ws, "h": "a" * 64, "p": principal, "pv": PV},
        )
    return iid


async def test_interaction_envelope_is_immutable_only_the_status_moves(app_engine, owner_engine):
    ws = await make_workspace(app_engine, "immutable", ["a"])
    iid = await make_interaction(app_engine, ws)
    async with tenant_session(app_engine, ctx(ws)) as c:
        await c.execute(
            text("UPDATE interactions SET processing_status = 'processing', status_changed_at = now() WHERE id = :i"), {"i": iid}
        )
    for sql in (
        "UPDATE interactions SET source_ref = 'changed'",
        "UPDATE interactions SET classification = 'public'",
        "UPDATE interactions SET kind = 'json'",
        "DELETE FROM interactions",
    ):
        with pytest.raises((ProgrammingError, DBAPIError)):
            async with tenant_session(app_engine, ctx(ws)) as c:
                await c.execute(text(sql))
    # the trigger holds even for the table owner: defence in depth beyond the column grants
    with pytest.raises((ProgrammingError, DBAPIError), match="immutable"):
        async with owner_engine.begin() as c:
            await c.execute(text("UPDATE interactions SET source_ref = 'changed' WHERE id = :i"), {"i": iid})


async def test_the_idempotency_key_is_unique_per_workspace_and_source_system(app_engine):
    ws = await make_workspace(app_engine, "unique", ["a"])
    await make_interaction(app_engine, ws)
    with pytest.raises(DBAPIError, match="interactions_workspace_id_source_system_idempotency_key_key"):
        await make_interaction(app_engine, ws)  # same (workspace, 'chat', 'k')


@pytest.mark.parametrize("table", P3_TABLES)
async def test_rls_is_enabled_and_forced_on_every_p3_table(app_engine, table):
    async with system_session(app_engine) as c:
        row = (await c.execute(text("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = :t"), {"t": table})).one()
        assert (row.relrowsecurity, row.relforcerowsecurity) == (True, True)
        assert (await c.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one() == 0  # no tenant set => no rows  # noqa: S608


async def test_tenants_never_see_each_others_p3_rows_and_a_pooled_connection_does_not_leak(test_settings, migrated_db, stage):
    from kg_db.engine import create_app_engine

    from tests.conftest import APP_URL

    engine = create_app_engine(test_settings, url=APP_URL, pool_size=1, max_overflow=0)  # one physical connection serves every request
    try:
        a = await make_workspace(engine, "rls-a", ["a"])
        b = await make_workspace(engine, "rls-b", ["b"])
        await add_jobs(engine, a, 2, stage=stage)
        await add_jobs(engine, b, 3, stage=stage)
        ia = await make_interaction(engine, a)
        async with tenant_session(engine, ctx(a)) as c:
            pid = (await c.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            assert (await c.execute(text("SELECT count(*) FROM outbox_jobs"))).scalar_one() == 2
        async with system_session(engine) as c:  # same connection, no tenant: nothing, and the ops functions still work across tenants
            assert (await c.execute(text("SELECT pg_backend_pid()"))).scalar_one() == pid
            assert (await c.execute(text("SELECT count(*) FROM outbox_jobs"))).scalar_one() == 0
        claimed = await OpsClient(engine).claim("w", [stage], 100, 30)  # outside the block: the pool has one connection
        assert {j.workspace_id for j in claimed} == {a, b}
        async with tenant_session(engine, ctx(b)) as c:
            assert (await c.execute(text("SELECT count(*) FROM outbox_jobs"))).scalar_one() == 3
            assert (await c.execute(text("SELECT count(*) FROM interactions WHERE id = :i"), {"i": ia})).scalar_one() == 0
            with pytest.raises((ProgrammingError, DBAPIError)):  # the policy also guards writes into another tenant
                await c.execute(
                    text(
                        "INSERT INTO outbox_jobs (id, workspace_id, stage, subject_type, subject_id, dedupe_key, max_attempts, pipeline_version) "
                        "VALUES (gen_random_uuid(), :w, 'normalize', 'interaction', gen_random_uuid(), 'x', 5, 'p3.v1')"
                    ),
                    {"w": a},
                )
    finally:
        await engine.dispose()


# ---- migration round trip and the backfill ---------------------------------------------------------------------------------------------------


async def test_migration_up_down_up_and_the_backfill_makes_one_job_per_pending_command():
    require(SCRATCH_URL, "postgres")
    from sqlalchemy.ext.asyncio import create_async_engine

    await asyncio.to_thread(migrate.downgrade, SCRATCH_URL, "base")
    await asyncio.to_thread(migrate.upgrade, SCRATCH_URL, "0002")
    engine = create_async_engine(SCRATCH_URL)
    ws, ver, principal = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    pending, done, rejected = (uuid.uuid4() for _ in range(3))
    try:
        async with engine.begin() as c:
            await c.execute(
                text("INSERT INTO workspaces (id, slug, display_name) VALUES (:w, :s, 's')"), {"w": ws, "s": f"bf-{ws.hex[:6]}"}
            )
            await c.execute(
                text("INSERT INTO principals (id, workspace_id, kind, handle, display_name) VALUES (:p, :w, 'agent', 'a', 'a')"),
                {"p": principal, "w": ws},
            )
            await c.execute(text("INSERT INTO ontology_versions (id, workspace_id) VALUES (:v, :w)"), {"v": ver, "w": ws})
            for cid, status, proj in (
                (pending, "applied", "pending"),
                (done, "applied", "projected"),
                (rejected, "rejected", "not_applicable"),
            ):
                await c.execute(
                    text(
                        "INSERT INTO commands (id, workspace_id, idempotency_key, request_hash, command_type, actor_principal_id, authority_class, classification, "
                        "ontology_version_id, payload, status, http_status, projection_status) VALUES (:i, :w, :k, 'h', 'entity.create', :p, 'admin_asserted', "
                        "'internal', :v, '{}', :s, 201, :proj)"
                    ),
                    {"i": cid, "w": ws, "k": str(cid), "p": principal, "v": ver, "s": status, "proj": proj},
                )
            await c.execute(
                text(
                    "INSERT INTO entities (id, workspace_id, type_key, display_name, ontology_version_id, created_by_command_id, last_command_id, projection_status) "
                    "VALUES (gen_random_uuid(), :w, 'T', 'Done', :v, :c, :c, 'projected'), (gen_random_uuid(), :w, 'T', 'Pend', :v, :c2, :c2, 'pending')"
                ),
                {"w": ws, "v": ver, "c": done, "c2": pending},
            )
        await asyncio.to_thread(migrate.upgrade, SCRATCH_URL, "0003")
        async with engine.begin() as c:
            jobs = (
                await c.execute(text("SELECT subject_id, dedupe_key, stage, payload FROM outbox_jobs WHERE workspace_id = :w"), {"w": ws})
            ).all()
        assert [(j.subject_id, j.stage) for j in jobs] == [(pending, "graph_projection")]
        assert jobs[0].dedupe_key == f"command:{pending}:graph_projection:g0" and jobs[0].payload == {
            "command_id": str(pending),
            "generation": 0,
        }
        await asyncio.to_thread(
            migrate.downgrade, SCRATCH_URL, "0002"
        )  # the backfill jobs go with the tables; the sweeper can be re-enabled
        await asyncio.to_thread(migrate.upgrade, SCRATCH_URL, "head")
        async with engine.begin() as c:
            assert (await c.execute(text("SELECT count(*) FROM outbox_jobs"))).scalar_one() == 1  # re-created, once
    finally:
        await engine.dispose()
        await asyncio.to_thread(migrate.downgrade, SCRATCH_URL, "base")
