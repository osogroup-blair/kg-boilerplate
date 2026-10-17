"""P5 WP8: SEC-02 / RET-04 (search half). Sentinel memories planted in every kind of private place; every way of asking is tried by principals that must not see them.

The sentinels are unique random words, so a hit anywhere (a result, a count that changed, a header, a log line, a metric, an audit entry, the call log, a job payload)
is unambiguous. The checks are about what a caller can OBSERVE: results, ids, counts, and the telemetry the platform keeps."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from kg_obs import metrics

from tests.support.p4 import ctx_of, sarah_story, sql
from tests.support.p5 import accept_event, act, get, ids_of, run_embeds, save, search

pytestmark = pytest.mark.security


def sentinel(label: str) -> str:
    return f"{label}{uuid.uuid4().hex}"


class Planted:
    """Four sentinels: an agent's private note, an agent's session note, a subject memory (invisible to a principal without the subject grant), another workspace's memory."""

    def __init__(self) -> None:
        self.private, self.session, self.subject, self.foreign = (sentinel(x) for x in ("zqprivate", "zqsession", "zqsubject", "zqforeign"))
        self.ids: dict[str, str] = {}

    @property
    def all(self) -> list[str]:
        return [self.private, self.session, self.subject, self.foreign]


async def plant(api, t, other) -> tuple[Planted, dict[str, str]]:  # type: ignore[no-untyped-def]
    ids = await sarah_story(t)
    for who in ("agent", "agent2"):
        await t.grant(who, "subject", ids["sarah"])
    p = Planted()
    r = await save(
        t, "agent", {"type": "semantic", "content": f"private note {p.private} about ledgers", "scopes": [{"kind": "agent", "ref": "self"}]}
    )
    p.ids["private"] = r.json()["memory_id"]
    r = await save(t, "agent", {"type": "working", "content": f"session note {p.session} about ledgers", "run_id": "r1"})
    p.ids["session"] = r.json()["memory_id"]
    r = await save(
        t,
        "admin",
        {
            "type": "semantic",
            "content": f"subject fact {p.subject} about ledgers",
            "subject_ids": [ids["sarah"]],
            "source_ref": "x",
            "purpose": "p",
        },
    )
    p.ids["subject"] = r.json()["memory_id"]
    r = await save(
        other,
        "admin",
        {
            "type": "semantic",
            "content": f"foreign fact {p.foreign} about ledgers",
            "scopes": [{"kind": "workspace"}],
            "source_ref": "x",
            "purpose": "p",
        },
    )
    p.ids["foreign"] = r.json()["memory_id"]
    await run_embeds(api, t)
    await run_embeds(api, other)
    return p, ids


def clean(response_text: str) -> str:
    """The body of an error without the request id (the only thing that legitimately differs between two refusals)."""
    return json.dumps({k: v for k, v in json.loads(response_text)["error"].items() if k != "request_id"}, sort_keys=True)


QUERIES = [
    "%",
    "*",
    "a | b",
    "'; DROP TABLE curated_memories; --",
    "zq",
    "ledgers",
    "private session subject foreign",
    "NOT zq",
    "(((",
    "ünïcödé ledgers",
    "x" * 900,
]


async def observed(t, who, p, **extra):  # type: ignore[no-untyped-def]
    """Everything one principal can see by asking in every way we know; returns the raw texts and headers it got back."""
    texts: list[str] = []
    for q in [*p.all, *(s[:6] for s in p.all), *QUERIES]:
        for variant in (
            {},
            {"all_runs": True},
            {"run_id": "r1"},
            {"as_of": "2030-01-01"},
            {"types": ["working", "semantic", "episodic", "procedural"]},
            {"limit": 50},
        ):
            r = await t.req("POST", "/v1/memories/search", who, json={"query": q, **variant, **extra})
            assert r.status_code in (200, 422), (q[:20], variant, r.status_code, r.text[:200])
            texts.append(r.text + json.dumps(dict(r.headers)))
    for params in ({}, {"status": "active"}, {"type": "working"}, {"origin": "extracted"}, {"limit": 100}, {"classification": "internal"}):
        r = await t.req("GET", "/v1/memories", who, params=params)
        texts.append(r.text + json.dumps(dict(r.headers)))
    for kind in ("private", "session", "subject", "foreign"):
        for mid in (p.ids[kind], str(uuid.uuid4())):
            r = await get(t, who, mid)
            texts.append(r.text + json.dumps(dict(r.headers)))
    return texts


