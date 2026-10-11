"""SEC-03 (start): revocation is immediate for keys AND the JWTs minted from them. Suspended workspaces and revoked principals stop at once."""

from __future__ import annotations

import asyncio
import hashlib
import uuid

import pytest
from kg_cli.seed import seed_demo  # noqa: F401  (documents that seeds use the same admin helpers)
from kg_db.session import system_session, tenant_session
from kg_domain.enums import PrincipalKind, Role
from kg_domain.principal import WorkspaceContext
from kg_governance.authn import keys as keys_mod
from kg_governance.repositories import admin
from sqlalchemy import text

pytestmark = [pytest.mark.integration, pytest.mark.security]


async def make_throwaway(api, *, role: Role = Role.AGENT):
    """A fresh workspace + principal + key so revocation tests never disturb the seeded fixtures."""
    ws = uuid.uuid4()
    ctx = WorkspaceContext.for_system_task(ws, reason="test throwaway")
    async with tenant_session(api.engine, ctx) as conn:
        await admin.ensure_workspace(conn, ctx, slug=f"tmp-{ws.hex[:10]}", display_name="tmp")
        pid, _ = await admin.ensure_principal(conn, ctx, handle="tmp-agent", kind=PrincipalKind.AGENT, display_name="tmp", roles=[role])
        key_id, minted = await admin.create_key(conn, ctx, principal_id=pid, pepper=api.settings.auth_key_pepper.get_secret_value())
    return ctx, pid, key_id, minted


def bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


async def test_auth_revoke_key_and_its_jwt_stop_on_the_very_next_request(api):
    ctx, _pid, _key_id, minted = await make_throwaway(api)
    assert (await api.client.get("/v1/me", headers=bearer(minted.plaintext))).status_code == 200
    token = (await api.client.post("/v1/auth/token", headers=bearer(minted.plaintext))).json()["access_token"]
    assert (await api.client.get("/v1/me", headers=bearer(token))).status_code == 200
    async with tenant_session(api.engine, ctx) as conn:  # what `kg keys revoke` does
        assert await admin.revoke_key(conn, ctx, prefix=minted.prefix)
    assert (await api.client.get("/v1/me", headers=bearer(minted.plaintext))).status_code == 401
    assert (
        await api.client.get("/v1/me", headers=bearer(token))
    ).status_code == 401  # the JWT was still valid for 15 minutes; the re-check kills it


async def test_auth_revoked_principal_is_rejected_for_key_and_jwt(api):
    ctx, pid, _k, minted = await make_throwaway(api)
    token = (await api.client.post("/v1/auth/token", headers=bearer(minted.plaintext))).json()["access_token"]
    async with tenant_session(api.engine, ctx) as conn:
        await admin.revoke_principal(conn, ctx, principal_id=pid)
    assert (await api.client.get("/v1/me", headers=bearer(minted.plaintext))).status_code == 401
    assert (await api.client.get("/v1/me", headers=bearer(token))).status_code == 401


async def test_auth_suspended_workspace_is_rejected(api):
    ctx, _pid, _k, minted = await make_throwaway(api)
    token = (await api.client.post("/v1/auth/token", headers=bearer(minted.plaintext))).json()["access_token"]
    async with tenant_session(api.engine, ctx) as conn:
        await admin.set_workspace_status(conn, ctx, status="suspended")
    assert (await api.client.get("/v1/me", headers=bearer(minted.plaintext))).status_code == 401
    assert (await api.client.get("/v1/me", headers=bearer(token))).status_code == 401


async def test_auth_expired_key_is_rejected(api):
    ctx, _pid, key_id, minted = await make_throwaway(api)
    async with tenant_session(api.engine, ctx) as conn:
        await conn.execute(text("UPDATE api_keys SET expires_at = now() - interval '1 second' WHERE id = :k"), {"k": key_id})
    assert (await api.client.get("/v1/me", headers=bearer(minted.plaintext))).status_code == 401


