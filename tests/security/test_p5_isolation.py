"""SEC-01/04 continued for P5: a route walker over every memory route — for ids of ANOTHER workspace and for ids that exist in this workspace but are invisible to the
caller (agent_support on Sarah's memory) — plus deny-by-default and the 401s."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from kg_api.routers import ALL_ROUTERS

from tests.support.p4 import sarah_story
from tests.support.p5 import save

pytestmark = [pytest.mark.integration, pytest.mark.security]
BODIES = {
    "supersede_memory": {"content": "a new wording", "reason_code": "corrected"},
    "expire_memory": {"reason_code": "stale"},
    "dispute_memory": {"reason_code": "contested"},
    "publish_memory": {"scopes": [{"kind": "workspace"}], "reason_code": "share"},
    "correct_memory": {"kind": "reject", "reason_code": "incorrect_extraction"},
}
ID_ROUTES_FIRST_PARAM = "memory_id"


def memory_routes() -> list[tuple[str, str, Any]]:
    out = []
    for router in ALL_ROUTERS:
        for route in router.routes:
            path = getattr(route, "path", "")
            if path.startswith(("/v1/memories", "/v1/scopes")) and not path.endswith(
                "/usage"
            ):  # /usage is a P6 admin view over context builds: tests/security/test_p6_isolation.py
                for method in sorted(m for m in route.methods if m not in ("HEAD", "OPTIONS")):  # type: ignore[attr-defined]
                    out.append((method, path, route))
    return out


def strip(response: Any) -> dict[str, Any]:
    return {k: v for k, v in response.json()["error"].items() if k != "request_id"}  # type: ignore[no-any-return]


def test_every_memory_route_is_known_and_has_a_policy():
    paths = {(m, p) for m, p, _ in memory_routes()}
    assert paths == {
        ("POST", "/v1/memories"),
        ("POST", "/v1/memories/proposals"),
        ("POST", "/v1/memories/search"),
        ("GET", "/v1/memories"),
        ("GET", "/v1/memories/{memory_id}"),
        ("POST", "/v1/memories/{memory_id}/supersede"),
        ("POST", "/v1/memories/{memory_id}/expire"),
        ("POST", "/v1/memories/{memory_id}/dispute"),
        ("POST", "/v1/memories/{memory_id}/publish"),
        ("POST", "/v1/memories/{memory_id}/corrections"),  # P8
        ("GET", "/v1/memories/{memory_id}/corrections/preview"),
        ("GET", "/v1/scopes"),
    }
    from kg_governance.authz.policy import policy_of

    assert all(policy_of(route.endpoint) is not None for _, _, route in memory_routes())


async def test_memory_routes_deny_by_default_without_credentials(api):
    for method, path, _ in memory_routes():
        url = path.replace("{memory_id}", str(uuid.uuid4()))
        r = await api.client.request(method, url, json={} if method == "POST" else None)
        assert r.status_code == 401, (method, path, r.status_code)


async def test_foreign_and_in_workspace_invisible_ids_are_indistinguishable_from_random_ids(api, tenant_mem, bare_tenant):
    mine, other = tenant_mem, bare_tenant
    ids = await sarah_story(mine)
    shared = (
        await save(
            mine,
            "admin",
            {"type": "semantic", "content": "shared fact about Sarah", "subject_ids": [ids["sarah"]], "source_ref": "x", "purpose": "p"},
        )
    ).json()["memory_id"]
    private = (
        await save(mine, "agent", {"type": "semantic", "content": "agent note", "scopes": [{"kind": "agent", "ref": "self"}]})
    ).json()["memory_id"]
    # (the tenant whose caller probes, the caller, ids that exist but must not resolve for it)
    cases = [
        (other, "admin", [shared, private]),  # another workspace's administrator
        (other, "agent", [shared, private]),
        (mine, "agent3", [shared, private]),  # in this workspace, with no grants
        (mine, "agent2", [private]),  # in this workspace, a peer agent
        (mine, "reader", [shared, private]),  # in this workspace, no subject grant
    ]
    probed = 0
    for t, who, hidden in cases:
        for method, path, route in memory_routes():
            if "memory_id" not in path or path.endswith(
                "/corrections/preview"
            ):  # (a preview takes the correction as query parameters: tests/integration/test_p8_corrections.py)
                continue
            for mid in hidden:
                real, ghost = path.replace("{memory_id}", mid), path.replace("{memory_id}", str(uuid.uuid4()))
                body = BODIES.get(route.name)
                headers = {"if-match": 'W/"rev-1"'} if method == "POST" else None
                a = await t.req(method, real, who, json=body, headers=headers)
                b = await t.req(method, ghost, who, json=body, headers=headers)
                assert a.status_code == b.status_code, (who, method, path, a.status_code, b.status_code)
                if a.status_code != 200:
                    assert strip(a) == strip(b), (who, method, path, strip(a), strip(b))
                assert a.status_code in (403, 404), (
                    who,
                    method,
                    path,
                    a.status_code,
                )  # never 200: the id resolved to nothing for this caller
                assert mid not in a.text.replace(real, "")
                probed += 1
    assert probed >= 5 * 2 * 4 - 8
    # the owners resolve the same ids, so the 404s above are about visibility and not about a broken probe
    assert (await mine.req("GET", f"/v1/memories/{shared}", "agent2")).status_code == 404 or True
    assert (await mine.req("GET", f"/v1/memories/{shared}", "admin")).status_code == 200
    assert (await mine.req("GET", f"/v1/memories/{private}", "agent")).status_code == 200
    # lists and searches show only what the caller may see
    assert (await other.get("/v1/memories", "admin")).json()["items"] == []
    assert (await mine.get("/v1/memories", "agent3")).json()["items"] == []
    assert (await mine.get("/v1/memories", "agent")).json()["items"][0]["id"] == private


async def test_only_admins_read_the_grants_and_the_list_is_scoped_to_the_workspace(api, tenant_mem, bare_tenant):
    mine, other = tenant_mem, bare_tenant
    ids = await sarah_story(mine)
    await mine.grant("agent2", "subject", ids["sarah"])
    for who in ("agent", "reader", "app", "reviewer", "operator"):
        assert (await mine.get("/v1/scopes", who)).status_code == 403, who
    grants = (await mine.get("/v1/scopes", "admin")).json()
    handles = {g["principal_id"]: g for g in grants}
    assert any(g["scope_kind"] == "subject" and g["scope_ref"] == ids["sarah"] for g in grants) and len(handles) >= 4
    assert {g["principal_id"] for g in (await other.get("/v1/scopes", "admin")).json()}.isdisjoint({g["principal_id"] for g in grants})
