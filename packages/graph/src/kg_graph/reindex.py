"""Blue/green graph rebuilds (phase-9 plan WP8, Flow F): the Neo4j half.

WHY PARKED UUIDS. Neo4j Community has one database, and the platform relies on two facts that forbid two live copies of the same graph in it: `Entity.uuid` and `RELATES_TO.uuid` are
GLOBALLY unique (P3's constraints, which make concurrent MERGEs safe), and Graphiti MERGEs on uuid alone, so a second node with the same uuid in another group would be
written by an operation meant for the first. A rebuilt namespace therefore holds its nodes, edges and episodes under a TAGGED uuid (`r2:<uuid>`) with the real one kept in `p_uuid`
(and the uuid lists of episodes and edges kept in `p_entity_edges` / `p_episodes`). While parked it is invisible to every read (the platform reads only the ACTIVE group, and a
parked uuid matches no platform id). The cutover is ONE Neo4j transaction that parks the old active group under its own tag and unparks the new one; the Postgres pointer flips right
after, still under the workspace's graph lock. Rolling back is the same exchange the other way.

This module touches only the groups it is handed, and a group can only be built with `GroupId.for_namespace(ctx, ...)`, so it can never name another workspace's data."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

from graphiti_core import Graphiti
from kg_common.errors import ValidationFailed
from kg_domain.principal import WorkspaceContext

from kg_graph.group import GroupId

_PARK = [
    "MATCH (n:Entity {group_id: $g}) WHERE n.p_uuid IS NULL SET n.p_uuid = n.uuid, n.uuid = $tag + n.uuid",
    "MATCH (n:Episodic {group_id: $g}) WHERE n.p_uuid IS NULL "
    "SET n.p_uuid = n.uuid, n.uuid = $tag + n.uuid, n.p_entity_edges = coalesce(n.entity_edges, []), n.entity_edges = [u IN coalesce(n.entity_edges, []) | $tag + u]",
    "MATCH ()-[r:RELATES_TO {group_id: $g}]->() WHERE r.p_uuid IS NULL "
    "SET r.p_uuid = r.uuid, r.uuid = $tag + r.uuid, r.p_episodes = coalesce(r.episodes, []), r.episodes = [u IN coalesce(r.episodes, []) | $tag + u]",
    "MATCH ()-[m:MENTIONS {group_id: $g}]->() WHERE m.p_uuid IS NULL SET m.p_uuid = m.uuid, m.uuid = $tag + m.uuid",
]
_UNPARK = [
    "MATCH (n:Entity {group_id: $g}) WHERE n.p_uuid IS NOT NULL SET n.uuid = n.p_uuid REMOVE n.p_uuid, n.p_rev",
    "MATCH (n:Episodic {group_id: $g}) WHERE n.p_uuid IS NOT NULL SET n.uuid = n.p_uuid, n.entity_edges = n.p_entity_edges REMOVE n.p_uuid, n.p_entity_edges, n.p_rev",
    "MATCH ()-[r:RELATES_TO {group_id: $g}]->() WHERE r.p_uuid IS NOT NULL SET r.uuid = r.p_uuid, r.episodes = r.p_episodes REMOVE r.p_uuid, r.p_episodes, r.p_rev",
    "MATCH ()-[m:MENTIONS {group_id: $g}]->() WHERE m.p_uuid IS NOT NULL SET m.uuid = m.p_uuid REMOVE m.p_uuid",
]
_STAMP = [
    # a group built directly under tagged uuids records the real ones
    "MATCH (n:Entity {group_id: $g}) WHERE n.p_uuid IS NULL AND n.uuid STARTS WITH $tag SET n.p_uuid = substring(n.uuid, $len)",
    "MATCH (n:Episodic {group_id: $g}) WHERE n.p_uuid IS NULL AND n.uuid STARTS WITH $tag "
    "SET n.p_uuid = substring(n.uuid, $len), n.p_entity_edges = [u IN coalesce(n.entity_edges, []) | substring(u, $len)]",
    "MATCH ()-[r:RELATES_TO {group_id: $g}]->() WHERE r.p_uuid IS NULL AND r.uuid STARTS WITH $tag "
    "SET r.p_uuid = substring(r.uuid, $len), r.p_episodes = [u IN coalesce(r.episodes, []) | substring(u, $len)]",
    "MATCH ()-[m:MENTIONS {group_id: $g}]->() WHERE m.p_uuid IS NULL AND m.uuid STARTS WITH $tag SET m.p_uuid = substring(m.uuid, $len)",
]


@dataclass(frozen=True)
class GroupCounts:
    nodes: int
    edges: int
    episodes: int
    mentions: int

    def as_dict(self) -> dict[str, int]:
        return {"nodes": self.nodes, "edges": self.edges, "episodes": self.episodes, "mentions": self.mentions}


def tag_for(namespace: str) -> str:
    """`ws-<uuid>-r3` -> `r3:`; the default group parks under `base:`."""
    last = namespace.rsplit("-", 1)[-1]
    return f"{last}:" if last.startswith("r") and last[1:].isdigit() else "base:"


class ReindexGraph:
    """Group-level operations for a rebuild. Build it from a WorkspaceContext; every statement is parameterised with a group it validated."""

    def __init__(self, graphiti: Graphiti, ctx: WorkspaceContext):
        self._g, self._ctx = graphiti, ctx

    def _group(self, namespace: str) -> str:
        return GroupId.for_namespace(self._ctx, namespace).value

    @property
    def _client(self) -> Any:
        return cast(Any, self._g.driver).client

    @property
    def _db(self) -> str:
        return str(cast(Any, self._g.driver)._database)

    async def _run(self, query: str, **params: Any) -> list[dict[str, Any]]:
        records, _, _ = await self._client.execute_query(query, parameters_=params, database_=self._db)
        return [dict(r) for r in records]

    async def counts(self, namespace: str) -> GroupCounts:
        g = self._group(namespace)
        n = (await self._run("MATCH (n:Entity {group_id: $g}) RETURN count(n) AS c", g=g))[0]["c"]
        e = (await self._run("MATCH ()-[r:RELATES_TO {group_id: $g}]->() RETURN count(r) AS c", g=g))[0]["c"]
        p = (await self._run("MATCH (n:Episodic {group_id: $g}) RETURN count(n) AS c", g=g))[0]["c"]
        m = (await self._run("MATCH ()-[r:MENTIONS {group_id: $g}]->() RETURN count(r) AS c", g=g))[0]["c"]
        return GroupCounts(int(n), int(e), int(p), int(m))

    async def clear(self, namespace: str) -> int:
        """Removes everything of ONE namespace (a failed or cancelled build, or a retired namespace past its retention)."""
        g = self._group(namespace)
        rows = await self._run(
            "MATCH (n {group_id: $g}) WITH collect(n) AS ns FOREACH (x IN ns | DETACH DELETE x) RETURN size(ns) AS c", g=g
        )
        return int(rows[0]["c"])

    async def stamp_originals(self, namespace: str) -> None:
        """After a group was built under tagged uuids: record the real uuids so it can be unparked."""
        g, tag = self._group(namespace), tag_for(namespace)
        for q in _STAMP:
            await self._run(q, g=g, tag=tag, len=len(tag))

    async def parked_stats(self, namespace: str) -> dict[str, int]:
        g = self._group(namespace)
        rows = await self._run("MATCH (n:Entity {group_id: $g}) RETURN count(n) AS nodes, count(n.p_uuid) AS parked", g=g)
        return {"nodes": int(rows[0]["nodes"]), "parked": int(rows[0]["parked"])}

    async def inventory(self, namespace: str) -> tuple[dict[str, int | None], dict[str, int | None]]:
        """(entity uuid -> revision, fact uuid -> revision) of a group, with the REAL uuids (parked or not) and the revision the builder stamped (`p_rev`)."""
        g = self._group(namespace)
        nodes = await self._run("MATCH (n:Entity {group_id: $g}) RETURN coalesce(n.p_uuid, n.uuid) AS u, n.p_rev AS rev", g=g)
        edges = await self._run("MATCH ()-[r:RELATES_TO {group_id: $g}]->() RETURN coalesce(r.p_uuid, r.uuid) AS u, r.p_rev AS rev", g=g)
        return ({r["u"]: r["rev"] for r in nodes}, {r["u"]: r["rev"] for r in edges})

    async def set_revisions(self, namespace: str, *, nodes: dict[str, int] | None = None, edges: dict[str, int] | None = None) -> None:
        """Stamps the Postgres revision a built node or edge was rendered from, so a resume or a catch-up redoes only what changed. Keyed by TAGGED uuid."""
        g, tag = self._group(namespace), tag_for(namespace)
        if nodes:
            await self._run(
                "UNWIND $rows AS row MATCH (n:Entity {group_id: $g, uuid: row.u}) SET n.p_rev = row.rev",
                g=g,
                rows=[{"u": tag + u, "rev": rev} for u, rev in nodes.items()],
            )
        if edges:
            await self._run(
                "UNWIND $rows AS row MATCH ()-[r:RELATES_TO {group_id: $g, uuid: row.u}]->() SET r.p_rev = row.rev",
                g=g,
                rows=[{"u": tag + u, "rev": rev} for u, rev in edges.items()],
            )

    async def delete_tagged(self, namespace: str, *, nodes: list[str], edges: list[str]) -> int:
        """Removes built nodes and edges by their REAL uuids (the builder's catch-up for rows that left the eligible set)."""
        g, tag = self._group(namespace), tag_for(namespace)
        n = 0
        if edges:
            rows = await self._run(
                "MATCH ()-[r:RELATES_TO {group_id: $g}]->() WHERE r.uuid IN $u WITH collect(r) AS rs FOREACH (x IN rs | DELETE x) RETURN size(rs) AS c",
                g=g,
                u=[tag + e for e in edges],
            )
            n += int(rows[0]["c"])
        if nodes:
            rows = await self._run(
                "MATCH (n:Entity {group_id: $g}) WHERE n.uuid IN $u WITH collect(n) AS ns FOREACH (x IN ns | DETACH DELETE x) RETURN size(ns) AS c",
                g=g,
                u=[tag + e for e in nodes],
            )
            n += int(rows[0]["c"])
        return n

    async def episode_uuids(self, namespace: str) -> set[str]:
        """The REAL uuids of the group's episodes."""
        g = self._group(namespace)
        rows = await self._run("MATCH (e:Episodic {group_id: $g}) RETURN coalesce(e.p_uuid, e.uuid) AS u", g=g)
        return {r["u"] for r in rows}

    async def ensure_mentions(self, namespace: str, pairs: list[tuple[str, str]]) -> int:
        """MENTIONS between built episodes and built nodes, by REAL uuids (episode, node). Used for episodes rebuilt from Postgres rather than copied."""
        if not pairs:
            return 0
        g, tag = self._group(namespace), tag_for(namespace)
        rows = await self._run(
            "UNWIND $pairs AS p MATCH (e:Episodic {group_id: $g, uuid: $tag + p.e}) MATCH (n:Entity {group_id: $g, uuid: $tag + p.n}) "
            "MERGE (e)-[m:MENTIONS {uuid: $tag + p.e + '|' + p.n}]->(n) ON CREATE SET m.group_id = $g, m.created_at = e.created_at RETURN count(m) AS c",
            g=g,
            tag=tag,
            pairs=[{"e": e, "n": n} for e, n in pairs],
        )
        return int(rows[0]["c"])

    async def copy_episodes(self, source: str, target: str) -> tuple[int, int]:
        """Copies the Episodic nodes of `source` (an active group) into `target` under tagged uuids, with their MENTIONS to the nodes that exist in `target`. Idempotent (MERGE
        on the tagged uuid). Returns (episodes, mentions). Episode text is copied as stored: the retention that blanked it in the source blanks it here too."""
        s, t, tag = self._group(source), self._group(target), tag_for(target)
        eps = await self._run(
            "MATCH (e:Episodic {group_id: $s}) MERGE (c:Episodic {uuid: $tag + e.uuid}) "
            "ON CREATE SET c.name = e.name, c.group_id = $t, c.source = e.source, c.source_description = e.source_description, c.content = e.content, "
            "c.created_at = e.created_at, c.valid_at = e.valid_at, c.labels = coalesce(e.labels, []), c.entity_edges = [u IN coalesce(e.entity_edges, []) | $tag + u] "
            "RETURN count(c) AS c",
            s=s,
            t=t,
            tag=tag,
        )
        ments = await self._run(
            "MATCH (e:Episodic {group_id: $s})-[m:MENTIONS]->(n:Entity {group_id: $s}) "
            "MATCH (ce:Episodic {group_id: $t, uuid: $tag + e.uuid}) MATCH (cn:Entity {group_id: $t, uuid: $tag + n.uuid}) "
            "MERGE (ce)-[cm:MENTIONS {uuid: $tag + coalesce(m.uuid, e.uuid + n.uuid)}]->(cn) ON CREATE SET cm.group_id = $t, cm.created_at = m.created_at "
            "RETURN count(cm) AS c",
            s=s,
            t=t,
            tag=tag,
        )
        return int(eps[0]["c"]), int(ments[0]["c"])

    async def exchange(self, *, retire: str, activate: str) -> None:
        """THE cutover: in ONE Neo4j transaction park `retire` (the group readers use now) and unpark `activate` (the built one). Rolling back is the same call with the
        arguments swapped. A group that is already in the requested state is left alone, so a repeated call after a crash finishes the job."""
        old, new = self._group(retire), self._group(activate)
        old_tag = tag_for(retire)
        if old == new:
            raise ValidationFailed("a namespace cannot replace itself")

        async def work(tx: Any) -> None:
            for q in _PARK:
                await tx.run(q, g=old, tag=old_tag)
            for q in _UNPARK:
                await tx.run(q, g=new)

        async with self._client.session(database=self._db) as session:
            await session.execute_write(work)

    async def run_in_tx(
        self, fn: Callable[[Any], Awaitable[None]]
    ) -> None:  # pragma: no cover - the seam for tests that inject a failure between the two steps
        async with self._client.session(database=self._db) as session:
            await session.execute_write(fn)
