"""P9 WP7: OIDC for human users. An IdP-signed token becomes a `user` principal through the group mapping (provisioned on first sight, audited, never more than the mapping
says today, status re-read on every request); the console's authorization-code + PKCE login; the API-key break-glass rules in production."""

from __future__ import annotations

import base64
import hashlib
import secrets
import uuid

import pytest

from tests.conftest import PRODUCTION_SECRETS
from tests.support.idp import AUDIENCE, ISSUER, TestIdp, hs256_confusion_token
from tests.support.p4 import sql
from tests.support.p5 import owner_sql
from tests.support.p9 import replicas
from tests.support.tenants import make_tenant

pytestmark = pytest.mark.integration
REDIRECT = "https://console.test/callback"
OIDC = dict(auth_oidc_enabled=True, oidc_issuer=ISSUER, oidc_audience=AUDIENCE, oidc_client_id=AUDIENCE, oidc_redirect_uris=[REDIRECT])


def attach(replica, idp: TestIdp) -> None:
    """The replica's verifier talks to the in-process IdP instead of the network."""
    replica.state.authenticator.oidc._http = idp.client()


def grp(t, name: str) -> str:
    """Tests share one database, and a group that maps into two workspaces is (by design) ambiguous: every test's groups carry its workspace's slug."""
    return f"{name}@{t.slug}"


async def map_group(t, group: str, roles: list[str], *, who: str = "admin"):
    return await t.req("POST", "/v1/oidc-group-mappings", who, json={"group_name": grp(t, group), "roles": roles, "issuer": ISSUER})


def bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


async def test_a_mapped_token_becomes_a_provisioned_user_with_exactly_the_mapped_roles(api, tenant):
    t, idp = tenant, TestIdp()
    assert (await map_group(t, "kg-admins", ["workspace_admin"])).status_code == 201
    assert (await map_group(t, "kg-readers", ["reader"])).status_code == 201
    async with replicas(api, 1, **OIDC) as (r,):
        attach(r, idp)
        me = await r.client.get("/v1/me", headers=bearer(idp.sign("alice", groups=[grp(t, "kg-admins")])))
        assert me.status_code == 200, me.text
        body = me.json()
        assert body["principal"]["kind"] == "user" and body["principal"]["roles"] == ["workspace_admin"]
        assert body["workspace"]["slug"] == t.slug and body["principal"]["handle"].startswith("oidc-")
        pid = body["principal"]["id"]
        # provisioned once, audited
        assert [
            x.n
            for x in await sql(api, t, "SELECT count(*) AS n FROM audit_log WHERE workspace_id = :ws AND action = 'oidc.user_provisioned'")
        ] == [1]
        again = await r.client.get("/v1/me", headers=bearer(idp.sign("alice", groups=[grp(t, "kg-admins")])))
        assert again.json()["principal"]["id"] == pid
        assert (await sql(api, t, "SELECT count(*) AS n FROM audit_log WHERE workspace_id = :ws AND action = 'oidc.user_provisioned'"))[
            0
        ].n == 1
        # the admin role works; a token whose groups map only to `reader` does not open admin routes
        assert (await r.client.get("/v1/principals", headers=bearer(idp.sign("alice", groups=[grp(t, "kg-admins")])))).status_code == 200
        # the same person with fewer groups today holds fewer roles today (roles are recomputed from the mapping on every request)
        down = bearer(idp.sign("alice", groups=[grp(t, "kg-readers")]))
        assert (await r.client.get("/v1/me", headers=down)).json()["principal"]["roles"] == ["reader"]
        assert (await r.client.get("/v1/principals", headers=down)).status_code == 403
        assert (await sql(api, t, "SELECT count(*) AS n FROM audit_log WHERE workspace_id = :ws AND action = 'oidc.roles_changed'"))[
            0
        ].n >= 1
        # no mapped group at all: no principal, one generic 401
        for groups in ([], ["unmapped"]):
            r401 = await r.client.get("/v1/me", headers=bearer(idp.sign("alice", groups=groups)))
            assert r401.status_code == 401 and r401.json()["error"]["code"] == "unauthenticated"


