"""SEC-01 continued for P7: a route walker that probes EVERY route taking an entity, fact or command id with an in-workspace id the caller is not allowed to see, next to a random id.
The two answers must be the same 404, byte for byte (minus the request id). Plus: every P7 route has a policy and denies by default, and a principal's management routes are
invisible across workspaces."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from kg_api.routers import ALL_ROUTERS

from tests.security.test_fact_acl_leaks import strip, world
from tests.support.p4 import strict_mock  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.security]
ID_KIND = {"entity_id": "entity", "fact_id": "fact", "command_id": "command"}
P7_PATH_PREFIXES = ("/v1/principals", "/v1/me/")


def hidden_id_routes() -> list[tuple[str, str, Any]]:
    """Every GET route with an entity / fact / command id in its path, anywhere in the API (new routes are picked up automatically)."""
    out = []
    for router in ALL_ROUTERS:
        for route in router.routes:
            path = getattr(route, "path", "")
            names = [p.name for p in route.dependant.path_params]  # type: ignore[attr-defined]
            if names and set(names) <= set(ID_KIND) and "GET" in route.methods:  # type: ignore[attr-defined]
                out.append(("GET", path, route))
    return out


def test_the_walker_finds_the_routes_it_is_meant_to_cover():
    paths = {p for _, p, _ in hidden_id_routes()}
    assert {
        "/v1/entities/{entity_id}",
        "/v1/entities/{entity_id}/relationships",
        "/v1/entities/{entity_id}/graph",
        "/v1/entities/{entity_id}/mentions",
        "/v1/entities/{entity_id}/attribute-claims",
        "/v1/entities/{entity_id}/summary",
        "/v1/relationships/{fact_id}",
        "/v1/relationships/{fact_id}/evidence",
        "/v1/commands/{command_id}",
    } <= paths, paths


async def test_an_invisible_id_is_the_same_404_as_a_random_id_on_every_route(api, tenant_agents, strict_mock):  # noqa: F811
    t = tenant_agents
    ids = await world(t)
    cmd = (await t.get("/v1/commands", "admin", limit=100)).json()["items"]
    sarah_command = next(c["id"] for c in cmd if c["entity_id"] == ids["sarah"])
    hidden = {"entity": ids["sarah"], "fact": ids["works"], "command": sarah_command}
    probed = 0
    for who in ("agent3", "operator"):
        for _, path, route in hidden_id_routes():
            name = route.dependant.path_params[0].name  # type: ignore[attr-defined]
            kind = ID_KIND[name]
            if kind == "command" and who == "agent3":
                continue  # agents hold no command.read at all: a 403, which says nothing about any id
            if path.endswith("/corrections/preview"):
                continue  # needs correction.apply, which neither an agent nor an operator holds: a 403 about the role, not about any id
            real = await t.get(path.replace("{" + name + "}", hidden[kind]), who)
            random = await t.get(path.replace("{" + name + "}", str(uuid.uuid4())), who)
            assert real.status_code == random.status_code == 404, (who, path, real.status_code, real.text[:200])
            assert strip(real) == strip(random), (who, path)
            assert ids["s_name"] not in real.text and hidden[kind] not in real.text
            probed += 1
    assert probed >= 15, probed
    # and the grant holder reaches the same ids: the 404s above are about visibility, not about a broken probe
    await t.grant("agent", "subject", ids["sarah"])
    for _, path, route in hidden_id_routes():
        name = route.dependant.path_params[0].name
        if ID_KIND[name] == "entity" and not path.endswith("/corrections/preview"):
            assert (await t.get(path.replace("{entity_id}", ids["sarah"]), "agent")).status_code == 200, path


def test_every_p7_route_has_a_policy_and_the_management_ones_are_for_admins():
    from kg_governance.authz.policy import Action, policy_of

    seen = 0
    for router in ALL_ROUTERS:
        for route in router.routes:
            path = getattr(route, "path", "")
            if not path.startswith(P7_PATH_PREFIXES):
                continue
            policy = policy_of(route.endpoint)  # type: ignore[attr-defined]
            assert policy is not None, path
            seen += 1
            writes = {m for m in route.methods if m in ("POST", "PATCH", "DELETE")}  # type: ignore[attr-defined]
            if writes and path != "/v1/principals/{principal_id}/activity":
                assert policy.kind == "requires" and policy.action is Action.PRINCIPAL_MANAGE, (path, policy)
    assert seen >= 12


async def test_the_p7_routes_deny_by_default_without_credentials(api, tenant_agents):
    t = tenant_agents
    pid = str(uuid.uuid4())
    for method, path in [
        ("POST", "/v1/principals"),
        ("PATCH", f"/v1/principals/{pid}"),
        ("POST", f"/v1/principals/{pid}/keys"),
        ("DELETE", f"/v1/principals/{pid}/keys/{uuid.uuid4()}"),
        ("GET", f"/v1/principals/{pid}/grants"),
        ("POST", f"/v1/principals/{pid}/grants"),
        ("DELETE", f"/v1/principals/{pid}/grants/{uuid.uuid4()}"),
        ("POST", f"/v1/principals/{pid}/revoke"),
        ("GET", f"/v1/principals/{pid}/activity"),
        ("GET", f"/v1/principals/{pid}/usage"),
        ("GET", "/v1/me/usage"),
    ]:
        for headers in ({}, {"authorization": "Bearer kgk_nopenope_" + "x" * 30}, {"authorization": "Bearer not-a-key"}):
            r = await t.client.request(method, path, headers={**headers, "idempotency-key": "k", "if-match": 'W/"rev-1"'}, json={})
            assert r.status_code == 401, (method, path, headers, r.status_code)
