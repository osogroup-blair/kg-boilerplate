"""P9 WP6 hardening: security headers on every response, a body limit per route class, no docs or schema in production, and the production-only refusals."""

from __future__ import annotations

import pytest

from tests.conftest import PRODUCTION_SECRETS
from tests.support.p9 import replicas

pytestmark = pytest.mark.integration
PROD = dict(env="production", **{**PRODUCTION_SECRETS, "deletion_journal_dir": "/tmp/kg-prod-journal"})  # noqa: S108


async def test_every_response_carries_the_security_headers_even_errors(api, tenant):
    t = tenant
    for resp in (
        await api.client.get("/health"),
        await api.client.get("/v1/me"),  # 401
        await api.client.get("/v1/nothing-here", headers=t.h("admin")),  # 404
        await t.get("/v1/me", "admin"),
    ):
        h = resp.headers
        assert h["x-content-type-options"] == "nosniff" and h["referrer-policy"] == "no-referrer"
        assert h["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'" and h["x-frame-options"] == "DENY"
        assert "strict-transport-security" not in h  # plain HTTP in development


async def test_in_production_tls_requests_get_hsts_and_the_docs_and_schema_are_gone(api, tenant):
    t = tenant
    prod = {
        **PROD,
        "auth_key_pepper": api.settings.auth_key_pepper.get_secret_value(),
        "auth_jwt_secret": api.settings.auth_jwt_secret.get_secret_value(),
        "allow_breakglass_keys": True,
    }
    async with replicas(api, 1, **prod) as (p,):
        over_tls = await p.client.get("/health", headers={"x-forwarded-proto": "https"})
        assert (
            over_tls.headers["strict-transport-security"].startswith("max-age=")
            and "includeSubDomains" in over_tls.headers["strict-transport-security"]
        )
        assert "strict-transport-security" not in (await p.client.get("/health")).headers  # never claimed over plain HTTP
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert (await p.client.get(path)).status_code == 404, path
        assert (await p.client.get("/v1/me", headers=t.h("admin"))).status_code == 200


async def test_a_body_limit_per_route_class(api, tenant):
    t = tenant
    big = "x" * (300 * 1024)
    refused = await t.req("POST", "/v1/entities", "app", json={"type": "Topic", "name": big})
    assert refused.status_code == 413 and refused.json()["error"]["code"] == "payload_too_large"  # an ordinary route takes 256 KiB at most
    near = "y" * (200 * 1024)
    accepted = await t.req(
        "POST",
        "/v1/interactions",
        "app",
        json={"source_system": "chat", "source_ref": "big-1", "kind": "message", "content": near, "occurred_at": "2026-10-10T09:00:00Z"},
    )
    assert accepted.status_code == 202, accepted.text  # an interaction may carry a large document
    huge = await t.req(
        "POST",
        "/v1/interactions",
        "app",
        json={"source_system": "chat", "source_ref": "big-2", "kind": "message", "content": "z" * (2 * 1024 * 1024)},
    )
    assert huge.status_code == 413  # but not without limit
