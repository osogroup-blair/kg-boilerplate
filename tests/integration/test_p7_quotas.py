"""P7 WP5: per-principal quotas and usage. Requests per minute (an in-process bucket, refilled by an injectable clock), context tokens and interactions per UTC day (read from
the durable tables by the DATABASE clock, so a restart or a crash loses nothing), a 429 `quota_exceeded` with the right `retry_after` and the quota's name, and the
usage a principal can read about itself."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from tests.support.p4 import sarah_story, sql, strict_mock  # noqa: F401
from tests.support.p5 import owner_sql
from tests.support.p7 import Agent, accept_event_as, propose, register

pytestmark = pytest.mark.integration


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture()
def clock(api):
    quotas = api.app.state.kg.quotas
    original = quotas._clock
    fake = Clock()
    quotas._clock = fake
    quotas._buckets.clear()
    yield fake
    quotas._clock = original
    quotas._buckets.clear()


def detail(r: Any) -> tuple[str, int]:
    err = r.json()["error"]
    return err["details"]["quota"], err["retry_after"]


async def test_requests_per_minute_is_a_bucket_that_refills_with_time(api, tenant_agents, clock):
    t = tenant_agents
    agent = await register(t, "chatty-agent", project=None, quotas={"requests_per_minute": 3})
    for _ in range(3):
        assert (await agent.get("/v1/me")).status_code == 200
    blocked = await agent.get("/v1/me")
    assert blocked.status_code == 429 and blocked.json()["error"]["code"] == "quota_exceeded"
    quota, retry = detail(blocked)
    assert (
        quota == "requests_per_minute" and retry == 20 and blocked.headers["retry-after"] == "20"
    )  # 3 per minute: one token every 20 seconds
    assert (await t.get("/v1/me", "admin")).status_code == 200  # another principal is not affected
    clock.now += 21
    assert (await agent.get("/v1/me")).status_code == 200  # recovers
    assert (await agent.get("/v1/me")).status_code == 429
    clock.now += 600
    for _ in range(3):  # the bucket never holds more than a minute's worth
        assert (await agent.get("/v1/me")).status_code == 200
    assert (await agent.get("/v1/me")).status_code == 429


async def test_a_quota_change_applies_without_waiting_for_the_cache(api, tenant_agents, clock):
    t = tenant_agents
    agent = await register(t, "tuned-agent", project=None, quotas={"requests_per_minute": 1})
    assert (await agent.get("/v1/me")).status_code == 200 and (await agent.get("/v1/me")).status_code == 429
    pid = agent.id
    rev = (await t.get(f"/v1/principals/{pid}", "admin")).json()["revision"]
    r = await t.req(
        "PATCH", f"/v1/principals/{pid}", "admin", json={"quotas": {"requests_per_minute": 1000}}, headers={"if-match": f'W/"rev-{rev}"'}
    )
    assert r.status_code == 200
    for _ in range(5):
        assert (await agent.get("/v1/me")).status_code == 200


async def test_context_tokens_per_day_is_counted_from_the_traces_and_resets_at_the_database_midnight(api, tenant_agents, strict_mock):  # noqa: F811
    t = tenant_agents
    ids = await sarah_story(t, projected=False)
    agent = await register(t, "reader-agent", subjects=(ids["sarah"],), quotas={"context_tokens_per_day": 100})
    first = await agent.build("What do we know about Sarah?", subject_ids=[ids["sarah"]], profile="quick_answer")
    assert first.status_code == 200 and first.json()["budgets"]["used_tokens"] > 0
    spent, blocked = first.json()["budgets"]["used_tokens"], None
    for _ in range(
        6
    ):  # a quota refuses the build AFTER it has been exhausted, so the last allowed build may overshoot it by up to one budget
        r = await agent.build("again", subject_ids=[ids["sarah"]], profile="quick_answer")
        if r.status_code == 429:
            blocked = r
            break
        assert r.status_code == 200
        spent += r.json()["budgets"]["used_tokens"]
    assert blocked is not None and spent >= 100
    quota, retry = detail(blocked)
    assert quota == "context_tokens_per_day" and 1 <= retry <= 86_400
    secs = (
        await owner_sql(
            "SELECT EXTRACT(EPOCH FROM (date_trunc('day', now() AT TIME ZONE 'UTC') AT TIME ZONE 'UTC' + interval '1 day' - now()))::int AS s"
        )
    )[0].s
    assert abs(retry - secs) <= 3  # the next UTC midnight by the DATABASE clock
    lab = await t.req(
        "POST",
        "/v1/context/build",
        "admin",
        json={"task": "x", "subject_ids": [ids["sarah"]], "profile": "quick_answer"},
        headers={"x-act-as-principal": agent.id},
        key="",
    )
    assert lab.status_code == 200  # an admin looking through the agent's permissions does not spend the agent's quota
    await owner_sql(
        "UPDATE context_traces SET created_at = created_at - interval '1 day' WHERE principal_id = :p", p=uuid.UUID(agent.id)
    )  # yesterday's builds
    assert (await agent.build("a new day", subject_ids=[ids["sarah"]], profile="quick_answer")).status_code == 200
    audit = await sql(
        api, t, "SELECT count(*) AS n FROM audit_log WHERE action = 'quota.exceeded' AND actor_principal_id = :p", p=uuid.UUID(agent.id)
    )
    assert audit[0].n == 1  # sampled: the first refusal per principal per hour, not one per refusal


async def test_interactions_per_day(api, tenant_agents):
    t = tenant_agents
    agent = await register(t, "writer-agent", project=None, quotas={"interactions_per_day": 2})
    for i in range(2):
        await accept_event_as(agent, {"kind": "message", "content": f"note {i}"})
    blocked = await agent.req(
        "POST",
        "/v1/interactions",
        json={"source_system": "chat", "source_ref": "n3", "occurred_at": "2026-10-10T09:00:00Z", "kind": "message", "content": "note 3"},
    )
    assert blocked.status_code == 429 and detail(blocked)[0] == "interactions_per_day"
    other = await register(t, "other-writer", project=None, quotas={"interactions_per_day": 2})
    await accept_event_as(other, {"kind": "message", "content": "unaffected"})


async def test_usage_is_the_callers_own_and_survives_a_crash_before_the_flush(api, tenant_agents, strict_mock):  # noqa: F811
    t = tenant_agents
    ids = await sarah_story(t, projected=False)
    agent = await register(
        t,
        "counted-agent",
        subjects=(ids["sarah"],),
        privileges=("read", "propose", "publish"),
        quotas={"context_tokens_per_day": 100_000, "requests_per_minute": 600},
    )
    built = await agent.build("What do we know about Sarah?", subject_ids=[ids["sarah"]], profile="quick_answer")
    event = await accept_event_as(agent, {"kind": "message", "content": "evidence"})
    prop = await propose(agent, "A finding about Sarah", event, subject_ids=[ids["sarah"]], scope={"kind": "subject", "ref": ids["sarah"]})
    assert built.status_code == 200 and prop.status_code == 202
    usage = (await agent.get("/v1/me/usage")).json()
    assert usage["context_builds"] == 1 and usage["context_tokens"] == built.json()["budgets"]["used_tokens"]
    assert usage["interactions"] == 1 and usage["proposals"] == 1 and usage["requests"] >= 4
    assert (
        usage["quotas"]["context_tokens_per_day"] == 100_000
        and usage["defaults"]["requests_per_minute"] == api.settings.principal_requests_per_minute
    )
    state = api.app.state.kg
    assert await state.quotas.flush() >= 1
    row = (
        await sql(
            api,
            t,
            "SELECT requests, context_builds, context_tokens, interactions, proposals FROM agent_usage_daily WHERE principal_id = :p",
            p=uuid.UUID(agent.id),
        )
    )[0]
    assert row.context_builds == 1 and row.interactions == 1 and row.proposals == 1 and row.requests >= 4
    # a crash before the next flush loses the buffered request count and NOTHING else: the other counters are recomputed from the durable tables
    await agent.get("/v1/me")
    state.quotas._buffer.clear()
    again = (await agent.get("/v1/me/usage")).json()
    assert (
        again["context_builds"] == 1
        and again["interactions"] == 1
        and again["proposals"] == 1
        and again["context_tokens"] == usage["context_tokens"]
    )
    assert 0 <= again["requests"] - row.requests <= 2  # bounded: at most the last window of request counts
    # an admin reads any principal's usage; a principal reads nobody else's
    admin_view = await t.get(f"/v1/principals/{agent.id}/usage", "admin")
    assert admin_view.status_code == 200 and admin_view.json()["context_builds"] == 1
    other = await register(t, "nosy-agent", project=None)
    assert (await other.get(f"/v1/principals/{agent.id}/usage")).status_code == 403


async def test_activity_counts_and_ids_only_never_content(api, tenant_agents, strict_mock):  # noqa: F811
    t = tenant_agents
    ids = await sarah_story(t, projected=False)
    agent = await register(t, "watched-agent", subjects=(ids["sarah"],), privileges=("read", "propose", "publish"))
    secret = f"zqprivate{uuid.uuid4().hex[:10]}"
    await agent.req(
        "POST", "/v1/memories", json={"type": "semantic", "content": f"{secret} private", "scopes": [{"kind": "agent", "ref": "self"}]}
    )
    event = await accept_event_as(agent, {"kind": "message", "content": f"evidence {secret}"})
    await propose(agent, f"finding {secret}", event, subject_ids=[ids["sarah"]])
    await agent.build("What do we know about Sarah?", subject_ids=[ids["sarah"]], profile="quick_answer")
    await agent.get("/v1/entities/" + str(uuid.uuid4()))  # a 404 is not a denial
    a = await t.get(f"/v1/principals/{agent.id}/activity", "admin")
    assert a.status_code == 200 and secret not in a.text
    out = a.json()
    assert out["interactions"] and sum(out["interactions"].values()) == 1 and out["proposals"].get("active") == 1
    assert (
        out["published_findings"] == 1
        and out["verified_findings"] == 0
        and out["context_builds"] == 1
        and out["context_profiles"] == {"quick_answer": 1}
    )
    assert out["last_seen_at"] is not None and out["denials"] == 0
    _ = Agent
