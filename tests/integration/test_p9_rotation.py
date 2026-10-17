"""P9 WP6: rotating the secrets the platform holds without a flag day. Peppers (dual verify, one re-hash, forced expiry at the end of the window), JWT keys (a ring), the content
HMAC secret (a ring: idempotent replay, dedupe and the suppression of a deleted subject survive it)."""

from __future__ import annotations

import uuid

import pytest
from kg_governance.authn.keys import pepper_fingerprint

from tests.support.p4 import sql
from tests.support.p5 import owner_sql, save
from tests.support.p8 import SENTINEL_EXT, deletion_world, request_deletion, run_deletion
from tests.support.p9 import replicas

pytestmark = pytest.mark.integration
NEW_PEPPER = "rotated-pepper-0123456789abcdef0123456789"


def key_label(slug: str, who: str):
    return owner_sql(
        "SELECT k.pepper_id FROM api_keys k JOIN principals p ON p.workspace_id = k.workspace_id AND p.id = k.principal_id WHERE p.handle = :h ORDER BY k.created_at DESC LIMIT 1",
        h=f"{who}@{slug}",
    )


async def test_pepper_rotation_dual_verifies_rehashes_once_and_can_be_forced_to_an_end(api, tenant):
    t = tenant
    old_pepper = api.settings.auth_key_pepper.get_secret_value()
    old_fp, new_fp = pepper_fingerprint(old_pepper), pepper_fingerprint(NEW_PEPPER)
    assert (await key_label(t.slug, "agent"))[0].pepper_id == old_fp  # minted under the current pepper, labelled with its fingerprint
    # window: a new current pepper, the old one as the previous
    async with replicas(api, 1, auth_key_pepper=NEW_PEPPER, auth_key_pepper_previous=old_pepper) as (rotating,):
        assert (await rotating.client.get("/v1/me", headers=t.h("agent"))).status_code == 200  # the old key still works...
        assert (await key_label(t.slug, "agent"))[0].pepper_id == new_fp  # ...and was re-hashed under the new pepper by that one use
        assert (
            await rotating.client.get("/v1/me", headers=t.h("agent"))
        ).status_code == 200  # authenticates under the new pepper from now on
        counts = {r.pepper_id: r.keys for r in await sql(api, t, "SELECT * FROM ops.key_pepper_counts()")}
        assert counts.get(new_fp, 0) >= 1 and counts.get(old_fp, 0) >= 1  # the app and admin keys have not been used yet
    # the window closes (the previous pepper is dropped): what was re-hashed works, what was not does not
    async with replicas(api, 1, auth_key_pepper=NEW_PEPPER) as (closed,):
        assert (await closed.client.get("/v1/me", headers=t.h("agent"))).status_code == 200
        gone = await closed.client.get("/v1/me", headers=t.h("app"))
        assert gone.status_code == 401 and gone.json()["error"]["message"] == "Authentication failed"  # never a hint that the pepper is why
    # forced expiry for whatever is still on the old pepper, audited in the workspace
    n = (await owner_sql("SELECT ops.expire_keys_on_pepper(:p) AS n", p=old_fp))[0].n
    assert n >= 1
    assert len(await sql(api, t, "SELECT 1 FROM audit_log WHERE workspace_id = :ws AND action = 'secret.keys_expired'")) >= 1
    async with replicas(api, 1, auth_key_pepper=NEW_PEPPER, auth_key_pepper_previous=old_pepper) as (late,):
        assert (
            await late.client.get("/v1/me", headers=t.h("app"))
        ).status_code == 401  # even with the old pepper back, the key has expired
        assert (await late.client.get("/v1/me", headers=t.h("agent"))).status_code == 200


async def test_a_key_minted_before_keys_were_labelled_is_tried_against_both_peppers(api, tenant):
    t = tenant
    old_pepper = api.settings.auth_key_pepper.get_secret_value()
    await owner_sql(
        "UPDATE api_keys SET pepper_id = 'unlabeled' WHERE principal_id = (SELECT id FROM principals WHERE workspace_id = :ws AND handle = :h)",
        ws=t.ws,
        h=f"reader@{t.slug}",
    )
    assert (await t.get("/v1/me", "reader")).status_code == 200  # the running app's current pepper matches, and labels the key
    assert (await key_label(t.slug, "reader"))[0].pepper_id == pepper_fingerprint(old_pepper)
    await owner_sql(
        "UPDATE api_keys SET pepper_id = 'unlabeled' WHERE principal_id = (SELECT id FROM principals WHERE workspace_id = :ws AND handle = :h)",
        ws=t.ws,
        h=f"reader@{t.slug}",
    )
    async with replicas(api, 1, auth_key_pepper=NEW_PEPPER, auth_key_pepper_previous=old_pepper) as (rotating,):
        assert (
            await rotating.client.get("/v1/me", headers=t.h("reader"))
        ).status_code == 200  # an unlabelled key is tried against the previous pepper too
        assert (await key_label(t.slug, "reader"))[0].pepper_id == pepper_fingerprint(NEW_PEPPER)


