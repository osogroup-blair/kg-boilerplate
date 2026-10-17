"""P6 WP5/WP8: every context route is policed, deny-by-default, and an id the caller may not see answers exactly like one that never existed (SEC-01, CTX-03).

A route walker over the context routes (and the memory-usage view that reads context builds): foreign ids, in-workspace ids that belong to another principal, and random ids."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from kg_api.routers import ALL_ROUTERS
from kg_governance.authz.policy import policy_of

from tests.support.p4 import strict_mock  # noqa: F401
from tests.support.p6 import ok_build, sarah_world

pytestmark = [pytest.mark.integration, pytest.mark.security]


def context_routes() -> list[tuple[str, str, Any]]:
    out = []
    for router in ALL_ROUTERS:
        for route in router.routes:
            path = getattr(route, "path", "")
            if (
                path.startswith("/v1/context") or path == "/v1/memories/{memory_id}/usage"
            ):  # the P7 usage routes (/v1/me/usage, /v1/principals/{id}/usage) are principal usage
                for method in sorted(m for m in route.methods if m not in ("HEAD", "OPTIONS")):  # type: ignore[attr-defined]
                    out.append((method, path, route))
    return out


def strip(response: Any) -> dict[str, Any]:
    return {k: v for k, v in response.json()["error"].items() if k != "request_id"}  # type: ignore[no-any-return]


def test_every_context_route_is_known_and_has_a_policy():
    paths = {(m, p) for m, p, _ in context_routes()}
    assert paths == {
        ("POST", "/v1/context/build"),
        ("GET", "/v1/context/profiles"),
        ("GET", "/v1/context"),
        ("GET", "/v1/context/{context_id}"),
        ("GET", "/v1/context/{context_id}/trace"),
        ("GET", "/v1/context/{context_id}/debug"),
        ("GET", "/v1/memories/{memory_id}/usage"),
    }
    assert all(policy_of(route.endpoint) is not None for _, _, route in context_routes())


async def test_context_routes_deny_by_default_without_credentials(api):
    for method, path, _ in context_routes():
        url = path.replace("{context_id}", str(uuid.uuid4())).replace("{memory_id}", str(uuid.uuid4()))
        r = await api.client.request(method, url, json={"task": "x"} if method == "POST" else None)
        assert r.status_code == 401, (method, path, r.status_code)


async def test_an_id_the_caller_may_not_see_is_the_same_404_as_one_that_never_existed(api, tenant_ctx, bare_tenant, strict_mock):  # noqa: F811
    t, other = tenant_ctx, bare_tenant
    ids = await sarah_world(api, t)
    out = await ok_build(t, "agent", subject_ids=[ids["sarah"]], profile="planning")
    cid, ghost = out["context_id"], str(uuid.uuid4())
    for suffix in ("", "/trace"):
        # another workspace's admin, and another principal of the same workspace, ask for the agent's build
        for tenant, who in ((other, "admin"), (other, "agent"), (t, "agent2"), (t, "reviewer")):
            real = await tenant.req("GET", f"/v1/context/{cid}{suffix}", who, key="")
            none = await tenant.req("GET", f"/v1/context/{ghost}{suffix}", who, key="")
            assert real.status_code == none.status_code == 404, (suffix, who)
            assert strip(real) == strip(none), (suffix, who)
    # the debug view is admin-only: another workspace's admin gets the same 404 as for an unknown id, everyone else a 403
    real = await other.req("GET", f"/v1/context/{cid}/debug", "admin", key="")
    none = await other.req("GET", f"/v1/context/{ghost}/debug", "admin", key="")
    assert real.status_code == none.status_code == 404 and strip(real) == strip(none)
    assert (await t.req("GET", f"/v1/context/{cid}/debug", "agent", key="")).status_code == 403
    # a memory's usage in contexts: admin only; another workspace's admin cannot probe this workspace's memory ids
    real = await other.req("GET", f"/v1/memories/{ids['business']}/usage", "admin", key="")
    none = await other.req("GET", f"/v1/memories/{ghost}/usage", "admin", key="")
    assert real.status_code == none.status_code == 404 and strip(real) == strip(none)
    # the listing shows each caller only its own builds (an admin sees the workspace's), and nothing of another workspace's
    assert [c["id"] for c in (await other.req("GET", "/v1/context", "admin", key="")).json()["items"]] == []
    assert [c["id"] for c in (await t.req("GET", "/v1/context", "agent2", key="")).json()["items"]] == []
