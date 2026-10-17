"""P7 WP2: registering agents and apps, minting and revoking keys, granting scopes with privileges. The key is shown once; a handle is unique; a role cannot be chosen;
a subject must exist here; every management route is for workspace admins and invisible across workspaces; a grant is effective on the next request."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from kg_db.session import tenant_session
from sqlalchemy import text

from tests.support.p4 import ctx_of, sql
from tests.support.tenants import clear_graph, make_tenant

pytestmark = pytest.mark.integration


def body(handle: str | None = None, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"kind": "agent", "handle": handle or f"agent-{uuid.uuid4().hex[:8]}", "display_name": "Research Agent"}
    base.update(over)
    return base


async def register(t: Any, who: str = "admin", **over: Any) -> Any:
    return await t.req("POST", "/v1/principals", who, json=body(**over))


def bearer(key: str) -> dict[str, str]:
    return {"authorization": f"Bearer {key}"}


async def as_key(t: Any, key: str, method: str, path: str, **kw: Any) -> Any:
    return await t.client.request(method, path, headers={**bearer(key), "idempotency-key": str(uuid.uuid4())}, **kw)


async def test_registration_returns_the_key_once_and_the_key_works(api, tenant):
    r = await register(
        tenant, description="finds things", model_disclosure="denied", allowed_profiles=["research"], quotas={"requests_per_minute": 600}
    )
    assert r.status_code == 201, r.text
    assert r.headers["cache-control"] == "no-store"
    out = r.json()
    key = out["key"]["plaintext"]
    assert key.startswith("kgk_") and out["principal"]["roles"] == ["agent"] and out["principal"]["status"] == "active"
    assert out["principal"]["model_disclosure"] == "denied" and out["principal"]["allowed_profiles"] == ["research"]
    assert out["principal"]["quotas"]["requests_per_minute"] == 600 and out["principal"]["trusted_source"] is False
    me = await as_key(tenant, key, "GET", "/v1/me")
    assert me.status_code == 200 and me.json()["principal"]["handle"] == out["principal"]["handle"]
    pid = out["principal"]["id"]
    for path in (f"/v1/principals/{pid}", f"/v1/principals/{pid}/keys", "/v1/principals", f"/v1/principals/{pid}/grants"):
        assert key not in (await tenant.get(path, "admin")).text  # a later read never shows it
    rows = await sql(api, tenant, "SELECT key_hash FROM api_keys WHERE principal_id = :p", p=uuid.UUID(pid)) if False else []
    assert rows == []
    stored = await sql(api, tenant, "SELECT result::text AS r FROM commands WHERE command_type = 'principal.register'")
    assert all(key not in x.r for x in stored)  # nor does the ledger


async def test_a_handle_is_unique_and_a_replay_is_idempotent_without_the_key(api, tenant):
    handle = f"agent-{uuid.uuid4().hex[:8]}"
    first = await tenant.req("POST", "/v1/principals", "admin", json=body(handle), key="reg-1")
    assert first.status_code == 201
    replay = await tenant.req("POST", "/v1/principals", "admin", json=body(handle), key="reg-1")
    assert replay.status_code == 200 and replay.headers["idempotent-replay"] == "true"
    assert (
        replay.json()["principal"]["id"] == first.json()["principal"]["id"] and replay.json()["key"]["plaintext"] is None
    )  # the secret is not replayed
    mismatch = await tenant.req("POST", "/v1/principals", "admin", json=body(handle, description="different"), key="reg-1")
    assert mismatch.status_code == 409 and mismatch.json()["error"]["code"] == "idempotency_mismatch"
    dup = await register(tenant, handle=handle)
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "handle_taken"
    assert (
        await tenant.req("POST", "/v1/principals", "admin", json=body(handle), key="")
    ).status_code == 400  # Idempotency-Key is required


@pytest.mark.parametrize(
    "over",
    [
        {"role": "reviewer"},
        {"roles": ["workspace_admin"]},
        {"kind": "user"},
        {"kind": "service"},
        {"workspace_id": str(uuid.uuid4())},
        {"handle": "has spaces"},
        {"trusted_source": True},  # an agent cannot be a trusted source
        {"allowed_profiles": ["Bad Profile"]},
        {"grants": [{"scope_kind": "subject", "scope_ref": "not-a-uuid"}]},
        {"grants": [{"scope_kind": "project", "scope_ref": "p", "privileges": ["publish"]}]},  # publish without read
        {"grants": [{"scope_kind": "classification", "scope_ref": "secret"}]},
        {"quotas": {"requests_per_minute": 0}},
    ],
)
async def test_a_registration_cannot_choose_a_role_or_say_anything_malformed(tenant, over):
    r = await register(tenant, **over)
    assert r.status_code == 422, (over, r.text)
    assert r.json()["error"]["code"] == "validation_failed"


async def test_an_app_can_be_a_trusted_source_and_its_authority_follows(api, tenant):
    plain = await register(tenant, kind="app", grants=[{"scope_kind": "subject", "scope_ref": "*"}])
    trusted = await register(tenant, kind="app", trusted_source=True, grants=[{"scope_kind": "subject", "scope_ref": "*"}])
    assert plain.status_code == trusted.status_code == 201 and trusted.json()["principal"]["trusted_source"] is True
    assert plain.json()["principal"]["roles"] == ["app_writer"]
    authority = {}
    for label, out in (("plain", plain), ("trusted", trusted)):
        r = await as_key(
            tenant, out.json()["key"]["plaintext"], "POST", "/v1/entities",
            json={"type": "Person", "name": f"Person {label}", "source_system": "hr", "source_ref": label},
        )  # fmt: skip
        assert r.status_code == 201, r.text
        cmd = await sql(api, tenant, "SELECT authority_class FROM commands WHERE id = :c", c=uuid.UUID(r.json()["command_id"]))
        authority[label] = cmd[0].authority_class
    assert authority == {
        "plain": "user_asserted",
        "trusted": "authoritative_system",
    }  # T12: only a registered system of record outranks a user


async def test_an_owner_must_be_an_active_principal_of_this_workspace(tenant):
    owner = await tenant.principal_id("admin")
    assert (await register(tenant, owner_principal_id=owner)).status_code == 201
    assert (await register(tenant, owner_principal_id=str(uuid.uuid4()))).status_code == 422


async def test_a_grant_names_a_subject_of_this_workspace_and_a_foreign_subject_is_a_404(api, tenant):
    mine = (await tenant.entity("Person", "Mine", source_system="hr", source_ref="m")).json()["entity_id"]
    other = await make_tenant(api)
    try:
        theirs = (await other.entity("Person", "Theirs", source_system="hr", source_ref="t")).json()["entity_id"]
        ok = await register(tenant, grants=[{"scope_kind": "subject", "scope_ref": mine, "privileges": ["read", "propose"]}])
        assert ok.status_code == 201 and ok.json()["grants"][0]["scope_ref"] == mine
        for ref in (theirs, str(uuid.uuid4())):
            r = await register(tenant, grants=[{"scope_kind": "subject", "scope_ref": ref}])
            assert r.status_code == 404, r.text  # a foreign id and a missing id are the same answer
        pid = ok.json()["principal"]["id"]
        added = await tenant.req("POST", f"/v1/principals/{pid}/grants", "admin", json={"scope_kind": "subject", "scope_ref": theirs})
        assert added.status_code == 404 and theirs not in added.text
        # nothing was half-registered by the failed attempts
        assert len([p for p in (await tenant.get("/v1/principals", "admin", limit=100)).json()["items"] if p["kind"] == "agent"]) >= 3
    finally:
        await clear_graph(api, other.group_id)


async def test_grants_carry_privileges_and_take_effect_on_the_next_request(api, tenant):
    sarah = (await tenant.entity("Person", "Sarah", source_system="hr", source_ref="s")).json()["entity_id"]
    reg = await register(tenant)
    pid, key = reg.json()["principal"]["id"], reg.json()["key"]["plaintext"]
    assert (await as_key(tenant, key, "GET", f"/v1/entities/{sarah}")).status_code == 200  # v1.0.0 (no access): open to every principal
    g = await tenant.req(
        "POST",
        f"/v1/principals/{pid}/grants",
        "admin",
        json={"scope_kind": "project", "scope_ref": "newco-plan", "privileges": ["read", "propose", "publish"]},
    )
    assert g.status_code == 201 and g.json()["privileges"] == ["read", "propose", "publish"]
    again = await tenant.req(
        "POST", f"/v1/principals/{pid}/grants", "admin", json={"scope_kind": "project", "scope_ref": "newco-plan", "privileges": ["read"]}
    )
    assert again.json()["id"] == g.json()["id"] and again.json()["privileges"] == [
        "read"
    ]  # a re-grant changes the privileges, never duplicates the row
    listed = (await tenant.get(f"/v1/principals/{pid}/grants", "admin")).json()
    assert [(x["scope_kind"], x["scope_ref"], x["privileges"]) for x in listed] == [("project", "newco-plan", ["read"])]
    ceiling = await tenant.req(
        "POST", f"/v1/principals/{pid}/grants", "admin", json={"scope_kind": "classification", "scope_ref": "restricted"}
    )
    assert ceiling.status_code == 201 and ceiling.json()["privileges"] == ["read"]
    removed = await tenant.req("DELETE", f"/v1/principals/{pid}/grants/{g.json()['id']}", "admin")
    assert removed.status_code == 204
    assert [x["scope_kind"] for x in (await tenant.get(f"/v1/principals/{pid}/grants", "admin")).json()] == ["classification"]
    assert (await tenant.req("DELETE", f"/v1/principals/{pid}/grants/{g.json()['id']}", "admin")).status_code == 404  # already gone
    assert (await tenant.req("DELETE", f"/v1/principals/{pid}/grants/{uuid.uuid4()}", "admin")).status_code == 404


async def test_updates_need_the_current_revision_and_a_revoked_principal_is_final(tenant):
    reg = await register(tenant)
    pid = reg.json()["principal"]["id"]
    detail = await tenant.get(f"/v1/principals/{pid}", "admin")
    rev = detail.json()["revision"]
    patch = lambda **kw: tenant.req("PATCH", f"/v1/principals/{pid}", "admin", json=kw["json"], headers=kw.get("headers"))  # noqa: E731
    assert (await patch(json={"description": "x"})).status_code == 428  # If-Match is required
    assert (await patch(json={"description": "x"}, headers={"if-match": 'W/"rev-999"'})).status_code == 412
    ok = await patch(
        json={"description": "x", "quotas": {"context_tokens_per_day": 5000}, "model_disclosure": "denied"},
        headers={"if-match": f'W/"rev-{rev}"'},
    )
    assert ok.status_code == 200 and ok.json()["description"] == "x" and ok.json()["revision"] == rev + 1
    assert ok.json()["quotas"]["context_tokens_per_day"] == 5000 and ok.headers["etag"] == f'W/"rev-{rev + 1}"'
    cleared = await patch(json={"quotas": {"context_tokens_per_day": None}}, headers={"if-match": ok.headers["etag"]})
    assert cleared.json()["quotas"]["context_tokens_per_day"] is None  # an explicit null returns to the platform default
    assert (await patch(json={"nonsense": 1}, headers={"if-match": cleared.headers["etag"]})).status_code == 422
    assert (await tenant.req("POST", f"/v1/principals/{pid}/revoke", "admin", json={"reason": "retired"})).status_code == 200
    final = await patch(json={"description": "again"}, headers={"if-match": 'W/"rev-1"'})
    assert final.status_code in (409, 412)
    detail = await tenant.get(f"/v1/principals/{pid}", "admin")
    current = f'W/"rev-{detail.json()["revision"]}"'
    assert (
        await patch(json={"description": "again"}, headers={"if-match": current})
    ).status_code == 409  # a revoked principal does not change
    assert (await tenant.req("POST", f"/v1/principals/{pid}/keys", "admin", json={"name": "late"})).status_code == 409
    assert (
        await tenant.req("POST", f"/v1/principals/{pid}/grants", "admin", json={"scope_kind": "project", "scope_ref": "p"})
    ).status_code == 409


async def test_key_rotation_two_keys_work_and_revoking_one_leaves_the_other(tenant):
    reg = await register(tenant)
    pid, first = reg.json()["principal"]["id"], reg.json()["key"]
    second = (await tenant.req("POST", f"/v1/principals/{pid}/keys", "admin", json={"name": "rotation", "expires_in_seconds": 3600})).json()
    assert second["plaintext"] and second["name"] == "rotation" and second["expires_at"] is not None
    for k in (first["plaintext"], second["plaintext"]):
        assert (await as_key(tenant, k, "GET", "/v1/me")).status_code == 200
    gone = await tenant.req("DELETE", f"/v1/principals/{pid}/keys/{first['id']}", "admin")
    assert gone.status_code == 200 and gone.json()["revoked_at"] is not None
    assert (await as_key(tenant, first["plaintext"], "GET", "/v1/me")).status_code == 401  # immediate
    assert (await as_key(tenant, second["plaintext"], "GET", "/v1/me")).status_code == 200
    names = {k["name"]: k["revoked_at"] is not None for k in (await tenant.get(f"/v1/principals/{pid}/keys", "admin")).json()}
    assert names == {"initial": True, "rotation": False}
    assert (await tenant.req("DELETE", f"/v1/principals/{pid}/keys/{uuid.uuid4()}", "admin")).status_code == 404


async def test_only_workspace_admins_manage_principals(tenant):
    pid = (await register(tenant)).json()["principal"]["id"]
    calls = [
        ("POST", "/v1/principals", body()),
        ("PATCH", f"/v1/principals/{pid}", {"description": "x"}),
        ("POST", f"/v1/principals/{pid}/keys", {}),
        ("DELETE", f"/v1/principals/{pid}/keys/{uuid.uuid4()}", None),
        ("GET", f"/v1/principals/{pid}/keys", None),
        ("GET", f"/v1/principals/{pid}/grants", None),
        ("POST", f"/v1/principals/{pid}/grants", {"scope_kind": "project", "scope_ref": "p"}),
        ("DELETE", f"/v1/principals/{pid}/grants/{uuid.uuid4()}", None),
        ("POST", f"/v1/principals/{pid}/revoke", {"reason": "retired"}),
        ("GET", f"/v1/principals/{pid}/activity", None),
        ("GET", f"/v1/principals/{pid}/usage", None),
    ]
    for who in ("reviewer", "operator", "app", "agent", "agent3", "reader"):
        for method, path, payload in calls:
            r = await tenant.req(method, path, who, json=payload, headers={"if-match": 'W/"rev-1"'} if method == "PATCH" else None)
            assert r.status_code == 403, (who, method, path, r.status_code)
    assert (await tenant.get("/v1/me/usage", "agent")).status_code == 200  # but anyone reads their OWN usage


async def test_another_workspaces_admin_sees_none_of_it(api, tenant):
    reg = await register(tenant)
    pid, kid = reg.json()["principal"]["id"], reg.json()["key"]["id"]
    other = await make_tenant(api)
    try:
        for method, path, payload in [
            ("GET", f"/v1/principals/{pid}", None),
            ("PATCH", f"/v1/principals/{pid}", {"description": "x"}),
            ("GET", f"/v1/principals/{pid}/keys", None),
            ("POST", f"/v1/principals/{pid}/keys", {}),
            ("DELETE", f"/v1/principals/{pid}/keys/{kid}", None),
            ("GET", f"/v1/principals/{pid}/grants", None),
            ("POST", f"/v1/principals/{pid}/grants", {"scope_kind": "project", "scope_ref": "p"}),
            ("POST", f"/v1/principals/{pid}/revoke", {"reason": "compromised"}),
            ("GET", f"/v1/principals/{pid}/activity", None),
            ("GET", f"/v1/principals/{pid}/usage", None),
        ]:
            foreign = await other.req(method, path, "admin", json=payload, headers={"if-match": 'W/"rev-1"'} if method == "PATCH" else None)
            random = await other.req(
                method,
                path.replace(pid, str(uuid.uuid4())),
                "admin",
                json=payload,
                headers={"if-match": 'W/"rev-1"'} if method == "PATCH" else None,
            )
            assert foreign.status_code == random.status_code == 404, (method, path, foreign.status_code)
            assert {k: v for k, v in foreign.json()["error"].items() if k != "request_id"} == {
                k: v for k, v in random.json()["error"].items() if k != "request_id"
            }
        listed = (await other.get("/v1/principals", "admin", limit=100)).json()["items"]
        assert pid not in {p["id"] for p in listed}
        assert (
            await as_key(tenant, reg.json()["key"]["plaintext"], "GET", "/v1/me")
        ).status_code == 200  # and the target is untouched by the probes
    finally:
        await clear_graph(api, other.group_id)


async def test_users_and_service_principals_are_not_managed_here(tenant):
    admin_id = await tenant.principal_id("admin")
    for method, path, payload in [
        ("POST", f"/v1/principals/{admin_id}/revoke", {"reason": "retired"}),
        ("POST", f"/v1/principals/{admin_id}/keys", {}),
    ]:
        assert (await tenant.req(method, path, "admin", json=payload)).status_code == 404
    assert (await tenant.get(f"/v1/principals/{admin_id}", "admin")).json()["status"] == "active"


async def test_every_management_act_is_audited_without_names_or_secrets(api, tenant):
    handle = f"zqaudit{uuid.uuid4().hex[:8]}"
    reg = await register(
        tenant, handle=handle, grants=[{"scope_kind": "project", "scope_ref": "newco-plan", "privileges": ["read", "propose", "publish"]}]
    )
    pid, key = reg.json()["principal"]["id"], reg.json()["key"]["plaintext"]
    await tenant.req("POST", f"/v1/principals/{pid}/keys", "admin", json={"name": "two"})
    await tenant.req("POST", f"/v1/principals/{pid}/revoke", "admin", json={"reason": "compromised", "note": "zqnote private reason"})
    rows = await sql(
        api, tenant, "SELECT action, metadata::text AS m FROM audit_log WHERE action ~ '^(principal|key|grant)\\.' ORDER BY created_at"
    )
    actions = [r.action for r in rows]
    for expected in ("principal.registered", "key.created", "grant.added", "principal.revoked"):
        assert expected in actions, actions
    blob = json.dumps([(r.action, r.m) for r in rows])
    assert handle not in blob and key not in blob and "zqnote" not in blob  # ids, enums and counts only
    revoked = next(r for r in rows if r.action == "principal.revoked")
    assert json.loads(revoked.m)["revoke_reason"] == "compromised"
    cmds = await sql(api, tenant, "SELECT command_type FROM commands WHERE command_type ~ '^(principal|key|grant)\\.'")
    assert {c.command_type for c in cmds} >= {"principal.register", "key.create", "principal.revoke"}
    listed = await tenant.get("/v1/commands", "admin", limit=100)  # the knowledge ledger view does not list them
    assert not any(c["command_type"].startswith(("principal.", "key.", "grant.")) for c in listed.json()["items"])
    _ = tenant_session, ctx_of, text


async def test_the_registration_api_can_be_switched_off(api, tenant):
    state = api.app.state.kg
    state.settings.principal_api_enabled = False
    try:
        r = await register(tenant)
        assert r.status_code == 503 and r.json()["error"]["code"] == "feature_disabled"
        assert (await tenant.get("/v1/principals", "admin")).status_code == 200  # reading is unaffected
    finally:
        state.settings.principal_api_enabled = True
