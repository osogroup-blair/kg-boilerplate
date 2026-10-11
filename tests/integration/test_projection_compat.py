"""WP0: the assumptions the projector depends on, checked against the real Neo4j (graphiti-core 0.30.2). Findings are recorded in ADR-021.

PJ-01 date/datetime attributes   PJ-02 saving without an embedding   PJ-03 edge round trip   PJ-04 idempotent upsert   PJ-05 zero chat calls
plus the tenancy guards on the new write methods (M8)."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode
from kg_common.errors import NotFound, ValidationFailed
from kg_domain.principal import WorkspaceContext
from kg_graph.errors import EndpointNotProjected
from kg_graph.service import GraphService
from kg_modelgw.egress import egress
from neo4j.exceptions import ClientError

from tests.conftest import NEO4J_URI, make_test_settings, require
from tests.support.router_env import Env

pytestmark = pytest.mark.integration
NOW = dt.datetime(2026, 10, 11, 12, 0, 0, 123456, tzinfo=dt.UTC)


def ctx() -> WorkspaceContext:
    return WorkspaceContext.for_system_task(uuid.uuid4(), reason="projection compat test")


class Rig:
    def __init__(self, env: Env, service: GraphService):
        self.env, self.service, self.groups = env, service, []  # type: ignore[var-annotated]

    def scoped(self, c: WorkspaceContext):  # type: ignore[no-untyped-def]
        self.groups.append(c.group_id)
        return self.service.scoped(c)

    def paths(self) -> list[str]:
        return [r["path"] for r in self.env.spy.requests]

    async def cypher(self, query: str, **params):  # type: ignore[no-untyped-def]
        records, _, _ = await self.service._graphiti.driver.client.execute_query(query, parameters_=params, database_="neo4j")
        return records


@pytest_asyncio.fixture()
async def rig() -> AsyncIterator[Rig]:
    require(NEO4J_URI, "neo4j")
    env = Env()
    service = GraphService(make_test_settings(oso_router_mock_url="http://mock/v1"), env.stack.transport, env.stack.config)
    await service.ensure_indices()
    r = Rig(env, service)
    yield r
    for gid in r.groups:
        await r.cypher("MATCH (n {group_id: $g}) DETACH DELETE n", g=gid)
    await service.aclose()


def emb(c: WorkspaceContext):  # type: ignore[no-untyped-def]
    return egress(classification="internal", purpose="graph_projection", workspace_id=str(c.workspace_id))


async def test_pj01_iso_string_attributes_round_trip_but_native_dates_do_not(rig: Rig) -> None:
    c = ctx()
    g = rig.scoped(c)
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    with emb(c):
        await g.upsert_entity_node(
            uuid=a, name="Sarah", type_key="Person", created_at=NOW, attributes={"hired": "2022-01-10", "seen": "2026-10-11T12:00:00Z"}
        )
    back = await g.read_node(a)
    assert back is not None and back.attributes == {
        "hired": "2022-01-10",
        "seen": "2026-10-11T12:00:00Z",
    }  # ISO strings come back as the same strings
    assert back.labels == frozenset({"Person"}) and back.name == "Sarah" and back.created_at == NOW
    # the finding: a python date would be stored as a native Neo4j Date and read back as one, so the projector serialises to ISO strings
    node = EntityNode(
        uuid=b,
        name="X",
        group_id=c.group_id,
        labels=["Person"],
        created_at=NOW,
        name_embedding=[0.1] * 384,
        attributes={"hired": dt.date(2022, 1, 10)},
    )
    await node.save(rig.service._graphiti.driver)
    stored = (await rig.cypher("MATCH (n:Entity {uuid: $u}) RETURN n.hired AS h", u=b))[0]["h"]
    assert not isinstance(stored, str)


async def test_pj01_non_scalar_and_reserved_attributes_are_refused(rig: Rig) -> None:
    c = ctx()
    g = rig.scoped(c)
    with emb(c):
        for bad in ({"x": [1, 2]}, {"x": {"a": 1}}, {"uuid": "nope"}, {"group_id": "ws-other"}, {"name_embedding": "x"}):
            with pytest.raises(ValidationFailed):
                await g.upsert_entity_node(uuid=str(uuid.uuid4()), name="N", type_key="Person", created_at=NOW, attributes=bad)
        with pytest.raises(ValidationFailed):
            await g.upsert_entity_node(
                uuid=str(uuid.uuid4()), name="N", type_key="Per son}) DETACH DELETE (n", created_at=NOW, attributes={}
            )


async def test_pj02_saving_without_an_embedding_fails_so_local_only_is_never_projected(rig: Rig) -> None:
    c = ctx()
    rig.scoped(c)
    node = EntityNode(name="NoEmbedding", group_id=c.group_id, labels=["Person"], created_at=NOW, name_embedding=None)
    with pytest.raises(ClientError):
        await node.save(rig.service._graphiti.driver)  # the vector-property procedure rejects null


async def test_pj03_edge_times_round_trip_exactly(rig: Rig) -> None:
    c = ctx()
    g = rig.scoped(c)
    a, b, e = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    start, end, recorded, superseded = NOW, NOW + dt.timedelta(days=30), NOW + dt.timedelta(days=1), NOW + dt.timedelta(days=2)
    with emb(c):
        await g.upsert_entity_node(uuid=a, name="Sarah", type_key="Person", created_at=NOW, attributes={})
        await g.upsert_entity_node(uuid=b, name="Acme", type_key="Organization", created_at=NOW, attributes={})
        await g.upsert_fact_edge(
            uuid=e,
            source_uuid=a,
            target_uuid=b,
            name="WORKS_AT",
            fact="Sarah works at Acme",
            valid_at=start,
            invalid_at=end,
            created_at=recorded,
            expired_at=superseded,
            attributes={"role": "Engineer", "platform_fact_id": e},
        )
    edge = await g.get_fact_edge(e)
    assert (edge.valid_at, edge.invalid_at, edge.created_at, edge.expired_at) == (start, end, recorded, superseded)
    assert edge.attributes == {"role": "Engineer", "platform_fact_id": e}  # M7: source_uuid/target_uuid stripped
    assert (edge.source_uuid, edge.target_uuid, edge.fact, edge.name) == (a, b, "Sarah works at Acme", "WORKS_AT")
    raw = await EntityEdge.get_by_uuid(rig.service._graphiti.driver, e)
    assert {"source_uuid", "target_uuid"} <= set(raw.attributes)  # the trap the read-back strips
    open_ended = str(uuid.uuid4())
    with emb(c):
        await g.upsert_fact_edge(
            uuid=open_ended,
            source_uuid=a,
            target_uuid=b,
            name="WORKS_AT",
            fact="x",
            valid_at=None,
            invalid_at=None,
            created_at=NOW,
            expired_at=None,
            attributes={},
        )
    assert (await g.get_fact_edge(open_ended)).invalid_at is None


async def test_pj04_upserting_the_same_uuid_updates_in_place(rig: Rig) -> None:
    c = ctx()
    g = rig.scoped(c)
    a, b, e = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    for _ in range(2):
        with emb(c):
            await g.upsert_entity_node(uuid=a, name="Sarah", type_key="Person", created_at=NOW, attributes={"v": 1})
            await g.upsert_entity_node(uuid=b, name="Acme", type_key="Organization", created_at=NOW, attributes={})
            await g.upsert_fact_edge(
                uuid=e,
                source_uuid=a,
                target_uuid=b,
                name="WORKS_AT",
                fact="f",
                valid_at=NOW,
                invalid_at=None,
                created_at=NOW,
                expired_at=None,
                attributes={},
            )
    assert (await g.node_count()) == 2
    assert len(await rig.cypher("MATCH ()-[r:RELATES_TO {uuid: $u}]->() RETURN r", u=e)) == 1
    with emb(c):
        await g.upsert_entity_node(uuid=a, name="Sarah Renamed", type_key="Person", created_at=NOW, attributes={"v": 2})
    back = await g.read_node(a)
    assert (
        back is not None and back.name == "Sarah Renamed" and back.attributes == {"v": 2}
    )  # replaced, not merged: removed attributes disappear
    with emb(c):
        await g.upsert_entity_node(uuid=a, name="Sarah Renamed", type_key="Person", created_at=NOW, attributes={})
    assert (await g.read_node(a)).attributes == {}  # type: ignore[union-attr]


async def test_pj05_direct_saves_make_embedding_calls_and_no_chat_calls(rig: Rig) -> None:
    c = ctx()
    g = rig.scoped(c)
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    with emb(c):
        await g.upsert_entity_node(uuid=a, name="Sarah", type_key="Person", created_at=NOW, attributes={})
        await g.upsert_entity_node(uuid=b, name="Acme", type_key="Organization", created_at=NOW, attributes={})
        await g.upsert_fact_edge(
            uuid=str(uuid.uuid4()),
            source_uuid=a,
            target_uuid=b,
            name="WORKS_AT",
            fact="f",
            valid_at=NOW,
            invalid_at=None,
            created_at=NOW,
            expired_at=None,
            attributes={},
        )
    paths = rig.paths()
    assert paths.count("/v1/embeddings") == 3 and not [p for p in paths if p.endswith("/chat/completions")]
    assert {r.purpose for r in rig.env.log} == {"graph_projection"} and {r.operation for r in rig.env.log} == {"embeddings"}


async def test_m8_another_groups_uuid_is_not_found_and_is_left_untouched(rig: Rig) -> None:
    mine, theirs = ctx(), ctx()
    gm, gt = rig.scoped(mine), rig.scoped(theirs)
    n = str(uuid.uuid4())
    with emb(theirs):
        await gt.upsert_entity_node(uuid=n, name="Theirs", type_key="Person", created_at=NOW, attributes={})
    with emb(mine), pytest.raises(NotFound):
        await gm.upsert_entity_node(uuid=n, name="Hijack", type_key="Person", created_at=NOW, attributes={})
    assert (await gt.read_node(n)).name == "Theirs"  # type: ignore[union-attr]
    assert await gm.read_node(n) is None
    with pytest.raises(NotFound):
        await gm.get_fact_edge(str(uuid.uuid4()))


async def test_m8_an_edge_cannot_reference_another_groups_nodes(rig: Rig) -> None:
    mine, theirs = ctx(), ctx()
    gm, gt = rig.scoped(mine), rig.scoped(theirs)
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    with emb(theirs):
        await gt.upsert_entity_node(uuid=b, name="Theirs", type_key="Organization", created_at=NOW, attributes={})
    with emb(mine):
        await gm.upsert_entity_node(uuid=a, name="Mine", type_key="Person", created_at=NOW, attributes={})
        with pytest.raises(EndpointNotProjected):
            await gm.upsert_fact_edge(
                uuid=str(uuid.uuid4()),
                source_uuid=a,
                target_uuid=b,
                name="WORKS_AT",
                fact="f",
                valid_at=NOW,
                invalid_at=None,
                created_at=NOW,
                expired_at=None,
                attributes={},
            )


async def test_orphan_listing_and_deletes_are_group_scoped(rig: Rig) -> None:
    mine, theirs = ctx(), ctx()
    gm, gt = rig.scoped(mine), rig.scoped(theirs)
    a, b, t = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    with emb(mine):
        await gm.upsert_entity_node(uuid=a, name="A", type_key="Person", created_at=NOW, attributes={})
        await gm.upsert_entity_node(uuid=b, name="B", type_key="Organization", created_at=NOW, attributes={})
    with emb(theirs):
        await gt.upsert_entity_node(uuid=t, name="T", type_key="Person", created_at=NOW, attributes={})
    nodes, edges = await gm.list_uuids()
    assert nodes == {a, b} and edges == set()
    await gm.delete_node(t)  # not ours: a no-op
    assert await gt.read_node(t) is not None
    await gm.delete_node(a)
    assert await gm.read_node(a) is None
