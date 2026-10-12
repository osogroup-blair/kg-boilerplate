"""P4 test rig: Sarah's story, scenario ingestion through the REAL pipeline (API -> outbox -> normalize -> graph_ingest -> restore), and strict mock mode.

Scenario texts match the fixtures in tools/mock_router/fixtures/scenarios (the strict mock fails loudly for anything else)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest_asyncio
from kg_db.session import tenant_session
from kg_domain.principal import WorkspaceContext
from sqlalchemy import text

from tests.support.worker import running_worker, wait_until

MOCK = "http://127.0.0.1:8782"
HR = lambda v: [{"system": "hr", "value": v}]  # noqa: E731

EVT003 = {
    "kind": "message",
    "content": "Sarah Johnson: I'm considering entrepreneurship, and Michael, who I've known for years, keeps encouraging me.",
}
NEWCO = {"kind": "message", "content": "Sarah Johnson: I work at NewCo now."}
CYCLIST = {"kind": "message", "content": "Sarah Johnson from the cycling club rode 40 km with Tom on Saturday."}
S_JOHNSON = {"kind": "message", "content": "S. Johnson confirmed the meeting for Thursday."}
MANAGER = {"kind": "message", "content": "Michael is Sarah's manager."}
BAD_JSON = {"kind": "message", "content": "Sarah Johnson: please add the unparseable extraction."}
BOARD = {"kind": "message", "content": "In March 2025 Sarah Johnson joined the board of Velo Club."}


@pytest_asyncio.fixture()
async def strict_mock() -> AsyncIterator[None]:
    """The compose mock router answers 500 `fixture_missing` for an unmatched structured request while a test runs (P4 Q12)."""
    async with httpx.AsyncClient() as c:
        await c.post(f"{MOCK}/__mock/strict", json={"enabled": True})
        await c.post(f"{MOCK}/__mock/reload")
    yield
    async with httpx.AsyncClient() as c:
        await c.delete(f"{MOCK}/__mock/fixtures")
        await c.post(f"{MOCK}/__mock/strict", json={"enabled": False})


def ctx_of(t: Any) -> WorkspaceContext:
    return WorkspaceContext.for_system_task(t.ws, reason="test")


async def sql(api: Any, t: Any, query: str, **params: Any) -> list[Any]:
    async with tenant_session(api.engine, ctx_of(t)) as c:
        return list((await c.execute(text(query), {"ws": t.ws, **params})).all())


async def sarah_story(t: Any, *, projected: bool = True) -> dict[str, str]:
    """The P2 state the P4 demo starts from: Sarah (Engineering Manager at Acme since 2024-06-01), Michael, Entrepreneurship."""
    sarah = (await t.entity("Person", "Sarah Johnson", external_ids=HR("E-1001"), source_system="hr", source_ref="E-1001")).json()
    acme = (await t.entity("Organization", "Acme", external_ids=HR("ORG-1"), source_system="hr", source_ref="ORG-1")).json()
    michael = (await t.entity("Person", "Michael", source_system="hr", source_ref="E-2")).json()
    topic = (await t.entity("Topic", "Entrepreneurship", source_system="hr", source_ref="T-1")).json()
    f1 = (
        await t.relationship(
            "WORKS_AT",
            sarah["entity_id"],
            acme["entity_id"],
            valid_from="2022-01-10",
            attributes={"role": "Software Engineer"},
            source_system="hr",
            source_ref="evt_001",
        )
    ).json()
    f2 = (
        await t.relationship(
            "WORKS_AT",
            sarah["entity_id"],
            acme["entity_id"],
            valid_from="2024-06-01",
            attributes={"role": "Engineering Manager"},
            source_system="hr",
            source_ref="evt_002",
        )
    ).json()
    for r in (sarah, acme, michael, topic, f1, f2):
        assert not projected or r["projection_status"] == "projected", r
    return {
        "sarah": sarah["entity_id"],
        "acme": acme["entity_id"],
        "michael": michael["entity_id"],
        "topic": topic["entity_id"],
        "swe": f1["fact_id"],
        "em": f2["fact_id"],
    }


async def ingest(
    t: Any, body: dict[str, Any], *, subjects: list[str] | None = None, occurred_at: str = "2026-10-10T09:00:00Z", **extra: Any
) -> str:
    payload = {"source_system": "chat", "source_ref": f"msg-{uuid.uuid4().hex[:8]}", "occurred_at": occurred_at, **body, **extra}
    if subjects:
        payload["subject_refs"] = [{"entity_id": s} for s in subjects]
    r = await t.req("POST", "/v1/interactions", "app", json=payload)
    assert r.status_code == 202, r.text
    return str(r.json()["event_id"])


async def wait_terminal(t: Any, event_id: str, *, timeout: float = 240.0) -> dict[str, Any]:  # noqa: ASYNC109
    async def done() -> dict[str, Any] | None:
        d = (await t.get(f"/v1/interactions/{event_id}", "admin")).json()
        return d if d["processing_status"] in ("indexed", "partial", "failed") else None

    return await wait_until(done, timeout=timeout, what=f"interaction {event_id} to finish")  # type: ignore[no-any-return]


async def run_event(api: Any, t: Any, body: dict[str, Any], **kw: Any) -> dict[str, Any]:
    """Ingests one event and runs the real worker until it is terminal, its restore job has run, and the outbox is quiet."""
    async with running_worker(api, concurrency=2):
        event_id = await ingest(t, body, **kw)
        detail = await wait_terminal(t, event_id)

        async def quiet() -> bool:
            rows = await sql(api, t, "SELECT count(*) AS n FROM outbox_jobs WHERE workspace_id = :ws AND status IN ('pending','leased')")
            return rows[0].n == 0

        await wait_until(quiet, timeout=120.0, what="the outbox to drain")
    return detail


def stage(detail: dict[str, Any], name: str) -> dict[str, Any]:
    return next(s for s in detail["stages"] if s["stage"] == name)


async def add_mock_rules(rules: list[dict[str, Any]]) -> None:
    """Run-time fixture rules for one test (the strict_mock fixture clears them)."""
    async with httpx.AsyncClient() as c:
        assert (await c.post(f"{MOCK}/__mock/fixtures", json={"rules": rules})).status_code == 200


class Spied:
    """Swaps a spy under the platform transport for a block: every router request (gateway AND Graphiti) is recorded."""

    def __init__(self, api: Any):
        from tests.support.spy import SpyTransport

        self.transport = api.app.state.kg.router.transport
        self.original = self.transport._inner
        self.spy = SpyTransport(self.original)

    def __enter__(self) -> Any:
        self.transport._inner = self.spy
        return self.spy

    def __exit__(self, *exc: object) -> None:
        self.transport._inner = self.original


def chat_calls(spy: Any) -> int:
    return sum(1 for r in spy.requests if r["path"].endswith("/chat/completions"))


async def reviews(t: Any, who: str = "reviewer", **params: Any) -> list[dict[str, Any]]:
    r = await t.get("/v1/reviews", who, **params)
    assert r.status_code == 200, r.text
    return list(r.json()["items"])


async def decide(
    t: Any,
    review_id: str,
    decision: str,
    *,
    rationale: str = "other",
    who: str = "reviewer",
    key: str | None = None,
    revision: str | None = None,
    text_: str | None = None,
) -> Any:
    """Reads the item for its ETag (unless one is given), then POSTs the decision."""
    if revision is None:
        got = await t.get(f"/v1/reviews/{review_id}", who)
        assert got.status_code == 200, got.text
        revision = got.headers["etag"]
    body: dict[str, Any] = {"decision": decision, "rationale_code": rationale}
    if text_ is not None:
        body["rationale_text"] = text_
    return await t.req("POST", f"/v1/reviews/{review_id}/decision", who, json=body, key=key, headers={"if-match": revision})
