"""SEC-01 continued: a route walker over every P2 route. A foreign id must be indistinguishable from a random id on EVERY route that takes an id,
and a new route that is not classified here fails the build (so it cannot ship without an isolation test)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from kg_api.routers import ALL_ROUTERS

from tests.support.tenants import clear_graph, make_tenant

pytestmark = [pytest.mark.integration, pytest.mark.security]
P2_PREFIXES = ("/v1/ontologies", "/v1/entities", "/v1/relationships", "/v1/commands")
BODIES: dict[str, Any] = {
    "replace_draft": {"content": "ontology: {key: x, version: 1.0.0}"},
    "publish_version": {},
    "update_entity": {},
    "end_relationship": {"valid_to": "2025-01-01"},
}
HEADERS = {"update_entity": {"If-Match": 'W/"rev-1"'}, "replace_draft": {"If-Match": 'W/"rev-1"'}}
# routes with no id in the path: covered by list/create tests; each must still be named here so a new one cannot slip in unclassified
NO_ID_ROUTES = {
    ("GET", "/v1/ontologies/versions"),
    ("POST", "/v1/ontologies/versions"),
    ("GET", "/v1/ontologies/active"),
    ("POST", "/v1/ontologies/active"),
    ("GET", "/v1/ontologies/rule-catalog"),
    ("POST", "/v1/entities"),
    ("GET", "/v1/entities"),
    ("POST", "/v1/relationships"),
    ("GET", "/v1/commands"),
}


def p2_routes() -> list[tuple[str, str, Any]]:
    out = []
    for router in ALL_ROUTERS:
        for route in router.routes:
            path = getattr(route, "path", "")
            if path.startswith(P2_PREFIXES):
                for method in sorted(m for m in route.methods if m not in ("HEAD", "OPTIONS")):  # type: ignore[attr-defined]
                    out.append((method, path, route))
    return out


def test_every_p2_route_is_classified():
    unclassified = [(m, p) for m, p, _ in p2_routes() if "{" not in p and (m, p) not in NO_ID_ROUTES]
    assert unclassified == [], "classify new routes: add them to NO_ID_ROUTES or give them a path id the walker can probe"


def id_kind(param: str) -> str:
    return {"version_id": "version", "entity_id": "entity", "fact_id": "fact", "command_id": "command"}[param]


async def test_foreign_ids_are_indistinguishable_from_random_ids_on_every_route(api, tenant):
    mine = tenant
    sarah = (await mine.entity("Person", "Sarah", source_system="hr", source_ref="1")).json()
    acme = (
        await mine.entity("Organization", "Acme", external_ids=[{"system": "hr", "value": "O"}], source_system="hr", source_ref="2")
    ).json()
    fact = (
        await mine.relationship(
            "WORKS_AT", sarah["entity_id"], acme["entity_id"], valid_from="2022-01-01", source_system="hr", source_ref="e"
        )
    ).json()
    foreign_ids = {
        "version": sarah["ontology_version_id"],
        "entity": sarah["entity_id"],
        "fact": fact["fact_id"],
        "command": fact["command_id"],
    }
    other = await make_tenant(api)
    probed = 0
    try:
        for method, path, route in p2_routes():
            params = [p.name for p in route.dependant.path_params]  # type: ignore[attr-defined]
            if not params:
                continue
            kind = id_kind(params[0])
            name = route.name
            body = BODIES.get(name, {} if method in ("POST", "PUT", "PATCH") else None)
            responses = []
            for ident in (foreign_ids[kind], str(uuid.uuid4())):
                r = await other.req(method, path.replace("{" + params[0] + "}", ident), "admin", json=body, headers=HEADERS.get(name))
                responses.append(r)
            foreign, random = responses
            assert foreign.status_code == random.status_code == 404, (method, path, foreign.status_code, foreign.text)
            strip = lambda r: {k: v for k, v in r.json()["error"].items() if k != "request_id"}  # noqa: E731
            assert strip(foreign) == strip(random), (method, path)
            assert foreign_ids[kind] not in foreign.text
            probed += 1
        assert probed >= 12, probed
        # and the owner still resolves its own ids, so the 404s above are about tenancy and not about a broken probe
        assert (await mine.get(f"/v1/entities/{sarah['entity_id']}", "admin")).status_code == 200
        assert (await mine.get(f"/v1/relationships/{fact['fact_id']}", "admin")).status_code == 200
        assert (await mine.get(f"/v1/commands/{fact['command_id']}", "admin")).status_code == 200
        assert (await mine.get(f"/v1/ontologies/versions/{sarah['ontology_version_id']}", "admin")).status_code == 200
    finally:
        await clear_graph(api, other.group_id)


async def test_workspace_headers_and_query_params_are_never_read_on_p2_routes(api, tenant):
    mine = tenant
    other = await make_tenant(api)
    try:
        sarah = (await mine.entity("Person", "Sarah", source_system="hr", source_ref="1")).json()["entity_id"]
        for hdr in ({"X-Workspace-Id": str(mine.ws)}, {"X-Workspace": mine.slug}, {"X-Tenant-Id": str(mine.ws)}):
            r = await other.req("GET", f"/v1/entities/{sarah}", "admin", headers=hdr)
            assert r.status_code == 404
            assert (await other.req("GET", "/v1/entities", "admin", headers=hdr)).json()["items"] == []
        assert (await other.req("GET", "/v1/entities", "admin", params={"workspace_id": str(mine.ws)})).json()["items"] == []
        body = await other.req("POST", "/v1/entities", "admin", json={"type": "Person", "name": "X", "workspace_id": str(mine.ws)})
        assert body.status_code == 422  # unknown body fields are refused, so a workspace_id in a body cannot be honoured either
    finally:
        await clear_graph(api, other.group_id)


async def test_a_revoked_app_key_stops_writing_commands_on_the_very_next_request(api, tenant):
    from kg_db.session import tenant_session
    from kg_domain.principal import WorkspaceContext
    from kg_governance.repositories import admin

    t = tenant
    assert (await t.entity("Person", "Sarah", source_system="hr", source_ref="1")).status_code == 201
    token = (await api.client.post("/v1/auth/token", headers=t.h("app"))).json()["access_token"]
    jwt_headers = {"authorization": f"Bearer {token}", "idempotency-key": str(uuid.uuid4())}
    assert (await api.client.post("/v1/entities", headers=jwt_headers, json={"type": "Person", "name": "Via JWT"})).status_code == 201
    async with tenant_session(api.engine, WorkspaceContext.for_system_task(t.ws, reason="test revoke")) as conn:
        # `kg keys revoke <prefix>`: find the key's real prefix from the stored rows
        from sqlalchemy import text

        stored = (
            await conn.execute(
                text(
                    "SELECT k.prefix FROM api_keys k JOIN principals p ON p.id = k.principal_id WHERE k.workspace_id = :w AND p.handle = :h"
                ),
                {"w": t.ws, "h": f"app@{t.slug}"},
            )
        ).scalar_one()
        assert await admin.revoke_key(conn, WorkspaceContext.for_system_task(t.ws, reason="test revoke"), prefix=stored)
    assert (await t.entity("Person", "After revoke")).status_code == 401
    jwt_headers["idempotency-key"] = str(uuid.uuid4())
    assert (
        await api.client.post("/v1/entities", headers=jwt_headers, json={"type": "Person", "name": "JWT after revoke"})
    ).status_code == 401
    assert (await t.get("/v1/entities", "admin")).status_code == 200  # other principals are unaffected
