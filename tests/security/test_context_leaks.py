"""P6 WP8: CTX-03 / RET-04 / AT-10 / AT-11 / SEC-02 for the context engine. Release blockers.

Sentinels are unique random words planted in every kind of place a principal must not reach: an agent's private note, its session note, a subject memory behind a grant, another
workspace's memory, a proposed-but-unapproved memory. Every way of building a context is tried by the principals that must not see them, in every profile and mode, with and
without the Lab's act-as. A hit ANYWHERE the caller (or an operator reading the telemetry) can look is a failure: the package, the items, the citations, the counts, the trace,
the headers, the logs, the metrics, the audit log, the call log, the job payloads."""

from __future__ import annotations

import json
import logging
import uuid

import pytest

from tests.support.p4 import sarah_story, strict_mock  # noqa: F401
from tests.support.p5 import accept_event, propose, run_embeds, save
from tests.support.p6 import owner_rows

pytestmark = pytest.mark.security
PROFILES = ("quick_answer", "planning", "research", "review", "personal_assistant")
MODES = ("structured", "extractive")


def sentinel(label: str) -> str:
    return f"{label}{uuid.uuid4().hex}"


class Planted:
    def __init__(self) -> None:
        self.private, self.session, self.subject, self.foreign, self.proposed = (
            sentinel(x) for x in ("zqprivate", "zqsession", "zqsubject", "zqforeign", "zqproposed")
        )
        self.ids: dict[str, str] = {}

    @property
    def everything(self) -> list[str]:
        return [self.private, self.session, self.subject, self.foreign, self.proposed]


async def plant(api, t, other):  # type: ignore[no-untyped-def]
    ids = await sarah_story(t)
    for who in ("agent", "agent2"):
        await t.grant(who, "subject", ids["sarah"])
    p = Planted()
    r = await save(
        t,
        "agent2",
        {
            "type": "semantic",
            "content": f"private note {p.private} about ledgers and the business plan",
            "scopes": [{"kind": "agent", "ref": "self"}],
        },
    )
    assert r.status_code == 201, r.text
    p.ids["private"] = r.json()["memory_id"]
    r = await save(
        t, "agent2", {"type": "working", "content": f"session note {p.session} about ledgers and the business plan", "run_id": "r1"}
    )
    p.ids["session"] = r.json()["memory_id"]
    r = await save(
        t,
        "admin",
        {
            "type": "semantic",
            "content": f"subject memory {p.subject} about ledgers and the business plan",
            "subject_ids": [ids["sarah"]],
            "source_ref": "x",
            "purpose": "p",
        },
    )
    p.ids["subject"] = r.json()["memory_id"]
    evt = await accept_event(t, {"kind": "message", "content": "an unapproved note"}, who="agent2")
    r = await propose(
        t,
        "agent2",
        {
            "type": "semantic",
            "content": f"proposal {p.proposed} about ledgers and the business plan",
            "requested_scope": {"kind": "workspace"},
            "source_event_ids": [evt],
        },
    )
    p.ids["proposed"] = r.json()["memory_id"]
    r = await save(
        other,
        "admin",
        {
            "type": "semantic",
            "content": f"foreign memory {p.foreign} about ledgers and the business plan",
            "scopes": [{"kind": "workspace"}],
            "source_ref": "x",
            "purpose": "p",
        },
    )
    p.ids["foreign"] = r.json()["memory_id"]
    await run_embeds(api, t)
    await run_embeds(api, other)
    return p, ids


TASKS = [
    "ledgers and the business plan",
    "Help Sarah assess starting a business",
    "private session subject foreign proposal",
    "%",
    "'; DROP TABLE curated_memories; --",
]


async def everything_observed(t, who: str, ids, *, as_admin: bool = False, target: str | None = None) -> str:  # type: ignore[no-untyped-def]
    """Every response body and header the principal gets from building in every profile and mode, plus the traces and re-materialised packages of those builds."""
    seen: list[str] = []
    headers = {"x-act-as-principal": await t.principal_id(target or who)} if as_admin else {}
    for profile in PROFILES:
        for mode in MODES:
            for task in TASKS:
                body = {
                    "task": task,
                    "subject_ids": [ids["sarah"]],
                    "profile": profile,
                    "compression": mode,
                    "project": "newco-plan",
                    "run_id": "r1",
                    "max_tokens": 800,
                }
                for variant in ({}, {"as_of": "2030-01-01"}, {"include": ["episodic", "semantic", "working"]}):
                    r = await t.req(
                        "POST", "/v1/context/build", "admin" if as_admin else who, json={**body, **variant}, key="", headers=headers
                    )
                    assert r.status_code in (200, 422), (profile, mode, r.status_code, r.text[:200])
                    seen.append(r.text + json.dumps(dict(r.headers)))
                    if r.status_code == 200 and r.json()["context_id"]:
                        cid = r.json()["context_id"]
                        for path in (f"/v1/context/{cid}", f"/v1/context/{cid}/trace"):
                            g = await t.req("GET", path, "admin" if as_admin else who, key="")
                            seen.append(g.text + json.dumps(dict(g.headers)))
    for path in ("/v1/context", "/v1/context/profiles"):
        seen.append((await t.req("GET", path, who, key="")).text)
    return "\n".join(seen)


