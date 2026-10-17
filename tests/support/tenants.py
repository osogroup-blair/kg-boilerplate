"""P2 test rig: a throw-away workspace (seeded principals, an active ontology) per test, plus a small client wrapper.

Workspace ids are derived from the slug, so a unique slug gives a fully isolated tenant in the shared test database and graph."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import httpx
from kg_cli.seed import SeedPrincipal, SeedResult, publish_ontology, seed_workspace, workspace_id_for
from kg_db.session import tenant_session
from kg_domain.enums import PrincipalKind, Role
from kg_domain.principal import WorkspaceContext
from kg_governance.repositories import admin

EXAMPLE_YAML = (Path(__file__).resolve().parents[2] / "examples" / "ontologies" / "personal-knowledge.yaml").read_text()
# The suites written before P5 never asked for memory: their tenants use the example ontology (v1.0.0, whose P2 memory-policy keys still parse) with memory
# EXTRACTION switched off by policy, so the memory stage skips itself before any model call and call counts and statuses in those suites are unchanged.
# The P5 suites ask for MEMORY_YAML: the v1.2.0 example with the typed policy, extraction on.
NO_MEMORY_YAML = EXAMPLE_YAML.replace("    archive_after_days: null\n", "    archive_after_days: null\n    extraction: disabled\n")
assert NO_MEMORY_YAML != EXAMPLE_YAML
MEMORY_YAML = (Path(__file__).resolve().parents[2] / "examples" / "ontologies" / "personal-knowledge-memory.yaml").read_text()
CONTEXT_YAML = (
    Path(__file__).resolve().parents[2] / "examples" / "ontologies" / "personal-knowledge-context.yaml"
).read_text()  # P6: v1.3.0, the context profiles
AGENTS_YAML = (
    Path(__file__).resolve().parents[2] / "examples" / "ontologies" / "personal-knowledge-agents.yaml"
).read_text()  # P7: v1.4.0, Person is subject_scoped; semantic findings of an agent holding `publish` need no review
GOVERNANCE_YAML = (
    Path(__file__).resolve().parents[2] / "examples" / "ontologies" / "personal-knowledge-governance.yaml"
).read_text()  # P8: v1.5.0, v1.4.0 plus MANAGES (Person -> Person) and the consolidation policy
ROLES: dict[str, tuple[PrincipalKind, Role]] = {
    "admin": (PrincipalKind.USER, Role.WORKSPACE_ADMIN),
    "reviewer": (PrincipalKind.USER, Role.REVIEWER),
    "operator": (PrincipalKind.USER, Role.OPERATOR),
    "app": (PrincipalKind.APP, Role.APP_WRITER),
    "agent": (PrincipalKind.AGENT, Role.AGENT),
    "agent2": (PrincipalKind.AGENT, Role.AGENT),
    "agent3": (PrincipalKind.AGENT, Role.AGENT),  # in the workspace, with no grants at all
    "reader": (PrincipalKind.USER, Role.READER),
}
# what `kg seed demo` grants (P5): the curation roles and the app see every subject and project; the two working agents share one project
DEFAULT_GRANTS: dict[str, list[tuple[str, str]]] = {
    "admin": [("subject", "*"), ("project", "*")],
    "reviewer": [("subject", "*"), ("project", "*")],
    "app": [("subject", "*")],
    "agent": [("project", "newco-plan")],
    "agent2": [("project", "newco-plan")],
}
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


class Tenant:
    def __init__(self, api: Any, slug: str, keys: dict[str, str]):
        self.api, self.slug, self.ws = api, slug, workspace_id_for(slug)
        self._keys = keys

    @property
    def client(self) -> httpx.AsyncClient:
        return self.api.client  # type: ignore[no-any-return]

    @property
    def group_id(self) -> str:
        return f"ws-{self.ws}"

    def h(self, who: str, **extra: str) -> dict[str, str]:
        return {"authorization": f"Bearer {self._keys[f'{self.slug}/{who}@{self.slug}']}", **extra}

    async def req(
        self,
        method: str,
        path: str,
        who: str = "app",
        *,
        json: Any = None,
        key: str | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        hdrs = self.h(who)
        if method in MUTATING and key != "":
            hdrs["idempotency-key"] = key or str(uuid.uuid4())
        hdrs.update(headers or {})
        return await self.client.request(method, path, headers=hdrs, json=json, params=params)

    async def get(self, path: str, who: str = "admin", **params: Any) -> httpx.Response:
        return await self.req("GET", path, who, params=params or None)

    async def grant(self, who: str, kind: str, ref: str) -> None:
        """A scope grant for a seeded principal (what `kg scopes grant` does)."""
        ctx = WorkspaceContext.for_system_task(self.ws, reason="test grant")
        async with tenant_session(self.api.engine, ctx) as conn:
            pid = await admin.principal_id_by_handle(conn, ctx, f"{who}@{self.slug}")
            await admin.grant_scope(conn, ctx, principal_id=pid, kind=kind, ref=ref)

    async def principal_id(self, who: str) -> str:
        ctx = WorkspaceContext.for_system_task(self.ws, reason="test lookup")
        async with tenant_session(self.api.engine, ctx) as conn:
            return str(await admin.principal_id_by_handle(conn, ctx, f"{who}@{self.slug}"))

    # ---- command shortcuts --------------------------------------------------------------------------------------------------------------

    async def entity(self, type_: str, name: str, *, who: str = "app", key: str | None = None, **body: Any) -> httpx.Response:
        return await self.req("POST", "/v1/entities", who, json={"type": type_, "name": name, **body}, key=key)

    async def relationship(
        self, type_: str, source: Any, target: Any, *, who: str = "app", key: str | None = None, **body: Any
    ) -> httpx.Response:
        ref = lambda x: {"entity_id": x} if isinstance(x, str) else x  # noqa: E731
        return await self.req(
            "POST", "/v1/relationships", who, json={"type": type_, "source": ref(source), "target": ref(target), **body}, key=key
        )


async def make_tenant(api: Any, *, ontology: str | None = NO_MEMORY_YAML, grants: bool = True) -> Tenant:
    slug = f"t{uuid.uuid4().hex[:10]}"
    result = SeedResult(new_keys={})
    # the rig's `app` is a system of record (a trusted source, P7): the P2-P6 suites assert authoritative_system authority for its commands
    principals = [
        SeedPrincipal(f"{name}@{slug}", kind, role, name.title(), trusted_source=name == "app") for name, (kind, role) in ROLES.items()
    ]
    await seed_workspace(
        api.engine,
        slug,
        f"Tenant {slug}",
        principals,
        pepper=api.settings.auth_key_pepper.get_secret_value(),
        rotate_keys=False,
        result=result,
        grants={f"{name}@{slug}": g for name, g in DEFAULT_GRANTS.items()} if grants else None,
    )
    assert result.new_keys
    if ontology is not None:
        assert await publish_ontology(api.engine, ontology, slug=slug)
    return Tenant(api, slug, result.new_keys)


async def clear_graph(api: Any, group_id: str) -> None:
    driver = api.app.state.kg.graph._graphiti.driver
    await driver.client.execute_query("MATCH (n {group_id: $g}) DETACH DELETE n", parameters_={"g": group_id}, database_="neo4j")
