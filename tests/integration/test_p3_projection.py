"""P3 WP5: graph projection on the outbox. One job per applied command, a no-op when the fast path already projected, retries with backoff,
budget pause without consuming an attempt, policy-terminal -> skipped_by_policy, dead letters and the operator retry (ING-02 mechanics)."""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
from kg_db.session import tenant_session
from kg_domain.principal import WorkspaceContext
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tests.conftest import OWNER_URL
from tests.integration.test_p2_projection import Spied
from tests.support.worker import running_worker, wait_until

pytestmark = pytest.mark.integration
MOCK = "http://127.0.0.1:8782"


async def set_router_faults(*faults: dict) -> None:
    async with httpx.AsyncClient() as c:
        r = await c.post(f"{MOCK}/__mock/faults", json={"faults": list(faults)})
        assert r.status_code == 200


@pytest.fixture(autouse=True)
async def _clear_router_faults():
    yield
    async with httpx.AsyncClient() as c:
        await c.delete(f"{MOCK}/__mock/requests")  # also clears the fault queue


async def q(api, t, sql, **params):  # type: ignore[no-untyped-def]
    async with tenant_session(api.engine, WorkspaceContext.for_system_task(t.ws, reason="test")) as c:
        return (await c.execute(text(sql), params)).all()


async def jobs_of(api, t, command_id):  # type: ignore[no-untyped-def]
    return await q(
        api,
        t,
        "SELECT id, status, attempts, last_error_code, last_error_category, payload, available_at > now() AS in_future FROM outbox_jobs "
        "WHERE subject_type = 'command' AND subject_id = :c ORDER BY created_at",
        c=uuid.UUID(command_id),
    )


async def command(api, t, command_id):  # type: ignore[no-untyped-def]
    return (await t.get(f"/v1/commands/{command_id}", "admin")).json()


async def test_every_applied_command_enqueues_exactly_one_projection_job_in_its_own_transaction(tenant, api):
    r = await tenant.entity("Person", "Sarah", external_ids=[{"system": "hr", "value": "E-1"}], source_system="hr", source_ref="1")
    assert r.status_code == 201
    cid = r.json()["command_id"]
    (job,) = await jobs_of(api, tenant, cid)
    assert (job.status, job.payload) == ("pending", {"command_id": cid, "generation": 0})
    (pin,) = await q(api, tenant, "SELECT pipeline_version, ontology_version_id, stage FROM outbox_jobs WHERE id = :i", i=job.id)
    assert (
        pin.pipeline_version == "p5.v1"
        and str(pin.ontology_version_id) == r.json()["ontology_version_id"]
        and pin.stage == "graph_projection"
    )
    same = await tenant.entity("Person", "Sarah", external_ids=[{"system": "hr", "value": "E-1"}], source_system="hr", source_ref="1")
    assert same.status_code == 200 and same.json()["matched_existing"]  # matched an existing entity: nothing to project, no job
    assert await jobs_of(api, tenant, same.json()["command_id"]) == []
    bad = await tenant.entity("Nope", "X")
    assert (
        bad.status_code == 422 and await jobs_of(api, tenant, bad.json()["error"]["details"]["command_id"]) == []
    )  # a rejected command enqueues nothing


async def test_when_the_fast_path_already_projected_the_job_is_a_noop_success(tenant, api):
    r = await tenant.entity("Person", "Sarah", source_system="hr", source_ref="1")
    cid = r.json()["command_id"]
    assert r.status_code == 201 and (await command(api, tenant, cid))["projection_status"] == "projected"
    with Spied(api) as spy:
        async with running_worker(api):
            await wait_until(lambda: _job_done(api, tenant, cid), what="job succeeded")
    (job,) = await jobs_of(api, tenant, cid)
    assert (job.status, job.attempts) == ("succeeded", 1)
    assert not [
        x for x in spy.requests if x["path"].endswith("/embeddings")
    ]  # state-based: already projected at this revision, nothing re-sent


async def _job_done(api, t, cid):  # type: ignore[no-untyped-def]
    jobs = await jobs_of(api, t, cid)
    return jobs and jobs[0].status in ("succeeded", "skipped", "dead")


async def test_a_failed_fast_path_is_finished_by_the_job(tenant, api):
    await set_router_faults({"code": "provider_unavailable", "endpoint": "embeddings", "count": 1})
    r = await tenant.entity("Person", "Michael", source_system="hr", source_ref="2")
    cid = r.json()["command_id"]
    assert r.status_code == 202 and r.json()["projection_status"] == "pending"  # the request answered honestly
    async with running_worker(api):
        await wait_until(lambda: _projected(api, tenant, cid), what="projected by the job")
    (job,) = await jobs_of(api, tenant, cid)
    assert job.status in ("succeeded", "pending")  # the first job run may itself have hit the retry delay


