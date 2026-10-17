"""P7 WP6: the Python SDK against the real API (in process): every method end to end as the two agents of the blueprint scenario, retries that the server sees as ONE logical
write, 429 handling, a revoked key, and no key in logs or a repr. SDK and HTTP authorise identically because the SDK is only a client."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import httpx
import pytest
from kg_platform_sdk import AuthError, InvalidEvidence, KGClient, NotFound, QuotaExceeded
from kg_platform_sdk import models as m

from tests.support.p4 import sarah_story, sql, strict_mock  # noqa: F401
from tests.support.p5 import run_embeds
from tests.support.p7 import FINDING, PROJECT, Agent, register, revoke

pytestmark = [pytest.mark.integration, pytest.mark.contract]


def sdk(api: Any, agent: Agent, **kw: Any) -> KGClient:
    return KGClient("http://kg.test", agent.key, transport=httpx.ASGITransport(app=api.app), **kw)


async def test_every_method_end_to_end_as_research_and_planning(api, tenant_agents, strict_mock):  # noqa: F811
    t = tenant_agents
    ids = await sarah_story(t, projected=False)
    research = await register(
        t, "research-agent", subjects=(ids["sarah"],), privileges=("read", "propose", "publish"), allowed_profiles=["research", "planning"]
    )
    planning = await register(t, "planning-agent", subjects=(ids["sarah"],), privileges=("read",), allowed_profiles=["planning"])
    async with sdk(api, research) as r, sdk(api, planning) as p:
        ctx = await r.build_context(
            "What do we know about Sarah's intended business?", subject_ids=[ids["sarah"]], profile="research", max_tokens=600
        )
        assert (
            ctx.status != "denied"
            and ctx.request_id
            and ctx.as_prompt_block() == ctx.context_text
            and ctx.context_text.startswith("[CONTEXT DATA")
        )
        accepted = await r.ingest_interaction(
            source_system="research-notes", source_ref="evt_005", kind="document", content_type="text/plain",
            content="NewCo needs a state licence to operate in this sector.", occurred_at="2026-10-11T09:00:00Z", subject_refs=[{"entity_id": ids["sarah"]}],
        )  # fmt: skip
        status = await r.get_interaction_status(accepted.event_id)
        assert (
            str(status.id) == str(accepted.event_id)
            and status.processing_status in ("accepted", "processing", "indexed", "partial")
            and status.request_id
        )
        note = await r.save_working_memory("r7", "check NewCo licensing", ttl_hours=2)
        assert note.status == "active" and note.request_id
        proposal = await r.propose_memory(
            type="semantic",
            content=FINDING,
            scopes=[{"kind": "project", "ref": PROJECT}],
            source_event_ids=[str(accepted.event_id)],
            subject_ids=[ids["sarah"]],
            purpose="finding",
        )
        assert proposal.status == "active" and proposal.review_id is not None
        private = await r.propose_memory(
            type="semantic",
            content="my own hunch",
            scopes=[{"kind": "agent", "ref": "self"}],
            source_event_ids=[str(accepted.event_id)],
            purpose="note",
        )
        shared = await r.publish_memory(
            private.memory_id, [{"kind": "project", "ref": PROJECT}], reason_code="share_finding"
        )  # the revision is read for us
        assert shared.status in ("active", "proposed") and shared.memory_id != private.memory_id
        await run_embeds(api, t)
        found = await p.search_memory("state licence", project=PROJECT)
        hit = next(h for h in found.hits if str(h.memory_id) == str(proposal.memory_id))
        assert hit.agent_generated is True and hit.evidence_status == "unverified" and found.request_id
        mine = await p.build_context(
            "What licence does NewCo need?", subject_ids=[ids["sarah"]], profile="planning", project=PROJECT, max_tokens=600
        )
        assert "state licence" in mine.context_text and "(agent_generated, unverified)" in mine.context_text
        assert not any("check NewCo licensing" in x.text for x in [*mine.memories, *mine.facts])  # AGT-02 through the SDK
        evidence = await p.get_evidence(ids["em"])
        assert evidence and evidence[0].kind == "command"
        profiles = await p.list_profiles()
        assert {x["name"] for x in profiles} >= {"planning", "research"}
        usage = await p.usage()
        assert usage.context_builds == 1 and usage.request_id
        with pytest.raises(InvalidEvidence) as err:
            await r.propose_memory(
                type="semantic",
                content="bad",
                scopes=[{"kind": "project", "ref": PROJECT}],
                source_event_ids=[str(uuid.uuid4())],
                purpose="x",
            )
        assert err.value.status == 422 and err.value.request_id
        with pytest.raises(NotFound):
            await p.get_evidence(uuid.uuid4())
        denied = await p.build_context("x", subject_ids=[uuid.uuid4()], profile="planning")
        assert denied.status == "denied"
        assert m.ContextOut.model_validate(ctx.model_dump(mode="json")).status == ctx.status  # a result round-trips to the API's own JSON


async def test_a_lost_response_is_retried_with_the_same_key_and_the_server_sees_one_write(api, tenant_agents):
    t = tenant_agents
    agent = await register(t, "flaky-agent", project=None)
    inner = httpx.ASGITransport(app=api.app)
    seen: list[httpx.Request] = []

    class LoseFirstAnswer(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            seen.append(request)
            response = await inner.handle_async_request(request)  # the server DID process it...
            if len(seen) == 1:
                await response.aread()
                return httpx.Response(
                    503, json={"error": {"code": "dependency_unavailable", "message": "bad gateway", "request_id": "x"}}
                )  # ...but the client heard 503
            return response

    slept: list[float] = []

    async def sleep(s: float) -> None:
        slept.append(s)

    async with KGClient("http://kg.test", agent.key, transport=LoseFirstAnswer(), sleep=sleep) as kg:
        out = await kg.ingest_interaction(
            source_system="chat", source_ref="msg-1", kind="message", content="hello", occurred_at="2026-10-10T09:00:00Z"
        )
    assert len(seen) == 2 and len(slept) == 1
    assert seen[0].headers["idempotency-key"] == seen[1].headers["idempotency-key"]
    rows = await sql(api, t, "SELECT count(*) AS n FROM interactions WHERE submitted_by = :p", p=uuid.UUID(agent.id))
    assert rows[0].n == 1 and out.event_id  # the replay answered; nothing was written twice


async def test_a_429_honours_retry_after_and_a_daily_quota_is_not_slept_on(api, tenant_agents):
    t = tenant_agents
    quotas = api.app.state.kg.quotas
    now = [1000.0]
    original = quotas._clock
    quotas._clock = lambda: now[0]
    quotas._buckets.clear()
    try:
        agent = await register(t, "limited-agent", project=None, quotas={"requests_per_minute": 2, "interactions_per_day": 1})
        waits: list[float] = []

        async def sleep(s: float) -> None:
            waits.append(s)
            now[0] += s  # the wait is real time for the server's bucket

        async with sdk(api, agent, sleep=sleep) as kg:
            await kg.usage()
            await kg.usage()
            await kg.usage()  # refused with retry_after, waited for, then served
            assert len(waits) == 1 and 30 <= waits[0] <= 33 + 1  # one token every 30 seconds
            now[0] += 60  # the bucket refills
            await kg.ingest_interaction(
                source_system="chat", source_ref="a", kind="message", content="one", occurred_at="2026-10-10T09:00:00Z"
            )
            now[0] += 120
            with pytest.raises(QuotaExceeded) as err:
                await kg.ingest_interaction(
                    source_system="chat", source_ref="b", kind="message", content="two", occurred_at="2026-10-10T09:00:00Z"
                )
            assert (
                err.value.details["quota"] == "interactions_per_day" and err.value.retry_after > 30
            )  # until midnight: raised, never slept on
            assert len(waits) == 1
    finally:
        quotas._clock = original
        quotas._buckets.clear()


async def test_a_revoked_key_raises_autherror_and_the_key_is_never_logged(api, tenant_agents, caplog):
    t = tenant_agents
    agent = await register(t, "doomed-agent", project=None)
    with caplog.at_level(logging.DEBUG):
        async with sdk(api, agent) as kg:
            assert (await kg.usage()).request_id
            assert (await revoke(t, agent, "retired")).status_code == 200
            with pytest.raises(AuthError) as err:
                await kg.usage()
            assert err.value.status == 401 and agent.key not in str(err.value) and agent.key not in repr(kg)
    assert agent.key not in caplog.text


async def test_sdk_and_http_authorise_identically(api, tenant_agents, strict_mock):  # noqa: F811
    t = tenant_agents
    ids = await sarah_story(t, projected=False)
    blind = await register(t, "blind-agent", project=None)
    async with sdk(api, blind) as kg:
        sdk_ctx = await kg.build_context("Tell me about Sarah", subject_ids=[ids["sarah"]])
        http_ctx = await blind.build("Tell me about Sarah", subject_ids=[ids["sarah"]])
        assert sdk_ctx.status == "denied" and http_ctx.json()["status"] == "denied"
        with pytest.raises(NotFound) as err:
            await kg.get_evidence(
                ids["em"]
            )  # a fact about no one in particular is still a fact in a workspace the agent has no grant on... open under v1.4.0? Sarah is hidden
        assert err.value.status == (await blind.get(f"/v1/relationships/{ids['em']}/evidence")).status_code == 404
