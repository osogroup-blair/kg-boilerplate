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
from dataclasses import dataclass, field
from typing import Any

from kg_common.errors import NotFound
from kg_common.ids import uuid7_str
from kg_db.session import system_session, tenant_session
from kg_domain.principal import WorkspaceContext
from kg_graph.errors import EndpointNotProjected
from kg_graph.scoped import ScopedGraph
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
from sqlalchemy.ext.asyncio import AsyncEngine

from kg_knowledge.specs import SpecCache

log = get_logger("kg.projection")
MAX_ATTEMPTS = 8
LEASE_SECONDS = 60
PROJECTED = ("projected", "skipped_by_policy")
_RETRYABLE_NAMES = frozenset(
    {
        "ServiceUnavailable",
        "SessionExpired",
        "TransientError",
        "DatabaseUnavailable",
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
                if names & {"ServiceUnavailable", "SessionExpired", "TransientError", "DatabaseUnavailable"}
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


def render_edge(row: Row[Any], source_name: str, target_name: str, spec: OntologySpec) -> RenderedEdge:
    declared = spec.relationship_types.get(row.rel_type)
    sensitive = {k for k, a in declared.attributes.items() if a.sensitive} if declared else set()
    public = {k: v for k, v in (row.attributes or {}).items() if k not in sensitive and isinstance(v, str | int | float | bool)}
    fact = render_fact(declared.fact_template if declared else None, row.rel_type, source_name, target_name, public)
    attrs = {
        **public,
        "platform_fact_id": str(row.id),
        "source_command_id": str(row.source_command_id),
        "authority_class": row.authority_class,
        "ontology_version": version_label(spec),
    }
    return RenderedEdge(
        str(row.id),
        str(row.source_entity_id),
        str(row.target_entity_id),
        row.rel_type,
        fact,
        row.valid_from,
        row.valid_to,
        row.recorded_at,
        row.superseded_at,
        attrs,
    )


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
                        "SELECT id, type_key, display_name, attributes, classification, revision, ontology_version_id, recorded_at, projection_status, projected_revision "
                        "FROM entities WHERE workspace_id = :ws AND id = :id"
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
        if spec is None:
            return await self._mark_failed(ctx, "entities", entity_id, row.revision, "ontology_unreadable", False, "entity")
        if row.projection_status in PROJECTED and row.projected_revision == row.revision:
            return str(row.projection_status)
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

    # ---- fact -----------------------------------------------------------------------------------------------------------------------------

    async def project_fact(self, ctx: WorkspaceContext, fact_id: uuid.UUID) -> str:
        async with tenant_session(self._engine, ctx) as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT f.id, f.rel_type, f.source_entity_id, f.target_entity_id, f.attributes, f.valid_from, f.valid_to, f.recorded_at, f.superseded_at, "
                        "f.authority_class, f.classification, f.source_command_id, f.revision, f.ontology_version_id, f.projection_status, f.projected_revision, "
                        "s.display_name AS s_name, s.classification AS s_cls, s.projection_status AS s_proj, "
                        "t.display_name AS t_name, t.classification AS t_cls, t.projection_status AS t_proj "
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
        if spec is None:
            return await self._mark_failed(ctx, "facts", fact_id, row.revision, "ontology_unreadable", False, "fact")
        if row.projection_status in PROJECTED and row.projected_revision == row.revision:
            return str(row.projection_status)
        classification = stricter(stricter(row.classification, row.s_cls), row.t_cls)  # the fact text carries both names
        if not self._config.egress_allows(classification) or "skipped_by_policy" in (row.s_proj, row.t_proj):
            await self._mark_done(ctx, "facts", fact_id, row.revision, "skipped_by_policy", "fact")
            return "skipped_by_policy"
        for endpoint, status in ((row.source_entity_id, row.s_proj), (row.target_entity_id, row.t_proj)):
            if status != "projected":
                await self.project_entity(ctx, endpoint)  # an edge needs both nodes; projecting them first is idempotent
        edge = render_edge(row, row.s_name, row.t_name, spec)
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
                    text("SELECT status, result, projection_status FROM commands WHERE workspace_id = :ws AND id = :id"),
                    {"ws": ctx.workspace_id, "id": command_id},
                )
            ).first()
        if row is None:
            raise NotFound()
        result = row.result or {}
        if row.status != "applied" or result.get("matched_existing"):
            return str(row.projection_status)
        if result.get("entity_id") and "fact_id" not in result:
            await self.project_entity(ctx, uuid.UUID(result["entity_id"]))
        fact_ids = [result["fact_id"], *result.get("ended_fact_ids", [])] if result.get("fact_id") else []
        for fid in dict.fromkeys(fact_ids):
            await self.project_fact(ctx, uuid.UUID(fid))
        return await self.refresh_command(ctx, command_id)

    async def retry_command(self, ctx: WorkspaceContext, command_id: uuid.UUID) -> str:
        """Operator retry (kg commands retry-projection): make the command's rows due again and project them now."""
        async with tenant_session(self._engine, ctx) as conn:
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

    # ---- sweeper (the P2 driver; OFF by default since P3 moved projection onto the outbox, deleted in P4) ---------------------------------------------------------

    async def _claim(self, ctx: WorkspaceContext, table: str, limit: int) -> list[uuid.UUID]:
        """Leases due rows (visibility timeout) with SKIP LOCKED, so parallel sweepers never take the same row and a crash just lets the lease expire."""
        async with tenant_session(self._engine, ctx) as conn:
            rows = (
                await conn.execute(
                    text(
                        f"UPDATE {table} SET projection_next_at = now() + (:lease * interval '1 second') WHERE workspace_id = :ws AND id IN ("  # noqa: S608
                        f"SELECT id FROM {table} WHERE workspace_id = :ws AND projection_status = 'pending' AND projection_next_at <= now() "  # noqa: S608
                        "ORDER BY recorded_at, id LIMIT :n FOR UPDATE SKIP LOCKED) RETURNING id"
                    ),
                    {"ws": ctx.workspace_id, "lease": LEASE_SECONDS, "n": limit},
                )
            ).all()
        return [r.id for r in rows]

    async def sweep_workspace(self, ctx: WorkspaceContext, limit: int = 25) -> int:
        done = 0
        for eid in await self._claim(ctx, "entities", limit):
            await self.project_entity(ctx, eid)
            done += 1
        for fid in await self._claim(ctx, "facts", limit):
            await self.project_fact(ctx, fid)
            done += 1
        if done:
            await self._refresh_recent_commands(ctx)
        return done

    async def _refresh_recent_commands(self, ctx: WorkspaceContext) -> None:
        async with tenant_session(self._engine, ctx) as conn:
            ids = [
                r.id
                for r in (
                    await conn.execute(
                        text(
                            "SELECT id FROM commands WHERE workspace_id = :ws AND status = 'applied' AND projection_status IN ('pending','failed') ORDER BY created_at LIMIT 100"
                        ),
                        {"ws": ctx.workspace_id},
                    )
                ).all()
            ]
        for cid in ids:
            await self.refresh_command(ctx, cid)

    async def sweep_once(self, *, workspaces: int = 20, per_workspace: int = 25) -> int:
        async with system_session(self._engine) as conn:
            ids = [
                r[0]
                for r in (await conn.execute(text("SELECT * FROM ops.workspaces_with_pending_projection(:n)"), {"n": workspaces})).all()
            ]
        total = 0
        pending = 0
        for ws in ids:
            ctx = WorkspaceContext.for_system_task(ws, reason="projection sweeper")
            try:
                total += await self.sweep_workspace(ctx, per_workspace)
                pending += await self._pending(ctx)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # one workspace's problem must not starve the others
                log.warning("projection_sweep_workspace_failed", error_type=type(exc).__name__)
        metrics.projection_pending.set(pending)
        return total

    async def _pending(self, ctx: WorkspaceContext) -> int:
        async with tenant_session(self._engine, ctx) as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT (SELECT count(*) FROM entities WHERE workspace_id = :ws AND projection_status = 'pending') + (SELECT count(*) FROM facts WHERE workspace_id = :ws AND projection_status = 'pending') AS n"
                    ),
                    {"ws": ctx.workspace_id},
                )
            ).one()
        return int(row.n)

    # ---- drift check ----------------------------------------------------------------------------------------------------------------------

    async def reconcile(self, ctx: WorkspaceContext, *, repair: bool = False) -> ReconcileReport:
        """Compares every Postgres entity and fact with its node or edge (M7: read-back keys are stripped) and optionally repairs drift."""
        report = ReconcileReport()
        graph = self._scoped(ctx)
        async with tenant_session(self._engine, ctx) as conn:
            entities = (
                await conn.execute(text("SELECT * FROM entities WHERE workspace_id = :ws AND status = 'active'"), {"ws": ctx.workspace_id})
            ).all()
            facts = (
                await conn.execute(
                    text(
                        "SELECT f.*, s.display_name AS s_name, t.display_name AS t_name FROM facts f "
                        "JOIN entities s ON s.workspace_id = f.workspace_id AND s.id = f.source_entity_id "
                        "JOIN entities t ON t.workspace_id = f.workspace_id AND t.id = f.target_entity_id WHERE f.workspace_id = :ws AND f.status <> 'deleted'"
                    ),
                    {"ws": ctx.workspace_id},
                )
            ).all()
            specs: dict[uuid.UUID, OntologySpec] = {}
            for vid in {r.ontology_version_id for r in (*entities, *facts)}:
                specs[vid] = await self._specs.get(conn, ctx.workspace_id, vid)
        graph_nodes, graph_edges = await graph.list_uuids()
        known = {str(e.id) for e in entities}, {str(f.id) for f in facts}
        repair_entities: list[uuid.UUID] = []
        repair_facts: list[uuid.UUID] = []
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
            diff = [
                name
                for name, same in (
                    ("name", got.name == want.name),
                    ("labels", got.labels == frozenset({want.type_key})),
                    ("attributes", got.attributes == want.attributes),
                    ("created_at", got.created_at == want.created_at),
                )
                if not same
            ]
            if diff:
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
            want_e = render_edge(f, f.s_name, f.t_name, specs[f.ontology_version_id])
            got_e = await graph.read_edge(want_e.uuid)
            if got_e is None:
                report.drift.append(Drift("edge", want_e.uuid, "missing"))
                repair_facts.append(f.id)
                continue
            diff = [
                name
                for name, same in (
                    ("name", got_e.name == want_e.name),
                    ("fact", got_e.fact == want_e.fact),
                    ("endpoints", (got_e.source_uuid, got_e.target_uuid) == (want_e.source_uuid, want_e.target_uuid)),
                    ("valid_at", got_e.valid_at == want_e.valid_at),
                    ("invalid_at", got_e.invalid_at == want_e.invalid_at),
                    ("created_at", got_e.created_at == want_e.created_at),
                    ("expired_at", got_e.expired_at == want_e.expired_at),
                    ("attributes", got_e.attributes == want_e.attributes),
                )
                if not same
            ]
            if diff:
                report.drift.append(Drift("edge", want_e.uuid, "mismatch", diff))
                repair_facts.append(f.id)
        orphan_nodes, orphan_edges = graph_nodes - known[0], graph_edges - known[1]
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
