"""The deletion service (phase-8 plan §5.4, Flow E, ADR-044): intake and freeze in one transaction, then a resumable job chain.

    received ─► frozen ─► planned ─► purging ─► verifying ─► completed | completed_with_exceptions
       │           └─► blocked_legal_hold (frozen stays; nothing is purged)                    └─► failed (retryable by an operator)
       └─► cancelled (a workspace deletion during its cooling-off only)

`request` is the synchronous half: authorise (a hidden or missing target is one 404), check legal holds, compute the closure, FREEZE it (status changes in this transaction, so
every read already hides it), list the identity rows as plan items, write the encrypted manifest, record the suppression of the subject's identifiers, cancel the jobs that
have not started, and answer 202. `run_plan`, `run_purge` and `run_verify` are the job steps; each can be run again after a crash.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from kg_common.errors import Conflict, Forbidden, IdempotencyMismatch, NotFound
from kg_common.ids import episode_uuid, uuid7
from kg_db.session import tenant_session
from kg_domain.principal import Principal, WorkspaceContext
from kg_obs import metrics
from kg_obs.logging import get_logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from kg_governance.audit import AuditMetadata, AuditSink, CommandType, SystemAudit
from kg_governance.authz.policy import Action, is_allowed
from kg_governance.deletion import faults
from kg_governance.deletion import plan as planner
from kg_governance.deletion.holds import Suppressions, blocking_holds
from kg_governance.deletion.journal import DeletionJournal
from kg_governance.deletion.models import DeletionOut, DeletionRequestIn
from kg_governance.deletion.plan import Closure, Sets
from kg_governance.deletion.ports import GraphPurgePort, JobEnqueuer, ManifestCipher
from kg_governance.deletion.purge import Purger, tombstone_sql
from kg_governance.deletion.verify import Residual, usable_terms, verify
from kg_governance.grants import SubjectFilter, entity_visible_sql

log = get_logger("kg.deletion")
STAGE_PLAN, STAGE_PURGE, STAGE_VERIFY = "deletion_plan", "deletion_purge", "deletion_verify"
TERMINAL = ("completed", "completed_with_exceptions", "cancelled")
_COLUMNS = (
    "id, kind, status, target_id, requested_by, reason_code, include_submissions, received_at, frozen_at, planned_at, purge_started_at, verified_at, completed_at, "
    "cancelled_at, cooling_off_until, error_code, receipt, revision"
)


class DeletionDisabled(Conflict):
    code, http_status, default_message = "feature_disabled", 503, "Deletion intake is switched off"


class InvalidRequestState(Conflict):
    code, default_message = "invalid_state", "This deletion request is not in a state that allows that"


@dataclass
class Intake:
    out: DeletionOut
    replayed: bool = False


def _sha(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


async def _lock(conn: AsyncConnection, ws: uuid.UUID, key: str) -> None:
    """The same advisory lock every ledger command takes for its idempotency key (kg_knowledge.idempotency.lock_key), so one key is one writer."""
    await conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"{ws}:cmd:{key}"})


def _out(r: Any, *, counts: dict[str, int] | None = None, open_exceptions: int = 0) -> DeletionOut:
    return DeletionOut(
        request_id=r.id,
        kind=r.kind,
        status=r.status,
        target_id=r.target_id,
        requested_by=r.requested_by,
        reason_code=r.reason_code,
        include_submissions=r.include_submissions,
        received_at=r.received_at,
        frozen_at=r.frozen_at,
        planned_at=r.planned_at,
        purge_started_at=r.purge_started_at,
        verified_at=r.verified_at,
        completed_at=r.completed_at,
        cancelled_at=r.cancelled_at,
        cooling_off_until=r.cooling_off_until,
        error_code=r.error_code,
        item_counts=counts or {},
        open_exceptions=open_exceptions,
        receipt=r.receipt,
        revision=r.revision,
        status_url=f"/v1/deletion-requests/{r.id}",
    )


class DeletionService:
    def __init__(
        self,
        *,
        cipher: ManifestCipher,
        enqueue: JobEnqueuer,
        suppressions: Suppressions,
        journal: DeletionJournal,
        graph: GraphPurgePort | None = None,
        backup_retention_days: int = 30,
        cooling_hours: float = 24.0,
        enabled: bool = True,
    ):
        self._cipher, self._enqueue, self._supp, self._journal, self._graph = cipher, enqueue, suppressions, journal, graph
        self._backup_days, self._cooling_hours, self._enabled = backup_retention_days, cooling_hours, enabled

    @property
    def suppressions(self) -> Suppressions:
        return self._supp

    # ---- intake ---------------------------------------------------------------------------------------------------------------------

    @staticmethod
    def _authorise_kind(principal: Principal, kind: str, trusted: bool) -> None:
        if is_allowed(principal, Action.DELETION_REQUEST):
            return
        if kind in ("subject", "interaction") and is_allowed(principal, Action.DELETION_REQUEST_SUBJECT) and trusted:
            return  # a trusted-source app attests an end subject's request (it may only target what it can see / submitted)
        if kind == "memory" and is_allowed(principal, Action.MEMORY_CURATE):
            return  # a reviewer may request the deletion of a memory
        raise Forbidden("Action not permitted", details={"action": Action.DELETION_REQUEST.value})

    async def request(
        self,
        conn: AsyncConnection,
        ctx: WorkspaceContext,
        principal: Principal,
        body: DeletionRequestIn,
        *,
        key: str,
        audit: AuditSink,
        subjects: SubjectFilter,
        memory_visible: Callable[[AsyncConnection, uuid.UUID], Awaitable[bool]] | None = None,
    ) -> Intake:
        if not self._enabled:
            raise DeletionDisabled()
        ws = ctx.workspace_id
        digest = _sha("deletion.request", str(principal.principal_id), body.model_dump(mode="json"))
        await _lock(conn, ws, key)
        prior = (
            await conn.execute(
                text("SELECT request_hash, command_type, result FROM commands WHERE workspace_id = :ws AND idempotency_key = :k"),
                {"ws": ws, "k": key},
            )
        ).first()
        if prior is not None:
            if prior.request_hash != digest or prior.command_type != CommandType.DELETION_REQUEST.value:
                raise IdempotencyMismatch()
            return Intake(await self.get(conn, ws, uuid.UUID(prior.result["request_id"])), replayed=True)
        trusted = bool(
            (
                await conn.execute(
                    text("SELECT trusted_source FROM principals WHERE workspace_id = :ws AND id = :p"),
                    {"ws": ws, "p": principal.principal_id},
                )
            ).scalar_one_or_none()
        )
        self._authorise_kind(principal, body.kind, trusted)
        started = dt.datetime.now(dt.UTC)
        request_id = uuid7()
        slug = (await conn.execute(text("SELECT slug FROM workspaces WHERE id = :ws"), {"ws": ws})).scalar_one()
        names: list[str] = []
        ext: list[dict[str, str]] = []
        closure = Closure()
        if body.kind == "workspace":
            if body.confirm_slug != slug:
                raise Forbidden("The workspace slug does not match", details={"field": "confirm_slug"})
        else:
            assert body.target_id is not None
            await self._check_target(conn, ctx, principal, body, subjects, trusted, memory_visible)
            closure = await planner.compute(conn, ws, body.kind, body.target_id, include_submissions=body.include_submissions)
            if closure.is_empty():
                raise NotFound()
            names, ext = await self._identities(conn, ws, closure.entities)
        holds = await blocking_holds(conn, ws, entities=closure.entities, interactions=closure.interactions)
        status = "blocked_legal_hold" if holds else "frozen"
        cooling = None
        if body.kind == "workspace":
            cooling = dt.datetime.now(dt.UTC) + dt.timedelta(hours=self._cooling_hours)
        on_behalf = self._supp.digest(ws, "on_behalf_of", "end_subject", body.on_behalf_of) if body.on_behalf_of else None
        await conn.execute(
            text(
                "INSERT INTO deletion_requests (id, workspace_id, kind, target_id, requested_by, on_behalf_of_hmac, reason_code, legal_basis, include_submissions, status, "
                "received_at, frozen_at, cooling_off_until) VALUES (:id, :ws, :kind, :t, :by, :ob, :rc, :lb, :inc, :st, now(), now(), :cool)"
            ),
            {
                "id": request_id,
                "ws": ws,
                "kind": body.kind,
                "t": body.target_id,
                "by": principal.principal_id,
                "ob": on_behalf,
                "rc": body.reason_code,
                "lb": body.legal_basis,
                "inc": body.include_submissions,
                "st": status,
                "cool": cooling,
            },
        )
        # the ledger row first: an idempotent replay finds the request through it
        await conn.execute(
            text(
                "INSERT INTO commands (id, workspace_id, idempotency_key, request_hash, command_type, actor_principal_id, authority_class, classification, source_system, source_ref, "
                "ontology_version_id, payload, status, decision, result, http_status, projection_status) VALUES (:id, :ws, :k, :h, 'deletion.request', :actor, :auth, 'internal', "
                "'deletion', :ref, NULL, CAST(:payload AS jsonb), 'applied', '[]'::jsonb, CAST(:result AS jsonb), 202, 'not_applicable')"
            ),
            {
                "id": uuid7(),
                "ws": ws,
                "k": key,
                "h": digest,
                "actor": principal.principal_id,
                "auth": "admin_asserted" if is_allowed(principal, Action.DELETION_REQUEST) else "user_asserted",
                "ref": str(request_id),
                "payload": json.dumps(
                    {"kind": body.kind, "target_id": str(body.target_id) if body.target_id else None, "reason_code": body.reason_code}
                ),
                "result": json.dumps({"request_id": str(request_id)}),
            },
        )
        # the manifest (names the planner and the verifier need), encrypted; deleted at completion
        manifest = {
            "names": names,
            "external_ids": ext,
            "retained_facts": len(closure.retained_facts),
            "retained_memories": len(closure.retained_memories),
        }
        kid, nonce, ct = self._cipher.seal(json.dumps(manifest).encode(), workspace_id=ws, request_id=request_id)
        await conn.execute(
            text("INSERT INTO deletion_manifests (request_id, workspace_id, kid, nonce, ciphertext) VALUES (:r, :ws, :kid, :n, :ct)"),
            {"r": request_id, "ws": ws, "kid": kid, "n": nonce, "ct": ct},
        )
        if body.kind == "workspace":
            await conn.execute(
                text("UPDATE workspaces SET status = 'suspended' WHERE id = :ws"), {"ws": ws}
            )  # its authentication stops now
        else:
            for item_kind, ids in (
                ("entities", closure.entities),
                ("facts", closure.facts),
                ("curated_memories", closure.memories),
                ("interactions", closure.interactions),
            ):
                await planner.add_items(conn, ws, request_id, item_kind, ids)
            await planner.freeze(conn, ws, closure)
            await planner.cancel_pending_jobs(conn, ws, closure)
            await self._supp.record(conn, ws, request_id, [(t, s, v) for t, s, v in self._external_tuples(ext)])
        await audit.record(
            "deletion.requested",
            resource_type="deletion_request",
            resource_id=request_id,
            metadata=AuditMetadata(
                request_id=request_id,
                deletion_kind=body.kind,
                deletion_status=status,
                reason_code=body.reason_code,
                count_entities=len(closure.entities),
                count_facts=len(closure.facts),
                count_memories=len(closure.memories),
                count=len(closure.interactions),
            ),
        )
        if holds:
            await self._hold_exception(conn, ws, request_id, holds)
        else:
            await self._enqueue(
                conn,
                ws,
                stage=STAGE_PLAN,
                request_id=request_id,
                delay_seconds=self._cooling_hours * 3600 if body.kind == "workspace" else 0.0,
            )
        metrics.deletion_freeze_seconds.observe((dt.datetime.now(dt.UTC) - started).total_seconds())
        metrics.deletion_requests_total.labels(kind=body.kind, outcome=status).inc()
        return Intake(await self.get(conn, ws, request_id))

    async def _check_target(
        self,
        conn: AsyncConnection,
        ctx: WorkspaceContext,
        principal: Principal,
        body: DeletionRequestIn,
        subjects: SubjectFilter,
        trusted: bool,
        memory_visible: Callable[[AsyncConnection, uuid.UUID], Awaitable[bool]] | None,
    ) -> None:
        """A target the caller cannot see answers like one that does not exist."""
        ws, target = ctx.workspace_id, body.target_id
        assert target is not None
        if body.kind == "subject":
            acl, params = entity_visible_sql(subjects, alias="e")
            row = (
                await conn.execute(
                    text(
                        f"SELECT 1 FROM entities e WHERE e.workspace_id = :ws AND e.id = :id AND e.status NOT IN ('deleted','deletion_pending') AND {acl}"  # noqa: S608
                    ),  # noqa: S608
                    {"ws": ws, "id": target, **params},
                )
            ).first()
            if row is None:
                raise NotFound()
        elif body.kind == "interaction":
            row = (
                await conn.execute(
                    text(
                        "SELECT submitted_by FROM interactions WHERE workspace_id = :ws AND id = :id AND processing_status <> 'deletion_pending'"
                    ),
                    {"ws": ws, "id": target},
                )
            ).first()
            if row is None or (not is_allowed(principal, Action.DELETION_REQUEST) and row.submitted_by != principal.principal_id):
                raise NotFound()  # an app may delete what it submitted
        elif body.kind == "memory":
            if memory_visible is None or not await memory_visible(conn, target):
                raise NotFound()
        elif body.kind == "principal_data":
            row = (
                await conn.execute(text("SELECT 1 FROM principals WHERE workspace_id = :ws AND id = :id"), {"ws": ws, "id": target})
            ).first()
            if row is None:
                raise NotFound()
        _ = trusted

    @staticmethod
    def _external_tuples(ext: list[dict[str, str]]) -> list[tuple[str, str, str]]:
        return [(x["type"], x["system"], x["value"]) for x in ext]

    async def _identities(self, conn: AsyncConnection, ws: uuid.UUID, entities: list[uuid.UUID]) -> tuple[list[str], list[dict[str, str]]]:
        """The names, aliases and strong identifiers of the entities being deleted: what the verifier searches for. They live only in the manifest."""
        if not entities:
            return [], []
        rows = (
            await conn.execute(
                text("SELECT display_name, aliases FROM entities WHERE workspace_id = :ws AND id = ANY(:ids)"), {"ws": ws, "ids": entities}
            )
        ).all()
        names = sorted({n for r in rows for n in [r.display_name, *list(r.aliases or [])] if n})
        # a name a surviving entity also carries (an extracted twin of someone who stays, say) is not evidence about the deleted subject: scanning for it would flag the survivor
        kept = (
            await conn.execute(
                text(
                    "SELECT display_name, aliases FROM entities WHERE workspace_id = :ws AND id <> ALL(:ids) AND status NOT IN ('deleted','deletion_pending','merged')"
                ),
                {"ws": ws, "ids": entities},
            )
        ).all()
        shared = {n.casefold() for r in kept for n in [r.display_name, *list(r.aliases or [])] if n}
        names = [n for n in names if n.casefold() not in shared]
        ext = [
            {"type": r.type_key, "system": r.system, "value": r.value}
            for r in (
                await conn.execute(
                    text(
                        "SELECT type_key, system, value FROM entity_external_ids WHERE workspace_id = :ws AND entity_id = ANY(:ids) ORDER BY type_key, system, value"
                    ),
                    {"ws": ws, "ids": entities},
                )
            ).all()
        ]
        return names, ext

    async def _hold_exception(self, conn: AsyncConnection, ws: uuid.UUID, request_id: uuid.UUID, holds: list[uuid.UUID]) -> None:
        await conn.execute(
            text(
                "INSERT INTO review_items (id, workspace_id, item_type, rule_kind, reason_code, deletion_request_id, detail, due_at) "
                "VALUES (:id, :ws, 'deletion_exception', 'deletion', 'legal_hold', :r, CAST(:d AS jsonb), now() + interval '30 days')"
            ),
            {
                "id": uuid7(),
                "ws": ws,
                "r": request_id,
                "d": json.dumps({"reason": "legal_hold", "holds": [str(h) for h in holds], "request_id": str(request_id)}),
            },
        )
        metrics.reviews_created_total.labels(item_type="deletion_exception").inc()

    # ---- reads ----------------------------------------------------------------------------------------------------------------------

    async def get(self, conn: AsyncConnection, ws: uuid.UUID, request_id: uuid.UUID, *, requester: uuid.UUID | None = None) -> DeletionOut:
        """`requester`: restrict to this principal's own requests (an app sees only what it asked for)."""
        row = (
            await conn.execute(
                text(
                    f"SELECT {_COLUMNS} FROM deletion_requests WHERE workspace_id = :ws AND id = :id AND (CAST(:by AS uuid) IS NULL OR requested_by = :by)"  # noqa: S608
                ),  # noqa: S608
                {"ws": ws, "id": request_id, "by": requester},
            )
        ).first()
        if row is None:
            raise NotFound()
        counts = {
            f"{r.store}.{r.action}.{r.status}": int(r.n)
            for r in (
                await conn.execute(
                    text(
                        "SELECT store, action, status, count(*) AS n FROM deletion_items WHERE request_id = :r GROUP BY store, action, status ORDER BY 1, 2, 3"
                    ),
                    {"r": request_id},
                )
            ).all()
        }
        open_ex = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM review_items WHERE workspace_id = :ws AND item_type = 'deletion_exception' AND status = 'open' AND deletion_request_id = :r"
                ),
                {"ws": ws, "r": request_id},
            )
        ).scalar_one()
        return _out(row, counts=counts, open_exceptions=int(open_ex))

    async def list_requests(
        self, conn: AsyncConnection, ws: uuid.UUID, *, requester: uuid.UUID | None = None, status: str | None = None, limit: int = 50
    ) -> list[DeletionOut]:
        rows = (
            await conn.execute(
                text(
                    f"SELECT {_COLUMNS} FROM deletion_requests WHERE workspace_id = :ws AND (CAST(:by AS uuid) IS NULL OR requested_by = :by) "  # noqa: S608
                    "AND (CAST(:st AS text) IS NULL OR status = :st) ORDER BY received_at DESC, id DESC LIMIT :n"
                ),
                {"ws": ws, "by": requester, "st": status, "n": max(1, min(limit, 200))},
            )
        ).all()
        return [_out(r) for r in rows]

    # ---- state ----------------------------------------------------------------------------------------------------------------------

    async def _next(self, conn: AsyncConnection, ws: uuid.UUID, request_id: uuid.UUID, stage: str, *, delay_seconds: float = 0.0) -> None:
        """Enqueues the next job of `stage` for this request: each run of a stage is a new generation (the dedupe key carries it), so a request that goes back to
        purge or verify (an exception decided, a retry) gets a fresh job instead of being swallowed by the finished one."""
        gen = int(
            (
                await conn.execute(
                    text("SELECT count(*) FROM outbox_jobs WHERE workspace_id = :ws AND stage = :s AND subject_id = :r"),
                    {"ws": ws, "s": stage, "r": request_id},
                )
            ).scalar_one()
        )
        await self._enqueue(conn, ws, stage=stage, request_id=request_id, generation=gen, delay_seconds=delay_seconds)

    @staticmethod
    async def _move(
        conn: AsyncConnection, ws: uuid.UUID, request_id: uuid.UUID, frm: tuple[str, ...], to: str, stamp: str | None = None, **cols: Any
    ) -> bool:
        sets = ["status = :to", "revision = revision + 1"]
        if stamp:
            sets.append(f"{stamp} = now()")
        for c in cols:
            sets.append(f"{c} = :{c}")
        res = await conn.execute(
            text(
                f"UPDATE deletion_requests SET {', '.join(sets)} WHERE workspace_id = :ws AND id = :id AND status = ANY(:frm) RETURNING id"  # noqa: S608
            ),  # noqa: S608 (columns are constants)
            {"ws": ws, "id": request_id, "frm": list(frm), "to": to, **cols},
        )
        return res.first() is not None

    async def cancel(self, conn: AsyncConnection, ws: uuid.UUID, request_id: uuid.UUID, audit: AuditSink) -> DeletionOut:
        """Only a workspace deletion in its cooling-off period can be cancelled; the workspace becomes active again."""
        row = (
            await conn.execute(
                text("SELECT kind, status, cooling_off_until FROM deletion_requests WHERE workspace_id = :ws AND id = :id FOR UPDATE"),
                {"ws": ws, "id": request_id},
            )
        ).first()
        if row is None:
            raise NotFound()
        cooling = row.cooling_off_until is not None and row.cooling_off_until > dt.datetime.now(dt.UTC)
        if row.kind != "workspace" or row.status != "frozen" or not cooling:
            raise InvalidRequestState("Only a workspace deletion that is still cooling off can be cancelled")
        await self._move(conn, ws, request_id, ("frozen",), "cancelled", "cancelled_at")
        await conn.execute(text("UPDATE workspaces SET status = 'active' WHERE id = :ws"), {"ws": ws})
        jobs = (
            await conn.execute(
                text("SELECT id FROM outbox_jobs WHERE workspace_id = :ws AND stage = :s AND subject_id = :r AND status = 'pending'"),
                {"ws": ws, "s": STAGE_PLAN, "r": request_id},
            )
        ).all()
        for j in jobs:
            await conn.execute(text("SELECT * FROM ops.cancel_job(:j)"), {"j": j.id})
        await conn.execute(text("DELETE FROM deletion_manifests WHERE workspace_id = :ws AND request_id = :r"), {"ws": ws, "r": request_id})
        await audit.record(
            "deletion.cancelled",
            resource_type="deletion_request",
            resource_id=request_id,
            metadata=AuditMetadata(request_id=request_id, deletion_kind="workspace"),
        )
        metrics.deletion_requests_total.labels(kind="workspace", outcome="cancelled").inc()
        return await self.get(conn, ws, request_id)

    async def resume(self, conn: AsyncConnection, ws: uuid.UUID, request_id: uuid.UUID, audit: AuditSink) -> DeletionOut:
        """After a hold is released (or an exception decided): a blocked request is frozen again and its plan job is enqueued."""
        row = (
            await conn.execute(
                text("SELECT kind, status, target_id FROM deletion_requests WHERE workspace_id = :ws AND id = :id FOR UPDATE"),
                {"ws": ws, "id": request_id},
            )
        ).first()
        if row is None:
            raise NotFound()
        if row.status != "blocked_legal_hold":
            return await self.get(conn, ws, request_id)
        sets = await planner.sets_of(conn, request_id)
        if await blocking_holds(conn, ws, entities=sets.e, interactions=sets.i):
            return await self.get(conn, ws, request_id)  # another hold still covers it
        await self._move(conn, ws, request_id, ("blocked_legal_hold",), "frozen")
        await conn.execute(
            text(
                "UPDATE review_items SET status = 'resolved', resolution_code = 'hold_released', resolved_at = now(), revision = revision + 1 WHERE workspace_id = :ws AND deletion_request_id = :r AND reason_code = 'legal_hold' AND status = 'open'"
            ),
            {"ws": ws, "r": request_id},
        )
        await self._next(conn, ws, request_id, STAGE_PLAN)
        await audit.record(
            "deletion.resumed", resource_type="deletion_request", resource_id=request_id, metadata=AuditMetadata(request_id=request_id)
        )
        return await self.get(conn, ws, request_id)

    async def retry(self, conn: AsyncConnection, ws: uuid.UUID, request_id: uuid.UUID, audit: AuditSink) -> DeletionOut:
        """Operator retry of a failed request: back to the step it failed in (the steps are idempotent)."""
        row = (
            await conn.execute(
                text("SELECT status FROM deletion_requests WHERE workspace_id = :ws AND id = :id FOR UPDATE"), {"ws": ws, "id": request_id}
            )
        ).first()
        if row is None:
            raise NotFound()
        if row.status != "failed":
            raise InvalidRequestState()
        planned = (await conn.execute(text("SELECT planned_at FROM deletion_requests WHERE id = :id"), {"id": request_id})).scalar_one()
        if planned is None:
            await self._move(conn, ws, request_id, ("failed",), "planned" if False else "planned")  # replanned below
            stage = STAGE_PLAN
        else:
            await self._move(conn, ws, request_id, ("failed",), "purging", "purge_started_at", error_code=None)
            stage = STAGE_PURGE
        await conn.execute(text("UPDATE deletion_requests SET error_code = NULL WHERE id = :id"), {"id": request_id})
        gen = int(
            (
                await conn.execute(
                    text("SELECT count(*) FROM outbox_jobs WHERE workspace_id = :ws AND subject_id = :r AND stage = :s"),
                    {"ws": ws, "r": request_id, "s": stage},
                )
            ).scalar_one()
        )
        await self._enqueue(conn, ws, stage=stage, request_id=request_id, generation=gen)
        await audit.record(
            "deletion.retried", resource_type="deletion_request", resource_id=request_id, metadata=AuditMetadata(request_id=request_id)
        )
        return await self.get(conn, ws, request_id)

    async def fail(self, engine: AsyncEngine, ctx: WorkspaceContext, request_id: uuid.UUID, code: str) -> None:
        async with tenant_session(engine, ctx) as conn:
            await conn.execute(
                text(
                    "UPDATE deletion_requests SET status = 'failed', error_code = :c, revision = revision + 1 WHERE workspace_id = :ws AND id = :id AND status NOT IN ('completed','completed_with_exceptions','cancelled','failed')"
                ),
                {"c": code[:60], "ws": ctx.workspace_id, "id": request_id},
            )
            await SystemAudit(conn, ctx.workspace_id).record(
                "deletion.failed",
                resource_type="deletion_request",
                resource_id=request_id,
                metadata=AuditMetadata(request_id=request_id, reason_code=code[:60]),
            )
        metrics.deletion_requests_total.labels(kind="any", outcome="failed").inc()

    # ---- job steps ------------------------------------------------------------------------------------------------------------------

    async def _manifest(self, conn: AsyncConnection, ws: uuid.UUID, request_id: uuid.UUID) -> dict[str, Any]:
        row = (
            await conn.execute(
                text("SELECT kid, nonce, ciphertext FROM deletion_manifests WHERE workspace_id = :ws AND request_id = :r"),
                {"ws": ws, "r": request_id},
            )
        ).first()
        if row is None:
            return {"names": [], "external_ids": [], "retained_facts": 0, "retained_memories": 0}
        return dict(json.loads(self._cipher.open(row.kid, bytes(row.nonce), bytes(row.ciphertext), workspace_id=ws, request_id=request_id)))

    async def run_plan(self, engine: AsyncEngine, ctx: WorkspaceContext, request_id: uuid.UUID) -> None:
        """Step 2: list every derivative in every store (the Postgres rows by predicate, the graph by listing), raise the exception items for free text that names the
        subject, then `planned` and the purge job."""
        ws = ctx.workspace_id
        async with tenant_session(engine, ctx) as conn:
            req = (
                await conn.execute(
                    text("SELECT kind, status FROM deletion_requests WHERE workspace_id = :ws AND id = :id FOR UPDATE"),
                    {"ws": ws, "id": request_id},
                )
            ).first()
            if req is None or req.status in ("planned", "purging", "verifying", *TERMINAL):
                return
            if req.status == "blocked_legal_hold":
                return
            if req.kind == "workspace":
                await self._move(conn, ws, request_id, ("frozen", "failed"), "planned", "planned_at")
                await self._next(conn, ws, request_id, STAGE_PURGE)
                return
            manifest = await self._manifest(conn, ws, request_id)
            s = await planner.sets_of(conn, request_id)
            terms = usable_terms([*manifest["names"], *(x["value"] for x in manifest["external_ids"])])
            await planner.plan_dependents(conn, ws, request_id, s, terms=terms)
            episodes = [
                r.episode_uuid
                for r in (
                    await conn.execute(
                        text("SELECT episode_uuid FROM graph_episodes WHERE workspace_id = :ws AND interaction_id = ANY(:i)"),
                        {"ws": ws, "i": s.i},
                    )
                ).all()
            ]
            # an extraction that died half-way may have written its episode before it recorded one: the id is deterministic, so every generation it could have used is named too
            gens = (
                await conn.execute(
                    text(
                        "SELECT interaction_id, COALESCE(max(generation), 0) AS g FROM interaction_stages WHERE workspace_id = :ws AND interaction_id = ANY(:i) AND stage = 'graph_ingest' GROUP BY 1"
                    ),
                    {"ws": ws, "i": s.i},
                )
            ).all()
            episodes = sorted({*episodes, *(episode_uuid(r.interaction_id, g) for r in gens for g in range(int(r.g) + 2))})
            mention_nodes = [
                r.node_uuid
                for r in (
                    await conn.execute(
                        text("SELECT DISTINCT node_uuid FROM entity_mentions WHERE workspace_id = :ws AND entity_id = ANY(:e)"),
                        {"ws": ws, "e": s.e},
                    )
                ).all()
            ]
            # survivors the removal can damage (GR-08): facts that rest partly on a removed episode, entities a removed episode mentioned, and the ends of those facts
            keep_facts = [
                r.id
                for r in (
                    await conn.execute(
                        text(
                            "SELECT DISTINCT f.id FROM facts f JOIN fact_evidence e ON e.workspace_id = f.workspace_id AND e.fact_id = f.id WHERE f.workspace_id = :ws "
                            "AND e.interaction_id = ANY(:i) AND f.id <> ALL(:f) AND f.status NOT IN ('deleted','deletion_pending')"
                        ),
                        {"ws": ws, "i": s.i, "f": s.f},
                    )
                ).all()
            ]
            keep_nodes = [
                r.id
                for r in (
                    await conn.execute(
                        text(
                            "SELECT DISTINCT x.id FROM (SELECT entity_id AS id FROM entity_mentions WHERE workspace_id = :ws AND interaction_id = ANY(:i) "
                            "UNION SELECT source_entity_id FROM facts WHERE workspace_id = :ws AND id = ANY(:kf) UNION SELECT target_entity_id FROM facts WHERE workspace_id = :ws AND id = ANY(:kf)) x "
                            "JOIN entities e ON e.workspace_id = :ws AND e.id = x.id AND e.status NOT IN ('deleted','deletion_pending','merged') WHERE x.id <> ALL(:e)"
                        ),
                        {"ws": ws, "i": s.i, "kf": keep_facts, "e": s.e},
                    )
                ).all()
            ]
        listing_nodes = sorted({*(str(x) for x in s.e), *mention_nodes})
        listing_edges = [str(x) for x in s.f]
        graph_items: list[tuple[str, str]] = []
        if self._graph is not None:
            listing = await self._graph.list_for(
                ctx, episode_uuids=episodes, node_uuids=listing_nodes, edge_uuids=listing_edges, terms=terms
            )
            graph_items = (
                [("episode", u) for u in listing.episode_uuids]
                + [("node", u) for u in listing.node_uuids]
                + [("edge", u) for u in listing.edge_uuids]
            )
            graph_items += [("summary", u) for u in listing.summary_nodes]
        graph_items += [("reproject_node", str(u)) for u in keep_nodes] + [("reproject_edge", str(u)) for u in keep_facts]
        async with tenant_session(engine, ctx) as conn:
            for kind, uid in graph_items:
                await conn.execute(
                    text(
                        "INSERT INTO deletion_items (request_id, workspace_id, store, item_kind, item_id, action) VALUES (:r, :ws, 'graph', :k, :i, :a) ON CONFLICT DO NOTHING"
                    ),
                    {
                        "r": request_id,
                        "ws": ws,
                        "k": kind,
                        "i": uid,
                        "a": "redact" if kind in ("summary", "reproject_node", "reproject_edge") else "delete",
                    },
                )
            await self._text_exceptions(conn, ws, request_id, s, terms)
            audit = SystemAudit(conn, ws)
            await audit.record(
                "deletion.planned",
                resource_type="deletion_request",
                resource_id=request_id,
                metadata=AuditMetadata(request_id=request_id, count=len(graph_items)),
            )
            await self._move(conn, ws, request_id, ("frozen", "failed"), "planned", "planned_at")
            await self._next(conn, ws, request_id, STAGE_PURGE)
        faults.hit(
            "deletion_plan", "after_side_effect"
        )  # FI-14: a crash between the freeze and the plan leaves the request frozen, and the job runs again

    async def _text_exceptions(self, conn: AsyncConnection, ws: uuid.UUID, request_id: uuid.UUID, s: Sets, terms: list[str]) -> None:
        """Memory text that names the subject but is not being deleted (another subject's memory): a person decides (redact it, or delete it)."""
        if not terms:
            return
        likes = [planner._like(t) for t in terms]
        rows = (
            await conn.execute(
                text(
                    "SELECT m.id FROM curated_memories m WHERE m.workspace_id = :ws AND m.status NOT IN ('deleted','deletion_pending') AND m.id <> ALL(:m) "
                    "AND (m.content ILIKE ANY(CAST(:likes AS text[])) OR m.summary ILIKE ANY(CAST(:likes AS text[]))) ORDER BY m.id"
                ),
                {"ws": ws, "m": s.m, "likes": likes},
            )
        ).all()
        for r in rows:
            await self._exception(
                conn, ws, request_id, "shared_memory_mentions_subject", memory_id=r.id, location="curated_memories.content"
            )

    async def _exception(
        self,
        conn: AsyncConnection,
        ws: uuid.UUID,
        request_id: uuid.UUID,
        reason: str,
        *,
        memory_id: uuid.UUID | None = None,
        location: str = "",
    ) -> uuid.UUID | None:
        dup = (
            await conn.execute(
                text(
                    "SELECT 1 FROM review_items WHERE workspace_id = :ws AND item_type = 'deletion_exception' AND deletion_request_id = :r AND status = 'open' "
                    "AND reason_code = :rc AND detail ->> 'location' = :loc AND COALESCE(detail ->> 'memory_id', '') = COALESCE(:m, '')"
                ),
                {"ws": ws, "r": request_id, "rc": reason, "loc": location, "m": str(memory_id) if memory_id else None},
            )
        ).first()
        if dup is not None:
            return None
        item = uuid7()
        await conn.execute(
            text(
                "INSERT INTO review_items (id, workspace_id, item_type, rule_kind, reason_code, deletion_request_id, detail, due_at) "
                "VALUES (:id, :ws, 'deletion_exception', 'deletion', :rc, :r, CAST(:d AS jsonb), now() + interval '30 days')"  # the memory is named in `detail`, not a foreign key: the purge deletes it
            ),
            {
                "id": item,
                "ws": ws,
                "rc": reason,
                "r": request_id,
                "d": json.dumps(
                    {
                        "reason": reason,
                        "request_id": str(request_id),
                        "location": location,
                        "memory_id": str(memory_id) if memory_id else None,
                    }
                ),
            },
        )
        metrics.reviews_created_total.labels(item_type="deletion_exception").inc()
        return item

    async def run_purge(self, engine: AsyncEngine, ctx: WorkspaceContext, request_id: uuid.UUID) -> None:
        """Step 3: purge. The request must be `planned` (or `purging`/`failed`, on a retry); it ends `verifying`."""
        ws = ctx.workspace_id
        async with tenant_session(engine, ctx) as conn:
            req = (
                await conn.execute(
                    text("SELECT kind, status FROM deletion_requests WHERE workspace_id = :ws AND id = :id FOR UPDATE"),
                    {"ws": ws, "id": request_id},
                )
            ).first()
            if req is None or req.status in ("verifying", *TERMINAL):
                return
            if req.status not in ("planned", "purging", "failed"):
                raise InvalidRequestState()
            if req.status != "purging":
                await self._move(conn, ws, request_id, ("planned", "failed"), "purging", "purge_started_at")
            kind = req.kind
        if kind == "workspace":
            await self._purge_workspace(engine, ctx, request_id)
        else:
            await Purger(engine, self._graph).run(ctx, request_id)
        async with tenant_session(engine, ctx) as conn:
            await SystemAudit(conn, ws).record(
                "deletion.purged", resource_type="deletion_request", resource_id=request_id, metadata=AuditMetadata(request_id=request_id)
            )
            await self._move(conn, ws, request_id, ("purging",), "verifying")
            await self._next(conn, ws, request_id, STAGE_VERIFY)

    async def _purge_workspace(self, engine: AsyncEngine, ctx: WorkspaceContext, request_id: uuid.UUID) -> None:
        ws = ctx.workspace_id
        if self._graph is not None:
            await self._graph.clear_workspace(ctx)
        faults.hit("deletion_purge", "after_side_effect")
        async with tenant_session(engine, ctx) as conn:
            counts = (await conn.execute(text("SELECT ops.purge_workspace_rows(:r)"), {"r": request_id})).scalar_one()
            await conn.execute(
                text("SELECT ops.write_platform_receipt(:r, :ws, 'workspace', CAST(:j AS jsonb))"),
                {"r": request_id, "ws": ws, "j": json.dumps({"purged": counts, "status": "purged"})},
            )

    async def run_verify(self, engine: AsyncEngine, ctx: WorkspaceContext, request_id: uuid.UUID) -> None:
        """Steps 4 and 5: id and residual-text scans, the receipt, the journal line, the tombstones; the manifest is deleted."""
        ws = ctx.workspace_id
        async with tenant_session(engine, ctx) as conn:
            req = (
                await conn.execute(
                    text(f"SELECT {_COLUMNS} FROM deletion_requests WHERE workspace_id = :ws AND id = :id FOR UPDATE"),  # noqa: S608
                    {"ws": ws, "id": request_id},
                )
            ).first()  # noqa: S608
            if req is None or req.status in TERMINAL:
                return
            if req.status not in ("verifying", "failed", "completed_with_exceptions"):
                raise InvalidRequestState()
            if req.status != "verifying":
                await self._move(conn, ws, request_id, ("failed", "completed_with_exceptions"), "verifying")
            if req.kind == "workspace":
                await self._finish_workspace(conn, ws, request_id)
                return
            manifest = await self._manifest(conn, ws, request_id)
            s = await planner.sets_of(conn, request_id)
            terms = usable_terms([*manifest["names"], *(x["value"] for x in manifest["external_ids"])])
        async with tenant_session(engine, ctx) as conn:
            result = await verify(conn, ctx, s, terms, self._graph)
        async with tenant_session(engine, ctx) as conn:
            now = dt.datetime.now(dt.UTC)
            for res in (*result.id_residuals, *result.text_residuals, *result.graph_residuals):
                await self._residual_exception(conn, ws, request_id, res)
            await conn.execute(text(tombstone_sql()), {"r": request_id})
            hmacs = [
                r.value_hmac
                for r in (
                    await conn.execute(
                        text("SELECT value_hmac FROM deletion_suppressions WHERE workspace_id = :ws AND request_id = :r"),
                        {"ws": ws, "r": request_id},
                    )
                ).all()
            ]
            open_ex = int(
                (
                    await conn.execute(
                        text(
                            "SELECT count(*) FROM review_items WHERE workspace_id = :ws AND item_type = 'deletion_exception' AND status = 'open' AND deletion_request_id = :r"
                        ),
                        {"ws": ws, "r": request_id},
                    )
                ).scalar_one()
            )
            clean = result.clean and open_ex == 0
            receipt = await self._receipt(conn, ws, req, manifest, result.as_dict(), now, clean)
            ids = {
                "entities": [str(x) for x in s.e],
                "facts": [str(x) for x in s.f],
                "interactions": [str(x) for x in s.i],
                "memories": [str(x) for x in s.m],
            }
            ref = self._journal.append(
                request_id=request_id, workspace_id=ws, kind=req.kind, completed_at=now, ids=ids, suppression_hmacs=hmacs
            )
            receipt["journal_ref"] = ref
            final = "completed" if clean else "completed_with_exceptions"
            await conn.execute(
                text(
                    "UPDATE deletion_requests SET status = :st, completed_at = now(), verified_at = now(), receipt = CAST(:rc AS jsonb), journal_ref = :ref, revision = revision + 1 "
                    "WHERE workspace_id = :ws AND id = :id"
                ),
                {"st": final, "rc": json.dumps(receipt, default=str), "ref": ref, "ws": ws, "id": request_id},
            )
            if clean:
                await conn.execute(
                    text("DELETE FROM deletion_manifests WHERE workspace_id = :ws AND request_id = :r"), {"ws": ws, "r": request_id}
                )
            audit = SystemAudit(conn, ws)
            deleted = sum(v.get("deleted", 0) for v in receipt["stores"].values())
            await audit.record(
                "deletion.completed",
                resource_type="deletion_request",
                resource_id=request_id,
                metadata=AuditMetadata(
                    request_id=request_id,
                    deletion_kind=req.kind,
                    deletion_status=final,
                    count_deleted=deleted,
                    count_residuals=sum(result.as_dict()[k] for k in ("id_residuals", "text_residuals", "graph_residuals")),
                ),
            )
            metrics.deletion_requests_total.labels(kind=req.kind, outcome=final).inc()
            metrics.deletion_completion_seconds.observe(max(0.0, (now - req.received_at).total_seconds()))

    async def _residual_exception(self, conn: AsyncConnection, ws: uuid.UUID, request_id: uuid.UUID, res: Residual) -> None:
        if res.store == "postgres" and res.location == "curated_memories.content":
            for mid in res.row_ids:  # a memory that names the subject: the same item the plan raised (one per memory)
                await self._exception(
                    conn, ws, request_id, "shared_memory_mentions_subject", memory_id=uuid.UUID(mid), location=res.location
                )
            return
        reason = (
            "residual_text"
            if res.location.split(".")[-1] not in ("id", "")
            and res.location.count(".")
            and res.location != "entities.id"
            and res.store != "postgres"
            or res.row_ids
            else "verification_failed"
        )
        await self._exception(conn, ws, request_id, reason, location=f"{res.store}:{res.location}")

    async def _receipt(
        self,
        conn: AsyncConnection,
        ws: uuid.UUID,
        req: Any,
        manifest: dict[str, Any],
        verification: dict[str, object],
        now: dt.datetime,
        clean: bool,
    ) -> dict[str, Any]:
        rows = (
            await conn.execute(
                text(
                    "SELECT store, item_kind, action, status, count(*) AS n FROM deletion_items WHERE request_id = :r GROUP BY store, item_kind, action, status"
                ),
                {"r": req.id},
            )
        ).all()
        stores: dict[str, dict[str, int]] = {}
        by_table: dict[str, dict[str, int]] = {}
        graph: dict[str, int] = {}
        for r in rows:
            if r.status != "done":
                continue
            bucket = stores.setdefault(r.store, {"deleted": 0, "redacted": 0, "retained": 0})
            bucket["deleted" if r.action == "delete" else "redacted"] += int(r.n)
            if r.store == "graph":
                graph[r.item_kind] = graph.get(r.item_kind, 0) + int(r.n)
            else:
                by_table.setdefault(r.item_kind, {})[r.action] = by_table.get(r.item_kind, {}).get(r.action, 0) + int(r.n)
        redacted_commands = by_table.get("commands", {}).get("redact", 0)
        retained = [
            {
                "class": "audit_log",
                "reason": "ids_and_codes_only",
                "count": int(
                    (await conn.execute(text("SELECT count(*) FROM audit_log WHERE workspace_id = :ws"), {"ws": ws})).scalar_one()
                ),
            },
            {"class": "commands", "reason": "ledger_continuity_payload_redacted", "count": redacted_commands},
            {"class": "facts", "reason": "independent_evidence", "count": int(manifest.get("retained_facts", 0))},
            {"class": "memories", "reason": "independent_evidence", "count": int(manifest.get("retained_memories", 0))},
            {
                "class": "tombstones",
                "reason": "ids_only_for_404_parity_and_restore",
                "count": sum(
                    v
                    for v in (
                        by_table.get("entities", {}).get("delete", 0),
                        by_table.get("facts", {}).get("delete", 0),
                        by_table.get("interactions", {}).get("delete", 0),
                        by_table.get("curated_memories", {}).get("delete", 0),
                    )
                ),
            },
        ]
        exceptions = [
            {"reason": r.reason_code, "status": r.status, "location": (r.detail or {}).get("location")}
            for r in (
                await conn.execute(
                    text(
                        "SELECT reason_code, status, detail FROM review_items WHERE workspace_id = :ws AND item_type = 'deletion_exception' AND deletion_request_id = :r ORDER BY created_at, id"
                    ),
                    {"ws": ws, "r": req.id},
                )
            ).all()
        ]
        days = await self._backup_days_for(conn, ws)
        holds = [{"hold_id": str(h)} for h in await blocking_holds(conn, ws, entities=[], interactions=[])]
        return {
            "request_id": str(req.id),
            "kind": req.kind,
            "status": "completed" if clean else "completed_with_exceptions",
            "completed_at": now.isoformat(),
            "stores": stores,
            "by_table": by_table,
            "graph": graph,
            "retained": retained,
            "verification": verification,
            "legal_hold": holds,
            "exceptions": exceptions,
            "backup_copies_expire_by": (now + dt.timedelta(days=days)).date().isoformat(),
        }

    async def _backup_days_for(self, conn: AsyncConnection, ws: uuid.UUID) -> int:
        v = (
            await conn.execute(
                text(
                    "SELECT (settings ->> 'backup_retention_days')::int AS d FROM workspace_settings WHERE workspace_id = :ws ORDER BY version DESC LIMIT 1"
                ),
                {"ws": ws},
            )
        ).scalar_one_or_none()
        return int(v) if v else self._backup_days

    async def _finish_workspace(self, conn: AsyncConnection, ws: uuid.UUID, request_id: uuid.UUID) -> None:
        now = dt.datetime.now(dt.UTC)
        receipt = {
            "request_id": str(request_id),
            "kind": "workspace",
            "status": "completed",
            "completed_at": now.isoformat(),
            "stores": {"postgres": {"deleted": 1, "redacted": 0, "retained": 0}},
            "retained": [
                {"class": "audit_log", "reason": "ids_and_codes_only", "count": 0},
                {"class": "workspaces", "reason": "tombstone_row", "count": 1},
            ],
            "verification": {"id_residuals": 0, "text_residuals": 0, "graph_residuals": 0, "locations": []},
            "backup_copies_expire_by": (now + dt.timedelta(days=self._backup_days)).date().isoformat(),
        }
        ref = self._journal.append(
            request_id=request_id, workspace_id=ws, kind="workspace", completed_at=now, ids={"workspace": [str(ws)]}, suppression_hmacs=[]
        )
        receipt["journal_ref"] = ref
        await conn.execute(
            text("SELECT ops.write_platform_receipt(:r, :ws, 'workspace', CAST(:j AS jsonb))"),
            {"r": request_id, "ws": ws, "j": json.dumps(receipt)},
        )
        await conn.execute(
            text(
                "UPDATE deletion_requests SET status = 'completed', completed_at = now(), verified_at = now(), receipt = CAST(:rc AS jsonb), journal_ref = :ref, revision = revision + 1 WHERE workspace_id = :ws AND id = :id"
            ),
            {"rc": json.dumps(receipt), "ref": ref, "ws": ws, "id": request_id},
        )
        await conn.execute(text("DELETE FROM deletion_manifests WHERE workspace_id = :ws AND request_id = :r"), {"ws": ws, "r": request_id})
        metrics.deletion_requests_total.labels(kind="workspace", outcome="completed").inc()

    # ---- retention (phase-8 plan §5.6): expiry is a deletion like any other, so it is audited, gated and has a receipt --------------------------------------

    _RETENTION: dict[str, list[tuple[str, str]]] = {
        "raw_content": [("interaction_contents", "t.form = 'raw' AND t.created_at < now() - make_interval(days => :d)")],
        "normalized_content": [("interaction_contents", "t.form = 'normalized' AND t.created_at < now() - make_interval(days => :d)")],
        "context_trace": [
            (
                "context_items",
                "t.context_id IN (SELECT x.id FROM context_traces x WHERE x.workspace_id = :ws AND x.created_at < now() - make_interval(days => :d))",
            ),
            (
                "context_debug_snapshots",
                "t.context_id IN (SELECT x.id FROM context_traces x WHERE x.workspace_id = :ws AND x.created_at < now() - make_interval(days => :d))",
            ),
            ("context_traces", "t.created_at < now() - make_interval(days => :d)"),
        ],
    }

    async def run_retention(self, engine: AsyncEngine, ctx: WorkspaceContext, *, retention_class: str, days: int) -> int:
        """Removes what a retention window has expired, as a system-generated deletion request of kind `retention`: items, the purge gate, a receipt. Returns how many rows went."""
        ws = ctx.workspace_id
        if retention_class not in self._RETENTION:
            raise ValueError(f"no retention for {retention_class}")
        request_id = uuid7()
        async with tenant_session(engine, ctx) as conn:
            await conn.execute(
                text(
                    "INSERT INTO deletion_requests (id, workspace_id, kind, reason_code, retention_class, status, frozen_at) "
                    "VALUES (:id, :ws, 'retention', 'retention_policy', :c, 'frozen', now())"
                ),
                {"id": request_id, "ws": ws, "c": retention_class},
            )
            listed = 0
            for kind, pred in self._RETENTION[retention_class]:
                store, table, key = planner.KEYS[kind]
                res = await conn.execute(
                    text(
                        f"INSERT INTO deletion_items (request_id, workspace_id, store, item_kind, item_id, action) SELECT :r, :ws, '{store}', '{kind}', {key}, 'delete' "  # noqa: S608
                        f"FROM {table} t WHERE t.workspace_id = :ws AND {pred} ON CONFLICT DO NOTHING"
                    ),
                    {"r": request_id, "ws": ws, "d": days},
                )
                listed += int(res.rowcount)
            if listed == 0:
                await self._move(
                    conn, ws, request_id, ("frozen",), "cancelled", "cancelled_at"
                )  # nothing had expired by the time the request was made
                return 0
            await self._move(conn, ws, request_id, ("frozen",), "planned", "planned_at")
            await self._move(conn, ws, request_id, ("planned",), "purging", "purge_started_at")
        await Purger(engine, None).run(ctx, request_id)
        async with tenant_session(engine, ctx) as conn:
            await self._move(conn, ws, request_id, ("purging",), "verifying")
            req = (await conn.execute(text(f"SELECT {_COLUMNS} FROM deletion_requests WHERE id = :id"), {"id": request_id})).first()  # noqa: S608
            now = dt.datetime.now(dt.UTC)
            receipt = await self._receipt(
                conn, ws, req, {}, {"id_residuals": 0, "text_residuals": 0, "graph_residuals": 0, "locations": []}, now, True
            )
            receipt["retention_class"] = retention_class
            await conn.execute(
                text(
                    "UPDATE deletion_requests SET status = 'completed', completed_at = now(), verified_at = now(), receipt = CAST(:rc AS jsonb), revision = revision + 1 WHERE id = :id"
                ),
                {"rc": json.dumps(receipt, default=str), "id": request_id},
            )
            await SystemAudit(conn, ws).record(
                "deletion.completed",
                resource_type="deletion_request",
                resource_id=request_id,
                metadata=AuditMetadata(
                    request_id=request_id,
                    deletion_kind="retention",
                    deletion_status="completed",
                    count_deleted=listed,
                    reason_code="retention_policy",
                ),
            )
        metrics.retention_deleted_total.labels(retention_class=retention_class).inc(listed)
        metrics.deletion_requests_total.labels(kind="retention", outcome="completed").inc()
        return listed

    # ---- exception decisions (called by the review handler) ---------------------------------------------------------------------------

    async def add_to_plan(
        self, conn: AsyncConnection, ws: uuid.UUID, request_id: uuid.UUID, *, memories: list[uuid.UUID], audit: AuditSink
    ) -> None:
        """A reviewer decided a memory that names the subject must go: it is frozen and listed with its dependents, and the request returns to `planned` so the purge
        takes it (the steps are idempotent; the items already done stay done)."""
        row = (
            await conn.execute(
                text("SELECT status FROM deletion_requests WHERE workspace_id = :ws AND id = :id FOR UPDATE"), {"ws": ws, "id": request_id}
            )
        ).first()
        if row is None:
            raise NotFound()
        if row.status in ("cancelled",):
            raise InvalidRequestState()
        closure = Closure()
        for m in memories:
            c = await planner.compute(conn, ws, "memory", m)
            closure.memories += c.memories
            closure.facts += c.facts
        closure.memories = sorted(set(closure.memories), key=str)
        await planner.add_items(conn, ws, request_id, "curated_memories", closure.memories)
        await planner.freeze(conn, ws, closure)
        s = await planner.sets_of(conn, request_id)
        await planner.plan_dependents(conn, ws, request_id, s, terms=[])
        if row.status in ("completed_with_exceptions", "verifying"):
            await self._move(conn, ws, request_id, ("completed_with_exceptions", "verifying"), "purging", "purge_started_at")
            await self._enqueue(
                conn,
                ws,
                stage=STAGE_PURGE,
                request_id=request_id,
                generation=int(
                    (
                        await conn.execute(
                            text("SELECT count(*) FROM outbox_jobs WHERE workspace_id = :ws AND subject_id = :r AND stage = :s"),
                            {"ws": ws, "r": request_id, "s": STAGE_PURGE},
                        )
                    ).scalar_one()
                ),
            )
        await audit.record(
            "deletion.extended",
            resource_type="deletion_request",
            resource_id=request_id,
            metadata=AuditMetadata(request_id=request_id, count_memories=len(closure.memories)),
        )

    async def reverify(self, conn: AsyncConnection, ws: uuid.UUID, request_id: uuid.UUID, audit: AuditSink) -> None:
        row = (
            await conn.execute(
                text("SELECT status FROM deletion_requests WHERE workspace_id = :ws AND id = :id FOR UPDATE"), {"ws": ws, "id": request_id}
            )
        ).first()
        if row is None:
            raise NotFound()
        if row.status == "completed_with_exceptions":
            await self._move(conn, ws, request_id, ("completed_with_exceptions",), "verifying")
            gen = int(
                (
                    await conn.execute(
                        text("SELECT count(*) FROM outbox_jobs WHERE workspace_id = :ws AND subject_id = :r AND stage = :s"),
                        {"ws": ws, "r": request_id, "s": STAGE_VERIFY},
                    )
                ).scalar_one()
            )
            await self._enqueue(conn, ws, stage=STAGE_VERIFY, request_id=request_id, generation=gen)
            await audit.record(
                "deletion.reverify", resource_type="deletion_request", resource_id=request_id, metadata=AuditMetadata(request_id=request_id)
            )
