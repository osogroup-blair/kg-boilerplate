"""SEC-01 (start): zero cross-workspace reads. T-SEC-01..03, T-AUTH-HDR, T-AUTHZ."""

from __future__ import annotations

import time
import uuid

import jwt
import pytest
from kg_cli.seed import principal_id_for, workspace_id_for

pytestmark = [pytest.mark.integration, pytest.mark.security]

DEMO, OTHER = workspace_id_for("demo"), workspace_id_for("other_demo")


def same_shape(a, b) -> None:
    """Same status, headers of interest and body, except the per-request id."""
    assert a.status_code == b.status_code
    ea, eb = a.json()["error"], b.json()["error"]
    assert {k: v for k, v in ea.items() if k != "request_id"} == {k: v for k, v in eb.items() if k != "request_id"}


async def test_sec01_foreign_workspace_id_is_indistinguishable_from_a_random_id(api):
    h = api.h("other_demo/admin@other")
    foreign, random = (
        await api.client.get(f"/v1/workspaces/{DEMO}", headers=h),
        await api.client.get(f"/v1/workspaces/{uuid.uuid4()}", headers=h),
    )
    assert foreign.status_code == 404
    same_shape(foreign, random)
    assert str(DEMO) not in foreign.text and "demo" not in foreign.text.lower()


@pytest.mark.parametrize("handle", ["admin@demo", "agent_research", "agent_planning", "conversation_app"])
async def test_sec01_foreign_principal_id_is_indistinguishable_from_a_random_id(api, handle):
    h = api.h("other_demo/admin@other")
    foreign = await api.client.get(f"/v1/principals/{principal_id_for('demo', handle)}", headers=h)
    random = await api.client.get(f"/v1/principals/{uuid.uuid4()}", headers=h)
    assert foreign.status_code == 404
    same_shape(foreign, random)


async def test_sec01_own_ids_still_resolve(api):
    assert (
        await api.client.get(f"/v1/principals/{principal_id_for('other_demo', 'agent_other')}", headers=api.h("other_demo/admin@other"))
    ).status_code == 200
    assert (await api.client.get(f"/v1/workspaces/{OTHER}", headers=api.h("other_demo/agent_other"))).status_code == 200


async def test_sec02_lists_never_contain_foreign_rows(api):
    r = await api.client.get("/v1/principals", headers=api.h("other_demo/admin@other"))
    assert {i["handle"] for i in r.json()["items"]} == {"admin@other", "agent_other"}
    for forbidden in ("admin@demo", "agent_research", "agent_planning", "conversation_app", str(DEMO)):
        assert forbidden not in r.text
    await api.client.post("/v1/auth/token", headers=api.h("demo/agent_planning"))  # produce demo audit rows
    audit = await api.client.get("/v1/audit", headers=api.h("other_demo/admin@other"))
    assert audit.status_code == 200 and str(DEMO) not in audit.text and "agent_planning" not in audit.text
    demo_audit = (await api.client.get("/v1/audit", headers=api.h("demo/admin@demo"))).json()["items"]
    assert demo_audit and str(OTHER) not in str(demo_audit)


async def test_sec03_jwt_with_a_tampered_workspace_claim_is_rejected(api):
    good = await api.jwt("other_demo/agent_other")
    claims = jwt.decode(good, options={"verify_signature": False})
    # 1) payload edited but signature kept: signature check fails
    header, _payload, sig = good.split(".")
    forged = jwt.utils.base64url_encode(__import__("json").dumps({**claims, "workspace_id": str(DEMO)}).encode()).decode()
    assert (await api.client.get("/v1/me", headers={"authorization": f"Bearer {header}.{forged}.{sig}"})).status_code == 401
    # 2) even a correctly signed token whose workspace claim disagrees with the key's workspace is rejected (defence in depth)
    secret = api.settings.auth_jwt_secret.get_secret_value()
    signed = jwt.encode(
        {**claims, "workspace_id": str(DEMO), "iat": int(time.time()), "nbf": int(time.time()), "exp": int(time.time()) + 300},
        secret,
        algorithm="HS256",
    )
    r = await api.client.get("/v1/me", headers={"authorization": f"Bearer {signed}"})
    assert r.status_code == 401
    # 3) a token naming a principal that does not own the key
    other_principal = jwt.encode(
        {**claims, "sub": str(principal_id_for("demo", "admin@demo")), "exp": int(time.time()) + 300}, secret, algorithm="HS256"
    )
    assert (await api.client.get("/v1/me", headers={"authorization": f"Bearer {other_principal}"})).status_code == 401


@pytest.mark.parametrize(
    "extra",
    [
        {"headers": {"x-workspace-id": str(DEMO)}},
        {"params": {"workspace_id": str(DEMO)}},
        {"headers": {"x-workspace-id": str(DEMO), "x-tenant-id": str(DEMO)}, "params": {"workspace": "demo"}},
    ],
)
async def test_auth_hdr_workspace_comes_only_from_the_credential(api, extra):
    headers = {**api.h("other_demo/agent_other"), **extra.get("headers", {})}
    r = await api.client.get("/v1/me", headers=headers, params=extra.get("params"))
    assert r.status_code == 200 and r.json()["workspace"]["id"] == str(OTHER) and r.json()["workspace"]["slug"] == "other_demo"
    cur = await api.client.get("/v1/workspaces/current", headers=headers, params=extra.get("params"))
    assert cur.json()["id"] == str(OTHER)


@pytest.mark.parametrize(
    "who,path,expected",
    [
        ("demo/admin@demo", "/v1/principals", 200),
        ("demo/admin@demo", "/v1/audit", 200),
        ("demo/admin@demo", "/v1/operations/health", 200),
        ("demo/operator@demo", "/v1/principals", 403),
        ("demo/operator@demo", "/v1/audit", 403),
        ("demo/operator@demo", "/v1/operations/health", 200),
        ("demo/reviewer@demo", "/v1/principals", 403),
        ("demo/reviewer@demo", "/v1/operations/health", 403),
        ("demo/reviewer@demo", "/v1/workspaces/current", 200),
        ("demo/conversation_app", "/v1/audit", 403),
        ("demo/agent_planning", "/v1/principals", 403),
        ("demo/agent_planning", "/v1/workspaces/current", 200),
        ("demo/agent_planning", "/v1/me", 200),
        ("demo/agent_research", "/v1/operations/health", 403),
    ],
)
async def test_authz_role_matrix_over_http(api, who, path, expected):
    assert (await api.client.get(path, headers=api.h(who))).status_code == expected


async def test_authz_router_check_is_diagnostics_only(api):
    for who, expected in (("demo/agent_planning", 403), ("demo/reviewer@demo", 403)):
        assert (await api.client.post("/v1/operations/router-check", headers=api.h(who))).status_code == expected


async def test_requests_never_share_identity_across_sequential_calls(api):
    """Regression: the principal must not be cached anywhere shared between requests."""
    for _ in range(3):
        for who, ws in (
            ("demo/admin@demo", DEMO),
            ("other_demo/admin@other", OTHER),
            ("demo/agent_research", DEMO),
            ("other_demo/agent_other", OTHER),
        ):
            body = (await api.client.get("/v1/me", headers=api.h(who))).json()
            assert body["workspace"]["id"] == str(ws) and body["principal"]["handle"] == who.split("/", 1)[1]
