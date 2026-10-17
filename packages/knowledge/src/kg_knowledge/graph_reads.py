"""Graph-shaped reads, answered from POSTGRES (P4 decision 6): the Graph Explorer's neighbourhood, an entity's mentions and attribute claims, a fact's evidence,
and the join that turns Graphiti's search hits into facts with status and evidence.

Neo4j serves retrieval; it is never the source of an answer a person acts on. A hit the platform has not reconciled yet is dropped and counted, and a hit whose
fact is not active is dropped too, so an expired or disputed edge cannot reach a reader even if the graph still carries it (ADR-018).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from typing import Any

from kg_domain.principal import WorkspaceContext
from kg_governance.grants import SubjectFilter, fact_visible_sql
from kg_graph.scoped import FactHit
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from kg_knowledge.models import (
    AttributeClaimOut,
    EvidenceOut,
    FactOut,
    GraphEdgeOut,
    GraphNodeOut,
    GraphOut,
    MentionOut,
)
from kg_knowledge.reads import FACT_SELECT, Reads, visibility_sql

MAX_NODES = 100
MAX_HOPS = 2


class GraphReads:
    def __init__(self, reads: Reads):
        self._reads = reads

    @property
    def reads(self) -> Reads:
        return self._reads

    async def neighborhood(
        self,
        conn: AsyncConnection,
        ctx: WorkspaceContext,
        entity_id: uuid.UUID,
        *,
        subjects: SubjectFilter,
        hops: int,
        as_of: dt.datetime | None,
        known_at: dt.datetime | None,
        include: Sequence[str],
        history: bool,
        types: Sequence[str],
        limit: int,
    ) -> GraphOut:
        """Breadth-first from the entity over the facts a read would show. Caps are enforced here, not trusted from the client (blueprint)."""
        ws = ctx.workspace_id
        hops, limit = max(1, min(hops, MAX_HOPS)), max(1, min(limit, MAX_NODES))
        await self._reads.entity_exists(conn, ctx, entity_id, subjects=subjects)  # a hidden centre is the same 404 as a missing one
        acl, acl_params = fact_visible_sql(subjects, source="s", target="t")
        extras = sorted({x for x in include if x in ("proposed", "disputed", "rejected")})
        visible = visibility_sql(include_history=history, known_at_given=known_at is not None)
        params: dict[str, Any] = {"ws": ws, "as_of": as_of, "known_at": known_at, "extras": extras, "types": list(types), **acl_params}
        hop_of: dict[uuid.UUID, int] = {entity_id: 0}
        frontier = [entity_id]
        truncated = False
        edge_rows: dict[uuid.UUID, Any] = {}
        for hop in range(1, hops + 1):
            rows = (
                await conn.execute(
                    text(
                        f"{FACT_SELECT} WHERE f.workspace_id = :ws AND (f.source_entity_id = ANY(:front) OR f.target_entity_id = ANY(:front)) "  # noqa: S608
                        f"AND {visible} AND {acl} AND f.status NOT IN ('deleted','deletion_pending') AND s.status <> 'merged' AND t.status <> 'merged' "
                        "AND (cardinality(CAST(:types AS text[])) = 0 OR (s.type_key = ANY(CAST(:types AS text[])) OR s.id = ANY(:front)) "
                        "AND (t.type_key = ANY(CAST(:types AS text[])) OR t.id = ANY(:front))) "
                        "ORDER BY f.recorded_at, f.id"
                    ),
                    {**params, "front": frontier},
                )
            ).all()
            if subjects.enforced:  # how many edges the filter kept out of this hop (a count; the hop never passes through a hidden node)
                everything = (
                    await conn.execute(
                        text(
                            f"SELECT count(*) AS n FROM ({FACT_SELECT} WHERE f.workspace_id = :ws AND (f.source_entity_id = ANY(:front) OR f.target_entity_id = ANY(:front)) "  # noqa: S608
                            f"AND {visible} AND f.status NOT IN ('deleted','deletion_pending') AND s.status <> 'merged' AND t.status <> 'merged') x"
                        ),
                        {k: v for k, v in params.items() if not k.startswith("sf")} | {"front": frontier},
                    )
                ).scalar_one()
                self._reads.hidden("graph", int(everything) - len(rows))
            nxt: list[uuid.UUID] = []
            for r in rows:
                for end in (r.source_entity_id, r.target_entity_id):
                    if end not in hop_of:
                        if len(hop_of) >= limit:
                            truncated = True
                            continue
                        hop_of[end] = hop
                        nxt.append(end)
                edge_rows[r.id] = r
            frontier = nxt
            if not frontier:
                break
        entities = (
            await conn.execute(
                text("SELECT id, type_key, display_name, status, origin FROM entities WHERE workspace_id = :ws AND id = ANY(:ids)"),
                {"ws": ws, "ids": list(hop_of)},
            )
        ).all()
        nodes = [
            GraphNodeOut(id=e.id, type=e.type_key, name=e.display_name, status=e.status, origin=e.origin, hop=hop_of[e.id])
            for e in sorted(entities, key=lambda x: (hop_of[x.id], x.display_name, str(x.id)))
        ]
        edges = [
            GraphEdgeOut(
                id=r.id,
                rel_type=r.rel_type,
                source_id=r.source_entity_id,
                target_id=r.target_entity_id,
                status=r.status,
                evidence_status=r.evidence_status,
                authority_class=r.authority_class,
                phase=r.phase,
                valid_from=r.valid_from,
                valid_to=r.valid_to,
                fact_text=r.fact_text,
            )
            for r in edge_rows.values()
            if r.source_entity_id in hop_of and r.target_entity_id in hop_of
        ]
        return GraphOut(center_id=entity_id, nodes=nodes, edges=edges, truncated=truncated)

    async def mentions(
        self, conn: AsyncConnection, ctx: WorkspaceContext, entity_id: uuid.UUID, *, subjects: SubjectFilter
    ) -> list[MentionOut]:
        await self._reads.entity_exists(conn, ctx, entity_id, subjects=subjects)
        rows = (
            await conn.execute(
                text(
                    "SELECT m.id, m.interaction_id, m.episode_uuid, m.resolution, m.status, m.created_at, i.source_system, i.source_ref, i.occurred_at, i.recorded_at "
                    "FROM entity_mentions m JOIN interactions i ON i.workspace_id = m.workspace_id AND i.id = m.interaction_id "
                    "WHERE m.workspace_id = :ws AND m.entity_id = :e ORDER BY i.recorded_at DESC, m.id LIMIT 200"
                ),
                {"ws": ctx.workspace_id, "e": entity_id},
            )
        ).all()
        return [MentionOut(**dict(r._mapping)) for r in rows]

    async def attribute_claims(
        self, conn: AsyncConnection, ctx: WorkspaceContext, entity_id: uuid.UUID, *, subjects: SubjectFilter
    ) -> list[AttributeClaimOut]:
        await self._reads.entity_exists(conn, ctx, entity_id, subjects=subjects)
        rows = (
            await conn.execute(
                text(
                    "SELECT id, attribute_key, value, status, interaction_id, episode_uuid, created_at FROM attribute_claims "
                    "WHERE workspace_id = :ws AND entity_id = :e ORDER BY created_at DESC, id LIMIT 200"
                ),
                {"ws": ctx.workspace_id, "e": entity_id},
            )
        ).all()
        return [AttributeClaimOut(**dict(r._mapping)) for r in rows]

    async def fact_evidence(
        self, conn: AsyncConnection, ctx: WorkspaceContext, fact_id: uuid.UUID, *, subjects: SubjectFilter
    ) -> list[EvidenceOut]:
        """The 'why does the system believe this?' answer (KGR-05): metadata only. Content is read through the audited evidence endpoints."""
        await self._reads.get_fact(
            conn, ctx, fact_id, can_see_sensitive=False, subjects=subjects
        )  # 404 for a missing, foreign or hidden fact
        return (await self._reads._evidence(conn, ctx.workspace_id, [fact_id])).get(fact_id, [])

    async def facts_for_hits(
        self,
        conn: AsyncConnection,
        ctx: WorkspaceContext,
        hits: Sequence[FactHit],
        *,
        can_see_sensitive: bool,
        include_past: bool,
        subjects: SubjectFilter,
    ) -> tuple[list[FactOut], int]:
        """Graph hits -> facts with status and evidence. Returns (facts in hit order, hits dropped because Postgres has no such fact).

        The subject filter is inside the join: a hit whose fact touches a hidden entity is dropped here and is NOT counted as unreconciled (that count stays for edges
        Postgres does not know)."""
        ids = [uuid.UUID(h.uuid) for h in hits if _is_uuid(h.uuid)]
        acl, acl_params = fact_visible_sql(subjects, source="s", target="t")
        rows = (
            (
                await conn.execute(
                    text(f"{FACT_SELECT} WHERE f.workspace_id = :ws AND f.id = ANY(:ids) AND {acl}"),  # noqa: S608
                    {"ws": ctx.workspace_id, "ids": ids, **acl_params},
                )
            ).all()
            if ids
            else []
        )
        by_id = {r.id: r for r in rows}
        unknown_to_postgres = len(hits) - len(by_id)
        if subjects.enforced and ids and len(by_id) < len(set(ids)):
            known = (
                await conn.execute(
                    text("SELECT count(*) AS n FROM facts WHERE workspace_id = :ws AND id = ANY(:ids)"),
                    {"ws": ctx.workspace_id, "ids": ids},
                )
            ).scalar_one()
            self._reads.hidden("graph_search", int(known) - len(by_id))
            unknown_to_postgres = len(hits) - int(known)
        evidence = await self._reads._evidence(conn, ctx.workspace_id, list(by_id))
        out: list[FactOut] = []
        for h in hits:
            r = by_id.get(uuid.UUID(h.uuid)) if _is_uuid(h.uuid) else None
            if r is None:
                continue
            ok = r.status in ("active", "superseded", "disputed") if include_past else r.status == "active"
            if ok:
                out.append(await self._reads._fact_out(conn, ctx.workspace_id, r, can_see_sensitive, evidence.get(r.id, [])))
        return out, unknown_to_postgres


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True