async def test_nothing_private_is_observable_by_a_peer_agent_an_agent_without_grants_or_another_workspace(api, tenant_mem, bare_tenant):
    t, other = tenant_mem, bare_tenant
    p, ids = await plant(api, t, other)
    # agent2 shares the project and the Sarah grant: it may see the subject memory and nothing private
    mine = "\n".join(await observed(t, "agent2", p))
    assert p.subject in mine and p.private not in mine and p.session not in mine and p.foreign not in mine
    # agent3 has no grants at all
    nothing = "\n".join(await observed(t, "agent3", p))
    assert not any(s in nothing for s in p.all)
    # a principal of ANOTHER workspace never sees this workspace's memory (and its own appears only to it)
    theirs = "\n".join(await observed(other, "agent", p))
    assert not any(s in theirs for s in (p.private, p.session, p.subject))
    # the roles that see broadly still do not see private memory, and a curator is no exception
    for who in ("app", "reviewer", "operator", "reader"):
        text = "\n".join(await observed(t, who, p))
        assert p.private not in text and p.session not in text, who
    # the admin reaches it only on request, and then only because the read is audited
    plain = "\n".join(await observed(t, "admin", p))
    assert p.private not in plain and p.session not in plain
    _ = ids


async def test_an_invisible_id_answers_exactly_like_a_random_one_for_every_verb(api, tenant_mem):
    t = tenant_mem
    other_t = None  # noqa: F841
    ids = await sarah_story(t)
    await t.grant("agent", "subject", ids["sarah"])
    private = (
        await save(t, "agent", {"type": "semantic", "content": f"note {sentinel('zq')}", "scopes": [{"kind": "agent", "ref": "self"}]})
    ).json()["memory_id"]
    subject = (
        await save(
            t,
            "admin",
            {"type": "semantic", "content": f"fact {sentinel('zq')}", "subject_ids": [ids["sarah"]], "source_ref": "x", "purpose": "p"},
        )
    ).json()["memory_id"]
    ghost = str(uuid.uuid4())
    for who in ("agent2", "agent3", "app", "reviewer"):
        for mid in (private,) if who in ("app", "reviewer", "agent3", "agent2") else (private, subject):
            seen, control = [], []
            reads = [("GET", "", None)] + [
                ("POST", f"/{v}", body)
                for v, body in (
                    ("supersede", {"content": "hello there", "reason_code": "x"}),
                    ("expire", {"reason_code": "x"}),
                    ("dispute", {"reason_code": "x"}),
                    ("publish", {"scopes": [{"kind": "workspace"}], "reason_code": "x"}),
                )
            ]
            for method, suffix, body in reads:
                hdr = {"if-match": 'W/"rev-1"'}
                a = await t.req(method, f"/v1/memories/{mid}{suffix}", who, json=body, headers=hdr)
                b = await t.req(method, f"/v1/memories/{ghost}{suffix}", who, json=body, headers=hdr)
                seen.append((a.status_code, clean(a.text)))
                control.append((b.status_code, clean(b.text)))
            assert seen == control, (who, mid, seen, control)
    # the subject memory is invisible to agent3 in the same way
    a, b = await get(t, "agent3", subject), await get(t, "agent3", ghost)
    assert (a.status_code, clean(a.text)) == (b.status_code, clean(b.text)) == (404, clean(b.text))
    _ = act


async def test_counts_scores_and_coverage_do_not_move_when_private_memory_exists(api, tenant_mem):
    """The control run: the same visible memory with and without a private sentinel gives the caller the same candidate count, coverage and scores."""
    with_private, control = tenant_mem, None
    from tests.support.tenants import MEMORY_YAML, clear_graph, make_tenant

    control = await make_tenant(api, ontology=MEMORY_YAML)
    try:
        for t in (with_private, control):
            ids = await sarah_story(t)
            await t.grant("agent2", "subject", ids["sarah"])
            await save(
                t,
                "admin",
                {
                    "type": "semantic",
                    "content": "shared ledgers fact about the quarterly close",
                    "subject_ids": [ids["sarah"]],
                    "source_ref": "x",
                    "purpose": "p",
                },
            )
        for n in range(3):  # three private memories in ONE workspace, one of them with the very words of the query
            await save(
                with_private,
                "agent",
                {
                    "type": "semantic",
                    "content": f"private ledgers note number {n} about the quarterly close",
                    "scopes": [{"kind": "agent", "ref": "self"}],
                },
            )
        await save(with_private, "agent", {"type": "working", "content": "session ledgers close", "run_id": "r1"})
        await run_embeds(api, with_private)
        await run_embeds(api, control)
        a = await search(with_private, "agent2", "ledgers quarterly close")
        b = await search(control, "agent2", "ledgers quarterly close")
        assert (
            (a["candidates_considered"], a["embedding_coverage"], len(a["hits"]))
            == (b["candidates_considered"], b["embedding_coverage"], len(b["hits"]))
            == (1, 1.0, 1)
        )
        assert a["hits"][0]["score"] == b["hits"][0]["score"] and a["hits"][0]["components"] == b["hits"][0]["components"]
        # and the owner sees ITS private memory in its own counts, which is the point of the control
        mine = await search(with_private, "agent", "ledgers quarterly close", all_runs=True)
        assert (
            mine["candidates_considered"] == 4
        )  # its three private notes and its session note; the shared one needs a grant it does not have
    finally:
        await clear_graph(api, control.group_id)


