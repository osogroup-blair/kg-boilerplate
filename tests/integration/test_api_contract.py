"""WP5: endpoint contract tests (status, schema, headers, envelope)."""

from __future__ import annotations

import json
import uuid

import pytest
from kg_cli.seed import principal_id_for, workspace_id_for
from kg_common.ids import is_uuid

pytestmark = [pytest.mark.integration, pytest.mark.contract]

DEMO = workspace_id_for("demo")
ENVELOPE_KEYS = {"code", "message", "request_id", "retry_after", "details"}


def assert_envelope(r, status: int, code: str):
    assert r.status_code == status, r.text
    body = r.json()
    assert set(body) == {"error"} and set(body["error"]) == ENVELOPE_KEYS
    assert body["error"]["code"] == code and body["error"]["request_id"] == r.headers["x-request-id"]
    return body["error"]


async def test_headers_request_and_correlation_ids(api):
    r = await api.client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
    assert is_uuid(r.headers["x-request-id"]) and r.headers["x-correlation-id"] == r.headers["x-request-id"]
    rid, cid = str(uuid.uuid4()), str(uuid.uuid4())
    r = await api.client.get("/health", headers={"x-request-id": rid, "x-correlation-id": cid})
    assert r.headers["x-request-id"] == rid and r.headers["x-correlation-id"] == cid
    r = await api.client.get(
        "/health", headers={"x-request-id": "not-a-uuid", "x-correlation-id": "also-bad"}
    )  # L6: replaced, never echoed
    assert (
        is_uuid(r.headers["x-request-id"])
        and r.headers["x-request-id"] != "not-a-uuid"
        and r.headers["x-correlation-id"] == r.headers["x-request-id"]
    )


async def test_ready_reports_dependencies_without_versions_or_hostnames(api):
    r = await api.client.get("/health/ready")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] in ("ok", "degraded") and set(body["checks"]) == {"postgres", "neo4j", "router", "worker"}
    assert body["checks"]["postgres"] == "ok" and body["checks"]["neo4j"] == "ok" and body["checks"]["router"] == "ok"
    text = json.dumps(body).lower()
    assert "127.0.0.1" not in text and "localhost" not in text and "version" not in text


async def test_token_exchange(api):
    r = await api.client.post("/v1/auth/token", headers=api.h("demo/agent_planning"))
    assert (
        r.status_code == 200
        and r.json()["token_type"] == "Bearer"
        and r.json()["expires_in"] == 900
        and r.json()["access_token"].count(".") == 2
    )
    token = r.json()["access_token"]
    # a token cannot be exchanged for another token, and bad credentials get one generic 401
    assert_envelope(await api.client.post("/v1/auth/token", headers={"authorization": f"Bearer {token}"}), 401, "unauthenticated")
    assert_envelope(await api.client.post("/v1/auth/token"), 401, "unauthenticated")
    bad = await api.client.post("/v1/auth/token", headers={"authorization": "Bearer kgk_abcdefgh_nope"})
    assert_envelope(bad, 401, "unauthenticated")
    assert bad.headers["www-authenticate"] == "Bearer"


async def test_me_with_key_and_with_jwt(api):
    for headers in (api.h("demo/agent_planning"), {"authorization": f"Bearer {await api.jwt('demo/agent_planning')}"}):
        r = await api.client.get("/v1/me", headers=headers)
        body = r.json()
        assert r.status_code == 200 and body["workspace"]["slug"] == "demo" and body["workspace"]["id"] == str(DEMO)
        assert (
            body["principal"]["handle"] == "agent_planning"
            and body["principal"]["kind"] == "agent"
            and body["principal"]["roles"] == ["agent"]
        )
        assert body["principal"]["id"] == str(principal_id_for("demo", "agent_planning"))


async def test_workspaces(api):
    h = api.h("demo/agent_planning")
    cur = await api.client.get("/v1/workspaces/current", headers=h)
    assert cur.status_code == 200 and cur.json() == {"id": str(DEMO), "slug": "demo", "display_name": "Demo Workspace", "status": "active"}
    assert (await api.client.get(f"/v1/workspaces/{DEMO}", headers=h)).json() == cur.json()
    assert_envelope(await api.client.get("/v1/workspaces/not-a-uuid", headers=h), 422, "validation_failed")


