"""Memory in the correction flow (phase-8 plan §5.1).

Two things live here:

* `MemoryCorrectionPort` is what `kg_knowledge.Corrections` asks of this package (kg_knowledge cannot import it): which memories rest on a fact, and the two propagation
  steps. A rejected or superseded fact makes the SHARED memories that cite it `disputed` and opens a `memory_conflict` item (`source_fact_rejected`); restoring the fact
  reverses both. Private memory (agent and session scopes only) is its writer's own business and is never flagged.
* `MemoryCorrections` is `POST /v1/memories/{id}/corrections` (supersede with new content, reject, expire, dispute): the writer does the work and the ledger command; this
  adds the correction record, closes the items the memory was waiting in, and shows the impact beforehand.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from kg_common.errors import NotFound
from kg_common.ids import uuid7
from kg_domain.principal import Principal, WorkspaceContext
from kg_governance.audit import AuditMetadata, CommandType
from kg_governance.authz.policy import Action, is_allowed
from kg_knowledge.commands import CommandContext
from kg_knowledge.correction_models import CorrectionOut, DependentMemory, MemoryCorrectionIn, PreviewOut
from kg_obs import metrics
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from kg_memory import store
from kg_memory.models import ReasonIn, SupersedeIn
from kg_memory.reads import MemoryReads, Viewer, visible_where
from kg_memory.scopes import ScopeGrants
from kg_memory.writers import MemoryWriter, WriteContext

_SHARED_SCOPE = "EXISTS (SELECT 1 FROM memory_scopes s WHERE s.workspace_id = m.workspace_id AND s.memory_id = m.id AND s.scope_kind IN ('workspace','subject','project'))"
_LIVE = ("active", "proposed", "disputed")


class MemoryCorrectionPort:
    """Implements `kg_knowledge.corrections.MemoryDependents`."""

    async def _viewer(self, conn: AsyncConnection, principal: Principal) -> Viewer:
        return Viewer(principal, await ScopeGrants.for_principal(conn, principal), curator=is_allowed(principal, Action.MEMORY_CURATE))

    async def visible(self, conn: AsyncConnection, principal: Principal, fact_ids: list[uuid.UUID]) -> list[DependentMemory]:
        where, params = visible_where(await self._viewer(conn, principal), prefix="dv")
        rows = (
            await conn.execute(
                text(
                    f"SELECT DISTINCT m.id, m.type, m.status FROM memory_evidence e JOIN curated_memories m ON m.workspace_id = e.workspace_id AND m.id = e.memory_id "  # noqa: S608
                    f"WHERE e.fact_id = ANY(:ids) AND m.status = ANY(:live) AND {where} ORDER BY m.id LIMIT 200"
                ),
                {"ids": fact_ids, "live": list(_LIVE), **params},
            )
        ).all()
        return [DependentMemory(id=r.id, type=r.type, status=r.status) for r in rows]

    async def about_entity(self, conn: AsyncConnection, principal: Principal, entity_id: uuid.UUID) -> list[DependentMemory]:
        where, params = visible_where(await self._viewer(conn, principal), prefix="av")
        rows = (
            await conn.execute(
                text(
                    f"SELECT DISTINCT m.id, m.type, m.status FROM memory_subjects s JOIN curated_memories m ON m.workspace_id = s.workspace_id AND m.id = s.memory_id "  # noqa: S608
                    f"WHERE s.entity_id = :e AND m.status = ANY(:live) AND {where} ORDER BY m.id LIMIT 200"
                ),
                {"e": entity_id, "live": list(_LIVE), **params},
            )
        ).all()
        return [DependentMemory(id=r.id, type=r.type, status=r.status) for r in rows]

    async def flag(self, cc: CommandContext, fact_ids: list[uuid.UUID], *, correction_id: uuid.UUID) -> list[uuid.UUID]:
        conn, ws = cc.conn, cc.ctx.workspace_id
        rows = (
            await conn.execute(
                text(
                    f"SELECT m.id, m.type FROM curated_memories m WHERE m.workspace_id = :ws AND m.status IN ('active','proposed') AND {_SHARED_SCOPE} "  # noqa: S608
                    "AND EXISTS (SELECT 1 FROM memory_evidence e WHERE e.workspace_id = m.workspace_id AND e.memory_id = m.id AND e.fact_id = ANY(:ids)) "
                    "ORDER BY m.id FOR UPDATE OF m"
                ),
                {"ws": ws, "ids": fact_ids},
            )
        ).all()
        flagged: list[uuid.UUID] = []
        for m in rows:
            await store.set_status(conn, ws, m.id, "disputed")
            item = uuid7()
            created = (
                await conn.execute(
                    text(
                        "INSERT INTO review_items (id, workspace_id, item_type, rule_kind, reason_code, memory_id, detail, due_at) "
                        "VALUES (:id, :ws, 'memory_conflict', 'correction', 'source_fact_rejected', :m, CAST(:d AS jsonb), now() + interval '7 days') ON CONFLICT DO NOTHING RETURNING id"
                    ),
                    {
                        "id": item,
                        "ws": ws,
                        "m": m.id,
                        "d": json.dumps({"correction_id": str(correction_id), "source_fact_ids": [str(f) for f in fact_ids]}),
                    },
                )
            ).first()
            await cc.audit.record(
                "memory.disputed",
                resource_type="memory",
                resource_id=m.id,
                metadata=AuditMetadata(
                    memory_id=m.id,
                    memory_type=m.type,
                    memory_status="disputed",
                    reason_code="source_fact_rejected",
                    review_id=created.id if created else None,
                ),
            )
            if created is not None:
                metrics.reviews_created_total.labels(item_type="memory_conflict").inc()
            flagged.append(m.id)
        return flagged

    async def unflag(self, cc: CommandContext, fact_ids: list[uuid.UUID], *, correction_id: uuid.UUID) -> list[uuid.UUID]:
        conn, ws = cc.conn, cc.ctx.workspace_id
        items = (
            await conn.execute(
                text(
                    "SELECT r.id, r.memory_id FROM review_items r JOIN curated_memories m ON m.workspace_id = r.workspace_id AND m.id = r.memory_id "
                    "WHERE r.workspace_id = :ws AND r.item_type = 'memory_conflict' AND r.reason_code = 'source_fact_rejected' AND r.status = 'open' AND m.status = 'disputed' "
                    "AND EXISTS (SELECT 1 FROM jsonb_array_elements_text(COALESCE(r.detail -> 'source_fact_ids', '[]'::jsonb)) x WHERE x = ANY(:ids)) ORDER BY r.id FOR UPDATE OF r, m"
                ),
                {"ws": ws, "ids": [str(f) for f in fact_ids]},
            )
        ).all()
        restored: list[uuid.UUID] = []
        for r in items:
            await store.set_status(conn, ws, r.memory_id, "active", confirm=True)
            await conn.execute(
                text(
                    "UPDATE review_items SET status = 'resolved', resolved_by = :by, resolved_at = now(), resolution_code = 'fact_restored', revision = revision + 1 "
                    "WHERE workspace_id = :ws AND id = :id"
                ),
                {"ws": ws, "id": r.id, "by": cc.principal.principal_id},
            )
            await cc.audit.record(
                "memory.restored",
                resource_type="memory",
                resource_id=r.memory_id,
                metadata=AuditMetadata(memory_id=r.memory_id, memory_status="active", reason_code="fact_restored"),
            )
            restored.append(r.memory_id)
        return restored


class MemoryCorrections:
    def __init__(self, writer: MemoryWriter, reads: MemoryReads, *, trace_retention_days: int = 30):
        self._writer, self._reads, self._trace_days = writer, reads, trace_retention_days

    async def apply(
        self, w: WriteContext, memory_id: uuid.UUID, body: MemoryCorrectionIn, key: str, if_match: int | None
    ) -> tuple[CorrectionOut, bool]:
        """Returns (result, replayed). The writer answers a repeated key with the stored result; the correction row is written once."""
        conn, ws = w.conn, w.ctx.workspace_id
        if body.kind == "supersede":
            assert body.content is not None
            written = await self._writer.supersede(
                w, memory_id, SupersedeIn(content=body.content, summary=body.summary, reason_code=body.reason_code), key, if_match
            )
        elif body.kind == "reject":
            written = await self._writer.reject(w, memory_id, ReasonIn(reason_code=body.reason_code), key, if_match)
        elif body.kind == "expire":
            written = await self._writer.expire(w, memory_id, ReasonIn(reason_code=body.reason_code), key, if_match)
        else:
            written = await self._writer.dispute(w, memory_id, ReasonIn(reason_code=body.reason_code), key, if_match)
        command_id = written.out.command_id
        assert command_id is not None
        new_id = written.out.memory_id if body.kind == "supersede" else None
        existing = (
            await conn.execute(text("SELECT id FROM corrections WHERE workspace_id = :ws AND command_id = :c"), {"ws": ws, "c": command_id})
        ).first()
        correction_id = existing.id if existing else uuid7()
        reviews: list[uuid.UUID] = []
        if existing is None:
            await conn.execute(
                text(
                    "INSERT INTO corrections (id, workspace_id, command_id, target_kind, target_id, kind, reason_code, reason_text, new_target_id, created_by) "
                    "VALUES (:id, :ws, :c, 'memory', :t, :k, :rc, :rt, :nt, :by)"
                ),
                {
                    "id": correction_id,
                    "ws": ws,
                    "c": command_id,
                    "t": memory_id,
                    "k": f"memory.{body.kind}",
                    "rc": body.reason_code,
                    "rt": body.reason_text,
                    "nt": new_id,
                    "by": w.principal.principal_id,
                },
            )
            if body.kind != "dispute":  # a correction ends the questions raised about the old wording
                await conn.execute(
                    text(
                        "UPDATE review_items SET status = 'resolved', resolved_by = :by, resolved_at = now(), resolution_code = 'corrected', revision = revision + 1 "
                        "WHERE workspace_id = :ws AND memory_id = :m AND status = 'open' AND item_type IN ('memory_conflict','memory_proposal')"
                    ),
                    {"ws": ws, "m": memory_id, "by": w.principal.principal_id},
                )
            await w.audit.record(
                "correction.applied",
                resource_type="memory",
                resource_id=memory_id,
                metadata=AuditMetadata(
                    correction_kind=f"memory.{body.kind}",
                    command_type=CommandType.CORRECTION_MEMORY,
                    reason_code=body.reason_code,
                    memory_id=memory_id,
                    target_id=new_id,
                    memory_status=written.out.status if body.kind != "supersede" else "superseded",
                ),
            )
            metrics.corrections_total.labels(kind=f"memory.{body.kind}").inc()
        return (
            CorrectionOut(
                correction_id=correction_id,
                command_id=command_id,
                kind=f"memory.{body.kind}",
                target_kind="memory",
                target_id=memory_id,
                new_target_id=new_id,
                revision=written.out.revision,
                projection_status="not_applicable",
                review_item_ids=reviews,
                status_url=f"/v1/memories/{new_id or memory_id}",
            ),
            written.replayed,
        )

    async def preview(
        self, conn: AsyncConnection, ctx: WorkspaceContext, v: Viewer, memory_id: uuid.UUID, kind: str, *, include_contexts: bool
    ) -> PreviewOut:
        ws = ctx.workspace_id
        try:
            row = await self._reads.visible_row(conn, v, memory_id)
        except NotFound:
            raise
        where, params = visible_where(v, prefix="pv")
        deps = (
            await conn.execute(
                text(
                    f"SELECT DISTINCT m.id, m.type, m.status FROM memory_evidence e JOIN curated_memories m ON m.workspace_id = e.workspace_id AND m.id = e.memory_id "  # noqa: S608
                    f"WHERE e.source_memory_id = :id AND m.status = ANY(:live) AND {where} ORDER BY m.id LIMIT 200"
                ),
                {"id": memory_id, "live": list(_LIVE), **params},
            )
        ).all()
        reviews = (
            await conn.execute(
                text("SELECT id FROM review_items WHERE workspace_id = :ws AND memory_id = :m AND status = 'open' ORDER BY created_at, id"),
                {"ws": ws, "m": memory_id},
            )
        ).all()
        contexts: int | None = None
        if include_contexts:
            contexts = int(
                (
                    await conn.execute(
                        text(
                            "SELECT count(DISTINCT i.context_id) FROM context_items i JOIN context_traces t ON t.workspace_id = i.workspace_id AND t.id = i.context_id "
                            "WHERE i.workspace_id = :ws AND i.item_kind = 'memory' AND i.item_id = :id AND i.included AND t.created_at > now() - make_interval(days => :d)"
                        ),
                        {"ws": ws, "id": str(memory_id), "d": self._trace_days},
                    )
                ).scalar_one()
            )
        counts: dict[str, int] = {}
        for d in deps:
            counts[d.status] = counts.get(d.status, 0) + 1
        ops = {
            "supersede": ["leave_search", "embed_replacement", "rematerialise_contexts"],
            "reject": ["leave_search", "rematerialise_contexts"],
            "expire": ["leave_search", "rematerialise_contexts"],
            "dispute": ["flag_in_context", "open_review"],
        }[kind.removeprefix("memory.")]
        return PreviewOut(
            kind=f"memory.{kind.removeprefix('memory.')}",
            target_kind="memory",
            target_id=memory_id,
            authoritative=row.authority_class == "authoritative_system",
            dependent_memories=[DependentMemory(id=r.id, type=r.type, status=r.status) for r in deps],
            dependent_memory_counts=counts,
            open_review_ids=[r.id for r in reviews],
            recent_context_count=contexts,
            graph_operations=ops,
        )

    async def history(self, conn: AsyncConnection, ws: uuid.UUID, memory_id: uuid.UUID) -> list[dict[str, Any]]:
        rows = (
            await conn.execute(
                text(
                    "SELECT m.id, l.relation, m.status FROM memory_lineage l JOIN curated_memories m ON m.workspace_id = l.workspace_id AND m.id = l.related_id "
                    "WHERE l.workspace_id = :ws AND l.memory_id = :id ORDER BY m.recorded_at"
                ),
                {"ws": ws, "id": memory_id},
            )
        ).all()
        return [{"id": r.id, "relation": r.relation, "status": r.status} for r in rows]