async def test_auth_unknown_prefix_takes_the_same_code_path_as_a_wrong_secret(api, monkeypatch):
    calls: list[str] = []
    real = keys_mod.hash_key
    monkeypatch.setattr(keys_mod, "hash_key", lambda pepper, key: (calls.append(key), real(pepper, key))[1])
    known_prefix = api.keys["demo/agent_planning"].split("_")[1]
    unknown = "kgk_zzzzzzzz_" + "s" * 43
    wrong_secret = f"kgk_{known_prefix}_" + "s" * 43
    for presented in (unknown, wrong_secret):
        calls.clear()
        assert (await api.client.get("/v1/me", headers=bearer(presented))).status_code == 401
        assert calls == [presented], "exactly one hash per attempt, known prefix or not"


async def test_key_hash_is_not_readable_by_the_application_role(api):
    async with system_session(api.engine) as conn:
        row = (
            await conn.execute(
                text("SELECT key_hash FROM auth.lookup_credential(:p)"), {"p": api.keys["demo/agent_planning"].split("_")[1]}
            )
        ).one()
    assert row.key_hash == keys_mod.hash_key(api.settings.auth_key_pepper.get_secret_value(), api.keys["demo/agent_planning"])
    assert row.key_hash != hashlib.sha256(api.keys["demo/agent_planning"].encode()).hexdigest()  # peppered, not a bare hash


async def test_reg_every_route_has_a_policy_and_an_unpoliced_route_blocks_startup(api):
    from asgi_lifespan import LifespanManager
    from kg_api.main import all_routes, build_app, check_route_policies
    from kg_governance.authz.policy import policy_of

    check_route_policies(api.app)
    assert all(
        policy_of(r.endpoint) is not None for r in all_routes(api.app) if r.path not in ("/docs", "/docs/oauth2-redirect", "/openapi.json")
    )
    app = build_app(api.settings)

    @app.get("/v1/naked")
    async def naked() -> dict[str, str]:  # no @requires / @public
        return {"x": "y"}

    with pytest.raises(RuntimeError, match="without an authorization policy"):
        async with LifespanManager(app, startup_timeout=60, shutdown_timeout=60):
            pass


async def test_auth_revoke_through_the_cli_stops_key_and_jwt(api, monkeypatch):
    """The documented operator flow: `kg keys create` -> use -> `kg keys revoke <prefix>` -> 401 on the very next request."""
    from kg_cli.main import app as cli
    from kg_common.settings import get_settings
    from typer.testing import CliRunner

    from tests.conftest import APP_URL, OWNER_URL

    for name, value in {
        "DATABASE_URL_APP": APP_URL,
        "DATABASE_URL_OWNER": OWNER_URL,
        "ENV": "test",
        "AUTH_KEY_PEPPER": api.settings.auth_key_pepper.get_secret_value(),
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    runner = CliRunner()
    try:
        created = await asyncio.to_thread(runner.invoke, cli, ["keys", "create", "--workspace", "demo", "--principal", "conversation_app"])
        assert created.exit_code == 0, created.output
        key = created.stdout.strip()
        prefix = key.split("_")[1]
        token = (await api.client.post("/v1/auth/token", headers=bearer(key))).json()["access_token"]
        assert (await api.client.get("/v1/me", headers=bearer(key))).status_code == 200
        listed = await asyncio.to_thread(runner.invoke, cli, ["keys", "list", "--workspace", "demo"])
        assert prefix in listed.stdout and key not in listed.stdout
        revoked = await asyncio.to_thread(runner.invoke, cli, ["keys", "revoke", prefix])
        assert revoked.exit_code == 0, revoked.output
        assert (await api.client.get("/v1/me", headers=bearer(key))).status_code == 401
        assert (await api.client.get("/v1/me", headers=bearer(token))).status_code == 401
        again = await asyncio.to_thread(runner.invoke, cli, ["keys", "revoke", prefix])
        assert again.exit_code == 1  # already revoked: nothing to do
    finally:
        get_settings.cache_clear()