async def test_bad_tokens_get_one_generic_401(api, tenant):
    t, idp = tenant, TestIdp()
    idp.default_groups = [grp(t, "kg-admins")]
    await map_group(t, "kg-admins", ["workspace_admin"])
    async with replicas(api, 1, **OIDC) as (r,):
        attach(r, idp)
        for token in (
            idp.sign("a", ttl=-120),
            idp.sign("a", aud="someone-else"),
            idp.sign("a", iss="https://evil.example"),
            hs256_confusion_token(idp, groups=idp.default_groups),
            idp.sign("a")[:-6] + "AAAAAA",
        ):
            resp = await r.client.get("/v1/me", headers=bearer(token))
            assert resp.status_code == 401 and resp.json()["error"]["message"] == "Authentication failed", token[:20]
        assert (await r.client.get("/v1/me", headers=bearer(idp.sign("a")))).status_code == 200


async def test_a_group_that_maps_into_two_workspaces_needs_the_workspace_claim(api, tenant):
    t, idp = tenant, TestIdp()
    other = await make_tenant(api)
    shared = f"shared-staff-{uuid.uuid4().hex[:8]}"
    for tenant_, role in ((t, "reader"), (other, "reviewer")):
        made = await tenant_.req("POST", "/v1/oidc-group-mappings", "admin", json={"group_name": shared, "roles": [role], "issuer": ISSUER})
        assert made.status_code == 201
    async with replicas(api, 1, **OIDC) as (r,):
        attach(r, idp)
        ambiguous = await r.client.get("/v1/me", headers=bearer(idp.sign("dana", groups=[shared])))
        assert ambiguous.status_code == 401  # never guess which workspace
        mine = await r.client.get("/v1/me", headers=bearer(idp.sign("dana", groups=[shared], kg_workspace=t.slug)))
        theirs = await r.client.get("/v1/me", headers=bearer(idp.sign("dana", groups=[shared], kg_workspace=other.slug)))
        assert mine.json()["workspace"]["slug"] == t.slug and mine.json()["principal"]["roles"] == ["reader"]
        assert theirs.json()["workspace"]["slug"] == other.slug and theirs.json()["principal"]["roles"] == ["reviewer"]
        assert mine.json()["principal"]["id"] != theirs.json()["principal"]["id"]  # one identity per workspace
        # naming a workspace the groups do not map into gets nothing
        assert (await r.client.get("/v1/me", headers=bearer(idp.sign("dana", groups=[shared], kg_workspace="nope")))).status_code == 401


async def test_revoking_the_principal_cuts_the_user_off_on_the_next_request(api, tenant):
    t, idp = tenant, TestIdp()
    await map_group(t, "kg-admins", ["workspace_admin"])
    async with replicas(api, 1, **OIDC) as (r,):
        attach(r, idp)
        token = bearer(idp.sign("erin", groups=[grp(t, "kg-admins")]))
        pid = (await r.client.get("/v1/me", headers=token)).json()["principal"]["id"]
        await owner_sql("UPDATE principals SET status = 'revoked', revoked_at = now() WHERE id = :p", p=uuid.UUID(pid))
        assert (await r.client.get("/v1/me", headers=token)).status_code == 401


async def test_key_rollover_at_the_idp_needs_no_restart(api, tenant):
    t, idp = tenant, TestIdp()
    idp.default_groups = [grp(t, "kg-admins")]
    await map_group(t, "kg-admins", ["workspace_admin"])
    async with replicas(api, 1, **OIDC) as (r,):
        attach(r, idp)
        assert (await r.client.get("/v1/me", headers=bearer(idp.sign("fay")))).status_code == 200
        new = idp.rotate("k-new")
        r.state.authenticator.oidc._last_forced = float("-inf")
        assert (await r.client.get("/v1/me", headers=bearer(idp.sign("fay", key=new)))).status_code == 200