async def _projected(api, t, cid):  # type: ignore[no-untyped-def]
    return (await command(api, t, cid))["projection_status"] == "projected"


async def test_a_retryable_failure_backs_off_and_stays_visible_then_succeeds(tenant, api):
    await set_router_faults({"code": "provider_unavailable", "endpoint": "embeddings", "count": 3})
    cid = (await tenant.entity("Person", "Retry", source_system="hr", source_ref="3")).json()["command_id"]  # fast path eats fault 1
    async with running_worker(api):
        job = await wait_until(lambda: _retry_scheduled(api, tenant, cid), what="retry scheduled")
    assert (
        job.status == "pending"
        and job.attempts >= 1
        and job.last_error_code == "router_unavailable"
        and job.last_error_category == "retryable"
    )
    c = await command(api, tenant, cid)
    assert c["projection_status"] == "pending" and c["last_error_code"] == "router_unavailable"
    attempts = await q(api, tenant, "SELECT outcome, error_code FROM job_attempts WHERE job_id = :j ORDER BY attempt_no", j=job.id)
    assert attempts[0].outcome == "retry" and attempts[0].error_code == "router_unavailable"
    # the backoff is on the database clock: make it due, clear the fault, and the next attempt succeeds
    await set_router_faults()
    await make_due(tenant)
    async with running_worker(api):
        await wait_until(lambda: _projected(api, tenant, cid), what="projected")
    assert (await jobs_of(api, tenant, cid))[0].status == "succeeded"


async def _retry_scheduled(api, t, cid):  # type: ignore[no-untyped-def]
    jobs = await jobs_of(api, t, cid)
    return jobs[0] if jobs and jobs[0].last_error_code and jobs[0].status == "pending" and jobs[0].in_future else None


async def make_due(t) -> None:  # type: ignore[no-untyped-def]
    engine = create_async_engine(OWNER_URL)
    try:
        async with engine.begin() as c:
            await c.execute(text("UPDATE outbox_jobs SET available_at = now() WHERE workspace_id = :w AND status = 'pending'"), {"w": t.ws})
    finally:
        await engine.dispose()


async def test_budget_exceeded_pauses_the_job_without_consuming_an_attempt(tenant, api):
    await set_router_faults({"code": "budget_exceeded", "endpoint": "embeddings", "count": 50})
    cid = (await tenant.entity("Person", "Budget", source_system="hr", source_ref="4")).json()["command_id"]
    async with running_worker(api):
        job = await wait_until(lambda: _retry_scheduled(api, tenant, cid), what="budget pause")
    assert (
        job.status == "pending" and job.attempts == 0 and job.last_error_category == "budget" and job.last_error_code == "budget_exceeded"
    )
    assert job.in_future  # paused ~15 minutes on the database clock
    (a,) = await q(api, tenant, "SELECT outcome FROM job_attempts WHERE job_id = :j", j=job.id)
    assert a.outcome == "budget_pause"
    await set_router_faults()
    await make_due(tenant)
    async with running_worker(api):
        await wait_until(lambda: _projected(api, tenant, cid), what="projected after budget returns")
    assert (await jobs_of(api, tenant, cid))[0].attempts == 1  # one real attempt in total


async def test_a_router_policy_error_is_skipped_by_policy_not_retried(tenant, api):
    await set_router_faults({"code": "classification_not_permitted", "endpoint": "embeddings", "count": 50})
    cid = (await tenant.entity("Person", "Policy", source_system="hr", source_ref="5")).json()["command_id"]
    async with running_worker(api):
        await wait_until(lambda: _job_done(api, tenant, cid), what="job skipped")
    (job,) = await jobs_of(api, tenant, cid)
    assert (job.status, job.attempts, job.last_error_category) == ("skipped", 1, "policy_terminal")
    c = await command(api, tenant, cid)
    assert c["projection_status"] == "skipped_by_policy"
    (e,) = await q(
        api, tenant, "SELECT projection_status, embedding_status FROM entities WHERE created_by_command_id = :c", c=uuid.UUID(cid)
    )
    assert (e.projection_status, e.embedding_status) == ("skipped_by_policy", "skipped_by_policy")


