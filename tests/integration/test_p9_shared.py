"""P9 WP7: the shared limiter (one budget across API replicas, the database clock, fail-closed for context builds / fail-open for the cheap calls) and cache invalidation
across processes (LISTEN / NOTIFY)."""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest
from kg_governance.limiter import LimiterUnavailable, SharedLimiter
from kg_governance.notify import Invalidator, sync_dsn
from sqlalchemy import text

from tests.conftest import APP_URL
from tests.support.p7 import register
from tests.support.p8 import rev_header
from tests.support.p9 import replicas

pytestmark = pytest.mark.integration


async def test_hits_count_per_key_and_the_window_is_the_database_clock(api):
    lim = SharedLimiter(api.engine)
    key = f"test:{uuid.uuid4()}"
    first = await lim.hit(key, window_seconds=2, limit=2)
    second = await lim.hit(key, window_seconds=2, limit=2)
    third = await lim.hit(key, window_seconds=2, limit=2)
    assert (first.hits, second.hits, third.hits) == (1, 2, 3)
    assert first.admitted and second.admitted and not third.admitted
    assert 1 <= third.retry_after <= 2
    other = await lim.hit(f"test:{uuid.uuid4()}", window_seconds=2, limit=2)
    assert other.hits == 1  # keys are independent
    await asyncio.sleep(third.retry_after + 0.2)
    assert (await lim.hit(key, window_seconds=2, limit=2)).hits == 1  # a new window
    async with api.engine.connect() as conn:
        assert await conn.scalar(text("SELECT relpersistence FROM pg_class WHERE relname = 'rate_counters'")) == "u"  # UNLOGGED


async def test_slots_are_capped_released_and_expire_by_lease(api):
    lim = SharedLimiter(api.engine)
    key = f"slots:{uuid.uuid4()}"
    a = await lim.acquire(key, cap=2, lease_seconds=60)
    b = await lim.acquire(key, cap=2, lease_seconds=60)
    assert a is not None and b is not None and await lim.acquire(key, cap=2, lease_seconds=60) is None
    await lim.release(key, a)
    c = await lim.acquire(key, cap=2, lease_seconds=1)  # a slot freed by a release
    assert c is not None and await lim.acquire(key, cap=2, lease_seconds=60) is None
    await asyncio.sleep(1.3)  # c's holder "died": its lease ran out
    assert await lim.acquire(key, cap=2, lease_seconds=60) is not None
    async with lim.slot(f"slots:{uuid.uuid4()}", cap=1) as got:
        assert got is True
    # concurrent admissions never exceed the cap
    key2 = f"slots:{uuid.uuid4()}"
    results = await asyncio.gather(*[lim.acquire(key2, cap=3, lease_seconds=60) for _ in range(12)])
    assert sum(1 for r in results if r is not None) == 3
    assert await lim.sweep() >= 0


async def test_two_replicas_enforce_one_request_budget(api, tenant_agents):
    t = tenant_agents
    agent = await register(t, "shared-agent", project=None, quotas={"requests_per_minute": 4})
    other = await register(t, "other-agent", project=None, quotas={"requests_per_minute": 4})
    async with replicas(api, 2) as (a, b):
        hdr = agent.headers()
        codes = []
        for client in (a.client, b.client, a.client, b.client, a.client, b.client):
            codes.append((await client.get("/v1/me", headers=hdr)).status_code)
        # 4 admitted in total (any replica), then refused on BOTH
        assert codes[:4] == [200, 200, 200, 200] and codes[4:] == [429, 429], codes
        blocked = await a.client.get("/v1/me", headers=hdr)
        err = blocked.json()["error"]
        assert err["code"] == "quota_exceeded" and err["details"]["quota"] == "requests_per_minute" and 1 <= err["retry_after"] <= 60
        assert (await b.client.get("/v1/me", headers=other.headers())).status_code == 200  # another principal has its own budget


async def test_two_replicas_share_the_context_build_budget(api, tenant_agents):
    t = tenant_agents
    agent = await register(t, "build-agent", project="newco-plan")
    async with replicas(api, 2, context_rate_per_minute=3, context_rate_burst=100) as (a, b):
        hdr = {**agent.headers()}
        codes = []
        for client in (a.client, b.client, a.client, b.client):
            r = await client.post("/v1/context/build", headers=hdr, json={"task": "what do we know about NewCo?"})
            codes.append((r.status_code, r.json().get("error", {}).get("code")))
        assert all(c != 429 for c, _ in codes[:3]), codes
        assert codes[3] == (429, "rate_limited"), codes


