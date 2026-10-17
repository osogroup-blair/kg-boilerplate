"""`kg deletions reapply` (phase-9 plan WP5, V5, ADR-045): after a restore, forget again what the platform had already been told to forget.

The out-of-band deletion journal outlives the database and its backups. A backup taken before a deletion brings the deleted data back, so a restore is not finished until every
journal line completed after the backup was made has been applied to the restored environment. For each such line this module:

1. verifies the HMAC chain of the WHOLE journal first (a removed, edited or reordered line stops everything: `JournalCorrupt`);
2. lists which of the line's ids exist in the restored Postgres. Nothing present and the tombstones in place: the line is already applied (idempotent);
3. otherwise drives the SAME deletion machinery the API uses on a synthetic request that reuses the line's request id (so the journal line is found, not duplicated): the
   identity rows go in the plan, the derived rows are listed by the real planner, the purge runs through the database's purge gate, the verifier scans for residuals, the
   receipt is written, the tombstones and the suppression HMACs of the line are recorded.

It only ever REMOVES: it never creates an entity, fact, memory or interaction, and the ids in a journal line are the closure the original request purged, so a line cannot reach a
row that was not part of it. The jobs a normal request enqueues between steps are replaced by direct calls, so a reapply leaves no job behind."""

from __future__ import annotations

import copy
import datetime as dt
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from kg_db.session import tenant_session
from kg_domain.principal import WorkspaceContext
from kg_obs import metrics
from kg_obs.logging import get_logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from kg_governance.audit import AuditMetadata, SystemAudit
from kg_governance.deletion import plan as planner
from kg_governance.deletion.journal import DeletionJournal
from kg_governance.deletion.requests import TERMINAL, DeletionService

log = get_logger("kg.deletion.reapply")

IDENTITY = (
    ("entities", "entities", "entity"),
    ("facts", "facts", "fact"),
    ("memories", "curated_memories", "memory"),
    ("interactions", "interactions", "interaction"),
)


async def _no_enqueue(*args: Any, **kwargs: Any) -> None:
    """A reapply calls plan, purge and verify itself: the outbox jobs a live request chains them with are not wanted."""
    return None


@dataclass
class ReapplyResult:
    seq: int
    request_id: uuid.UUID
    workspace_id: uuid.UUID
    kind: str
    outcome: str  # applied | already_applied | would_apply | workspace_absent | attention
    present: dict[str, int] = field(default_factory=dict)
    status: str | None = None
    residuals: int = 0
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "request_id": str(self.request_id),
            "workspace_id": str(self.workspace_id),
            "kind": self.kind,
            "outcome": self.outcome,
            "present": self.present,
            "status": self.status,
            "residuals": self.residuals,
            "detail": self.detail,
        }


