"""The purge (phase-8 plan §5.4.1 step 3): store by store, in dependency order, each step resumable.

A step deletes (or redacts) the rows its pending items list, inside a transaction that has named the request (`app.purge_request_id`), so the database's purge gate
(migration 0008) is what allows it: the application role can touch nothing the plan did not list, and only while the request is `purging`. A step then marks its items
`done`; a re-run after a crash finds nothing pending for the steps that finished and completes the rest. The graph is purged LAST, through its port, from the final
Postgres state: it re-projects whatever survived (ADR-018 d).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

from kg_db.session import tenant_session
from kg_domain.principal import WorkspaceContext
from kg_obs import metrics
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from kg_governance.deletion import faults
from kg_governance.deletion.plan import KEYS
from kg_governance.deletion.ports import GraphPurgePort

# (item_kind, action). Children before parents; every table that points at another is emptied first.
STEPS: list[tuple[str, str]] = [
    ("context_items", "delete"),
    ("context_debug_snapshots", "delete"),
    ("context_traces", "delete"),  # a retention request removes whole traces; a deletion request only redacts the ids in them
    ("context_traces", "redact"),
    ("idempotency_records", "delete"),
    ("memory_embeddings", "delete"),
    ("job_attempts", "delete"),
    ("outbox_jobs", "delete"),
    ("review_items", "delete"),
    ("review_items", "redact"),
    ("memory_lineage", "delete"),
    ("memory_evidence", "delete"),
    ("memory_scopes", "delete"),
    ("memory_subjects", "delete"),
    ("curated_memories", "delete"),
    ("fact_evidence", "delete"),
    ("facts", "delete"),
    ("corrections", "delete"),
    ("attribute_corrections", "delete"),
    ("graph_episodes", "delete"),
    ("interaction_contents", "delete"),
    ("interaction_stages", "delete"),
    ("interaction_subjects", "delete"),
    ("entity_mentions", "delete"),
    ("attribute_claims", "delete"),
    ("entity_external_ids", "delete"),
    ("identity_non_matches", "delete"),
    ("entity_lineage", "delete"),
    ("principal_scopes", "delete"),
    ("entities", "delete"),
    ("interactions", "delete"),
    ("commands", "redact"),
]


@dataclass
class PurgeResult:
    steps: int = 0
    graph: dict[str, int] = field(default_factory=dict)


class Purger:
    def __init__(self, engine: AsyncEngine, graph: GraphPurgePort | None):
        self._engine, self._graph = engine, graph

    async def run(self, ctx: WorkspaceContext, request_id: uuid.UUID) -> PurgeResult:
        ws = ctx.workspace_id
        out = PurgeResult()
        for kind, action in STEPS:
            async with tenant_session(self._engine, ctx) as conn:
                await conn.execute(text("SELECT set_config('app.purge_request_id', :r, true)"), {"r": str(request_id)})
                if kind == "facts":
                    await self._detach_facts(conn, ws, request_id)
                n = await self._step(conn, ws, request_id, kind, action)
            if n:
                out.steps += 1
                store = KEYS[kind][0]
                metrics.deletion_items_total.labels(store=store, action=action).inc(n)
            faults.hit("deletion_purge", "after_side_effect")  # FI-13: a crash after one store's step; the re-run finishes the rest
        out.graph = await self._graph_step(ctx, request_id)
        return out

    async def _detach_facts(self, conn: AsyncConnection, ws: uuid.UUID, request_id: uuid.UUID) -> None:
        """Before the facts go: a surviving fact that pointed at a deleted one (succession) stops pointing, and a surviving fact whose first evidence was a deleted
        interaction is re-attached to the next one it has (it is retained because it HAS independent evidence)."""
        await conn.execute(
            text(
                "UPDATE facts t SET succeeded_by_fact_id = NULL FROM deletion_items i WHERE i.request_id = :r AND i.item_kind = 'facts' AND i.action = 'delete' "
                "AND t.workspace_id = :ws AND t.succeeded_by_fact_id::text = i.item_id AND NOT EXISTS ("
                "  SELECT 1 FROM deletion_items j WHERE j.request_id = :r AND j.item_kind = 'facts' AND j.action = 'delete' AND j.item_id = t.id::text)"
            ),
            {"r": request_id, "ws": ws},
        )
        await conn.execute(
            text(
                "UPDATE facts f SET source_interaction_id = ev.interaction_id, source_episode_uuid = ev.episode_uuid FROM ("
                "  SELECT DISTINCT ON (e.fact_id) e.fact_id, e.interaction_id, e.episode_uuid FROM fact_evidence e "
                "  WHERE e.workspace_id = :ws AND e.interaction_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM deletion_items d WHERE d.request_id = :r "
                "    AND d.item_kind = 'interactions' AND d.action = 'delete' AND d.item_id = e.interaction_id::text) "
                "  ORDER BY e.fact_id, e.added_at, e.id) ev "
                "WHERE f.workspace_id = :ws AND f.id = ev.fact_id AND f.source_interaction_id IS NOT NULL AND EXISTS ("
                "  SELECT 1 FROM deletion_items d WHERE d.request_id = :r AND d.item_kind = 'interactions' AND d.action = 'delete' AND d.item_id = f.source_interaction_id::text)"
            ),
            {"r": request_id, "ws": ws},
        )

    async def _step(self, conn: AsyncConnection, ws: uuid.UUID, request_id: uuid.UUID, kind: str, action: str) -> int:
        store, table, key = KEYS[kind]
        pending = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM deletion_items WHERE request_id = :r AND store = :s AND item_kind = :k AND action = :a AND status = 'pending'"
                ),
                {"r": request_id, "s": store, "k": kind, "a": action},
            )
        ).scalar_one()
        if not pending:
            return 0
        p = {"r": request_id, "ws": ws, "s": store, "k": kind, "a": action}
        if action == "delete":
            await conn.execute(
                text(
                    f"DELETE FROM {table} AS t USING deletion_items AS i WHERE i.request_id = :r AND i.store = :s AND i.item_kind = :k AND i.action = 'delete' "  # noqa: S608 (KEYS constants)
                    f"AND i.status = 'pending' AND t.workspace_id = :ws AND {key} = i.item_id"
                ),
                p,
            )
        elif kind == "commands":
            await conn.execute(
                text(
                    "UPDATE commands AS t SET payload = jsonb_build_object('redacted', true, 'hash', encode(sha256(convert_to(t.payload::text, 'UTF8')), 'hex')), "
                    "source_ref = CASE WHEN t.source_ref IS NULL THEN NULL ELSE '[redacted]' END "
                    "FROM deletion_items AS i WHERE i.request_id = :r AND i.store = :s AND i.item_kind = :k AND i.action = 'redact' AND i.status = 'pending' "
                    "AND t.workspace_id = :ws AND t.id::text = i.item_id AND NOT (t.payload ? 'redacted')"
                ),
                p,
            )
        elif kind == "review_items":
            await conn.execute(
                text(
                    "UPDATE review_items AS t SET rationale_text = CASE WHEN t.rationale_text IS NULL THEN NULL ELSE '[redacted]' END, detail = '{}'::jsonb "
                    "FROM deletion_items AS i WHERE i.request_id = :r AND i.store = :s AND i.item_kind = :k AND i.action = 'redact' AND i.status = 'pending' "
                    "AND t.workspace_id = :ws AND t.id::text = i.item_id"
                ),
                p,
            )
        elif kind == "context_traces":
            ids = [
                r.item_id
                for r in (
                    await conn.execute(
                        text("SELECT item_id FROM deletion_items WHERE request_id = :r AND item_kind = 'entities' AND action = 'delete'"),
                        {"r": request_id},
                    )
                ).all()
            ]
            rows = (
                await conn.execute(
                    text(
                        "SELECT t.id, t.request FROM context_traces t JOIN deletion_items i ON i.request_id = :r AND i.store = 'trace' AND i.item_kind = 'context_traces' "
                        "AND i.action = 'redact' AND i.status = 'pending' AND t.id::text = i.item_id WHERE t.workspace_id = :ws"
                    ),
                    {"r": request_id, "ws": ws},
                )
            ).all()
            for row in rows:
                body = json.dumps(row.request)
                for eid in ids:
                    body = body.replace(eid, "redacted")
                await conn.execute(
                    text("UPDATE context_traces SET request = CAST(:b AS jsonb) WHERE workspace_id = :ws AND id = :id"),
                    {"b": body, "ws": ws, "id": row.id},
                )
        else:  # pragma: no cover - the table above lists every redaction
            raise ValueError(f"no redaction for {kind}")
        await conn.execute(
            text(
                "UPDATE deletion_items SET status = 'done', attempts = attempts + 1 WHERE request_id = :r AND store = :s AND item_kind = :k AND action = :a AND status = 'pending'"
            ),
            p,
        )
        return int(pending)

    async def _graph_step(self, ctx: WorkspaceContext, request_id: uuid.UUID) -> dict[str, int]:
        if self._graph is None:
            return {}
        async with tenant_session(self._engine, ctx) as conn:
            rows = (
                await conn.execute(
                    text("SELECT item_kind, item_id FROM deletion_items WHERE request_id = :r AND store = 'graph' AND status = 'pending'"),
                    {"r": request_id},
                )
            ).all()
        by: dict[str, list[str]] = {"episode": [], "node": [], "edge": [], "summary": [], "reproject_node": [], "reproject_edge": []}
        for r in rows:
            by.setdefault(r.item_kind, []).append(r.item_id)
        counts = await self._graph.purge(
            ctx,
            episode_uuids=by["episode"],
            node_uuids=by["node"],
            edge_uuids=by["edge"],
            summary_nodes=by["summary"],
            reproject_nodes=by["reproject_node"],
            reproject_edges=by["reproject_edge"],
        )
        faults.hit("deletion_purge", "after_side_effect")
        async with tenant_session(self._engine, ctx) as conn:
            await conn.execute(
                text(
                    "UPDATE deletion_items SET status = 'done', attempts = attempts + 1 WHERE request_id = :r AND store = 'graph' AND status = 'pending'"
                ),
                {"r": request_id},
            )
        for v in counts.__dict__.values():
            metrics.deletion_items_total.labels(store="graph", action="delete").inc(int(v) if isinstance(v, int) else 0)
        return {k: int(v) for k, v in counts.__dict__.items()}


def tombstone_sql() -> str:
    return (
        "INSERT INTO deletion_tombstones (workspace_id, kind, id, request_id) "
        "SELECT i.workspace_id, CASE i.item_kind WHEN 'entities' THEN 'entity' WHEN 'facts' THEN 'fact' WHEN 'interactions' THEN 'interaction' ELSE 'memory' END, "
        "CAST(i.item_id AS uuid), i.request_id FROM deletion_items i WHERE i.request_id = :r AND i.store = 'postgres' AND i.action = 'delete' "
        "AND i.item_kind IN ('entities','facts','interactions','curated_memories') ON CONFLICT DO NOTHING"
    )