async def test_principals_list_detail_and_pagination(api):
    h = api.h("demo/admin@demo")
    r = await api.client.get("/v1/principals", headers=h)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 6 and r.json()["next_cursor"] is None
    assert {i["handle"] for i in items} == {
        "admin@demo",
        "reviewer@demo",
        "operator@demo",
        "conversation_app",
        "agent_research",
        "agent_planning",
    }
    one = next(i for i in items if i["handle"] == "agent_planning")
    assert one["kind"] == "agent" and one["roles"] == ["agent"] and len(one["keys"]) >= 1
    assert set(one["keys"][0]) == {"prefix", "created_at", "expires_at", "revoked_at", "last_used_at"}  # never a hash
    assert "key_hash" not in r.text and "kgk_" not in r.text.replace(one["keys"][0]["prefix"], "")
    seen, cursor = [], None
    for _ in range(5):
        page = (await api.client.get("/v1/principals", headers=h, params={"limit": 2, **({"cursor": cursor} if cursor else {})})).json()
        seen += [i["handle"] for i in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert sorted(seen) == sorted(i["handle"] for i in items) and len(seen) == 6  # keyset pages cover all rows exactly once
    detail = await api.client.get(f"/v1/principals/{one['id']}", headers=h)
    assert detail.status_code == 200 and detail.json()["handle"] == "agent_planning"
    assert_envelope(await api.client.get("/v1/principals", headers=h, params={"limit": 0}), 422, "validation_failed")
    assert_envelope(await api.client.get("/v1/principals", headers=h, params={"cursor": "###"}), 422, "validation_failed")


async def test_audit_lists_token_issuance_and_denials(api):
    await api.client.post("/v1/auth/token", headers=api.h("demo/agent_research"))
    await api.client.get("/v1/principals", headers=api.h("demo/agent_research"))  # agent lacks principal.read
    r = await api.client.get("/v1/audit", headers=api.h("demo/admin@demo"))
    assert r.status_code == 200
    events = r.json()["items"]
    actions = {(e["action"], e["outcome"]) for e in events}
    assert ("auth.token_issued", "allowed") in actions and ("authz.denied", "denied") in actions
    denied = next(e for e in events if e["action"] == "authz.denied")
    assert denied["metadata"] == {"action_name": "principal.read", "reason": "missing_role", "status_code": 403}
    assert [e["created_at"] for e in events] == sorted((e["created_at"] for e in events), reverse=True)


async def test_operations_health(api):
    r = await api.client.get("/v1/operations/health", headers=api.h("demo/operator@demo"))
    body = r.json()
    assert r.status_code == 200 and body["postgres"] == "ok" and body["neo4j"] == "ok" and body["router"]["mode"] == "mock"
    assert body["router"]["reachability"] == "ok" and body["router"]["missing_aliases"] == []
    assert body["principal_counts"] == {"user": 3, "app": 1, "agent": 2}
    # the stack's Neo4j is shared with `make up` (whose demo workspace has the same deterministic id), so compare with the graph itself, not with zero
    from kg_cli.seed import workspace_id_for
    from kg_domain.principal import WorkspaceContext

    expected = await api.app.state.kg.graph.scoped(WorkspaceContext.for_system_task(workspace_id_for("demo"), reason="test")).node_count()
    assert body["graph_node_count"] == expected
    assert body["worker"]["status"] in ("ok", "stale", "none")


async def test_router_check_writes_a_content_free_model_call(api):
    from kg_db.session import tenant_session
    from kg_domain.principal import WorkspaceContext
    from sqlalchemy import text

    r = await api.client.post("/v1/operations/router-check", headers=api.h("demo/operator@demo"))
    body = r.json()
    assert r.status_code == 200 and body["ok"] and body["dims"] == 384 and body["alias"] == "oso/embedding" and body["latency_ms"] > 0
    ctx = WorkspaceContext.for_system_task(DEMO, reason="test read")
    async with tenant_session(api.engine, ctx) as c:
        row = (await c.execute(text("SELECT * FROM model_calls WHERE router_request_id = :r"), {"r": body["router_request_id"]})).one()
    assert (
        row.purpose == "embedding"
        and row.operation == "embeddings"
        and row.outcome == "ok"
        and row.classification == "internal"
        and row.service == "kg-api"
    )
    assert row.workspace_id == DEMO and row.prompt_tokens and row.est_cost_usd is not None
    assert "kg platform router probe" not in json.dumps({k: str(v) for k, v in row._mapping.items()})


async def test_errors_use_one_envelope(api):
    assert_envelope(await api.client.get("/v1/does-not-exist", headers=api.h("demo/agent_planning")), 404, "not_found")
    assert_envelope(await api.client.get("/nope"), 404, "not_found")
    assert_envelope(await api.client.post("/health"), 405, "method_not_allowed")
    big = await api.client.post(
        "/v1/auth/token",
        content=b"x" * (api.settings.max_body_bytes + 1),
        headers={**api.h("demo/agent_planning"), "content-type": "text/plain"},
    )
    assert_envelope(big, 413, "payload_too_large")
    r = await api.client.get("/v1/principals", headers={"authorization": "Basic abc"})
    assert_envelope(r, 401, "unauthenticated")


async def test_validation_errors_never_echo_values(api):
    r = await api.client.get("/v1/principals/SENTINEL-NOT-A-UUID", headers=api.h("demo/admin@demo"))
    err = assert_envelope(r, 422, "validation_failed")
    assert "SENTINEL" not in r.text and err["details"] == {"fields": ["path.principal_id"]}


async def test_openapi_declares_security_errors_and_idempotency_component(api):
    spec = (await api.client.get("/openapi.json")).json()
    assert spec["info"]["version"] == "0.1.0" and spec["components"]["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"
    assert "IdempotencyKey" in spec["components"]["parameters"] and "ErrorEnvelope" in spec["components"]["schemas"]
    for path, ops in spec["paths"].items():
        for op in ops.values():
            assert {"401", "403", "404", "422", "500"} <= set(op["responses"]), path
            assert op["security"] == ([] if path.startswith("/health") else [{"bearerAuth": []}]), path


async def test_api_refuses_to_start_with_an_rls_bypassing_role(migrated_db, test_settings):
    from asgi_lifespan import LifespanManager
    from kg_api.main import build_app
    from kg_db.engine import UnsafeDatabaseRole

    from tests.conftest import OWNER_URL, make_test_settings

    app = build_app(make_test_settings(database_url_app=OWNER_URL))
    with pytest.raises(UnsafeDatabaseRole):
        async with LifespanManager(app, startup_timeout=60, shutdown_timeout=60):
            pass