class Reapplier:
    def __init__(self, service: DeletionService, journal: DeletionJournal, engine: AsyncEngine):
        self._svc = copy.copy(service)
        self._svc._enqueue = _no_enqueue
        self._journal, self._engine = journal, engine

    async def reapply(self, since: dt.datetime, *, dry_run: bool = False, workspace_id: uuid.UUID | None = None) -> list[ReapplyResult]:
        """Every line completed at or after `since` (the backup's time). Raises `JournalCorrupt` before touching anything if the chain does not hold."""
        entries = self._journal.since(since)
        out: list[ReapplyResult] = []
        for entry in entries:
            if workspace_id is not None and uuid.UUID(entry["workspace_id"]) != workspace_id:
                continue
            result = await self._one(entry, dry_run=dry_run)
            out.append(result)
            if not dry_run and result.outcome == "applied":
                metrics.deletion_requests_total.labels(kind=result.kind, outcome="reapplied").inc()
        return out

    # ---- one line ----------------------------------------------------------------------------------------------------------------------------
    async def _one(self, entry: dict[str, Any], *, dry_run: bool) -> ReapplyResult:
        ws, rid, kind, seq = uuid.UUID(entry["workspace_id"]), uuid.UUID(entry["request_id"]), str(entry["kind"]), int(entry["seq"])
        ctx = WorkspaceContext.for_system_task(ws, reason="deletions reapply")
        ids: dict[str, list[uuid.UUID]] = {k: [uuid.UUID(x) for x in entry["ids"].get(k, [])] for k, _t, _s in IDENTITY}
        base = ReapplyResult(seq, rid, ws, kind, "attention")
        async with tenant_session(self._engine, ctx) as conn:
            wrow = (await conn.execute(text("SELECT status FROM workspaces WHERE id = :ws"), {"ws": ws})).first()
            if wrow is None:
                base.outcome = "workspace_absent"
                return base
            existing = (
                await conn.execute(
                    text("SELECT status FROM deletion_requests WHERE workspace_id = :ws AND id = :id"), {"ws": ws, "id": rid}
                )
            ).first()
            if kind == "workspace":
                present = {"workspace": 0 if wrow.status == "deleted" else 1}
            else:
                present = {key: await self._count(conn, ws, table, ids[key]) for key, table, _s in IDENTITY}
            base.present = {k: v for k, v in present.items() if v}
            if not base.present:
                if not dry_run:
                    await self._record_marks(conn, ws, rid, entry, ids)
                base.outcome = "already_applied" if existing is None or existing.status in TERMINAL else "attention"
                base.status = existing.status if existing else None
                if base.outcome == "attention":
                    base.detail = "nothing left to delete, but the request row is still open: resume it with `kg deletions retry`"
                return base
            if dry_run:
                base.outcome = "would_apply"
                return base
            if existing is not None and existing.status in TERMINAL:
                base.detail = f"the restored database already holds this request as {existing.status}, yet rows named by it are present"
                return base  # attention: a restore from a database that disagrees with itself needs a person
            if existing is None:
                await self._create(conn, ctx, rid, kind, entry, ids)
        status = await self._drive(ctx, rid)
        async with tenant_session(self._engine, ctx) as conn:
            await self._record_marks(conn, ws, rid, entry, ids)
            row = (
                await conn.execute(
                    text("SELECT status, receipt FROM deletion_requests WHERE workspace_id = :ws AND id = :id"), {"ws": ws, "id": rid}
                )
            ).first()
            residuals = 0
            if row is not None and row.receipt:
                v = row.receipt.get("verification", {})
                residuals = int(v.get("id_residuals", 0)) + int(v.get("text_residuals", 0)) + int(v.get("graph_residuals", 0))
            await SystemAudit(conn, ws).record(
                "deletions.reapplied",
                resource_type="deletion_request",
                resource_id=rid,
                metadata=AuditMetadata(
                    request_id=rid, deletion_status=status, count_reapplied=sum(base.present.values()), count_residuals=residuals
                ),
            )
        base.status, base.residuals = status, residuals
        base.outcome = "applied" if status in ("completed", "completed_with_exceptions") and residuals == 0 else "attention"
        if base.outcome == "attention":
            base.detail = f"request ended {status} with {residuals} residuals"
        log.info("deletion_reapplied", seq=seq, outcome=base.outcome, kind=kind, status=status)
        return base

    @staticmethod
    async def _count(conn: AsyncConnection, ws: uuid.UUID, table: str, ids: list[uuid.UUID]) -> int:
        if not ids:
            return 0
        sql = f"SELECT count(*) FROM {table} WHERE workspace_id = :ws AND id = ANY(:ids)"  # noqa: S608 (the table comes from IDENTITY, a constant)
        return int((await conn.execute(text(sql), {"ws": ws, "ids": ids})).scalar_one())

    async def _create(
        self,
        conn: AsyncConnection,
        ctx: WorkspaceContext,
        rid: uuid.UUID,
        kind: str,
        entry: dict[str, Any],
        ids: dict[str, list[uuid.UUID]],
    ) -> None:
        ws = ctx.workspace_id
        target = None
        if kind != "workspace":
            for key in ("entities", "interactions", "memories", "facts"):
                if ids.get(key):
                    target = ids[key][0]
                    break
        await conn.execute(
            text(
                "INSERT INTO deletion_requests (id, workspace_id, kind, target_id, reason_code, status, received_at, frozen_at) "
                "VALUES (:id, :ws, :kind, :t, 'journal_reapply', 'frozen', now(), now())"
            ),
            {"id": rid, "ws": ws, "kind": kind, "t": target},
        )
        manifest = {"names": [], "external_ids": [], "retained_facts": 0, "retained_memories": 0}  # the journal carries no names, by design
        kid, nonce, ct = self._svc._cipher.seal(json.dumps(manifest).encode(), workspace_id=ws, request_id=rid)
        await conn.execute(
            text("INSERT INTO deletion_manifests (request_id, workspace_id, kid, nonce, ciphertext) VALUES (:r, :ws, :kid, :n, :ct)"),
            {"r": rid, "ws": ws, "kid": kid, "n": nonce, "ct": ct},
        )
        if kind == "workspace":
            await conn.execute(text("UPDATE workspaces SET status = 'suspended' WHERE id = :ws"), {"ws": ws})
            return
        for item_kind, key in (
            ("entities", "entities"),
            ("facts", "facts"),
            ("curated_memories", "memories"),
            ("interactions", "interactions"),
        ):
            await planner.add_items(conn, ws, rid, item_kind, ids[key])
        closure = planner.Closure(ids["entities"], ids["facts"], ids["memories"], ids["interactions"])
        await planner.freeze(conn, ws, closure)
        await planner.cancel_pending_jobs(conn, ws, closure)

    async def _drive(self, ctx: WorkspaceContext, rid: uuid.UUID) -> str:
        ws = ctx.workspace_id
        for _ in range(8):
            async with tenant_session(self._engine, ctx) as conn:
                row = (
                    await conn.execute(
                        text("SELECT status FROM deletion_requests WHERE workspace_id = :ws AND id = :id"), {"ws": ws, "id": rid}
                    )
                ).one()
            status = str(row.status)
            if status in TERMINAL:
                return status
            if status in ("frozen", "failed"):
                await self._svc.run_plan(self._engine, ctx, rid)
            elif status in ("planned", "purging"):
                await self._svc.run_purge(self._engine, ctx, rid)
            elif status == "verifying":
                await self._svc.run_verify(self._engine, ctx, rid)
            else:  # received, blocked_legal_hold, ...: a state the reapply does not own
                return status
        return status

    @staticmethod
    async def _record_marks(
        conn: AsyncConnection, ws: uuid.UUID, rid: uuid.UUID, entry: dict[str, Any], ids: dict[str, list[uuid.UUID]]
    ) -> None:
        """The line's tombstones (so a late arrival of the same ids is refused) and suppression HMACs, whether or not the rows were still there. Both are deletion bookkeeping, not data."""
        has_request = (
            await conn.execute(text("SELECT 1 FROM deletion_requests WHERE workspace_id = :ws AND id = :id"), {"ws": ws, "id": rid})
        ).first()
        if has_request is None:
            return  # the line was applied before this database knew the request; there is no row for the marks to reference
        for key, _table, tomb in IDENTITY:
            for item in ids.get(key, []):
                await conn.execute(
                    text(
                        "INSERT INTO deletion_tombstones (workspace_id, kind, id, request_id) VALUES (:ws, :k, :id, :r) ON CONFLICT DO NOTHING"
                    ),
                    {"ws": ws, "k": tomb, "id": item, "r": rid},
                )
        for hmac_value in entry.get("suppression_hmacs", []):
            await conn.execute(
                text(
                    "INSERT INTO deletion_suppressions (workspace_id, kind, value_hmac, request_id) VALUES (:ws, 'external_id', :h, :r) "
                    "ON CONFLICT DO NOTHING"
                ),
                {"ws": ws, "h": hmac_value, "r": rid},
            )
