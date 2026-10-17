"""Shared fixtures for the cross-package suites. Integration tests need `make stack-up` (postgres on 5544, neo4j on 7688, mock router)."""

from __future__ import annotations

import asyncio
import base64
import os
import socket
import uuid
from collections.abc import AsyncIterator
from urllib.parse import urlparse

import pytest
import pytest_asyncio
from kg_common.settings import Settings
from kg_db import migrate
from kg_db.engine import create_app_engine
from kg_domain.principal import WorkspaceContext
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.support.p4 import strict_mock  # noqa: F401  (a fixture shared by the P4 suites)

# P8: the suites write the out-of-band deletion journal; keep their lines out of the development stack's own journal (the CLI subprocesses inherit this variable).
os.environ.setdefault(
    "DELETION_JOURNAL_DIR", str(__import__("pathlib").Path(__file__).resolve().parents[1] / ".local" / "test-deletion-journal")
)

OWNER_URL = os.environ.get("TEST_DATABASE_URL_OWNER", "postgresql+psycopg://kg_owner:kg_owner_dev@127.0.0.1:5544/kg_test")
APP_URL = os.environ.get("TEST_DATABASE_URL_APP", "postgresql+psycopg://kg_app:kg_app_dev@127.0.0.1:5544/kg_test")
SCRATCH_URL = os.environ.get("TEST_SCRATCH_DATABASE_URL_OWNER", "postgresql+psycopg://kg_owner:kg_owner_dev@127.0.0.1:5544/kg_scratch")
NEO4J_URI = os.environ.get("TEST_NEO4J_URI", "bolt://127.0.0.1:7688")
NEO4J_USER = os.environ.get("TEST_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("TEST_NEO4J_PASSWORD", "kg-neo4j-dev")
REQUIRE_STACK = os.environ.get("KG_REQUIRE_STACK") == "1"
ROOT_COMPOSE_ARGS = ["-f", "deploy/compose/docker-compose.yml", "--env-file", ".env.example"]  # run from the repository root


def _reachable(url: str) -> bool:
    parsed = urlparse(url.replace("bolt://", "http://").replace("postgresql+psycopg://", "http://"))
    try:
        with socket.create_connection((parsed.hostname or "127.0.0.1", parsed.port or 80), timeout=1):
            return True
    except OSError:
        return False


def require(url: str, what: str) -> None:
    if not _reachable(url):
        msg = f"{what} is not reachable ({urlparse(url.replace('+psycopg', '')).netloc}); run `make stack-up`"
        if REQUIRE_STACK:
            pytest.fail(msg)
        pytest.skip(msg)


# What ENV=production requires beyond the development defaults (P1 secrets, plus P3's content HMAC secret and payload keyring).
PRODUCTION_SECRETS: dict[str, str] = {
    "auth_key_pepper": "p" * 40,
    "auth_jwt_secret": "j" * 40,
    "telemetry_hmac_secret": "t" * 40,
    "content_hmac_secret": "c" * 40,
    "payload_keys": "prod1:" + base64.b64encode(b"k" * 32).decode(),
    "payload_active_kid": "prod1",
    "deletion_journal_dir": "/var/lib/kg/deletion-journal",
}


def make_test_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "env": "test",
        "database_url_app": APP_URL,
        "database_url_owner": OWNER_URL,
        "neo4j_uri": NEO4J_URI,
        "neo4j_user": NEO4J_USER,
        "neo4j_password": NEO4J_PASSWORD,
        "auth_key_pepper": "test-pepper-0123456789abcdef0123456789",
        "auth_jwt_secret": "test-jwt-secret-0123456789abcdef012345",
        "telemetry_hmac_secret": "test-telemetry-0123456789abcdef012345",
        "log_level": "WARNING",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


@pytest.fixture(scope="session")
def test_settings() -> Settings:
    return make_test_settings()


@pytest_asyncio.fixture(scope="session")
async def migrated_db() -> AsyncIterator[str]:
    """kg_test at head, from scratch (also exercises downgrade)."""
    require(OWNER_URL, "postgres")
    await asyncio.to_thread(
        lambda: migrate.downgrade(
            OWNER_URL,
            "base",
            force_drop_extracted=True,
            force_drop_memories=True,
            force_forget_privileges=True,
            force_drop_governance=True,
            force_drop_release=True,
        )
    )
    await asyncio.to_thread(migrate.upgrade, OWNER_URL, "head")
    yield OWNER_URL


@pytest_asyncio.fixture(scope="session")
async def app_engine(migrated_db: str, test_settings: Settings) -> AsyncIterator[AsyncEngine]:
    engine = create_app_engine(test_settings, url=APP_URL)
    yield engine
    await engine.dispose()


@pytest.fixture()
def ctx_for() -> object:
    def make(workspace_id: uuid.UUID) -> WorkspaceContext:
        return WorkspaceContext.for_system_task(workspace_id, reason="test fixture")

    return make


# ---- API stack: seeded DB + the real app (lifespan included) talking to the compose mock router --------------------------------------------
class ApiStack:
    def __init__(self, app, client, keys, engine, settings):  # type: ignore[no-untyped-def]
        self.app, self.client, self.keys, self.engine, self.settings = app, client, keys, engine, settings

    def h(self, who: str) -> dict[str, str]:
        """Authorization header for a seeded principal, e.g. h('demo/agent_planning')."""
        return {"authorization": f"Bearer {self.keys[who]}"}

    async def jwt(self, who: str) -> str:
        r = await self.client.post("/v1/auth/token", headers=self.h(who))
        assert r.status_code == 200, r.text
        return str(r.json()["access_token"])


@pytest_asyncio.fixture(scope="session")
async def api(migrated_db: str, test_settings: Settings) -> AsyncIterator[ApiStack]:
    import httpx
    from asgi_lifespan import LifespanManager
    from kg_api.main import build_app
    from kg_cli.seed import seed_demo

    require("http://127.0.0.1:8782", "mock router")
    require(NEO4J_URI, "neo4j")
    # a generous projection budget: the host may be loaded, and tests that want a deferral (202) cause one on purpose
    settings = make_test_settings(
        oso_router_mock_url="http://127.0.0.1:8782/v1",
        oso_router_api_key_api="mock-key",
        projection_request_budget_seconds=60.0,
        context_rate_per_minute=1_000_000,
        context_rate_burst=100_000,  # the suites build thousands of contexts; the limiter has its own tests
        principal_requests_per_minute=1_000_000,  # likewise for the per-principal request quota (P7); its tests set a registered quota
    )
    engine = create_app_engine(settings, pool_size=3, max_overflow=2)
    seeded = await seed_demo(engine, pepper=settings.auth_key_pepper.get_secret_value())
    assert seeded.new_keys, "fresh database should mint keys"
    app = build_app(settings)
    async with LifespanManager(app, startup_timeout=60, shutdown_timeout=60) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://kg.test") as client:
            yield ApiStack(app, client, seeded.new_keys, engine, settings)
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _no_leftover_jobs(request: pytest.FixtureRequest) -> AsyncIterator[None]:
    """Tests share kg_test, and a test that accepts an event or applies a command leaves outbox jobs behind. A later test that runs a worker would pick
    them up (and spend its injected faults on them), so every database-backed test cancels what it left pending."""
    yield
    if not {"api", "app_engine", "migrated_db", "tenant", "bare_tenant"} & set(request.fixturenames):
        return
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(OWNER_URL)
    try:
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE outbox_jobs SET status = 'cancelled', lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL, finished_at = now() "
                    "WHERE status IN ('pending', 'leased')"
                )
            )
    except Exception:  # the schema may not exist yet (a test that migrates from scratch)  # noqa: S110
        pass
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def _reset_root_logging():
    """configure_logging(force=True) attaches a handler to whatever sys.stdout is at the time (pytest's capture); drop it after each test."""
    import logging

    yield
    logging.getLogger().handlers.clear()