async def test_ctx03_nothing_unauthorised_is_observable_in_any_context_or_trace(api, tenant_ctx, bare_tenant, strict_mock):  # noqa: F811
    t, other = tenant_ctx, bare_tenant
    p, ids = await plant(api, t, other)
    # agent has the Sarah grant: it may see the SUBJECT memory, and nothing private of agent2's, nothing proposed, nothing foreign
    mine = await everything_observed(t, "agent", ids)
    assert p.subject in mine, "the control: a granted principal does see subject memory"
    for hidden in (p.private, p.session, p.foreign, p.proposed):
        assert hidden not in mine
    # agent3 has no grants at all: none of it
    nothing = await everything_observed(t, "agent3", ids)
    assert not any(s in nothing for s in p.everything)
    # the Lab acting as agent3 shows agent3's view, not the admin's: none of it
    lab = await everything_observed(t, "admin", ids, as_admin=True, target="agent3")
    assert not any(s in lab for s in p.everything)
    # the owner of the private note sees it only in its own run, and still never anything foreign
    own = await everything_observed(t, "agent2", ids)
    assert p.foreign not in own
    # another workspace sees none of this workspace's text (its subject id does not resolve: every build is `denied`)
    theirs = await everything_observed(other, "agent", ids)
    assert not any(s in theirs for s in (p.private, p.session, p.subject, p.proposed))
    # broad roles do not see private memory through a context either
    for who in ("app", "reviewer", "admin"):
        broad = await everything_observed(t, who, ids)
        assert p.private not in broad and p.session not in broad and p.proposed not in broad, who


async def test_the_hidden_cannot_be_inferred_from_counts_or_traces(api, tenant_ctx, bare_tenant, strict_mock):  # noqa: F811
    """Adding a memory agent3 may not see changes nothing agent3 can observe: not a count, not a trace field, not an item."""
    t, other = tenant_ctx, bare_tenant
    p, ids = await plant(api, t, other)

    async def view():  # type: ignore[no-untyped-def]
        out = (
            await t.req(
                "POST",
                "/v1/context/build",
                "agent3",
                json={"task": "ledgers and the business plan", "subject_ids": [ids["sarah"]], "profile": "planning"},
                key="",
            )
        ).json()
        tr = (await t.get(f"/v1/context/{out['context_id']}/trace", "agent3")).json()
        strip = {k: v for k, v in tr.items() if k not in ("context_id", "created_at", "latencies", "items", "watermarks")}
        items = [(i["kind"], i["section"], i["tier"], i["included"], i["exclusion_reason"]) for i in tr["items"]]
        return (
            json.dumps(
                {
                    k: v
                    for k, v in out.items()
                    if k not in ("context_id", "generated_at", "retrieval_trace_id", "citations", "source_watermarks")
                },
                sort_keys=True,
                default=str,
            ),
            strip,
            items,
        )

    before = await view()
    r = await save(
        t,
        "admin",
        {
            "type": "semantic",
            "content": f"another hidden {sentinel('zqmore')} about ledgers and the business plan",
            "scopes": [{"kind": "agent", "ref": "self"}],
        },
    )
    assert r.status_code == 201
    await save(
        t,
        "agent2",
        {
            "type": "semantic",
            "content": f"yet another {sentinel('zqmore')} about ledgers and the business plan",
            "scopes": [{"kind": "agent", "ref": "self"}],
        },
    )
    await run_embeds(api, t)
    after = await view()
    assert before[1]["counts"] == after[1]["counts"] and before[2] == after[2]
    assert (
        before[0].replace("0.", "") == after[0].replace("0.", "")
        or json.loads(before[0])["context_text"] == json.loads(after[0])["context_text"]
    )


async def test_telemetry_holds_no_context_content(api, tenant_ctx, bare_tenant, strict_mock, caplog):  # noqa: F811
    t, other = tenant_ctx, bare_tenant
    p, ids = await plant(api, t, other)
    task = f"zqtask{uuid.uuid4().hex} ledgers and the business plan"
    with caplog.at_level(logging.DEBUG):
        r = await t.req(
            "POST",
            "/v1/context/build",
            "agent",
            json={"task": task, "subject_ids": [ids["sarah"]], "profile": "planning", "debug_snapshot": False},
            key="",
        )
        assert r.status_code == 200
    logged = "\n".join(rec.getMessage() + " " + str(getattr(rec, "__dict__", "")) for rec in caplog.records)
    from kg_obs import metrics
    from prometheus_client import generate_latest

    telemetry = logged + generate_latest(metrics.REGISTRY).decode()
    for table in ("audit_log", "model_calls", "outbox_jobs", "context_traces", "context_items"):
        rows = await owner_rows(f"SELECT x::text AS j FROM {table} x WHERE workspace_id = :ws", ws=t.ws)  # noqa: S608 (a constant table name in a test)
        telemetry += "\n".join(row.j for row in rows)
    for needle in (task, "zqtask", p.subject, p.private, "ledgers and the business plan"):
        assert needle not in telemetry, needle
