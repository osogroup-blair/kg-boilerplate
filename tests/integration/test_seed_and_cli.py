"""WP9: seed is idempotent and safe; credentials file is private; production refuses to seed."""

from __future__ import annotations

import json
import stat

import pytest
from kg_cli.seed import SEED, seed_demo, workspace_id_for, write_credentials
from kg_db.session import tenant_session
from kg_domain.principal import WorkspaceContext
from sqlalchemy import text

pytestmark = pytest.mark.integration


async def counts(api):
    out = {}
    for slug in SEED:
        ctx = WorkspaceContext.for_system_task(workspace_id_for(slug), reason="test read")
        async with tenant_session(api.engine, ctx) as c:
            out[slug] = (
                (await c.execute(text("SELECT count(*) FROM principals"))).scalar_one(),
                (await c.execute(text("SELECT count(*) FROM api_keys"))).scalar_one(),
                (await c.execute(text("SELECT count(*) FROM role_assignments"))).scalar_one(),
            )
    return out


async def test_seed_is_idempotent_and_mints_keys_only_once(api):
    before = await counts(api)
    again = await seed_demo(api.engine, pepper=api.settings.auth_key_pepper.get_secret_value())
    assert again.created_workspaces == 0 and again.created_principals == 0 and again.new_keys == {}
    assert await counts(api) == before
    assert before["demo"][0] == 6 and before["other_demo"][0] == 2
    assert workspace_id_for("demo") == workspace_id_for("demo") != workspace_id_for("other_demo")


def test_credentials_file_is_private_and_merges(tmp_path):
    path = tmp_path / "creds" / "dev-credentials.json"
    write_credentials(path, {"demo/a": "kgk_one"})
    write_credentials(path, {"demo/b": "kgk_two"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text()) == {"demo/a": "kgk_one", "demo/b": "kgk_two"}


def test_seed_refuses_in_production(monkeypatch):
    from kg_cli.main import app as cli
    from kg_common.settings import get_settings
    from typer.testing import CliRunner

    from tests.conftest import PRODUCTION_SECRETS

    for k, v in {"ENV": "production", **{name.upper(): value for name, value in PRODUCTION_SECRETS.items()}}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    try:
        result = CliRunner().invoke(cli, ["seed", "demo"])
        assert result.exit_code == 1 and "ENV=production" in result.output
    finally:
        get_settings.cache_clear()


async def test_docs_are_disabled_in_production_and_metrics_use_route_templates(api):
    from kg_api.main import build_app
    from kg_obs import metrics

    from tests.conftest import PRODUCTION_SECRETS, make_test_settings

    prod = build_app(make_test_settings(env="production", **PRODUCTION_SECRETS))
    assert prod.docs_url is None and prod.openapi_url is None
    await api.client.get(f"/v1/principals/{'0' * 8}-0000-4000-8000-000000000000", headers=api.h("demo/admin@demo"))
    await api.client.get("/v1/something/else")
    routes = {s.labels["route"] for f in metrics.REGISTRY.collect() if f.name == "http_requests" for s in f.samples}
    assert "/v1/principals/{principal_id}" in routes and "unmatched" in routes
    assert not any("0000-4000" in r or "something" in r for r in routes)


async def test_metrics_endpoint_text_has_no_identifiers(api):
    from kg_common.settings import get_settings  # noqa: F401
    from kg_obs import metrics

    await api.client.get("/v1/me", headers=api.h("demo/agent_planning"))
    text_out = metrics.render().decode()
    assert "http_requests_total" in text_out and "auth_failures_total" in text_out
    for ident in (str(workspace_id_for("demo")), "agent_planning", "admin@demo"):
        assert ident not in text_out
