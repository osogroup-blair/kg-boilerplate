"""P8 WP9 (ADR-047, SEC-06): workspace settings (versioned, optimistic, audited), egress narrowing that can never widen the platform policy, and retention that goes through the
purge gate as a deletion of kind `retention`."""

from __future__ import annotations

import json
import uuid

import pytest

from tests.support.p4 import EVT003, Spied, chat_calls, run_event, sarah_story, sql, stage, strict_mock  # noqa: F401
from tests.support.p8 import deletion_world, owner_exec, raw, rev_header
from tests.support.tenants import GOVERNANCE_YAML, clear_graph, make_tenant

pytestmark = pytest.mark.integration
SETTINGS = "/v1/workspaces/current/settings"


async def put(t, body, version, who="admin", key=None):  # type: ignore[no-untyped-def]
    return await t.req("PUT", SETTINGS, who, json=body, headers=rev_header(version) if version is not None else None, key=key)


async def test_settings_are_versioned_optimistic_idempotent_audited_and_admin_only(api, tenant_governance):
    t = tenant_governance
    first = (await t.get(SETTINGS, "admin")).json()
    assert first["version"] == 0 and first["settings"]["retention"]["raw_content_days"] is None
    assert (
        first["effective_egress"]["platform_denied"] == ["local_only"]
        and "local_only" not in first["effective_egress"]["effective_allowed"]
    )
    for who in ("reviewer", "app", "agent", "operator"):
        assert (await t.get(SETTINGS, who)).status_code == 403, who
        assert (await put(t, {}, 0, who)).status_code == 403, who
    assert (await put(t, {"retention": {"raw_content_days": 30}}, None)).status_code == 428  # If-Match is required
    body = {"retention": {"raw_content_days": 30}, "backup_retention_days": 14}
    key = f"set-{uuid.uuid4()}"
    r = await put(t, body, 0, key=key)
    assert r.status_code == 200 and r.json()["version"] == 1 and r.json()["settings"]["retention"]["raw_content_days"] == 30
    replay = await put(t, body, 0, key=key)
    assert replay.status_code == 200 and replay.headers["idempotent-replay"] == "true" and replay.json()["version"] == 1
    assert (await put(t, {"backup_retention_days": 1}, 0, key=key)).status_code == 409  # same key, different request
    assert (await put(t, {"retention": {"raw_content_days": 7}}, 0)).status_code == 412  # a stale version
    r2 = await put(t, {"retention": {"raw_content_days": 7}}, 1)
    assert r2.status_code == 200 and r2.json()["version"] == 2
    hist = (await t.get(SETTINGS + "/history", "admin")).json()
    assert [h["version"] for h in hist] == [2, 1] and hist[1]["settings"]["retention"]["raw_content_days"] == 30
    audit = await sql(api, t, "SELECT action FROM audit_log WHERE workspace_id = :ws AND action LIKE 'settings.%'")
    assert len(audit) == 2
    assert len(await sql(api, t, "SELECT 1 FROM commands WHERE workspace_id = :ws AND command_type = 'settings.update'")) == 2
    assert (await put(t, {"retention": {"raw_content_days": 0}}, 2)).status_code == 422  # out of range
    assert (await put(t, {"unknown": 1}, 2)).status_code == 422


async def test_egress_narrowing_makes_zero_router_calls_and_cannot_widen_the_platform_policy(api, strict_mock):  # noqa: F811
    t = await make_tenant(api, ontology=GOVERNANCE_YAML)
    try:
        ids = await sarah_story(t)
        # there is no field that allows more: a workspace cannot name an allowed class, and the platform's own deny list is always part of the effective one
        assert (await put(t, {"egress": {"allowed_classifications": ["local_only"]}}, 0)).status_code == 422
        assert (await put(t, {"egress": {"denied_classifications": ["secret"]}}, 0)).status_code == 422
        same = await put(t, {"egress": {"denied_classifications": []}}, 0)
        assert same.status_code == 200 and "local_only" in same.json()["effective_egress"]["effective_denied"]
        narrowed = await put(t, {"egress": {"denied_classifications": ["internal", "confidential"]}}, 1)
        eff = narrowed.json()["effective_egress"]
        assert eff["workspace_denied"] == ["internal", "confidential"] and eff["effective_denied"] == [
            "internal",
            "confidential",
            "local_only",
        ]
        assert eff["effective_allowed"] == ["public", "restricted"]
        with Spied(api) as spy:
            detail = await run_event(api, t, EVT003, subjects=[ids["sarah"], ids["michael"], ids["topic"]])
        st = stage(detail, "graph_ingest")
        assert st["status"] != "succeeded", st  # an internal-class event may not leave this workspace any more
        assert [r for r in spy.requests if r["path"].endswith(("/chat/completions", "/embeddings"))] == [] and chat_calls(spy) == 0
        assert (await sql(api, t, "SELECT count(*) AS n FROM model_calls WHERE workspace_id = :ws AND outcome = 'success'"))[0].n == 0
        # lifting the narrowing (removing the workspace's own entries) is allowed: it only ever moves back to the platform policy
        back = await put(t, {"egress": {"denied_classifications": []}}, 2)
        assert back.json()["effective_egress"]["effective_denied"] == ["local_only"]
    finally:
        await clear_graph(api, t.group_id)