async def test_only_admins_manage_the_mapping_and_it_validates(api, tenant):
    t = tenant
    for who in ("reviewer", "operator", "app", "agent", "reader"):
        assert (await map_group(t, "g", ["reader"], who=who)).status_code == 403, who
        assert (await t.get("/v1/oidc-group-mappings", who)).status_code == 403, who
    assert (await map_group(t, "g", ["root"])).status_code == 422  # not a role
    assert (await map_group(t, "g", [])).status_code == 422
    made = await map_group(t, "ops", ["operator", "reader"])
    assert made.status_code == 201 and made.json()["roles"] == ["operator", "reader"] and made.json()["issuer"] == ISSUER
    # without an issuer in the request and none configured, there is nothing to map from
    assert (await t.req("POST", "/v1/oidc-group-mappings", "admin", json={"group_name": "x", "roles": ["reader"]})).status_code == 422
    listed = (await t.get("/v1/oidc-group-mappings", "admin")).json()
    assert any(m["group_name"] == grp(t, "ops") for m in listed)
    gone = await t.req("DELETE", f"/v1/oidc-group-mappings/{made.json()['id']}", "admin")
    assert gone.status_code == 204
    assert (await t.req("DELETE", f"/v1/oidc-group-mappings/{made.json()['id']}", "admin")).status_code == 404
    # another workspace cannot see or delete this one's mappings
    other = await make_tenant(api)
    assert (await other.get("/v1/oidc-group-mappings", "admin")).json() == []


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    return verifier, base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


async def test_console_login_authorization_code_with_pkce_and_a_rotating_refresh_cookie(api, tenant):
    t, idp = tenant, TestIdp()
    await map_group(t, "kg-admins", ["workspace_admin"])
    async with replicas(api, 1, **OIDC) as (r,):
        attach(r, idp)
        cfg = (await r.client.get("/v1/auth/oidc/config")).json()
        assert (
            cfg["enabled"]
            and cfg["client_id"] == AUDIENCE
            and cfg["authorization_endpoint"].endswith("/auth")
            and cfg["redirect_uris"] == [REDIRECT]
        )
        verifier, _ = pkce()
        code = idp.issue_code(verifier=verifier, redirect_uri=REDIRECT, sub="gina", groups=[grp(t, "kg-admins")])
        # a redirect URI that is not registered, a wrong verifier and a replayed code are all refused
        assert (
            await r.client.post(
                "/v1/auth/oidc/callback", json={"code": code, "code_verifier": verifier, "redirect_uri": "https://evil.test/cb"}
            )
        ).status_code == 422
        assert (
            await r.client.post(
                "/v1/auth/oidc/callback", json={"code": code, "code_verifier": secrets.token_urlsafe(48), "redirect_uri": REDIRECT}
            )
        ).status_code == 401
        code = idp.issue_code(verifier=verifier, redirect_uri=REDIRECT, sub="gina", groups=[grp(t, "kg-admins")])
        ok = await r.client.post("/v1/auth/oidc/callback", json={"code": code, "code_verifier": verifier, "redirect_uri": REDIRECT})
        assert ok.status_code == 200, ok.text
        session = ok.json()
        assert session["roles"] == ["workspace_admin"] and session["expires_in"] > 0
        set_cookie = ok.headers["set-cookie"].lower()
        assert (
            "kg_oidc_refresh=" in set_cookie
            and "httponly" in set_cookie
            and "samesite=strict" in set_cookie
            and "path=/v1/auth/oidc" in set_cookie
        )
        assert session["id_token"] not in set_cookie  # only the refresh token is in the cookie
        assert (await r.client.get("/v1/me", headers=bearer(session["id_token"]))).status_code == 200
        replay = await r.client.post("/v1/auth/oidc/callback", json={"code": code, "code_verifier": verifier, "redirect_uri": REDIRECT})
        assert replay.status_code == 401
        # refresh: needs the cookie AND the console header; the cookie rotates
        cookie = ok.cookies.get("kg_oidc_refresh")
        r.client.cookies.clear()  # the test client keeps a jar like a browser; the explicit cases below pass the cookie by hand
        assert (await r.client.post("/v1/auth/oidc/refresh", cookies={"kg_oidc_refresh": cookie})).status_code == 401  # no header
        assert (await r.client.post("/v1/auth/oidc/refresh", headers={"x-requested-with": "kg-console"})).status_code == 401  # no cookie
        fresh = await r.client.post(
            "/v1/auth/oidc/refresh", cookies={"kg_oidc_refresh": cookie}, headers={"x-requested-with": "kg-console"}
        )
        assert fresh.status_code == 200 and fresh.json()["id_token"]
        old = await r.client.post("/v1/auth/oidc/refresh", cookies={"kg_oidc_refresh": cookie}, headers={"x-requested-with": "kg-console"})
        assert old.status_code == 401  # a used refresh token is dead
        r.client.cookies.clear()
        out = await r.client.post("/v1/auth/oidc/logout")
        assert out.status_code == 204 and "kg_oidc_refresh" in out.headers["set-cookie"]


