"""P7 test rig: registered agents (through the registration API, so the flow under test is the flow used), their keys, and calls made WITH a key rather than as a seeded role."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from tests.support.p4 import EVT003, ctx_of, sql  # noqa: F401
from tests.support.p5 import accept_event  # noqa: F401
from tests.support.worker import running_worker, wait_until

PROJECT = "newco-plan"
FINDING = "NewCo needs a state licence to operate in this sector (state licensing board handbook, 2026)."


@dataclass
class Agent:
    t: Any
    id: str
    handle: str
    key: str
    key_id: str

    def headers(self, **extra: str) -> dict[str, str]:
        return {"authorization": f"Bearer {self.key}", **extra}

    async def req(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        key: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        hdrs = self.headers(**(headers or {}))
        if method in ("POST", "PUT", "PATCH", "DELETE") and key != "":
            hdrs["idempotency-key"] = key or str(uuid.uuid4())
        return await self.t.client.request(method, path, headers=hdrs, json=json, params=params)

    async def get(self, path: str, **params: Any) -> httpx.Response:
        return await self.req("GET", path, params=params or None)

    async def build(self, task: str, **body: Any) -> httpx.Response:
        return await self.req("POST", "/v1/context/build", json={"task": task, **body}, key="")

    async def token(self) -> str:
        r = await self.t.client.post("/v1/auth/token", headers=self.headers())
        assert r.status_code == 200, r.text
        return str(r.json()["access_token"])


async def register(
    t: Any,
    handle: str | None = None,
    *,
    subjects: tuple[str, ...] = (),
    project: str | None = PROJECT,
    privileges: tuple[str, ...] = ("read", "propose"),
    kind: str = "agent",
    **over: Any,
) -> Agent:
    """Registers an agent as the tenant's admin, with grants on `subjects` (entity ids, read + propose) and on `project` (with `privileges`)."""
    grants: list[dict[str, Any]] = [{"scope_kind": "subject", "scope_ref": s, "privileges": ["read", "propose"]} for s in subjects]
    if project:
        grants.append({"scope_kind": "project", "scope_ref": project, "privileges": list(privileges)})
    body = {"kind": kind, "handle": handle or f"agent-{uuid.uuid4().hex[:8]}", "display_name": handle or "Agent", "grants": grants, **over}
    r = await t.req("POST", "/v1/principals", "admin", json=body)
    assert r.status_code == 201, r.text
    out = r.json()
    return Agent(t, out["principal"]["id"], out["principal"]["handle"], out["key"]["plaintext"], out["key"]["id"])


async def revoke(t: Any, agent: Agent, reason: str = "retired", **kw: Any) -> httpx.Response:
    return await t.req("POST", f"/v1/principals/{agent.id}/revoke", "admin", json={"reason": reason, **kw})


async def extract_as(api: Any, t: Any, agent: Agent, body: dict[str, Any], *, subjects: list[str]) -> str:
    """The agent submits one event and the real worker extracts it (the strict mock answers from scenarios)."""
    async with running_worker(api, concurrency=2):
        event = await accept_event_as(agent, body, subjects)

        async def done() -> bool:
            d = (await t.get(f"/v1/interactions/{event}", "admin")).json()
            return bool(d["processing_status"] in ("indexed", "partial", "failed"))

        await wait_until(done, timeout=240.0, what="the interaction to finish")

        async def quiet() -> bool:
            rows = await sql(
                api,
                t,
                "SELECT count(*) AS n FROM outbox_jobs WHERE workspace_id = :ws AND status IN ('pending','leased') AND available_at <= now()",
            )
            return bool(rows[0].n == 0)

        await wait_until(quiet, timeout=120.0, what="the outbox to drain")
    return event


async def accept_event_as(agent: Agent, body: dict[str, Any], subjects: list[str] | None = None) -> str:
    payload = {"source_system": "chat", "source_ref": f"msg-{uuid.uuid4().hex[:8]}", "occurred_at": "2026-10-10T09:00:00Z", **body}
    if subjects:
        payload["subject_refs"] = [{"entity_id": s} for s in subjects]
    r = await agent.req("POST", "/v1/interactions", json=payload)
    assert r.status_code == 202, r.text
    return str(r.json()["event_id"])


async def propose(
    agent: Agent, content: str, event: str, *, scope: dict[str, str] | None = None, type_: str = "semantic", **extra: Any
) -> httpx.Response:
    return await agent.req(
        "POST",
        "/v1/memories/proposals",
        json={
            "type": type_,
            "content": content,
            "scopes": [scope or {"kind": "project", "ref": PROJECT}],
            "source_event_ids": [event],
            "purpose": "finding",
            **extra,
        },
    )


_ = ctx_of
