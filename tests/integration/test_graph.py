"""WP7: graph adapter against the real Neo4j. T-GR-01..05 (T-GR-06 is the import-linter contract, see tests/contract/test_boundaries.py)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from graphiti_core.cross_encoder.client import CrossEncoderClient
from graphiti_core.edges import EntityEdge
from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
from graphiti_core.nodes import EntityNode
from kg_common.errors import NotFound, ValidationFailed
from kg_domain.principal import WorkspaceContext
from kg_graph.adapters import E5Embedder, NoCrossEncoder
from kg_graph.group import GroupId
from kg_graph.service import GraphService
from kg_modelgw.egress import egress

from tests.conftest import NEO4J_URI, make_test_settings, require
from tests.support.router_env import Env

pytestmark = pytest.mark.integration


def ctx() -> WorkspaceContext:
    return WorkspaceContext.for_system_task(uuid.uuid4(), reason="graph test")


class Rig:
    def __init__(self, env: Env, service: GraphService):
        self.env, self.service = env, service
        self.groups: list[str] = []

    async def seed(self, c: WorkspaceContext, person: str, org: str) -> tuple[EntityNode, EntityNode, EntityEdge]:
        """Deterministic saves, no LLM (ADR-016). Embeddings come from the mock router through the platform transport."""
        g = self.service._graphiti
        self.groups.append(c.group_id)
        now = datetime.now(UTC)
        a = EntityNode(name=person, group_id=c.group_id, labels=["Entity", "Person"], summary=f"{person} is a person", created_at=now)
        b = EntityNode(name=org, group_id=c.group_id, labels=["Entity", "Organization"], summary=f"{org} is a company", created_at=now)
        e = EntityEdge(
            group_id=c.group_id,
            source_node_uuid=a.uuid,
            target_node_uuid=b.uuid,
            created_at=now,
            name="WORKS_AT",
            fact=f"{person} works at {org}",
            episodes=[],
            valid_at=now,
        )
        with egress(classification="internal", purpose="graph_extraction", workspace_id=str(c.workspace_id)):
            await a.generate_name_embedding(g.embedder)
            await b.generate_name_embedding(g.embedder)
            await e.generate_embedding(g.embedder)
        await a.save(g.driver)
        await b.save(g.driver)
        await e.save(g.driver)
        return a, b, e

    async def cleanup(self) -> None:
        for gid in self.groups:
            await self.service._graphiti.driver.client.execute_query(
                "MATCH (n {group_id: $g}) DETACH DELETE n", parameters_={"g": gid}, database_="neo4j"
            )


@pytest_asyncio.fixture()
async def rig() -> AsyncIterator[Rig]:
    require(NEO4J_URI, "neo4j")
    env = Env()
    settings = make_test_settings(oso_router_mock_url="http://mock/v1")
    service = GraphService(settings, env.stack.transport, env.stack.config)
    await service.ensure_indices()
    r = Rig(env, service)
    yield r
    await r.cleanup()
    await service.aclose()


async def test_gr_ping_and_node_count(rig):
    c = ctx()
    assert await rig.service.ping() and await rig.service.scoped(c).ping()
    assert await rig.service.scoped(c).node_count() == 0
    await rig.seed(c, "Sarah", "Acme")
    assert await rig.service.scoped(c).node_count() == 2


async def test_gr01_scoped_search_never_returns_another_groups_data(rig):
    a, b = ctx(), ctx()
    await rig.seed(a, "Sarah", "Acme")
    await rig.seed(b, "Sarah", "Acme")
    for mine in (a, b):
        with egress(classification="internal", purpose="graph_extraction", workspace_id=str(mine.workspace_id)):
            facts = await rig.service.scoped(mine).search("Where does Sarah work?")
        assert facts and {f.group_id for f in facts} == {mine.group_id}
    # the trap the facade closes: the raw Graphiti search without group_ids DOES leak across groups
    with egress(classification="internal", purpose="graph_extraction"):
        raw = await rig.service._graphiti.search("Where does Sarah work?")
    assert {a.group_id, b.group_id} <= {f.group_id for f in raw}


async def test_gr01_search_recipe_is_copied_not_mutated(rig):
    from graphiti_core.search.search_config_recipes import NODE_HYBRID_SEARCH_RRF

    c = ctx()
    await rig.seed(c, "Sarah", "Acme")
    before = NODE_HYBRID_SEARCH_RRF.model_dump()
    with egress(classification="internal", purpose="graph_extraction"):
        res = await rig.service.scoped(c).search("Sarah", NODE_HYBRID_SEARCH_RRF)
    assert {n.group_id for n in res.nodes} <= {c.group_id} and NODE_HYBRID_SEARCH_RRF.model_dump() == before


async def test_gr02_get_node_of_another_group_is_not_found(rig):
    a, b = ctx(), ctx()
    node_a, _, _ = await rig.seed(a, "Sarah", "Acme")
    node_b, _, _ = await rig.seed(b, "Sarah", "Acme")
    assert (await rig.service.scoped(a).get_node(node_a.uuid)).uuid == node_a.uuid
    with pytest.raises(NotFound):
        await rig.service.scoped(a).get_node(node_b.uuid)  # exists, but in another workspace
    with pytest.raises(NotFound):
        await rig.service.scoped(a).get_node(str(uuid.uuid4()))  # does not exist: identical outcome
    assert (await rig.service._graphiti.driver.client.execute_query("RETURN 1", database_="neo4j")) is not None


def test_gr03_group_id_cannot_be_built_from_a_raw_string_or_none():
    for bad in ("ws-abc", "", None):
        with pytest.raises(TypeError):
            GroupId.for_workspace(bad)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        WorkspaceContext("x")  # type: ignore[call-arg]


async def test_gr04_cypher_helper_requires_scope_and_rejects_group_override(rig):
    s = rig.service.scoped(ctx())
    with pytest.raises(ValidationFailed):
        await s.run_cypher("MATCH (n:Entity) RETURN count(n) AS n")
    with pytest.raises(ValidationFailed):
        await s.run_cypher("MATCH (n:Entity {group_id: $group_id}) RETURN n", group_id="ws-someone-else")
    assert (await s.run_cypher("MATCH (n:Entity {group_id: $group_id}) RETURN count(n) AS n"))[0]["n"] == 0


async def test_gr05_factory_wiring(rig):
    g = rig.service._graphiti
    assert type(g.llm_client) is OpenAIGenericClient  # NOT the default client (it calls /v1/responses)
    assert isinstance(g.cross_encoder, NoCrossEncoder) and isinstance(g.cross_encoder, CrossEncoderClient)
    assert isinstance(g.embedder, E5Embedder) and g.embedder.inner.config.embedding_dim == 384 and g.embedder.PREFIX == "query: "
    with pytest.raises(RuntimeError, match="disabled"):
        await g.cross_encoder.rank("q", ["p"])
    # the SDK clients under both LLM and embedder sit on the platform RouterTransport (L7)
    assert g.llm_client.client._client._transport is rig.env.stack.transport  # noqa: SLF001
    assert g.embedder.inner.client._client._transport is rig.env.stack.transport  # noqa: SLF001
    # and the driver did not schedule its own index build (L12)
    assert g.driver._init_task is None  # noqa: SLF001


async def test_gr05_graphiti_traffic_is_refused_without_an_egress_context(rig):
    g = rig.service._graphiti
    with pytest.raises(Exception):
        await g.embedder.create("hello")
    assert rig.env.spy.requests == []
    with egress(classification="internal", purpose="graph_extraction"):
        vec = await g.embedder.create("hello")
    assert len(vec) == 384 and rig.env.spy.requests[-1]["body"]["input"] == "query: hello"