async def test_oidc_is_off_by_default(api):
    async with replicas(api, 1) as (r,):
        assert (await r.client.get("/v1/auth/oidc/config")).json() == {
            "enabled": False,
            "issuer": None,
            "client_id": None,
            "authorization_endpoint": None,
            "scopes": [],
            "redirect_uris": [],
        }
        assert (
            await r.client.post("/v1/auth/oidc/callback", json={"code": "x", "code_verifier": "v" * 43, "redirect_uri": REDIRECT})
        ).status_code == 404
        # and an OIDC-looking token is just an invalid token
        assert (await r.client.get("/v1/me", headers=bearer(TestIdp().sign("a")))).status_code == 401


PROD = dict(
    env="production",
    **{**PRODUCTION_SECRETS, "deletion_journal_dir": "/tmp/kg-prod-journal"},  # noqa: S108
    cors_allow_origins=["https://console.test"],
)


async def test_in_production_a_humans_api_key_is_break_glass_only_and_every_use_is_audited(api, tenant):
    t = tenant
    # the seeded keys were minted with the TEST pepper, so use the replica with the same pepper in production mode
    prod = {
        **PROD,
        "auth_key_pepper": api.settings.auth_key_pepper.get_secret_value(),
        "auth_jwt_secret": api.settings.auth_jwt_secret.get_secret_value(),
    }
    async with replicas(api, 1, **prod) as (denied,):
        assert (await denied.client.get("/v1/me", headers=t.h("admin"))).status_code == 401  # a user key, break-glass not allowed
        assert (await denied.client.get("/v1/me", headers=t.h("agent"))).status_code == 200  # agents and apps keep their keys
        assert (await denied.client.get("/v1/me", headers=t.h("app"))).status_code == 200
    async with replicas(api, 1, allow_breakglass_keys=True, **prod) as (allowed,):
        before = (await sql(api, t, "SELECT count(*) AS n FROM audit_log WHERE workspace_id = :ws AND action = 'breakglass.used'"))[0].n
        for _ in range(2):
            assert (await allowed.client.get("/v1/me", headers=t.h("admin"))).status_code == 200
        after = (await sql(api, t, "SELECT count(*) AS n FROM audit_log WHERE workspace_id = :ws AND action = 'breakglass.used'"))[0].n
        assert after - before == 2
        assert (await allowed.client.get("/v1/me", headers=t.h("agent"))).status_code == 200
        assert (await sql(api, t, "SELECT count(*) AS n FROM audit_log WHERE workspace_id = :ws AND action = 'breakglass.used'"))[
            0
        ].n == after  # not for agents
    # development keeps working with no flag at all
    assert (await api.client.get("/v1/me", headers=t.h("admin"))).status_code == 200
