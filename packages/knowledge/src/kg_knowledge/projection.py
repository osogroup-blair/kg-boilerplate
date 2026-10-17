"""State-based projection, Postgres -> Neo4j (ADR-016, ADR-017, ADR-018).

The projector makes the graph EQUAL the current Postgres row. It never replays deltas, so it is idempotent and independent of order: running it twice,
or out of order, gives the same graph. Nodes and edges are written by DIRECT SAVE through ScopedGraph, so no step can call an LLM; only embeddings
go to the router, under an egress context (purpose `graph_projection`). `local_only` data is never sent, so it is never projected
(`skipped_by_policy`): Graphiti cannot save a node or edge without an embedding (WP0 / PJ-02).

Postgres stays authoritative: reads never depend on the graph, and a failed projection leaves the API consistent.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from kg_common.errors import NotFound
from kg_common.ids import uuid7_str
from kg_db.session import tenant_session
from kg_domain.principal import WorkspaceContext
from kg_graph.errors import EndpointNotProjected
from kg_graph.scoped import EdgeReadBack, NodeReadBack, ScopedGraph
from kg_graph.service import GraphService
from kg_modelgw.config import ModelConfig
from kg_modelgw.egress import egress
from kg_modelgw.errors import CATEGORY_BY_CODE, categorize
from kg_modelgw.transport import stricter
from kg_obs import context, metrics
from kg_obs.logging import get_logger
from kg_ontology import OntologySpec
from kg_ontology.template import render_fact
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from kg_knowledge.specs import SpecCache

log = get_logger("kg.projection")
MAX_ATTEMPTS = 8
LEASE_SECONDS = 60
PROJECTED = ("projected", "skipped_by_policy")
LEDGER_COMMANDS = frozenset(  # commands whose graph work is a restore of the rows they changed (a review decision, a merge, and P8's corrections and identity split)
    {
        "review.decide",
        "entity.merge",
        "entity.split",
        "correction.fact_reject",
        "correction.fact_supersede",
        "correction.fact_end",
        "correction.fact_restore",
        "correction.attribute",
        "correction.rename",
    }
)
_RETRYABLE_NAMES = frozenset(
    {
        "ServiceUnavailable",
        "SessionExpired",
        "TransientError",
        "DatabaseUnavailable",
        "DatabaseError",  # Neo4j could not write (a full disk surfaces as one): retryable and named for the operator, never a silent loss
        "ConnectionResetError",
        "APIConnectionError",
        "APITimeoutError",
        "TimeoutException",
        "TransportError",
        "TimeoutError",
        "ConnectError",
        "RateLimitError",
        "InternalServerError",
        "EndpointNotProjected",
    }
)


def _router_outcome(category: str) -> tuple[str, bool]:
    """Router error category (docs/router-contract.md §4) -> (projection error code, retryable): budget pauses the queue, a policy error is final,
    config errors are not retried, everything else is transient."""
    if category == "budget":
        return "budget_exceeded", True
    if category == "policy_terminal":
        return "router_policy", False
    if category == "config_terminal":
        return "router_rejected", False
    return "router_unavailable", True


def classify_error(exc: BaseException) -> tuple[str, bool]:
    """(error code, retryable). Names only: the exception text can echo content, so it is never stored or logged."""
    chain: list[BaseException] = []
    cur: BaseException | None = exc
    while cur is not None and len(chain) < 6:
        chain.append(cur)
        cur = cur.__cause__ or cur.__context__
    for e in chain:
        names = {c.__name__ for c in type(e).__mro__}
        if "EndpointNotProjected" in names:
            return "endpoint_not_projected", True
        if "EgressDenied" in names:
            return "egress_denied", False
        status = getattr(e, "status_code", None)
        if isinstance(status, int) and "APIStatusError" in names:
            # Graphiti reaches the router through the OpenAI SDK: the router's own error code survives as `.code`, and it decides the category
            # (a 429 can be rate_limited, which is retried, or budget_exceeded, which pauses the queue)
            router_code = getattr(e, "code", None)
            if isinstance(router_code, str) and router_code in CATEGORY_BY_CODE:
                return _router_outcome(categorize(router_code).value)
            return ("router_unavailable", True) if status in (408, 409, 425, 429) or status >= 500 else ("router_rejected", False)
        if names & {"RouterError"}:
            return _router_outcome(str(getattr(getattr(e, "category", None), "value", "retryable")))
        if names & _RETRYABLE_NAMES:
            return (
                "graph_unavailable"
                if names & {"ServiceUnavailable", "SessionExpired", "TransientError", "DatabaseUnavailable", "DatabaseError"}
                else "router_unavailable"
            ), True
        if getattr(e, "code", None) == "Neo.ClientError.Schema.ConstraintValidationFailed":
            return "graph_conflict", True  # a concurrent writer won the MERGE on this uuid; the next attempt matches the node it created
        if names & {"ClientError", "ValidationFailed", "NotFound", "CypherSyntaxError"}:
            return "graph_rejected", False
    return "unexpected_error", True


@dataclass(frozen=True)
class RenderedNode:
    uuid: str
    name: str
    type_key: str
    attributes: dict[str, Any]
    created_at: dt.datetime


@dataclass(frozen=True)
class RenderedEdge:
    uuid: str
    source_uuid: str
    target_uuid: str
    name: str
    fact: str
    valid_at: dt.datetime | None
    invalid_at: dt.datetime | None
    created_at: dt.datetime
    expired_at: dt.datetime | None
    attributes: dict[str, Any]
    episodes: tuple[str, ...] = ()


def version_label(spec: OntologySpec) -> str:
    return f"{spec.key}@{spec.version}"


def render_node(row: Row[Any], spec: OntologySpec) -> RenderedNode:
    """Sensitive attributes are Postgres-only (§5.5): they never reach the node, the embedding or the logs."""
    declared = spec.entity_types.get(row.type_key)
    sensitive = {k for k, a in declared.attributes.items() if a.sensitive} if declared else set()
    attrs = {k: v for k, v in (row.attributes or {}).items() if k not in sensitive and isinstance(v, str | int | float | bool)}
    attrs["platform_entity_id"] = str(row.id)
    attrs["ontology_version"] = version_label(spec)
    return RenderedNode(str(row.id), row.display_name, row.type_key, attrs, row.recorded_at)


NOT_CURRENT = frozenset({"proposed", "rejected", "disputed", "deleted", "deletion_pending"})


def render_edge(row: Row[Any], source_name: str, target_name: str, spec: OntologySpec, episodes: tuple[str, ...] = ()) -> RenderedEdge:
    """A command fact's text comes from the ontology template (no model); an extracted fact keeps the text its source gave. A fact that is not
    current (proposed, rejected, disputed) is EXPIRED on the edge, so the standard `expired_at IS NULL` search filter hides it (ADR-018)."""
    declared = spec.relationship_types.get(row.rel_type)
    sensitive = {k for k, a in declared.attributes.items() if a.sensitive} if declared else set()
    public = {k: v for k, v in (row.attributes or {}).items() if k not in sensitive and isinstance(v, str | int | float | bool)}
    extracted = row.source_type not in ("command", "correction")  # a correction's text comes from the ontology template, like a command's
    fact = (
        (row.fact_text or "")
        if extracted and row.fact_text
        else render_fact(declared.fact_template if declared else None, row.rel_type, source_name, target_name, public)
    )
    attrs: dict[str, Any] = {
        **public,
        "platform_fact_id": str(row.id),
        "authority_class": row.authority_class,
        "ontology_version": version_label(spec),
    }
    if row.source_command_id is not None:
        attrs["source_command_id"] = str(row.source_command_id)
    if extracted or row.status != "active":
        attrs["platform_status"] = row.status
        attrs["evidence_status"] = row.evidence_status
        attrs["source_type"] = row.source_type
    expired = row.decided_at if row.status in NOT_CURRENT and row.decided_at is not None else row.superseded_at
    return RenderedEdge(
        str(row.id),
        str(row.source_entity_id),
        str(row.target_entity_id),
        row.rel_type,
        fact,
        row.valid_from,
        row.valid_to,
        row.recorded_at,
        expired,
        attrs,
        episodes,
    )


def node_diff(want: RenderedNode, got: NodeReadBack) -> list[str]:
    return [
        name
        for name, same in (
            ("name", got.name == want.name),
            ("labels", got.labels == frozenset({want.type_key})),
            ("attributes", got.attributes == want.attributes),
            ("created_at", got.created_at == want.created_at),
        )
        if not same
    ]


def edge_diff(want: RenderedEdge, got: EdgeReadBack) -> list[str]:
    return [
        name
        for name, same in (
            ("name", got.name == want.name),
            ("fact", got.fact == want.fact),
            ("endpoints", (got.source_uuid, got.target_uuid) == (want.source_uuid, want.target_uuid)),
            ("valid_at", got.valid_at == want.valid_at),
            ("invalid_at", got.invalid_at == want.invalid_at),
            ("created_at", got.created_at == want.created_at),
            ("expired_at", got.expired_at == want.expired_at),
            ("attributes", got.attributes == want.attributes),
            ("episodes", tuple(got.episodes) == tuple(want.episodes)),
        )
        if not same
    ]


@dataclass
class Drift:
    kind: str  # node | edge
    id: str
    problem: str  # missing | mismatch | orphan | unexpected
    fields: list[str] = field(default_factory=list)


@dataclass
class ReconcileReport:
    checked_entities: int = 0
    checked_facts: int = 0
    pending: int = 0
    skipped_by_policy: int = 0
    drift: list[Drift] = field(default_factory=list)
    repaired: int = 0


@dataclass
class RestoreReport:
    """What restoring a scope from Postgres did. `unsettled` are rows whose projection failed (the code is recorded on the row; a retry finishes them)."""

    checked: int = 0
    repaired: int = 0
    unsettled: list[tuple[str, uuid.UUID, str | None]] = field(default_factory=list)


class Projector:
    def __init__(self, engine: AsyncEngine, graph: GraphService, specs: SpecCache, config: ModelConfig, *, service: str):
        self._engine, self._graph, self._specs, self._config, self._service = engine, graph, specs, config, service

    # ---- helpers --------------------------------------------------------------------------------------------------------------------------

    def _egress(self, ctx: WorkspaceContext, classification: str):  # type: ignore[no-untyped-def]
        return egress(
            classification=classification,
            purpose="graph_projection",
            workspace_id=str(ctx.workspace_id),
            correlation_id=context.correlation_id_var.get() or uuid7_str(),
            service=self._service,
        )

    async def _mark_done(self, ctx: WorkspaceContext, table: str, row_id: uuid.UUID, revision: int, status: str, kind: str) -> None:
        embedding = "embedded" if status == "projected" else "skipped_by_policy"
        async with tenant_session(self._engine, ctx) as conn:
            res = await conn.execute(
                text(
                    f"UPDATE {table} SET projection_status = :st, embedding_status = :es, projected_revision = :rev, projected_at = now(), projection_attempts = 0, "  # noqa: S608
                    "last_error_code = NULL WHERE workspace_id = :ws AND id = :id AND revision = :rev RETURNING EXTRACT(EPOCH FROM (now() - recorded_at)) AS lag"
                ),
                {"st": status, "es": embedding, "rev": revision, "ws": ctx.workspace_id, "id": row_id},
            )
            row = res.first()
        if row is not None:  # a newer revision since we read it means a newer projection is already queued; this one is simply stale
            metrics.projection_lag_seconds.observe(max(0.0, float(row.lag)))
        metrics.projection_total.labels(kind=kind, outcome=status).inc()

    async def _mark_failed(
        self, ctx: WorkspaceContext, table: str, row_id: uuid.UUID, revision: int, code: str, retryable: bool, kind: str
    ) -> str:
        async with tenant_session(self._engine, ctx) as conn:
            res = await conn.execute(
                text(
                    f"UPDATE {table} SET projection_attempts = projection_attempts + 1, "  # noqa: S608
                    "projection_status = CASE WHEN :retry AND projection_attempts + 1 < :max THEN 'pending' ELSE 'failed' END, "
                    "projection_next_at = now() + (LEAST(300, 5 * power(2, projection_attempts)) * interval '1 second'), last_error_code = :code "
                    "WHERE workspace_id = :ws AND id = :id AND revision = :rev RETURNING projection_status"
                ),
                {"retry": retryable, "max": MAX_ATTEMPTS, "code": code, "ws": ctx.workspace_id, "id": row_id, "rev": revision},
            )
            row = res.first()
        status = row.projection_status if row else "pending"
        metrics.projection_total.labels(kind=kind, outcome="failed" if status == "failed" else "retry").inc()
        log.warning("projection_attempt_failed", kind=kind, code=code, retryable=retryable, final=status == "failed")
        return str(status)

    def _scoped(self, ctx: WorkspaceContext) -> ScopedGraph:
        return self._graph.scoped(ctx)

    # ---- entity ---------------------------------------------------------------------------------------------------------------------------

    async def project_entity(self, ctx: WorkspaceContext, entity_id: uuid.UUID) -> str:
        """Returns the row's projection status afterwards. Never raises for a projection failure: the failure is recorded on the row."""
        async with tenant_session(self._engine, ctx) as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT id, type_key, display_name, attributes, classification, revision, ontology_version_id, recorded_at, projection_status, projected_revision, "
                        "status, merged_into_id FROM entities WHERE workspace_id = :ws AND id = :id"
                    ),
                    {"ws": ctx.workspace_id, "id": entity_id},
                )
            ).first()
            if row is None:
                raise NotFound()
            try:
                spec = await self._specs.get(conn, ctx.workspace_id, row.ontology_version_id)
            except Exception:
                spec = None
        if (
            row.status == "deletion_pending"
        ):  # frozen by a deletion request (P8): nothing is written to the graph for it any more; the purge removes what is there
            return "skipped_by_policy"
        if spec is None:
            return await self._mark_failed(ctx, "entities", entity_id, row.revision, "ontology_unreadable", False, "entity")
        if row.projection_status in PROJECTED and row.projected_revision == row.revision:
            return str(row.projection_status)
        if row.status == "merged":  # the node is gone: its episodes mention the canonical node now (ADR-028)
            try:
                await self._merge_node_away(ctx, row.id, row.merged_into_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                code, retryable = classify_error(exc)
                return await self._mark_failed(ctx, "entities", entity_id, row.revision, code, retryable, "entity")
            await self._mark_done(ctx, "entities", entity_id, row.revision, "projected", "entity")
            return "projected"
        if not self._config.egress_allows(row.classification):
            await self._mark_done(ctx, "entities", entity_id, row.revision, "skipped_by_policy", "entity")
            return "skipped_by_policy"
        node = render_node(row, spec)
        try:
            with self._egress(ctx, row.classification):
                await self._scoped(ctx).upsert_entity_node(
                    uuid=node.uuid, name=node.name, type_key=node.type_key, attributes=node.attributes, created_at=node.created_at
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            code, retryable = classify_error(exc)
            return await self._mark_failed(ctx, "entities", entity_id, row.revision, code, retryable, "entity")
        log.info("entity_projected", ontology_version=version_label(spec))
        await self._mark_done(ctx, "entities", entity_id, row.revision, "projected", "entity")
        return "projected"

    async def _merge_node_away(self, ctx: WorkspaceContext, from_id: uuid.UUID, into_id: uuid.UUID | None) -> None:
        """Deletes the merged entity's node after moving its episodes to the canonical node. Idempotent: a node that is already gone is a success."""
        graph = self._scoped(ctx)
        if into_id is None or await graph.read_node(str(from_id)) is None:
            return
        if await graph.read_node(str(into_id)) is None:  # the canonical node must exist before anything is moved onto it
            await self.project_entity(ctx, into_id)
        await graph.merge_nodes(str(from_id), str(into_id))

    # ---- fact -----------------------------------------------------------------------------------------------------------------------------

    async def _episodes_of(self, conn: AsyncConnection, ws: uuid.UUID, fact_id: uuid.UUID) -> tuple[str, ...]:
        rows = (
            await conn.execute(
                text(
                    "SELECT episode_uuid FROM fact_evidence WHERE workspace_id = :ws AND fact_id = :f AND source_kind = 'episode' ORDER BY added_at, id"
                ),
                {"ws": ws, "f": fact_id},
            )
        ).all()
        return tuple(r.episode_uuid for r in rows)

    async def project_fact(self, ctx: WorkspaceContext, fact_id: uuid.UUID) -> str:
        async with tenant_session(self._engine, ctx) as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT f.id, f.rel_type, f.source_entity_id, f.target_entity_id, f.attributes, f.valid_from, f.valid_to, f.recorded_at, f.superseded_at, "
                        "f.authority_class, f.classification, f.source_command_id, f.revision, f.ontology_version_id, f.projection_status, f.projected_revision, "
                        "f.status, f.source_type, f.fact_text, f.evidence_status, f.decided_at, "
                        "s.display_name AS s_name, s.classification AS s_cls, s.projection_status AS s_proj, s.status AS s_status, "
                        "t.display_name AS t_name, t.classification AS t_cls, t.projection_status AS t_proj, t.status AS t_status "
                        "FROM facts f JOIN entities s ON s.workspace_id = f.workspace_id AND s.id = f.source_entity_id "
                        "JOIN entities t ON t.workspace_id = f.workspace_id AND t.id = f.target_entity_id WHERE f.workspace_id = :ws AND f.id = :id"
                    ),
                    {"ws": ctx.workspace_id, "id": fact_id},
                )
            ).first()
            if row is None:
                raise NotFound()
            try:
                spec = await self._specs.get(conn, ctx.workspace_id, row.ontology_version_id)
            except Exception:
                spec = None
            episodes = await self._episodes_of(conn, ctx.workspace_id, fact_id)
        if row.status == "deletion_pending":
            return "skipped_by_policy"
        if spec is None:
            return await self._mark_failed(ctx, "facts", fact_id, row.revision, "ontology_unreadable", False, "fact")
        if row.projection_status in PROJECTED and row.projected_revision == row.revision:
            return str(row.projection_status)
        classification = stricter(stricter(row.classification, row.s_cls), row.t_cls)  # the fact text carries both names
        if not self._config.egress_allows(classification) or "skipped_by_policy" in (row.s_proj, row.t_proj):
            await self._mark_done(ctx, "facts", fact_id, row.revision, "skipped_by_policy", "fact")
            return "skipped_by_policy"
        for endpoint, status, estatus in (
            (row.source_entity_id, row.s_proj, row.s_status),
            (row.target_entity_id, row.t_proj, row.t_status),
        ):
            if status != "projected" and estatus != "merged":
                await self.project_entity(ctx, endpoint)  # an edge needs both nodes; projecting them first is idempotent
        edge = render_edge(row, row.s_name, row.t_name, spec, episodes)
        try:
            with self._egress(ctx, classification):
                await self._scoped(ctx).upsert_fact_edge(
                    uuid=edge.uuid,
                    source_uuid=edge.source_uuid,
                    target_uuid=edge.target_uuid,
                    name=edge.name,
                    fact=edge.fact,
                    valid_at=edge.valid_at,
                    invalid_at=edge.invalid_at,
                    created_at=edge.created_at,
                    expired_at=edge.expired_at,
                    attributes=edge.attributes,
                    episodes=edge.episodes,
                )
        except asyncio.CancelledError:
            raise
        except EndpointNotProjected:
            return await self._mark_failed(ctx, "facts", fact_id, row.revision, "endpoint_not_projected", True, "fact")
        except Exception as exc:
            code, retryable = classify_error(exc)
            return await self._mark_failed(ctx, "facts", fact_id, row.revision, code, retryable, "fact")
        log.info("fact_projected", ontology_version=version_label(spec))
        await self._mark_done(ctx, "facts", fact_id, row.revision, "projected", "fact")
        return "projected"

    # ---- command --------------------------------------------------------------------------------------------------------------------------

    async def refresh_command(self, ctx: WorkspaceContext, command_id: uuid.UUID) -> str:
        """Derives the command's projection status from the rows it created or last changed."""
        async with tenant_session(self._engine, ctx) as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT projection_status, last_error_code FROM entities WHERE workspace_id = :ws AND (created_by_command_id = :c OR last_command_id = :c) "
                        "UNION ALL SELECT projection_status, last_error_code FROM facts WHERE workspace_id = :ws AND (source_command_id = :c OR last_command_id = :c)"
                    ),
                    {"ws": ctx.workspace_id, "c": command_id},
                )
            ).all()
            statuses = {r.projection_status for r in rows}
            if not rows:
                status = "not_applicable"
            elif "failed" in statuses:
                status = "failed"
            elif "pending" in statuses:
                status = "pending"
            elif statuses == {"skipped_by_policy"}:
                status = "skipped_by_policy"
            else:
                status = "projected"
            code = next((r.last_error_code for r in rows if r.projection_status in ("failed", "pending") and r.last_error_code), None)
            await conn.execute(
                text(
                    "UPDATE commands SET projection_status = :st, last_error_code = :code, projected_at = CASE WHEN :st IN ('projected','skipped_by_policy') THEN now() END, "
                    "projection_attempts = projection_attempts + 1 WHERE workspace_id = :ws AND id = :c AND status = 'applied'"
                ),
                {"st": status, "code": code, "ws": ctx.workspace_id, "c": command_id},
            )
        return status

    async def project_command(self, ctx: WorkspaceContext, command_id: uuid.UUID) -> str:
        async with tenant_session(self._engine, ctx) as conn:
            row = (
                await conn.execute(
                    text("SELECT status, result, projection_status, command_type FROM commands WHERE workspace_id = :ws AND id = :id"),
                    {"ws": ctx.workspace_id, "id": command_id},
                )
            ).first()
        if row is None:
            raise NotFound()
        result = row.result or {}
        if row.status != "applied" or result.get("matched_existing"):
            return str(row.projection_status)
        if row.command_type in LEDGER_COMMANDS:  # a review decision or a merge changed rows on both sides of the graph: restore them all
            report = await self.restore_command(ctx, command_id)
            status = "projected" if not report.unsettled else "pending"
            async with tenant_session(self._engine, ctx) as conn:
                await conn.execute(
                    text(
                        "UPDATE commands SET projection_status = :st, projection_attempts = projection_attempts + 1, last_error_code = CASE WHEN :st = 'pending' THEN 'graph_unavailable' END, "
                        "projected_at = CASE WHEN :st = 'projected' THEN now() END WHERE workspace_id = :ws AND id = :id"
                    ),
                    {"st": status, "ws": ctx.workspace_id, "id": command_id},
                )
            return status
        if result.get("entity_id") and "fact_id" not in result:
            await self.project_entity(ctx, uuid.UUID(result["entity_id"]))
        fact_ids = [result["fact_id"], *result.get("ended_fact_ids", [])] if result.get("fact_id") else []
        for fid in dict.fromkeys(fact_ids):
            await self.project_fact(ctx, uuid.UUID(fid))
        return await self.refresh_command(ctx, command_id)

    async def retry_command(self, ctx: WorkspaceContext, command_id: uuid.UUID) -> str:
        """Operator retry (kg commands retry-projection): make the command's rows due again and project them now."""
        async with tenant_session(self._engine, ctx) as conn:
            kind = (
                await conn.execute(
                    text("SELECT command_type FROM commands WHERE workspace_id = :ws AND id = :id"),
                    {"ws": ctx.workspace_id, "id": command_id},
                )
            ).first()
            if kind is not None and kind.command_type in LEDGER_COMMANDS:
                return await self.project_command(ctx, command_id)
            for table, own in (("entities", "created_by_command_id"), ("facts", "source_command_id")):
                await conn.execute(
                    text(
                        f"UPDATE {table} SET projection_status = 'pending', projection_attempts = 0, projection_next_at = now(), last_error_code = NULL "  # noqa: S608
                        f"WHERE workspace_id = :ws AND ({own} = :c OR last_command_id = :c) AND projection_status = 'failed'"
                    ),
                    {"ws": ctx.workspace_id, "c": command_id},
                )
        return await self.project_command(ctx, command_id)

    # ---- outbox hand-over (P3): what the projection job does when its attempts are over ---------------------------------------------------

    async def mark_command_skipped(self, ctx: WorkspaceContext, command_id: uuid.UUID, code: str) -> str:
        """A policy-terminal error: the command's rows that are still waiting are `skipped_by_policy` (never retried), like data that may not leave."""
        async with tenant_session(self._engine, ctx) as conn:
            for table, own in (("entities", "created_by_command_id"), ("facts", "source_command_id")):
                await conn.execute(
                    text(
                        f"UPDATE {table} SET projection_status = 'skipped_by_policy', embedding_status = 'skipped_by_policy', projected_revision = revision, "  # noqa: S608
                        f"projected_at = now(), last_error_code = :code WHERE workspace_id = :ws AND ({own} = :c OR last_command_id = :c) "
                        "AND projection_status IN ('pending','failed')"
                    ),
                    {"code": code, "ws": ctx.workspace_id, "c": command_id},
                )
        return await self.refresh_command(ctx, command_id)

    async def mark_command_failed(self, ctx: WorkspaceContext, command_id: uuid.UUID, code: str) -> str:
        """The job is dead: rows still pending become `failed` with the reason, so the command and the console show a terminal state."""
        async with tenant_session(self._engine, ctx) as conn:
            for table, own in (("entities", "created_by_command_id"), ("facts", "source_command_id")):
                await conn.execute(
                    text(
                        f"UPDATE {table} SET projection_status = 'failed', last_error_code = COALESCE(last_error_code, :code) "  # noqa: S608
                        f"WHERE workspace_id = :ws AND ({own} = :c OR last_command_id = :c) AND projection_status = 'pending'"
                    ),
                    {"code": code, "ws": ctx.workspace_id, "c": command_id},
                )
        return await self.refresh_command(ctx, command_id)

    async def command_error_code(self, ctx: WorkspaceContext, command_id: uuid.UUID) -> str | None:
        async with tenant_session(self._engine, ctx) as conn:
            row = (
                await conn.execute(
                    text("SELECT last_error_code FROM commands WHERE workspace_id = :ws AND id = :id"),
                    {"ws": ctx.workspace_id, "id": command_id},
                )
            ).first()
        return None if row is None else row.last_error_code

    # ---- restore from Postgres (P4: reconcile-then-restore, ADR-027) -------------------------------------------------------------------------

    async def _force_pending(self, ctx: WorkspaceContext, table: str, ids: list[uuid.UUID]) -> None:
        assert table in ("entities", "facts")
        if not ids:
            return
        async with tenant_session(self._engine, ctx) as conn:
            await conn.execute(
                text(
                    f"UPDATE {table} SET projection_status = 'pending', projected_revision = NULL, projection_attempts = 0, projection_next_at = now() "  # noqa: S608
                    "WHERE workspace_id = :ws AND id = ANY(:ids)"
                ),
                {"ws": ctx.workspace_id, "ids": ids},
            )

    async def restore_scope(
        self, ctx: WorkspaceContext, *, entity_ids: Iterable[uuid.UUID], fact_ids: Iterable[uuid.UUID]
    ) -> RestoreReport:
        """Makes the graph equal Postgres for these rows, whatever Graphiti did to them: every node and edge is compared and the ones that differ are
        written again from the row (state-based, so it is idempotent and independent of order). The graph is never trusted; it is only made to agree."""
        report = RestoreReport()
        graph = self._scoped(ctx)
        ent_ids, f_ids = sorted(set(entity_ids), key=str), sorted(set(fact_ids), key=str)
        async with tenant_session(self._engine, ctx) as conn:
            entities = (
                await conn.execute(
                    text("SELECT * FROM entities WHERE workspace_id = :ws AND id = ANY(:ids)"), {"ws": ctx.workspace_id, "ids": ent_ids}
                )
            ).all()
            facts = (
                await conn.execute(
                    text(
                        "SELECT f.*, s.display_name AS s_name, t.display_name AS t_name FROM facts f "
                        "JOIN entities s ON s.workspace_id = f.workspace_id AND s.id = f.source_entity_id "
                        "JOIN entities t ON t.workspace_id = f.workspace_id AND t.id = f.target_entity_id WHERE f.workspace_id = :ws AND f.id = ANY(:ids)"
                    ),
                    {"ws": ctx.workspace_id, "ids": f_ids},
                )
            ).all()
            episodes: dict[uuid.UUID, list[str]] = {}
            for ev in (
                await conn.execute(
                    text(
                        "SELECT fact_id, episode_uuid FROM fact_evidence WHERE workspace_id = :ws AND source_kind = 'episode' AND fact_id = ANY(:ids) "
                        "ORDER BY added_at, id"
                    ),
                    {"ws": ctx.workspace_id, "ids": f_ids},
                )
            ).all():
                episodes.setdefault(ev.fact_id, []).append(ev.episode_uuid)
            specs: dict[uuid.UUID, OntologySpec] = {}
            for vid in {r.ontology_version_id for r in (*entities, *facts)}:
                specs[vid] = await self._specs.get(conn, ctx.workspace_id, vid)
        force_e: list[uuid.UUID] = []
        force_f: list[uuid.UUID] = []
        for e in entities:
            report.checked += 1
            if e.status == "merged":
                if await graph.read_node(str(e.id)) is not None:
                    force_e.append(e.id)
                continue
            if e.projection_status == "skipped_by_policy":
                continue
            got = await graph.read_node(str(e.id))
            if (
                got is None
                or e.projection_status != "projected"
                or e.projected_revision != e.revision
                or node_diff(render_node(e, specs[e.ontology_version_id]), got)
            ):
                force_e.append(e.id)
        for f in facts:
            report.checked += 1
            if f.projection_status == "skipped_by_policy":
                continue
            want_e = render_edge(f, f.s_name, f.t_name, specs[f.ontology_version_id], tuple(episodes.get(f.id, ())))
            got_e = await graph.read_edge(want_e.uuid)
            if got_e is None or f.projection_status != "projected" or f.projected_revision != f.revision or edge_diff(want_e, got_e):
                force_f.append(f.id)
        await self._force_pending(ctx, "entities", force_e)
        await self._force_pending(ctx, "facts", force_f)
        for eid in force_e:
            status = await self.project_entity(ctx, eid)
            if status not in PROJECTED:
                report.unsettled.append(("entity", eid, None))
        for fid in force_f:
            status = await self.project_fact(ctx, fid)
            if status not in PROJECTED:
                report.unsettled.append(("fact", fid, None))
        report.repaired = len(force_e) + len(force_f)
        return report

    async def interaction_scope(self, ctx: WorkspaceContext, interaction_id: uuid.UUID) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
        """Every entity and fact an interaction's extraction can have touched: what it mentions, what it produced, and every fact that hangs off
        those entities (Graphiti's contradiction step can expire ANY edge of a node it resolved)."""
        async with tenant_session(self._engine, ctx) as conn:
            ents = {
                r.id
                for r in (
                    await conn.execute(
                        text(
                            "SELECT entity_id AS id FROM entity_mentions WHERE workspace_id = :ws AND interaction_id = :i "
                            "UNION SELECT node_uuid::uuid FROM entity_mentions WHERE workspace_id = :ws AND interaction_id = :i AND node_uuid ~ '^[0-9a-f-]{36}$' "
                            "AND EXISTS (SELECT 1 FROM entities x WHERE x.workspace_id = :ws AND x.id = node_uuid::uuid)"
                        ),
                        {"ws": ctx.workspace_id, "i": interaction_id},
                    )
                ).all()
            }
            facts = {
                r.id
                for r in (
                    await conn.execute(
                        text(
                            "SELECT id FROM facts WHERE workspace_id = :ws AND (source_interaction_id = :i OR source_entity_id = ANY(:e) OR target_entity_id = ANY(:e)) "
                            "UNION SELECT fact_id FROM fact_evidence WHERE workspace_id = :ws AND interaction_id = :i "
                            # rows the reconciler put back: Graphiti can change a fact on a node this interaction does not mention (a name it resolved to
                            # a different person than the one the platform kept apart), which the mentions alone would not lead to
                            "UNION SELECT x::uuid FROM interaction_stages s, jsonb_array_elements_text(COALESCE(s.result -> 'revert_fact_ids', '[]'::jsonb)) x "
                            "WHERE s.workspace_id = :ws AND s.interaction_id = :i AND s.stage = 'graph_ingest'"
                        ),
                        {"ws": ctx.workspace_id, "i": interaction_id, "e": sorted(ents, key=str)},
                    )
                ).all()
            }
            ents |= {
                r.id
                for r in (
                    await conn.execute(
                        text(
                            "SELECT x::uuid AS id FROM interaction_stages s, jsonb_array_elements_text(COALESCE(s.result -> 'revert_entity_ids', '[]'::jsonb)) x "
                            "WHERE s.workspace_id = :ws AND s.interaction_id = :i AND s.stage = 'graph_ingest'"
                        ),
                        {"ws": ctx.workspace_id, "i": interaction_id},
                    )
                ).all()
            }
        return ents, facts

    async def fix_mentions(self, ctx: WorkspaceContext, interaction_id: uuid.UUID) -> int:
        """Makes the graph's MENTIONS edges agree with `entity_mentions`: a mention the platform did not accept as Graphiti resolved it (a fork) or linked
        by a rule (a merged name variant) is moved to the right node (ADR-028). Idempotent."""
        graph = self._scoped(ctx)
        async with tenant_session(self._engine, ctx) as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT m.node_uuid, m.entity_id, m.episode_uuid, (SELECT x.status FROM entities x WHERE x.workspace_id = m.workspace_id "
                        "AND x.id::text = m.node_uuid) AS node_status FROM entity_mentions m WHERE m.workspace_id = :ws AND m.interaction_id = :i"
                    ),
                    {"ws": ctx.workspace_id, "i": interaction_id},
                )
            ).all()
        moved = 0
        for m in rows:
            if m.node_uuid == str(m.entity_id):
                continue
            if await graph.read_node(m.node_uuid) is None:
                continue  # already moved
            if await graph.read_node(str(m.entity_id)) is None:
                await self.project_entity(ctx, m.entity_id)
            if m.node_status is not None and m.node_status != "merged":
                await graph.move_mentions(
                    m.node_uuid, str(m.entity_id), episode_uuid=m.episode_uuid
                )  # a fork: only THIS episode's mention moves
            else:
                await graph.merge_nodes(m.node_uuid, str(m.entity_id))  # a name variant that never became an entity, or a merged entity
            moved += 1
        return moved

    async def restore_interaction(self, ctx: WorkspaceContext, interaction_id: uuid.UUID) -> RestoreReport:
        """The graph_projection job of an interaction: restores everything its extraction could have touched, fixes the mentions, stamps the episode."""
        ents, facts = await self.interaction_scope(ctx, interaction_id)
        report = await self.restore_scope(ctx, entity_ids=ents, fact_ids=facts)
        async with tenant_session(self._engine, ctx) as conn:
            episodes = (
                await conn.execute(
                    text(
                        "SELECT g.episode_uuid, i.recorded_at FROM graph_episodes g JOIN interactions i ON i.workspace_id = g.workspace_id AND i.id = g.interaction_id "
                        "WHERE g.workspace_id = :ws AND g.interaction_id = :i"
                    ),
                    {"ws": ctx.workspace_id, "i": interaction_id},
                )
            ).all()
        graph = self._scoped(ctx)
        await self.fix_mentions(ctx, interaction_id)
        for ep in episodes:
            await graph.restamp_episode(ep.episode_uuid, created_at=ep.recorded_at)
        return report

    async def restore_command(self, ctx: WorkspaceContext, command_id: uuid.UUID) -> RestoreReport:
        """A review decision or a merge: the ids it changed are in its result."""
        async with tenant_session(self._engine, ctx) as conn:
            row = (
                await conn.execute(
                    text("SELECT result FROM commands WHERE workspace_id = :ws AND id = :id"), {"ws": ctx.workspace_id, "id": command_id}
                )
            ).first()
        if row is None:
            raise NotFound()
        result = row.result or {}
        ents = {uuid.UUID(x) for x in result.get("entity_ids", [])}
        facts = {uuid.UUID(x) for x in result.get("fact_ids", [])}
        report = await self.restore_scope(ctx, entity_ids=ents, fact_ids=facts)
        split = result.get("split")
        if split:  # an identity split: the episodes whose mentions moved now link to the split-off node (the original's node keeps every other episode)
            graph = self._scoped(ctx)
            for m in split.get("mentions", []):
                if await graph.read_node(split["original"]) is not None and await graph.read_node(split["new"]) is not None:
                    await graph.move_mentions(split["original"], split["new"], episode_uuid=m["episode_uuid"])
        return report

    # ---- drift check ----------------------------------------------------------------------------------------------------------------------

    async def reconcile(self, ctx: WorkspaceContext, *, repair: bool = False) -> ReconcileReport:
        """Compares every Postgres entity and fact with its node or edge (M7: read-back keys are stripped) and optionally repairs drift."""
        report = ReconcileReport()
        graph = self._scoped(ctx)
        async with tenant_session(self._engine, ctx) as conn:
            entities = (
                await conn.execute(
                    text("SELECT * FROM entities WHERE workspace_id = :ws AND status IN ('active','candidate','untyped')"),
                    {"ws": ctx.workspace_id},
                )
            ).all()
            merged = (
                await conn.execute(text("SELECT id FROM entities WHERE workspace_id = :ws AND status = 'merged'"), {"ws": ctx.workspace_id})
            ).all()
            facts = (
                await conn.execute(
                    text(
                        "SELECT f.*, s.display_name AS s_name, t.display_name AS t_name FROM facts f "
                        "JOIN entities s ON s.workspace_id = f.workspace_id AND s.id = f.source_entity_id "
                        "JOIN entities t ON t.workspace_id = f.workspace_id AND t.id = f.target_entity_id WHERE f.workspace_id = :ws AND f.status NOT IN ('deleted','deletion_pending')"
                    ),
                    {"ws": ctx.workspace_id},
                )
            ).all()
            episodes: dict[uuid.UUID, list[str]] = {}
            for ev in (
                await conn.execute(
                    text(
                        "SELECT fact_id, episode_uuid FROM fact_evidence WHERE workspace_id = :ws AND source_kind = 'episode' ORDER BY added_at, id"
                    ),
                    {"ws": ctx.workspace_id},
                )
            ).all():
                episodes.setdefault(ev.fact_id, []).append(ev.episode_uuid)
            specs: dict[uuid.UUID, OntologySpec] = {}
            for vid in {r.ontology_version_id for r in (*entities, *facts)}:
                specs[vid] = await self._specs.get(conn, ctx.workspace_id, vid)
        graph_nodes, graph_edges = await graph.list_uuids()
        known = {str(e.id) for e in entities}, {str(f.id) for f in facts}
        repair_entities: list[uuid.UUID] = []
        repair_facts: list[uuid.UUID] = []
        for m in merged:  # a merged entity has no node of its own any more
            if str(m.id) in graph_nodes:
                report.drift.append(Drift("node", str(m.id), "unexpected"))
                repair_entities.append(m.id)
        for e in entities:
            report.checked_entities += 1
            if e.projection_status == "skipped_by_policy":
                report.skipped_by_policy += 1
                if str(e.id) in graph_nodes:
                    report.drift.append(Drift("node", str(e.id), "unexpected"))
                continue
            if e.projection_status != "projected":
                report.pending += 1
                continue
            want = render_node(e, specs[e.ontology_version_id])
            got = await graph.read_node(want.uuid)
            if got is None:
                report.drift.append(Drift("node", want.uuid, "missing"))
                repair_entities.append(e.id)
                continue
            if diff := node_diff(want, got):
                report.drift.append(Drift("node", want.uuid, "mismatch", diff))
                repair_entities.append(e.id)
        for f in facts:
            report.checked_facts += 1
            if f.projection_status == "skipped_by_policy":
                report.skipped_by_policy += 1
                if str(f.id) in graph_edges:
                    report.drift.append(Drift("edge", str(f.id), "unexpected"))
                continue
            if f.projection_status != "projected":
                report.pending += 1
                continue
            want_e = render_edge(f, f.s_name, f.t_name, specs[f.ontology_version_id], tuple(episodes.get(f.id, ())))
            got_e = await graph.read_edge(want_e.uuid)
            if got_e is None:
                report.drift.append(Drift("edge", want_e.uuid, "missing"))
                repair_facts.append(f.id)
                continue
            if diff := edge_diff(want_e, got_e):
                report.drift.append(Drift("edge", want_e.uuid, "mismatch", diff))
                repair_facts.append(f.id)
        orphan_nodes, orphan_edges = graph_nodes - known[0] - {str(m.id) for m in merged}, graph_edges - known[1]
        report.drift += [Drift("edge", u, "orphan") for u in sorted(orphan_edges)] + [
            Drift("node", u, "orphan") for u in sorted(orphan_nodes)
        ]
        if repair:
            report.repaired = await self._repair(ctx, graph, repair_entities, repair_facts, orphan_nodes, orphan_edges)
        return report

    async def _repair(
        self,
        ctx: WorkspaceContext,
        graph: ScopedGraph,
        entities: list[uuid.UUID],
        facts: list[uuid.UUID],
        orphan_nodes: set[str],
        orphan_edges: set[str],
    ) -> int:
        for u in orphan_edges:
            await graph.delete_edge(u)  # edges first: deleting a node also deletes its edges
        for u in orphan_nodes:
            await graph.delete_node(u)
        async with tenant_session(self._engine, ctx) as conn:
            for table, ids in (("entities", entities), ("facts", facts)):
                for rid in ids:
                    await conn.execute(
                        text(
                            f"UPDATE {table} SET projection_status = 'pending', projection_attempts = 0, projection_next_at = now(), projected_revision = NULL WHERE workspace_id = :ws AND id = :id"  # noqa: S608
                        ),
                        {"ws": ctx.workspace_id, "id": rid},
                    )
        for rid in entities:
            await self.project_entity(ctx, rid)
        for rid in facts:
            await self.project_fact(ctx, rid)
        return len(entities) + len(facts) + len(orphan_nodes) + len(orphan_edges)