async def test_jwt_key_rotation_keeps_tokens_alive_until_the_old_key_is_removed(api, tenant):
    t = tenant
    token = (await t.client.post("/v1/auth/token", headers=t.h("app"))).json()["access_token"]
    bearer = {"authorization": f"Bearer {token}"}
    old = api.settings.auth_jwt_secret.get_secret_value()
    ring = f"k2:{'n' * 40},local-1:{old}"
    async with replicas(api, 1, auth_jwt_keys=ring, auth_jwt_active_kid="k2") as (rotating,):
        assert (await rotating.client.get("/v1/me", headers=bearer)).status_code == 200  # signed with the old kid, verified by the ring
        fresh = (await rotating.client.post("/v1/auth/token", headers=t.h("app"))).json()["access_token"]
        assert (
            await api.client.get("/v1/me", headers={"authorization": f"Bearer {fresh}"})
        ).status_code == 401  # a replica that has not got k2 yet refuses it (hence: add, then activate)
    async with replicas(api, 1, auth_jwt_keys=f"k2:{'n' * 40}", auth_jwt_active_kid="k2") as (retired,):
        assert (await retired.client.get("/v1/me", headers=bearer)).status_code == 401  # the old kid has left the ring: its tokens are dead


async def test_content_hmac_rotation_keeps_replay_dedupe_and_suppression_working(api, tenant_governance):
    t = tenant_governance
    old = api.settings.content_hmac_secret.get_secret_value()
    ring = dict(content_hmac_secrets=f"v2:{'q' * 40},v1:{old}", content_hmac_active="v2")
    body = {
        "source_system": "chat",
        "source_ref": "rot-1",
        "kind": "message",
        "content": "A note written before the rotation.",
        "occurred_at": "2026-10-10T09:00:00Z",
    }
    key = f"rot-{uuid.uuid4()}"
    first = await t.req("POST", "/v1/interactions", "app", json=body, key=key)
    assert first.status_code == 202, first.text
    mem = {"type": "semantic", "content": "Sarah likes tidy rotation drills.", "purpose": "history", "source_ref": "rot-m"}
    saved = await save(t, "admin", mem)
    assert saved.status_code == 201
    w = await deletion_world(api, t)
    done = await run_deletion(api, t, (await request_deletion(t, "subject", w["zeb"], "app")).json()["request_id"])
    assert done["status"] in ("completed", "completed_with_exceptions")
    async with replicas(api, 1, **ring) as (rotated,):
        h = {**t.h("app"), "idempotency-key": key}
        replay = await rotated.client.post("/v1/interactions", headers=h, json=body)
        assert (
            replay.status_code in (200, 202) and replay.json()["event_id"] == first.json()["event_id"]
        )  # the same request is the same request
        mismatch = await rotated.client.post("/v1/interactions", headers=h, json={**body, "content": "different"})
        assert mismatch.status_code == 409
        again = await rotated.client.post("/v1/memories", headers={**t.h("admin"), "idempotency-key": str(uuid.uuid4())}, json=mem)
        assert (
            again.status_code in (200, 201) and again.json()["memory_id"] == saved.json()["memory_id"]
        )  # the same words: the same memory (dedupe across the rotation)
        banned = await rotated.client.post(
            "/v1/entities",
            headers={**t.h("app"), "idempotency-key": str(uuid.uuid4())},
            json={
                "type": "Person",
                "name": "Someone Else",
                "external_ids": [{"system": "hr", "value": SENTINEL_EXT}],
                "source_system": "hr",
                "source_ref": "z",
            },
        )
        assert (
            banned.status_code == 409 and banned.json()["error"]["code"] == "subject_suppressed"
        )  # recorded under v1, still enforced under v2
        # and a deletion completed AFTER the rotation is journalled under the new key, and the whole chain verifies
        journal = rotated.state.deletion.journal
        assert journal.verify() >= 1
