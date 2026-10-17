"""ScopedGraph: the ONLY public way to read or write the graph. Built from a WorkspaceContext, so group_id always comes from the
authenticated workspace. Closes the GR-04 traps: search() without group_ids, get_by_uuid across groups, group_id=None -> ''."""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from graphiti_core import Graphiti
from graphiti_core.driver.neo4j_driver import Neo4jDriver
from graphiti_core.edges import EntityEdge
from graphiti_core.errors import NodeNotFoundError
from graphiti_core.nodes import EntityNode, EpisodeType, EpisodicNode
from graphiti_core.search.search import search as graphiti_search
from graphiti_core.search.search_config import SearchConfig, SearchResults
from graphiti_core.search.search_config_recipes import EDGE_HYBRID_SEARCH_NODE_DISTANCE, EDGE_HYBRID_SEARCH_RRF
from graphiti_core.search.search_filters import ComparisonOperator, DateFilter, SearchFilters
from kg_common.errors import NotFound, ValidationFailed
from kg_domain.principal import WorkspaceContext

from kg_graph.compile import Compiled
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
    summary: str = ""  # Graphiti's derived text: the platform preserves it and never uses it as a fact source


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
    episodes: tuple[str, ...] = ()


@dataclass(frozen=True)
class GNode:
    """A node as Graphiti returned it for an episode: a plain value, so nothing outside kg_graph touches a Graphiti model."""

    uuid: str
    name: str
    labels: frozenset[str]  # without the generic 'Entity'
    summary: str
    attributes: dict[str, Any]
    name_embedding: tuple[float, ...] | None = None


@dataclass(frozen=True)
class GEdge:
    uuid: str
    name: str
    fact: str
    source_uuid: str
    target_uuid: str
    valid_at: dt.datetime | None
    invalid_at: dt.datetime | None
    created_at: dt.datetime | None
    expired_at: dt.datetime | None
    episodes: tuple[str, ...]
    attributes: dict[str, Any]


@dataclass(frozen=True)
class EpisodeResult:
    """What add_episode produced. `edges` is Graphiti's resolved + invalidated edges (an invalidated one does not list this episode)."""

    episode_uuid: str
    nodes: tuple[GNode, ...]
    edges: tuple[GEdge, ...]
    reused: bool = False  # True: the episode already existed with results, so no model was called


