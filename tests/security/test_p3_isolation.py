"""SEC-01 continued for P3: a route walker over every interaction and operations-job route. A foreign id must be indistinguishable from a random id on
EVERY route that takes an id, a route nobody classified fails the build, deny-by-default holds, and workspace inputs are never honoured."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from kg_api.routers import ALL_ROUTERS
from kg_db.session import tenant_session
from kg_domain.principal import WorkspaceContext
from sqlalchemy import text

from tests.support.tenants import make_tenant

pytestmark = [pytest.mark.integration, pytest.mark.security]
P3_PREFIXES = ("/v1/interactions", "/v1/operations/jobs", "/v1/operations/backlog")
BODIES: dict[str, Any] = {
    "replay_interaction": {"reason_code": "probe"},
    "retry_job": {"reason_code": "probe"},
    "cancel_job": {"reason_code": "probe"},
}
# routes with no id in the path: each must still be named so a new one cannot slip in unclassified
NO_ID_ROUTES = {
    ("POST", "/v1/interactions"),
    ("GET", "/v1/interactions"),
    ("GET", "/v1/operations/jobs"),
    ("GET", "/v1/operations/backlog"),
}
WHO = {"get_job": "operator", "retry_job": "operator", "cancel_job": "operator", "replay_interaction": "operator"}
ID_KIND = {"event_id": "event", "job_id": "job"}


def p3_routes() -> list[tuple[str, str, Any]]:
    out = []
    for router in ALL_ROUTERS:
        for route in router.routes:
            path = getattr(route, "path", "")
            if path.startswith(P3_PREFIXES):
                for method in sorted(m for m in route.methods if m not in ("HEAD", "OPTIONS")):  # type: ignore[attr-defined]
                    out.append((method, path, route))
    return out


def test_every_p3_route_is_classified():
    unclassified = [(m, p) for m, p, _ in p3_routes() if "{" not in p and (m, p) not in NO_ID_ROUTES]
    assert unclassified == [], "classify new routes: add them to NO_ID_ROUTES or give them a path id the walker can probe"


async def test_foreign_ids_are_indistinguishable_from_random_ids_on_every_p3_route(api, tenant):
    mine = tenant
    event = (
        await mine.req(
            "POST",
            "/v1/interactions",
            "app",
            json={"source_system": "chat", "source_ref": "r", "kind": "message", "content": "x", "occurred_at": "2025-01-01T00:00:00Z"},
        )
    ).json()
    async with tenant_session(api.engine, WorkspaceContext.for_system_task(mine.ws, reason="test")) as c:
        job_id = str((await c.execute(text("SELECT id FROM outbox_jobs LIMIT 1"))).scalar_one())
    foreign_ids = {"event": event["event_id"], "job": job_id}
    other = await make_tenant(api)
    probed = 0
    for method, path, route in p3_routes():
        params = [p.name for p in route.dependant.path_params]  # type: ignore[attr-defined]
        if not params:
            continue
        kind = ID_KIND[params[0]]
        body = BODIES.get(route.name, {} if method in ("POST", "PUT", "PATCH") else None)
        who = WHO.get(route.name, "admin")
        responses = [
            await other.req(method, path.replace("{" + params[0] + "}", ident), who, json=body)
            for ident in (foreign_ids[kind], str(uuid.uuid4()))
        ]
        foreign, random = responses
        assert foreign.status_code == random.status_code == 404, (method, path, foreign.status_code, foreign.text)
        strip = lambda r: {k: v for k, v in r.json()["error"].items() if k != "request_id"}  # noqa: E731
        assert strip(foreign) == strip(random), (method, path)
        assert foreign_ids[kind] not in foreign.text
        probed += 1
    assert probed >= 5, probed
    # and the owner still resolves its own ids, so the 404s above are about tenancy and not about a broken probe
    assert (await mine.get(f"/v1/interactions/{event['event_id']}", "admin")).status_code == 200
    assert (await mine.get(f"/v1/interactions/{event['event_id']}/content", "admin")).status_code == 200
    assert (await mine.get(f"/v1/operations/jobs/{job_id}", "operator")).status_code == 200


async def test_lists_and_backlog_show_only_the_callers_workspace(api, tenant):
    mine = tenant
    other = await make_tenant(api)
    body = {"source_system": "chat", "source_ref": "r", "kind": "message", "content": "x", "occurred_at": "2025-01-01T00:00:00Z"}
    await mine.req("POST", "/v1/interactions", "app", json=body)
    assert (await other.get("/v1/interactions", "admin")).json()["items"] == []
    assert (await other.get("/v1/operations/jobs", "admin")).json()["items"] == []
    assert (await other.get("/v1/operations/backlog", "admin")).json()["cells"] == []
    assert len((await mine.get("/v1/interactions", "admin")).json()["items"]) == 1


async def test_workspace_inputs_are_never_honoured_on_p3_routes(api, tenant):
    mine = tenant
    other = await make_tenant(api)
    event = (
        await mine.req(
            "POST",
            "/v1/interactions",
            "app",
            json={"source_system": "chat", "source_ref": "r", "kind": "message", "content": "x", "occurred_at": "2025-01-01T00:00:00Z"},
        )
    ).json()
    for hdr in ({"X-Workspace-Id": str(mine.ws)}, {"X-Workspace": mine.slug}, {"X-Tenant-Id": str(mine.ws)}):
        assert (await other.req("GET", f"/v1/interactions/{event['event_id']}", "admin", headers=hdr)).status_code == 404
        assert (await other.req("GET", "/v1/interactions", "admin", headers=hdr)).json()["items"] == []
    assert (await other.req("GET", "/v1/interactions", "admin", params={"workspace_id": str(mine.ws)})).json()["items"] == []
    smuggled = await other.req(
        "POST",
        "/v1/interactions",
        "app",
        json={
            "source_system": "chat",
            "source_ref": "r",
            "kind": "message",
            "content": "x",
            "occurred_at": "2025-01-01T00:00:00Z",
            "workspace_id": str(mine.ws),
        },
    )
    assert smuggled.status_code == 422  # unknown body fields are refused, so a workspace_id in a body cannot be honoured either
    assert (await mine.get("/v1/interactions", "admin")).json()["items"][0]["id"] == event["event_id"]


async def test_p3_routes_deny_by_default_without_credentials(api):
    for method, path, _ in p3_routes():
        url = path.replace("{event_id}", str(uuid.uuid4())).replace("{job_id}", str(uuid.uuid4()))
        r = await api.client.request(method, url, json={} if method == "POST" else None)
        assert r.status_code == 401, (method, path, r.status_code)


async def test_a_revoked_agent_key_stops_submitting_on_the_very_next_request(api, tenant):
    from kg_governance.repositories import admin

    t = tenant
    body = {"source_system": "chat", "source_ref": "r", "kind": "message", "content": "x", "occurred_at": "2025-01-01T00:00:00Z"}
    assert (await t.req("POST", "/v1/interactions", "agent", json=body)).status_code == 202
    ctx = WorkspaceContext.for_system_task(t.ws, reason="test revoke")
    async with tenant_session(api.engine, ctx) as conn:
        prefix = (
            await conn.execute(
                text(
                    "SELECT k.prefix FROM api_keys k JOIN principals p ON p.id = k.principal_id WHERE k.workspace_id = :w AND p.handle = :h"
                ),
                {"w": t.ws, "h": f"agent@{t.slug}"},
            )
        ).scalar_one()
        assert await admin.revoke_key(conn, ctx, prefix=prefix)
    assert (await t.req("POST", "/v1/interactions", "agent", json={**body, "source_ref": "r2"})).status_code == 401
    assert (await t.get("/v1/interactions", "admin")).status_code == 200