async def test_a_config_terminal_failure_dead_letters_at_once_and_an_operator_retry_succeeds(tenant, api):
    await set_router_faults({"code": "model_not_found", "endpoint": "embeddings", "count": 50})
    cid = (await tenant.entity("Person", "Dead", source_system="hr", source_ref="6")).json()["command_id"]
    async with running_worker(api):
        await wait_until(lambda: _job_done(api, tenant, cid), what="dead letter")
    (job,) = await jobs_of(api, tenant, cid)
    assert (job.status, job.attempts, job.last_error_category, job.last_error_code) == ("dead", 1, "config_terminal", "router_rejected")
    assert (await command(api, tenant, cid))["projection_status"] == "failed"
    await set_router_faults()  # the operator fixes the router configuration, then retries with a reason
    r = await tenant.req("POST", f"/v1/operations/jobs/{job.id}/retry", "operator", json={"reason_code": "router_fixed"})
    assert r.status_code == 202 and r.json()["generation"] == 1
    async with running_worker(api):
        await wait_until(lambda: _projected(api, tenant, cid), what="projected after the operator retry")
    jobs = await jobs_of(api, tenant, cid)
    assert [(j.status, j.payload["generation"]) for j in jobs] == [("dead", 0), ("succeeded", 1)]


async def test_attempts_run_out_into_a_dead_letter_with_the_last_error_code(tenant, api):
    await set_router_faults({"code": "provider_unavailable", "endpoint": "embeddings", "count": 500})
    cid = (await tenant.entity("Person", "Exhaust", source_system="hr", source_ref="7")).json()["command_id"]
    engine = create_async_engine(OWNER_URL)
    try:
        async with running_worker(api):
            for _ in range(12):
                await make_due(tenant)
                done = await wait_until(lambda: _settled_or_retrying(api, tenant, cid), timeout=30, what="attempt")
                if done.status == "dead":
                    break
    finally:
        await engine.dispose()
    (job,) = await jobs_of(api, tenant, cid)
    assert (
        job.status == "dead"
        and job.attempts == 8
        and job.last_error_code == "router_unavailable"
        and job.last_error_category == "retryable"
    )
    assert (await command(api, tenant, cid))["projection_status"] == "failed"


async def _settled_or_retrying(api, t, cid):  # type: ignore[no-untyped-def]
    jobs = await jobs_of(api, t, cid)
    return jobs[0] if jobs and (jobs[0].status == "dead" or (jobs[0].status == "pending" and jobs[0].in_future)) else None


async def test_concurrent_projection_of_one_row_never_duplicates_the_node(resilient_projection, tenant, api):
    """Found in P3: Graphiti MERGEs on uuid alone, and without a uniqueness constraint two writers that save the same uuid at once create two nodes
    (the outbox job and the request's own fast path now do exactly that). The constraints in GraphService.ensure_indices close it."""
    st = api.app.state.kg
    await st.graph.ensure_indices()
    ids = []
    for i in range(6):
        r = await tenant.entity("Person", f"Race {i}", source_system="hr", source_ref=str(i))
        assert r.status_code == 202  # the fast path is off: nothing projected yet
        ids.append((await command(api, tenant, r.json()["command_id"]))["entity_id"])
    ctx = WorkspaceContext.for_system_task(tenant.ws, reason="test")
    # four writers per row, all at once
    await asyncio.gather(*(st.knowledge.projector.project_entity(ctx, uuid.UUID(eid)) for eid in ids for _ in range(4)))
    driver = st.graph._graphiti.driver
    records, _, _ = await driver.client.execute_query(
        "MATCH (n:Entity {group_id: $g}) RETURN n.uuid AS u, count(*) AS c", parameters_={"g": tenant.group_id}, database_="neo4j"
    )
    counts = {r["u"]: r["c"] for r in records}
    assert set(counts) == set(ids) and set(counts.values()) == {1}, counts
    names, _, _ = await driver.client.execute_query("SHOW CONSTRAINTS YIELD name RETURN collect(name) AS names", database_="neo4j")
    assert {"kg_entity_uuid", "kg_relates_to_uuid"} <= set(names[0]["names"])


@pytest.fixture()
def resilient_projection(api):  # noqa: ANN001, ANN201
    """The request fast path off, so the rows are projected only by what the test starts."""
    settings = api.app.state.kg.settings
    original = settings.projection_request_budget_seconds
    settings.projection_request_budget_seconds = 0.0
    yield
    settings.projection_request_budget_seconds = original
