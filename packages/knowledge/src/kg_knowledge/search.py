"""Diagnostic graph search (P4, building block for P6 context building): Graphiti's hybrid edge search through the scoped recipe, joined to Postgres.

The query text is embedded through the platform transport (purpose `graph_search`); the platform's recipe (ADR-019) filters expired edges and applies valid-time
and record-time bounds; every hit is looked up in Postgres by edge uuid and returned with its status, authority and evidence. A hit Postgres does not know
yet is dropped and counted, never shown (the graph is not a source of truth)."""

from __future__ import annotations

from collections.abc import Sequence

from kg_common.ids import uuid7_str
from kg_domain.principal import WorkspaceContext
from kg_governance.grants import SubjectFilter
from kg_graph.service import GraphService
from kg_modelgw.egress import egress
from kg_obs import context, metrics
from sqlalchemy.ext.asyncio import AsyncConnection

from kg_knowledge.graph_reads import GraphReads
from kg_knowledge.models import GraphSearchIn, GraphSearchOut


class GraphSearch:
    def __init__(self, graph: GraphService, reads: GraphReads, *, service: str):
        self._graph, self._reads, self._service = graph, reads, service

    async def run(
        self,
        conn: AsyncConnection,
        ctx: WorkspaceContext,
        body: GraphSearchIn,
        *,
        can_see_sensitive: bool,
        subjects: SubjectFilter,
        classification: str = "internal",
        query_vector: Sequence[float] | None = None,
    ) -> GraphSearchOut:
        """`classification` is the classification of the QUERY (P6, S3): the egress policy judges the text that leaves for the router, so a caller whose task
        touches restricted material says so, and a `local_only` query is refused before it leaves. `query_vector` skips the query embedding (CX-01)."""
        if body.center_entity_id is not None and subjects.enforced:
            await self._reads.reads.entity_exists(
                conn, ctx, body.center_entity_id, subjects=subjects
            )  # a hidden centre is the same 404 as a missing one
        scoped = self._graph.scoped(ctx)
        try:
            with egress(
                classification=classification,
                purpose="graph_search",
                workspace_id=str(ctx.workspace_id),
                correlation_id=context.correlation_id_var.get() or uuid7_str(),
                service=self._service,
            ):
                hits = await scoped.search_facts(
                    body.query,
                    as_of=body.as_of,
                    known_at=body.known_at,
                    center_entity_id=str(body.center_entity_id) if body.center_entity_id else None,
                    limit=body.limit,
                    query_vector=query_vector,
                )
        except Exception:
            metrics.graph_search_total.labels(outcome="error").inc()
            raise
        facts, missing = await self._reads.facts_for_hits(
            conn, ctx, hits, can_see_sensitive=can_see_sensitive, include_past=body.known_at is not None, subjects=subjects
        )
        metrics.graph_search_total.labels(outcome="ok").inc()
        if missing:
            metrics.graph_search_unreconciled_hits_total.inc(missing)
        return GraphSearchOut(facts=facts, unreconciled_hits=missing)