@pytest_asyncio.fixture()
async def tenant(api):  # type: ignore[no-untyped-def]
    """A fresh workspace with the example ontology active. Its graph data is removed afterwards."""
    from tests.support.tenants import clear_graph, make_tenant

    t = await make_tenant(api)
    yield t
    await clear_graph(api, t.group_id)


@pytest_asyncio.fixture()
async def tenant_mem(api):  # type: ignore[no-untyped-def]
    """A fresh workspace with the v1.2.0 example ontology (typed memory policy, extraction on): the P5 suites."""
    from tests.support.tenants import MEMORY_YAML, clear_graph, make_tenant

    t = await make_tenant(api, ontology=MEMORY_YAML)
    yield t
    await clear_graph(api, t.group_id)


@pytest_asyncio.fixture()
async def tenant_ctx(api):  # type: ignore[no-untyped-def]
    """A fresh workspace with the v1.3.0 example ontology (typed memory policy AND the planning / quick_answer context profiles with expected signals): the P6 suites."""
    from tests.support.tenants import CONTEXT_YAML, clear_graph, make_tenant

    t = await make_tenant(api, ontology=CONTEXT_YAML)
    yield t
    await clear_graph(api, t.group_id)


@pytest_asyncio.fixture()
async def tenant_agents(api):  # type: ignore[no-untyped-def]
    """A fresh workspace with the v1.4.0 example ontology (Person is subject_scoped, agents holding `publish` activate their findings): the P7 suites."""
    from tests.support.tenants import AGENTS_YAML, clear_graph, make_tenant

    t = await make_tenant(api, ontology=AGENTS_YAML)
    yield t
    await clear_graph(api, t.group_id)


@pytest_asyncio.fixture()
async def tenant_governance(api):  # type: ignore[no-untyped-def]
    """A fresh workspace with the v1.5.0 example ontology (v1.4.0's subject ACLs plus MANAGES and the consolidation policy): the P8 suites."""
    from tests.support.tenants import GOVERNANCE_YAML, clear_graph, make_tenant

    t = await make_tenant(api, ontology=GOVERNANCE_YAML)
    yield t
    await clear_graph(api, t.group_id)


@pytest_asyncio.fixture()
async def bare_tenant(api):  # type: ignore[no-untyped-def]
    """A fresh workspace with NO ontology (for the no_active_ontology and designer flows)."""
    from tests.support.tenants import clear_graph, make_tenant

    t = await make_tenant(api, ontology=None)
    yield t
    await clear_graph(api, t.group_id)
