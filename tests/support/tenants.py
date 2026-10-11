"""P2 test rig: a throw-away workspace (seeded principals, an active ontology) per test, plus a small client wrapper.

Workspace ids are derived from the slug, so a unique slug gives a fully isolated tenant in the shared test database and graph."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import httpx
from kg_cli.seed import SeedPrincipal, SeedResult, publish_ontology, seed_workspace, workspace_id_for
from kg_domain.enums import PrincipalKind, Role

EXAMPLE_YAML = (Path(__file__).resolve().parents[2] / "examples" / "ontologies" / "personal-knowledge.yaml").read_text()
ROLES: dict[str, tuple[PrincipalKind, Role]] = {
    "admin": (PrincipalKind.USER, Role.WORKSPACE_ADMIN),
    "reviewer": (PrincipalKind.USER, Role.REVIEWER),
    "operator": (PrincipalKind.USER, Role.OPERATOR),
    "app": (PrincipalKind.APP, Role.APP_WRITER),
    "agent": (PrincipalKind.AGENT, Role.AGENT),
    "reader": (PrincipalKind.USER, Role.READER),
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


async def make_tenant(api: Any, *, ontology: str | None = EXAMPLE_YAML) -> Tenant:
    slug = f"t{uuid.uuid4().hex[:10]}"
    result = SeedResult(new_keys={})
    principals = [SeedPrincipal(f"{name}@{slug}", kind, role, name.title()) for name, (kind, role) in ROLES.items()]
    await seed_workspace(
        api.engine,
        slug,
        f"Tenant {slug}",
        principals,
        pepper=api.settings.auth_key_pepper.get_secret_value(),
        rotate_keys=False,
        result=result,
    )
    assert result.new_keys
    if ontology is not None:
        assert await publish_ontology(api.engine, ontology, slug=slug)
    return Tenant(api, slug, result.new_keys)


async def clear_graph(api: Any, group_id: str) -> None:
    driver = api.app.state.kg.graph._graphiti.driver
    await driver.client.execute_query("MATCH (n {group_id: $g}) DETACH DELETE n", parameters_={"g": group_id}, database_="neo4j")