async def test_concurrent_requests_on_pooled_connections_never_cross_principals(api, tenant_mem, bare_tenant):
    t, other = tenant_mem, bare_tenant
    p, _ = await plant(api, t, other)

    async def one(tenant, who, q):  # type: ignore[no-untyped-def]
        r = await tenant.req("POST", "/v1/memories/search", who, json={"query": q, "all_runs": True, "limit": 50})
        return who, r.text

    jobs = []
    for i in range(60):
        who, tenant = (("agent2", t), ("agent3", t), ("agent", other))[i % 3]
        jobs.append(one(tenant, who, ["ledgers", p.private, p.session, p.subject, p.foreign][i % 5]))
    for who, text in await asyncio.gather(*jobs):
        if who == "agent3":
            assert not any(s in text for s in p.all)
        elif who == "agent2":
            assert p.private not in text and p.session not in text and p.foreign not in text
        else:
            assert not any(s in text for s in (p.private, p.session, p.subject))


async def test_no_sentinel_reaches_logs_metrics_audit_the_call_log_jobs_or_the_command_api(api, tenant_mem, bare_tenant, capsys):
    from kg_obs.logging import configure_logging

    t, other = tenant_mem, bare_tenant
    configure_logging("INFO", service="test", json_output=True)
    try:
        p, _ = await plant(api, t, other)
        for who in ("agent", "agent2", "agent3"):
            await observed(t, who, p)
    finally:
        out = capsys.readouterr().out
        configure_logging("WARNING", service="test", json_output=True)
    assert not any(s in out for s in p.all), "log"
    exposition = metrics.render().decode()
    assert not any(s in exposition for s in p.all), "metrics"
    for tenant in (t, other):
        audit = " ".join(
            r.m
            for r in await sql(
                api, tenant, "SELECT metadata::text || ' ' || coalesce(resource_id, '') AS m FROM audit_log WHERE workspace_id = :ws"
            )
        )
        calls = " ".join(str(tuple(r)) for r in await sql(api, tenant, "SELECT * FROM model_calls WHERE workspace_id = :ws"))
        jobs = " ".join(
            r.m for r in await sql(api, tenant, "SELECT payload::text || dedupe_key AS m FROM outbox_jobs WHERE workspace_id = :ws")
        )
        commands = " ".join(
            r.m
            for r in await sql(
                api,
                tenant,
                "SELECT payload::text || ' ' || result::text || ' ' || request_hash AS m FROM commands WHERE workspace_id = :ws",
            )
        )
        for where, blob in (("audit", audit), ("model_calls", calls), ("outbox", jobs), ("commands", commands)):
            assert not any(s in blob for s in p.all), where
    for who in ("admin", "app", "operator", "reviewer"):
        listing = (await t.get("/v1/commands", who, limit=100)).text
        assert not any(s in listing for s in p.all), who
    # the labels of every metric stay closed: no workspace, principal, memory or scope identifier
    ids = {str(t.ws), *p.ids.values(), await t.principal_id("agent")}
    for family in metrics.REGISTRY.collect():
        for sample in family.samples:
            assert not any(v in ids for v in sample.labels.values()), sample


async def test_the_embedding_neighbour_of_a_private_memory_is_not_a_back_door(api, tenant_mem, bare_tenant):
    """The vector half of the search is scoped by the same predicate: querying with the very text of a private memory finds the visible lookalike, never the private one."""
    t = tenant_mem
    ids = await sarah_story(t)
    await t.grant("agent2", "subject", ids["sarah"])
    words = "quarterly planning offsite agenda for the leadership team"
    private = (await save(t, "agent", {"type": "semantic", "content": words, "scopes": [{"kind": "agent", "ref": "self"}]})).json()[
        "memory_id"
    ]
    twin = (
        await save(
            t,
            "admin",
            {"type": "semantic", "content": words + " in Lisbon", "subject_ids": [ids["sarah"]], "source_ref": "x", "purpose": "p"},
        )
    ).json()["memory_id"]
    await run_embeds(api, t)
    out = await search(t, "agent2", words)
    assert ids_of(out) == [twin] and out["candidates_considered"] == 1 and out["hits"][0]["components"]["vec_rank"] == 1
    assert private not in json.dumps(out)
    _ = (accept_event, ctx_of)
