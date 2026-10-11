"""ScopedGraph: the ONLY public way to read or write the graph. Built from a WorkspaceContext, so group_id always comes from the
authenticated workspace. Closes the GR-04 traps: search() without group_ids, get_by_uuid across groups, group_id=None -> ''."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any, cast

from graphiti_core import Graphiti
from graphiti_core.driver.neo4j_driver import Neo4jDriver
from graphiti_core.edges import EntityEdge
from graphiti_core.errors import NodeNotFoundError
from graphiti_core.nodes import EntityNode
from graphiti_core.search.search_config import SearchConfig, SearchResults
from kg_common.errors import NotFound, ValidationFailed
from kg_domain.principal import WorkspaceContext

from kg_graph.errors import EndpointNotProjected
from kg_graph.group import GroupId

_LABEL = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
# properties Graphiti (or the projector) owns on a node/edge; an attribute with one of these names would overwrite them
_NODE_OWNED = frozenset({"uuid", "name", "group_id", "labels", "created_at", "summary", "name_embedding", "attributes"})
_EDGE_OWNED = frozenset(
    {
        "uuid",
        "group_id",
        "name",
        "fact",
        "fact_embedding",
        "episodes",
        "created_at",
        "expired_at",
        "valid_at",
        "invalid_at",
        "reference_time",
        "attributes",
    }
    | {"source_uuid", "target_uuid", "source_node_uuid", "target_node_uuid"}
)


@dataclass(frozen=True)
class NodeReadBack:
    uuid: str
    name: str
    labels: frozenset[str]
    created_at: dt.datetime | None
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EdgeReadBack:
    uuid: str
    name: str
    fact: str
    source_uuid: str
    target_uuid: str
    valid_at: dt.datetime | None
    invalid_at: dt.datetime | None
    created_at: dt.datetime | None
    expired_at: dt.datetime | None
    attributes: dict[str, Any] = field(default_factory=dict)  # M7: source_uuid/target_uuid (stored as properties by Graphiti) are stripped


def _native(value: Any) -> Any:
    to_native = getattr(value, "to_native", None)
    return to_native() if callable(to_native) else value


def _check_attributes(attributes: dict[str, Any], owned: frozenset[str]) -> None:
    for key, value in attributes.items():
        if key in owned:
            raise ValidationFailed("attribute name is reserved by the graph")
        if not isinstance(value, str | int | float | bool):
            raise ValidationFailed("graph attributes must be scalars")


class ScopedGraph:
    def __init__(self, graphiti: Graphiti, ctx: WorkspaceContext):
        self._g = graphiti
        self.group_id = GroupId.for_workspace(ctx)

    @property
    def _neo4j(self) -> Neo4jDriver:
        return cast(Neo4jDriver, self._g.driver)

    @property
    def _db(self) -> str:
        return str(self._neo4j._database)

    async def ping(self) -> bool:
        records, _, _ = await self._neo4j.client.execute_query("RETURN 1 AS ok", database_=self._db)
        return bool(records and records[0]["ok"] == 1)

    async def run_cypher(self, query: str, **params: Any) -> list[dict[str, Any]]:
        """Scoped Cypher: the text MUST reference $group_id (static check), and callers cannot supply their own group_id."""
        if "$group_id" not in query:
            raise ValidationFailed("graph query must be scoped with $group_id")
        if "group_id" in params:
            raise ValidationFailed("group_id is supplied by the adapter")
        records, _, _ = await self._neo4j.client.execute_query(
            query, parameters_={**params, "group_id": self.group_id.value}, database_=self._db
        )
        return [dict(r) for r in records]

    async def node_count(self) -> int:
        rows = await self.run_cypher("MATCH (n:Entity {group_id: $group_id}) RETURN count(n) AS n")
        return int(rows[0]["n"])

    async def search(self, query: str, config: SearchConfig | None = None, *, num_results: int = 10) -> list[EntityEdge] | SearchResults:
        """Always `group_ids=[this workspace]`. A caller-provided recipe is copied, never mutated."""
        if config is None:
            return await self._g.search(query, group_ids=[self.group_id.value], num_results=num_results)
        return await self._g.search_(query, config=config.model_copy(deep=True), group_ids=[self.group_id.value])

    async def get_node(self, uuid: str) -> EntityNode:
        """Graphiti's by-uuid fetch is group-unaware: fetch, then verify the group. Another workspace's node is NotFound (no oracle)."""
        try:
            node = await EntityNode.get_by_uuid(self._g.driver, uuid)
        except NodeNotFoundError as exc:
            raise NotFound() from exc
        if node.group_id != self.group_id.value:
            raise NotFound()
        return cast(EntityNode, node)

    # ---- writes (ADR-016: direct saves, never add_triplet, so no LLM call). The group id is never a parameter (M8). ----------------------------

    async def _owner_group(self, kind: str, uuid: str) -> str | None:
        """The group that owns a node/edge uuid anywhere in the database, or None. Graphiti MERGEs on uuid alone, so a write must first prove the uuid is
        free or ours; otherwise it would overwrite (and re-home) another workspace's data."""
        match = (
            "MATCH (n:Entity {uuid: $uuid}) RETURN n.group_id AS g"
            if kind == "node"
            else "MATCH ()-[e:RELATES_TO {uuid: $uuid}]->() RETURN e.group_id AS g"
        )
        records, _, _ = await self._neo4j.client.execute_query(match, parameters_={"uuid": uuid}, database_=self._db)
        return str(records[0]["g"]) if records else None

    async def _assert_writable(self, kind: str, uuid: str) -> bool:
        """True if the uuid exists in this group (an update), False if it is free (an insert). Another group's uuid is NotFound."""
        owner = await self._owner_group(kind, uuid)
        if owner is not None and owner != self.group_id.value:
            raise NotFound()
        return owner is not None

    async def upsert_entity_node(self, *, uuid: str, name: str, type_key: str, attributes: dict[str, Any], created_at: dt.datetime) -> None:
        """Idempotent: the node becomes exactly this state (PJ-04). Embeds the name through the platform transport, so the caller must hold an egress
        context. A node cannot be saved without an embedding (PJ-02), so `local_only` data is never projected."""
        if not _LABEL.fullmatch(type_key):
            raise ValidationFailed("invalid entity type key")
        _check_attributes(attributes, _NODE_OWNED)
        await self._assert_writable("node", uuid)
        node = EntityNode(
            uuid=uuid, name=name, group_id=self.group_id.value, labels=[type_key], created_at=created_at, summary="", attributes=attributes
        )
        await node.generate_name_embedding(self._g.embedder)
        await node.save(self._g.driver)

    async def upsert_fact_edge(
        self,
        *,
        uuid: str,
        source_uuid: str,
        target_uuid: str,
        name: str,
        fact: str,
        valid_at: dt.datetime | None,
        invalid_at: dt.datetime | None,
        created_at: dt.datetime,
        expired_at: dt.datetime | None,
        attributes: dict[str, Any],
    ) -> None:
        if not _LABEL.fullmatch(name):
            raise ValidationFailed("invalid relationship type key")
        _check_attributes(attributes, _EDGE_OWNED)
        rows = await self.run_cypher(
            "MATCH (n:Entity {group_id: $group_id}) WHERE n.uuid IN $uuids RETURN n.uuid AS u", uuids=[source_uuid, target_uuid]
        )
        if {r["u"] for r in rows} != {source_uuid, target_uuid}:
            raise EndpointNotProjected()
        if await self._assert_writable("edge", uuid):
            existing = await self.read_edge(uuid)
            if existing is not None and (existing.source_uuid, existing.target_uuid) != (source_uuid, target_uuid):
                raise ValidationFailed("a fact's endpoints never change")
        edge = EntityEdge(
            uuid=uuid,
            group_id=self.group_id.value,
            source_node_uuid=source_uuid,
            target_node_uuid=target_uuid,
            created_at=created_at,
            name=name,
            fact=fact,
            episodes=[],
            valid_at=valid_at,
            invalid_at=invalid_at,
            expired_at=expired_at,
            attributes=attributes,
        )
        await edge.generate_embedding(self._g.embedder)
        await edge.save(self._g.driver)

    # ---- reads for the projection check ----------------------------------------------------------------------------------------------------

    async def read_node(self, uuid: str) -> NodeReadBack | None:
        rows = await self.run_cypher(
            "MATCH (n:Entity {group_id: $group_id, uuid: $uuid}) RETURN properties(n) AS p, labels(n) AS l", uuid=uuid
        )
        if not rows:
            return None
        props: dict[str, Any] = {k: _native(v) for k, v in rows[0]["p"].items()}
        attrs = {k: v for k, v in props.items() if k not in _NODE_OWNED}
        return NodeReadBack(uuid, str(props.get("name", "")), frozenset(rows[0]["l"]) - {"Entity"}, props.get("created_at"), attrs)

    async def read_edge(self, uuid: str) -> EdgeReadBack | None:
        rows = await self.run_cypher(
            "MATCH (a:Entity {group_id: $group_id})-[e:RELATES_TO {uuid: $uuid}]->(b:Entity {group_id: $group_id}) "
            "WHERE e.group_id = $group_id RETURN properties(e) AS p, a.uuid AS s, b.uuid AS t",
            uuid=uuid,
        )
        if not rows:
            return None
        props: dict[str, Any] = {k: _native(v) for k, v in rows[0]["p"].items()}
        attrs = {k: v for k, v in props.items() if k not in _EDGE_OWNED}
        return EdgeReadBack(
            uuid,
            str(props.get("name", "")),
            str(props.get("fact", "")),
            rows[0]["s"],
            rows[0]["t"],
            props.get("valid_at"),
            props.get("invalid_at"),
            props.get("created_at"),
            props.get("expired_at"),
            attrs,
        )

    async def get_fact_edge(self, uuid: str) -> EdgeReadBack:
        """Group-checked: another workspace's edge is NotFound, indistinguishable from a missing one."""
        edge = await self.read_edge(uuid)
        if edge is None:
            raise NotFound()
        return edge

    async def list_uuids(self) -> tuple[set[str], set[str]]:
        """Every node and edge uuid in this workspace's group (for orphan detection)."""
        nodes = await self.run_cypher("MATCH (n:Entity {group_id: $group_id}) RETURN n.uuid AS u")
        edges = await self.run_cypher(
            "MATCH (:Entity {group_id: $group_id})-[e:RELATES_TO]->(:Entity) WHERE e.group_id = $group_id RETURN e.uuid AS u"
        )
        return {r["u"] for r in nodes}, {r["u"] for r in edges}

    async def delete_node(self, uuid: str) -> None:
        await self.run_cypher("MATCH (n:Entity {group_id: $group_id, uuid: $uuid}) DETACH DELETE n", uuid=uuid)

    async def delete_edge(self, uuid: str) -> None:
        await self.run_cypher(
            "MATCH (:Entity {group_id: $group_id})-[e:RELATES_TO {uuid: $uuid}]->(:Entity) WHERE e.group_id = $group_id DELETE e", uuid=uuid
        )