async def test_raw_content_expires_through_the_gate_the_interaction_says_so_and_a_receipt_exists(api, tenant_governance):
    t = tenant_governance
    w = await deletion_world(api, t)
    old, young = w["i_zeb"], w["i_sar"]
    await owner_exec("UPDATE interaction_contents SET created_at = now() - interval '40 days' WHERE interaction_id = :i", i=uuid.UUID(old))
    assert (await put(t, {"retention": {"raw_content_days": 30}}, 0)).status_code == 200
    swept = await sweep(api)
    assert swept.get("raw_content", 0) >= 1, swept
    forms = {
        (r.interaction_id, r.form)
        for r in await raw(api, t, "SELECT interaction_id::text AS interaction_id, form FROM interaction_contents WHERE workspace_id = :ws")
    }
    assert (old, "raw") not in forms and (young, "raw") in forms  # only the old one lost its raw form
    detail = (await t.get(f"/v1/interactions/{old}", "admin")).json()
    assert (
        detail["raw_content_expired"] is True and (await t.get(f"/v1/interactions/{young}", "admin")).json()["raw_content_expired"] is False
    )
    assert (await t.get(f"/v1/interactions/{old}/content", "admin", form="raw")).status_code == 404
    req = await raw(api, t, "SELECT kind, status, receipt FROM deletion_requests WHERE workspace_id = :ws AND kind = 'retention'")
    assert req and req[0].status.startswith("completed") and req[0].receipt["stores"]["postgres"]["deleted"] >= 1
    assert (await sweep(api)).get("raw_content", 0) == 0  # nothing left to do
    # the ledger and the audit trail record who changed the policy, and the deletion is audited like any other
    assert (
        len(
            await sql(
                api,
                t,
                "SELECT 1 FROM audit_log WHERE workspace_id = :ws AND action = 'deletion.completed' AND metadata->>'deletion_kind' = 'retention'",
            )
        )
        >= 1
    )


async def test_model_calls_old_memories_and_episode_text_expire_by_their_own_windows(api, tenant_governance, strict_mock):  # noqa: F811
    t = tenant_governance
    ids = await sarah_story(t)
    await run_event(api, t, EVT003, subjects=[ids["sarah"], ids["michael"], ids["topic"]])
    calls = (await raw(api, t, "SELECT count(*) AS n FROM model_calls WHERE workspace_id = :ws"))[0].n
    assert calls > 0
    await owner_exec("UPDATE model_calls SET created_at = now() - interval '100 days' WHERE workspace_id = :ws", ws=t.ws)
    episode = (await raw(api, t, "SELECT episode_uuid FROM graph_episodes WHERE workspace_id = :ws"))[0].episode_uuid
    await owner_exec("UPDATE graph_episodes SET created_at = now() - interval '100 days' WHERE workspace_id = :ws", ws=t.ws)
    mem = (
        await t.req(
            "POST",
            "/v1/memories",
            "admin",
            json={"type": "semantic", "content": "Sarah prefers tea", "subject_ids": [ids["sarah"]], "purpose": "pref", "source_ref": "m"},
        )
    ).json()["memory_id"]
    await owner_exec(
        "UPDATE curated_memories SET recorded_at = now() - interval '100 days', last_confirmed_at = NULL WHERE id = :m", m=uuid.UUID(mem)
    )
    body = {"retention": {"model_calls_days": 90, "episode_content_days": 90, "memory_archive_after_days": 90}}
    assert (await put(t, body, 0)).status_code == 200
    graph = api.app.state.kg.graph.scoped(__import__("tests.support.p4", fromlist=["ctx_of"]).ctx_of(t))
    before = await graph.run_cypher("MATCH (e:Episodic {group_id: $group_id, uuid: $u}) RETURN e.content AS c", u=str(episode))
    assert before and before[0]["c"]
    swept = await sweep(api)
    assert swept.get("model_calls", 0) >= calls and swept.get("episode_content", 0) == 1 and swept.get("memory_archive", 0) >= 1, swept
    assert (
        await raw(api, t, "SELECT count(*) AS n FROM model_calls WHERE workspace_id = :ws AND created_at < now() - interval '90 days'")
    )[0].n == 0  # (a call logged since is newer and stays)
    after = await graph.run_cypher("MATCH (e:Episodic {group_id: $group_id, uuid: $u}) RETURN e.content AS c", u=str(episode))
    assert after and after[0]["c"] == ""  # the node stays (provenance), its text does not
    assert (await raw(api, t, "SELECT status FROM curated_memories WHERE id = :m", m=uuid.UUID(mem)))[0].status == "expired"
    assert json.dumps(swept)


async def sweep(api) -> dict[str, int]:  # type: ignore[no-untyped-def]
    from kg_memory.deletion import RetentionSweeper

    return await RetentionSweeper(api.engine, api.app.state.kg.deletion.service, api.app.state.kg.graph).sweep()