@dataclass(frozen=True)
class FactHit:
    uuid: str
    name: str
    fact: str
    source_uuid: str
    target_uuid: str
    valid_at: dt.datetime | None
    invalid_at: dt.datetime | None
    expired_at: dt.datetime | None
    created_at: dt.datetime | None
    episodes: tuple[str, ...]


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
    def __init__(self, graphiti: Graphiti, ctx: WorkspaceContext, group: GroupId | None = None):
        self._g = graphiti
        self.group_id = group or GroupId.for_workspace(
            ctx
        )  # a simulation's staging namespace is built by GraphService.staging, from the same context

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
        match = {
            "node": "MATCH (n:Entity {uuid: $uuid}) RETURN n.group_id AS g",
            "episode": "MATCH (n:Episodic {uuid: $uuid}) RETURN n.group_id AS g",
            "edge": "MATCH ()-[e:RELATES_TO {uuid: $uuid}]->() RETURN e.group_id AS g",
        }[kind]
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
        context. A node cannot be saved without an embedding (PJ-02), so `local_only` data is never projected.

        Graphiti owns `summary` (derived text), so an existing summary is kept; the platform owns name, labels and attributes, so anything else a
        model wrote on the node is dropped (P4 reconcile-then-restore, ADR-027)."""
        if not _LABEL.fullmatch(type_key):
            raise ValidationFailed("invalid entity type key")
        _check_attributes(attributes, _NODE_OWNED)
        existed = await self._assert_writable("node", uuid)
        summary = ""
        if existed:
            current = await self.read_node(uuid)
            summary = current.summary if current is not None else ""
        node = EntityNode(
            uuid=uuid,
            name=name,
            group_id=self.group_id.value,
            labels=[type_key],
            created_at=created_at,
            summary=summary,
            attributes=attributes,
        )
        await node.generate_name_embedding(self._g.embedder)
        await node.save(self._g.driver)
        if existed:
            await self._drop_other_labels(uuid, keep={"Entity", type_key})

    async def _drop_other_labels(self, uuid: str, *, keep: set[str]) -> None:
        """EntityNode.save only ever ADDS labels; a model that typed a node a second way would leave both. Labels are interpolated, so each must pass the
        identifier check (they come from the database, but the check is cheap and the same one Graphiti applies)."""
        rows = await self.run_cypher("MATCH (n:Entity {group_id: $group_id, uuid: $uuid}) RETURN labels(n) AS l", uuid=uuid)
        extra = [lbl for lbl in (rows[0]["l"] if rows else []) if lbl not in keep and _LABEL.fullmatch(lbl)]
        if extra:
            clause = ":".join(extra)
            await self.run_cypher(f"MATCH (n:Entity {{group_id: $group_id, uuid: $uuid}}) REMOVE n:{clause}", uuid=uuid)  # noqa: S608

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
        episodes: Sequence[str] = (),
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
                # a merge (or an identity split) changed an endpoint: Neo4j cannot re-point a relationship, so it is recreated with the SAME uuid
                await self.delete_edge(uuid)
        edge = EntityEdge(
            uuid=uuid,
            group_id=self.group_id.value,
            source_node_uuid=source_uuid,
            target_node_uuid=target_uuid,
            created_at=created_at,
            name=name,
            fact=fact,
            episodes=list(episodes),
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
        return NodeReadBack(
            uuid,
            str(props.get("name", "")),
            frozenset(rows[0]["l"]) - {"Entity"},
            props.get("created_at"),
            attrs,
            str(props.get("summary") or ""),
        )

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
            tuple(props.get("episodes") or ()),
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

    # ---- episodes (P4): Graphiti's extraction, reached ONLY through here so the group always comes from the authenticated workspace --------------

    async def ensure_episode_node(
        self,
        *,
        uuid: str,
        name: str,
        body: str,
        source: str,
        source_description: str,
        valid_at: dt.datetime,
        created_at: dt.datetime,
    ) -> bool:
        """Pre-creates the episode with a deterministic uuid (GR-09: `add_episode(uuid=)` only CONTINUES an episode that exists). True when it was new.
        An existing episode is left alone: saving it again would reset the entity edges it already lists. `created_at` is the platform's record time
        (Q2), so Graphiti's wall clock never reaches a read path."""
        owner = await self._owner_group("episode", uuid)
        if owner is not None and owner != self.group_id.value:
            raise NotFound()
        if owner is not None:
            return False
        node = EpisodicNode(
            uuid=uuid,
            name=name,
            group_id=self.group_id.value,
            labels=[],
            source=EpisodeType(source),
            content=body,
            source_description=source_description,
            created_at=created_at,
            valid_at=valid_at,
        )
        await node.save(self._g.driver)
        return True

    async def add_episode_scoped(
        self,
        *,
        uuid: str,
        name: str,
        body: str,
        source_description: str,
        reference_time: dt.datetime,
        source: str,
        compiled: Compiled,
        previous_episode_uuids: list[str] | None = None,
        **forbidden: Any,
    ) -> EpisodeResult:
        """Runs Graphiti's extraction on an episode that `ensure_episode_node` created. The caller holds the egress context (purpose graph_extraction).
        Communities are never updated (P9), the group is never a parameter, and the compiled types are passed as they are (sensitive attributes are not
        in them)."""
        if forbidden:
            raise ValidationFailed("add_episode_scoped does not accept caller-supplied groups or options")
        if (await self._owner_group("episode", uuid)) != self.group_id.value:
            raise NotFound()
        results = await self._g.add_episode(
            name=name,
            episode_body=body,
            source_description=source_description,
            reference_time=reference_time,
            source=EpisodeType(source),
            group_id=self.group_id.value,
            uuid=uuid,
            update_communities=False,
            entity_types=compiled.entity_types or None,
            edge_types=compiled.edge_types or None,
            edge_type_map=compiled.edge_type_map or None,
            previous_episode_uuids=previous_episode_uuids,
        )
        return EpisodeResult(
            episode_uuid=str(results.episode.uuid),
            nodes=tuple(_gnode(n) for n in results.nodes),
            edges=tuple(_gedge(e) for e in results.edges),
        )

    async def episode_has_results(self, uuid: str) -> bool:
        """True when Graphiti already saved this episode's entities or edges: a re-run after a crash (between Graphiti's save and our commit) must not
        call the model again (Q1/EX-04)."""
        rows = await self.run_cypher(
            "MATCH (e:Episodic {group_id: $group_id, uuid: $uuid}) "
            "RETURN size(coalesce(e.entity_edges, [])) AS edges, EXISTS { (e)-[:MENTIONS]->(:Entity) } AS mentions",
            uuid=uuid,
        )
        return bool(rows) and (int(rows[0]["edges"]) > 0 or bool(rows[0]["mentions"]))

    async def read_episode_results(self, uuid: str) -> EpisodeResult:
        """The nodes the episode mentions and the edges it lists, read back from the graph (the skip-the-model path)."""
        node_rows = await self.run_cypher(
            "MATCH (e:Episodic {group_id: $group_id, uuid: $uuid})-[:MENTIONS]->(n:Entity {group_id: $group_id}) "
            "RETURN properties(n) AS p, labels(n) AS l ORDER BY n.uuid",
            uuid=uuid,
        )
        edge_rows = await self.run_cypher(
            "MATCH (e:Episodic {group_id: $group_id, uuid: $uuid}) UNWIND coalesce(e.entity_edges, []) AS eu "
            "MATCH (a:Entity {group_id: $group_id})-[r:RELATES_TO {uuid: eu}]->(b:Entity {group_id: $group_id}) WHERE r.group_id = $group_id "
            "RETURN properties(r) AS p, a.uuid AS s, b.uuid AS t ORDER BY r.uuid",
            uuid=uuid,
        )
        nodes = []
        for r in node_rows:
            props = {k: _native(v) for k, v in r["p"].items() if k != "name_embedding"}
            nodes.append(
                GNode(
                    str(props["uuid"]),
                    str(props.get("name", "")),
                    frozenset(r["l"]) - {"Entity"},
                    str(props.get("summary") or ""),
                    {k: v for k, v in props.items() if k not in _NODE_OWNED},
                )
            )
        edges = []
        for r in edge_rows:
            props = {k: _native(v) for k, v in r["p"].items() if k != "fact_embedding"}
            edges.append(
                GEdge(
                    str(props["uuid"]),
                    str(props.get("name", "")),
                    str(props.get("fact", "")),
                    r["s"],
                    r["t"],
                    props.get("valid_at"),
                    props.get("invalid_at"),
                    props.get("created_at"),
                    props.get("expired_at"),
                    tuple(props.get("episodes") or ()),
                    {k: v for k, v in props.items() if k not in _EDGE_OWNED},
                )
            )
        return EpisodeResult(uuid, tuple(nodes), tuple(edges), reused=True)

    async def read_name_embeddings(self, uuids: Sequence[str]) -> dict[str, tuple[float, ...]]:
        rows = await self.run_cypher(
            "MATCH (n:Entity {group_id: $group_id}) WHERE n.uuid IN $uuids AND n.name_embedding IS NOT NULL RETURN n.uuid AS u, n.name_embedding AS v",
            uuids=list(uuids),
        )
        return {r["u"]: tuple(float(x) for x in r["v"]) for r in rows}

    async def similar_nodes(
        self, label: str, embedding: Sequence[float], *, exclude: Sequence[str], min_score: float, limit: int = 5
    ) -> list[tuple[str, float]]:
        """Same-type nodes of THIS group by cosine similarity of the stored name embedding (no model call: the vectors already exist)."""
        if not _LABEL.fullmatch(label):
            raise ValidationFailed("invalid entity type key")
        rows = await self.run_cypher(
            f"MATCH (n:Entity:{label} {{group_id: $group_id}}) WHERE NOT n.uuid IN $exclude AND n.name_embedding IS NOT NULL "  # noqa: S608
            "WITH n, vector.similarity.cosine(n.name_embedding, $emb) AS s WHERE s >= $min RETURN n.uuid AS u, s ORDER BY s DESC LIMIT $limit",
            exclude=list(exclude),
            emb=list(embedding),
            min=min_score,
            limit=limit,
        )
        return [(r["u"], float(r["s"])) for r in rows]

    async def episode_entity_edge_uuids(self, uuid: str) -> list[str]:
        rows = await self.run_cypher(
            "MATCH (e:Episodic {group_id: $group_id, uuid: $uuid}) RETURN coalesce(e.entity_edges, []) AS u", uuid=uuid
        )
        return list(rows[0]["u"]) if rows else []

    async def recent_episode_uuids(self, *, before: dt.datetime, exclude: str, limit: int) -> list[str]:
        """The latest earlier episodes of THIS group, newest first (the optional extraction context; 0 by default)."""
        rows = await self.run_cypher(
            "MATCH (e:Episodic {group_id: $group_id}) WHERE e.valid_at <= $before AND e.uuid <> $exclude "
            "RETURN e.uuid AS u ORDER BY e.valid_at DESC, e.uuid LIMIT $limit",
            before=before,
            exclude=exclude,
            limit=max(0, limit),
        )
        return [r["u"] for r in rows]

    async def restamp_episode(self, uuid: str, *, created_at: dt.datetime) -> None:
        """Graphiti stamps its own wall clock on the episodic edges it creates (GR-06); the platform's record time replaces it (Q2)."""
        await self.run_cypher(
            "MATCH (e:Episodic {group_id: $group_id, uuid: $uuid}) SET e.created_at = $at "
            "WITH e MATCH (e)-[m:MENTIONS]->(:Entity {group_id: $group_id}) SET m.created_at = $at",
            uuid=uuid,
            at=created_at,
        )

    async def move_mentions(self, node_uuid: str, to_uuid: str, *, episode_uuid: str | None = None) -> None:
        """Re-points the MENTIONS edges of `node_uuid` (all episodes, or one) at `to_uuid`, keeping each edge's uuid. Both nodes must be this group's."""
        for u in (node_uuid, to_uuid):
            if (await self._owner_group("node", u)) != self.group_id.value:
                raise NotFound()
        await self.run_cypher(
            "MATCH (e:Episodic {group_id: $group_id})-[m:MENTIONS]->(a:Entity {group_id: $group_id, uuid: $from}) "
            "WHERE $ep IS NULL OR e.uuid = $ep "
            "MATCH (b:Entity {group_id: $group_id, uuid: $into}) "
            "MERGE (e)-[m2:MENTIONS]->(b) ON CREATE SET m2.uuid = m.uuid, m2.group_id = m.group_id, m2.created_at = m.created_at "
            "DELETE m",
            **{"from": node_uuid, "into": to_uuid, "ep": episode_uuid},
        )

    async def merge_nodes(self, from_uuid: str, into_uuid: str) -> None:
        """Identity merge (ADR-028): the episodes that mentioned `from` now mention `into`, then `from` is deleted. RELATES_TO edges of `from` go with it;
        the projector re-creates them (same uuids, new endpoint) from the re-pointed Postgres facts."""
        if from_uuid == into_uuid:
            raise ValidationFailed("a node cannot be merged into itself")
        await self.move_mentions(from_uuid, into_uuid)
        await self.delete_node(from_uuid)

    # ---- deletion (P8, ADR-018 / ADR-044): the ONLY graph code the purge uses. Every statement is scoped to this workspace's group ----------------------------------

    async def remove_episode_scoped(self, uuid: str) -> bool:
        """Removes ONE episode and its MENTIONS edges, nothing else. Graphiti's own `remove_episode` also deletes edges it believes the episode owns and nodes only it
        mentions, which would take a retained fact's edge or a surviving entity's node with it (GR-08); the platform lists exactly what must go and re-projects what must stay."""
        rows = await self.run_cypher("MATCH (e:Episodic {group_id: $group_id, uuid: $uuid}) DETACH DELETE e RETURN 1 AS n", uuid=uuid)
        return bool(rows)

    async def delete_nodes_by_uuid(self, uuids: Sequence[str]) -> int:
        if not uuids:
            return 0
        rows = await self.run_cypher(
            "MATCH (n:Entity {group_id: $group_id}) WHERE n.uuid IN $uuids WITH collect(n) AS ns FOREACH (x IN ns | DETACH DELETE x) RETURN size(ns) AS n",
            uuids=list(uuids),
        )
        return int(rows[0]["n"])

    async def delete_edges_by_uuid(self, uuids: Sequence[str]) -> int:
        if not uuids:
            return 0
        rows = await self.run_cypher(
            "MATCH (:Entity {group_id: $group_id})-[r:RELATES_TO]->(:Entity {group_id: $group_id}) WHERE r.group_id = $group_id AND r.uuid IN $uuids "
            "WITH collect(r) AS rs FOREACH (x IN rs | DELETE x) RETURN size(rs) AS n",
            uuids=list(uuids),
        )
        return int(rows[0]["n"])

    async def clear_summaries(self, uuids: Sequence[str]) -> int:
        """Graphiti's derived summaries of surviving neighbours may cite a deleted subject. They are emptied (never regenerated by a model here); Graphiti writes a fresh one
        the next time an episode mentions the node, and the API already labels summaries as derived."""
        if not uuids:
            return 0
        rows = await self.run_cypher(
            "MATCH (n:Entity {group_id: $group_id}) WHERE n.uuid IN $uuids AND coalesce(n.summary, '') <> '' SET n.summary = '' RETURN count(n) AS n",
            uuids=list(uuids),
        )
        return int(rows[0]["n"])

    async def prune_dangling_refs(self) -> int:
        """Edges list the episodes that support them and episodes list the edges they created; after a removal both can name something that is gone (GR-08). Prunes both."""
        a = await self.run_cypher(
            "MATCH (:Entity {group_id: $group_id})-[r:RELATES_TO]->(:Entity {group_id: $group_id}) WHERE r.group_id = $group_id AND r.episodes IS NOT NULL "
            "WITH r, [u IN r.episodes WHERE EXISTS { MATCH (:Episodic {group_id: $group_id, uuid: u}) }] AS keep WHERE size(keep) <> size(r.episodes) "
            "SET r.episodes = keep RETURN count(r) AS n"
        )
        b = await self.run_cypher(
            "MATCH (e:Episodic {group_id: $group_id}) WHERE e.entity_edges IS NOT NULL "
            "WITH e, [u IN e.entity_edges WHERE EXISTS { MATCH (:Entity {group_id: $group_id})-[r:RELATES_TO {uuid: u}]->(:Entity {group_id: $group_id}) WHERE r.group_id = $group_id }] AS keep "
            "WHERE size(keep) <> size(e.entity_edges) SET e.entity_edges = keep RETURN count(e) AS n"
        )
        return int(a[0]["n"]) + int(b[0]["n"])

    async def orphan_sweep(self, known_nodes: Sequence[str]) -> int:
        """Nodes nothing mentions and nothing relates, that the platform does not know (extraction debris). A platform entity with no edges yet is NOT an orphan."""
        rows = await self.run_cypher(
            "MATCH (n:Entity {group_id: $group_id}) WHERE NOT n.uuid IN $known AND NOT (n)<-[:MENTIONS]-(:Episodic) AND NOT (n)-[:RELATES_TO]-() "
            "WITH collect(n) AS ns FOREACH (x IN ns | DETACH DELETE x) RETURN size(ns) AS n",
            known=list(known_nodes),
        )
        return int(rows[0]["n"])

    async def summary_hits(self, terms: Sequence[str], exclude: Sequence[str]) -> list[str]:
        """Surviving nodes whose derived summary mentions one of the terms."""
        if not terms:
            return []
        rows = await self.run_cypher(
            "MATCH (n:Entity {group_id: $group_id}) WHERE NOT n.uuid IN $exclude AND coalesce(n.summary, '') <> '' "
            "AND any(t IN $terms WHERE toLower(n.summary) CONTAINS toLower(t)) RETURN n.uuid AS u ORDER BY u",
            terms=list(terms),
            exclude=list(exclude),
        )
        return [r["u"] for r in rows]

    async def episode_edge_refs(self, episode_uuids: Sequence[str]) -> list[str]:
        """The edge uuids these episodes list (so the plan can name the edges that were created from them)."""
        if not episode_uuids:
            return []
        rows = await self.run_cypher(
            "MATCH (e:Episodic {group_id: $group_id}) WHERE e.uuid IN $uuids UNWIND coalesce(e.entity_edges, []) AS u RETURN DISTINCT u ORDER BY u",
            uuids=list(episode_uuids),
        )
        return [r["u"] for r in rows]

    async def scan_text(self, terms: Sequence[str], ids: Sequence[str]) -> list[tuple[str, int]]:
        """Verification (ADR-018 g): how many nodes, edges and episodes of this group still hold a term in any text property, or carry a deleted id. Locations and counts only."""
        out: list[tuple[str, int]] = []
        t = [x.lower() for x in terms]
        probes: list[tuple[str, str, dict[str, Any]]] = []
        if t:
            has = "any(k IN keys({v}) WHERE k <> 'name_embedding' AND k <> 'fact_embedding' AND {v}[k] IS :: STRING AND any(t IN $terms WHERE toLower({v}[k]) CONTAINS t))"
            probes += [
                ("node.text", f"MATCH (n:Entity {{group_id: $group_id}}) WHERE {has.format(v='n')} RETURN count(n) AS c", {"terms": t}),
                (
                    "edge.text",
                    f"MATCH (:Entity {{group_id: $group_id}})-[r:RELATES_TO]->(:Entity {{group_id: $group_id}}) WHERE r.group_id = $group_id AND {has.format(v='r')} RETURN count(r) AS c",
                    {"terms": t},
                ),
                (
                    "episode.text",
                    f"MATCH (e:Episodic {{group_id: $group_id}}) WHERE {has.format(v='e')} RETURN count(e) AS c",
                    {"terms": t},
                ),
            ]
        if ids:
            probes += [
                ("node.id", "MATCH (n:Entity {group_id: $group_id}) WHERE n.uuid IN $ids RETURN count(n) AS c", {"ids": list(ids)}),
                (
                    "edge.id",
                    "MATCH (:Entity {group_id: $group_id})-[r:RELATES_TO]->(:Entity {group_id: $group_id}) WHERE r.group_id = $group_id AND r.uuid IN $ids RETURN count(r) AS c",
                    {"ids": list(ids)},
                ),
                ("episode.id", "MATCH (e:Episodic {group_id: $group_id}) WHERE e.uuid IN $ids RETURN count(e) AS c", {"ids": list(ids)}),
            ]
        for label, query, params in probes:
            rows = await self.run_cypher(query, **params)
            if rows and int(rows[0]["c"]):
                out.append((label, int(rows[0]["c"])))
        return out

    async def blank_episode_content(self, uuids: Sequence[str]) -> int:
        """Retention (`retention.episode_content_days`): Graphiti keeps the text of an episode in Neo4j; after the window the text goes and the episode node stays."""
        if not uuids:
            return 0
        rows = await self.run_cypher(
            "MATCH (e:Episodic {group_id: $group_id}) WHERE e.uuid IN $uuids AND coalesce(e.content, '') <> '' SET e.content = '' RETURN count(e) AS n",
            uuids=list(uuids),
        )
        return int(rows[0]["n"])

    async def clear_group(self) -> int:
        """`clear_data([group])` (ADR-015 §5): this workspace's nodes and edges, nobody else's. For a workspace deletion."""
        rows = await self.run_cypher(
            "MATCH (n {group_id: $group_id}) WITH collect(n) AS ns FOREACH (x IN ns | DETACH DELETE x) RETURN size(ns) AS n"
        )
        return int(rows[0]["n"])

    async def clear_staging_groups(self) -> int:
        """A workspace deletion also removes the staging namespaces of its simulations (`<group>-sim-*`)."""
        records, _, _ = await self._neo4j.client.execute_query(
            "MATCH (n) WHERE n.group_id STARTS WITH $prefix WITH collect(n) AS ns FOREACH (x IN ns | DETACH DELETE x) RETURN size(ns) AS n",
            parameters_={"prefix": f"{self.group_id.value}-sim-"},
            database_=self._db,
        )
        return int(records[0]["n"]) if records else 0

    # ---- search (P4 building block for P6): the platform recipe, never Graphiti's defaults (ADR-019) ---------------------------------------------

    async def search_facts(
        self,
        query: str,
        *,
        as_of: dt.datetime | None = None,
        known_at: dt.datetime | None = None,
        center_entity_id: str | None = None,
        limit: int = 10,
        query_vector: Sequence[float] | None = None,
    ) -> list[FactHit]:
        """Edge-only hybrid search (BM25 + vectors, RRF; node distance when centred), always scoped to this group and always filtered:
        `expired_at IS NULL` (ADR-018: Postgres status is authoritative, and non-active facts are expired on the edge) unless a known_at is asked,
        then valid time (`as_of`, an unknown start counts as always) and record time (`known_at`). No cross-encoder (the router drops logprobs)."""
        if center_entity_id is not None and (await self._owner_group("node", center_entity_id)) != self.group_id.value:
            raise NotFound()
        recipe = (EDGE_HYBRID_SEARCH_NODE_DISTANCE if center_entity_id else EDGE_HYBRID_SEARCH_RRF).model_copy(deep=True)
        recipe.limit = max(1, min(limit, 100))
        null, op = DateFilter(comparison_operator=ComparisonOperator.is_null), ComparisonOperator
        filters = SearchFilters()
        if known_at is None:
            filters.expired_at = [[null]]
        else:
            filters.created_at = [[DateFilter(date=known_at, comparison_operator=op.less_than_equal)]]
            filters.expired_at = [[null], [DateFilter(date=known_at, comparison_operator=op.greater_than)]]
        # "current" means true NOW unless a date is asked for: an ended fact (a past job) is not an answer to "where does she work"
        when = as_of or dt.datetime.now(dt.UTC)
        filters.valid_at = [[DateFilter(date=when, comparison_operator=op.less_than_equal)], [null]]
        filters.invalid_at = [[DateFilter(date=when, comparison_operator=op.greater_than)], [null]]
        if query_vector is not None:
            # CX-01 (ADR-032): Graphiti's `search_` embeds the query itself and cannot take a vector, but the module-level `search` it wraps can. A caller that has
            # already embedded the query (the context engine, once for the graph and the memory search) passes the vector so the router is called once.
            found = await graphiti_search(
                self._g.clients,
                query,
                [self.group_id.value],
                recipe,
                filters,
                center_entity_id,
                None,
                [float(x) for x in query_vector],
            )
        else:
            found = await self._g.search_(
                query, config=recipe, group_ids=[self.group_id.value], center_node_uuid=center_entity_id, search_filter=filters
            )
        return [
            FactHit(
                str(e.uuid),
                e.name,
                e.fact,
                e.source_node_uuid,
                e.target_node_uuid,
                e.valid_at,
                e.invalid_at,
                e.expired_at,
                e.created_at,
                tuple(e.episodes or ()),
            )
            for e in found.edges
            if e.group_id == self.group_id.value  # belt and braces: the query is scoped, and a hit from another group is still dropped
        ]


def _gnode(n: EntityNode) -> GNode:
    attrs = {k: v for k, v in (n.attributes or {}).items() if isinstance(v, str | int | float | bool)}
    emb = tuple(float(x) for x in n.name_embedding) if n.name_embedding else None
    return GNode(str(n.uuid), n.name, frozenset(n.labels or []) - {"Entity"}, n.summary or "", attrs, emb)


def _gedge(e: EntityEdge) -> GEdge:
    attrs = {k: v for k, v in (e.attributes or {}).items() if isinstance(v, str | int | float | bool)}
    return GEdge(
        str(e.uuid),
        e.name,
        e.fact,
        e.source_node_uuid,
        e.target_node_uuid,
        e.valid_at,
        e.invalid_at,
        e.created_at,
        e.expired_at,
        tuple(e.episodes or ()),
        attrs,
    )