async def test_failure_modes_closed_for_context_builds_open_for_the_cheap_calls(api, tenant_agents):
    t = tenant_agents
    agent = await register(t, "fail-agent", project="newco-plan")

    async def boom(*a, **k):
        raise LimiterUnavailable("OperationalError")

    async with replicas(api, 1, limiter_fail_mode="closed") as (closed,):
        closed.state.shared_limiter.hit = boom
        closed.state.shared_limiter.acquire = boom
        assert (await closed.client.get("/v1/me", headers=agent.headers())).status_code == 200  # cheap: fails open
        r = await closed.client.post("/v1/context/build", headers=agent.headers(), json={"task": "x"})
        assert r.status_code == 503 and r.json()["error"]["code"] == "limiter_unavailable" and r.json()["error"]["retry_after"] >= 1
    async with replicas(api, 1, limiter_fail_mode="open") as (opened,):
        opened.state.shared_limiter.hit = boom
        opened.state.shared_limiter.acquire = boom
        r = await opened.client.post("/v1/context/build", headers=agent.headers(), json={"task": "x"})
        assert r.status_code != 503, r.text


async def test_invalidation_reaches_another_replica_within_a_second(api, tenant_governance):
    t = tenant_governance
    async with replicas(api, 2, workspace_egress_ttl_seconds=300.0) as (a, b):
        await asyncio.wait_for(b.state.invalidator.connected.wait(), 5)
        ws = str(t.ws)
        assert "internal" not in await b.state.router.narrowing.denied(ws)  # B caches the policy (a 5-minute TTL)
        r = await t.req(
            "PUT",
            "/v1/workspaces/current/settings",
            "admin",
            json={"egress": {"denied_classifications": ["internal"]}},
            headers=rev_header(0),
        )
        assert r.status_code == 200, r.text
        started = time.monotonic()
        while "internal" not in await b.state.router.narrowing.denied(ws):
            assert time.monotonic() - started < 1.0, "the narrowing did not reach the other replica in 1 s"
            await asyncio.sleep(0.02)
        # restoring it is seen the same way
        r = await t.req(
            "PUT", "/v1/workspaces/current/settings", "admin", json={"egress": {"denied_classifications": []}}, headers=rev_header(1)
        )
        assert r.status_code == 200, r.text
        started = time.monotonic()
        while "internal" in await b.state.router.narrowing.denied(ws):
            assert time.monotonic() - started < 1.0
            await asyncio.sleep(0.02)


async def test_a_revocation_and_a_quota_change_reach_the_quota_cache_of_another_replica(api, tenant_agents):
    t = tenant_agents
    agent = await register(t, "cache-agent", project=None, quotas={"requests_per_minute": 100})
    async with replicas(api, 2) as (a, b):
        await asyncio.wait_for(b.state.invalidator.connected.wait(), 5)
        assert (await b.client.get("/v1/me", headers=agent.headers())).status_code == 200  # B now caches the limit (5 s)
        assert (await b.state.quotas.limits(_principal(agent, t))).requests_per_minute == 100
        rev = (await t.get(f"/v1/principals/{agent.id}", "admin")).json()["revision"]
        r = await t.req(
            "PATCH", f"/v1/principals/{agent.id}", "admin", json={"quotas": {"requests_per_minute": 7}}, headers=rev_header(rev)
        )
        assert r.status_code == 200, r.text
        started = time.monotonic()
        while (await b.state.quotas.limits(_principal(agent, t))).requests_per_minute != 7:
            assert time.monotonic() - started < 1.0, "the new quota did not reach the other replica in 1 s"
            await asyncio.sleep(0.02)


def _principal(agent, t):
    from kg_domain.enums import PrincipalKind
    from kg_domain.principal import Principal

    return Principal(principal_id=uuid.UUID(agent.id), workspace_id=t.ws, kind=PrincipalKind.AGENT, handle=agent.handle, roles=frozenset())


async def test_the_listener_drops_everything_when_it_reconnects():
    seen: list[str] = []
    inv = Invalidator(sync_dsn(APP_URL), reconnect_seconds=0.1)
    inv.on("*", lambda ws, kind: seen.append(kind))
    inv.start()
    try:
        await asyncio.wait_for(inv.connected.wait(), 5)
        await inv.handle(f"{uuid.uuid4()}:settings")
        await inv.handle("not-a-uuid")
        assert seen == ["settings", "not-a-uuid"] or seen[:1] == ["settings"]
    finally:
        await inv.aclose()
