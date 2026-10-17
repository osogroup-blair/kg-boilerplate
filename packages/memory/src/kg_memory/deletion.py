"""Deletion wiring (phase-8 plan §5.4): the adapters that connect `kg_governance.deletion` to the rest of the platform, and the review handler for deletion exceptions.

kg_governance cannot import the payload cipher, the outbox or the graph, so this module (which can) builds the `DeletionService` with:
    * a ManifestCipher over the payload keyring,
    * a JobEnqueuer over the transactional outbox (the next stage commits with the state change that needs it),
    * the GraphPurger (ADR-018), and the out-of-band journal.
It also decides `deletion_exception` review items: a person redacts the text that names a deleted subject, deletes the item, keeps it under a legal hold, or asks for the
verification to run again."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kg_common.errors import Conflict, ValidationFailed
from kg_common.settings import REPO_ROOT, Settings
from kg_common.versions import PIPELINE_VERSION
from kg_governance.audit import AuditMetadata
from kg_governance.deletion import faults
from kg_governance.deletion.holds import Holds, Suppressions
from kg_governance.deletion.journal import DeletionJournal
from kg_governance.deletion.requests import DeletionService
from kg_ingestion.crypto import ContentHasher, PayloadCipher, Sealed
from kg_jobs import faults as job_faults
from kg_jobs.outbox import enqueue
from kg_knowledge.commands import CommandContext
from kg_knowledge.graph_purge import GraphHandle, GraphPurger
from kg_knowledge.models import DecisionIn
from kg_knowledge.services import KnowledgeServices
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from kg_memory.models import SupersedeIn
from kg_memory.scopes import ScopeGrants
from kg_memory.writers import MemoryWriter, WriteContext


class ManifestCipherAdapter:
    def __init__(self, cipher: PayloadCipher):
        self._c = cipher

    def seal(self, plaintext: bytes, *, workspace_id: uuid.UUID, request_id: uuid.UUID) -> tuple[str, bytes, bytes]:
        s = self._c.encrypt(plaintext, workspace_id=workspace_id, interaction_id=request_id, form="manifest")
        return s.kid, s.nonce, s.ciphertext

    def open(self, kid: str, nonce: bytes, ciphertext: bytes, *, workspace_id: uuid.UUID, request_id: uuid.UUID) -> bytes:
        return self._c.decrypt(Sealed(kid, nonce, ciphertext), workspace_id=workspace_id, interaction_id=request_id, form="manifest")


async def enqueue_stage(
    conn: AsyncConnection, workspace_id: uuid.UUID, *, stage: str, request_id: uuid.UUID, generation: int = 0, delay_seconds: float = 0.0
) -> None:
    """A deletion stage is a job whose subject is the request. Purge and verify run one per workspace at a time (they are not safe to interleave)."""
    await enqueue(
        conn,
        workspace_id=workspace_id,
        stage=stage,
        subject_type="command",
        subject_id=request_id,
        dedupe_key=f"deletion:{request_id}:{stage}:g{generation}",
        payload={"request_id": str(request_id), "generation": generation},
        pipeline_version=PIPELINE_VERSION,
        delay_seconds=delay_seconds,
        concurrency_key=f"deletion:{workspace_id}",
    )


@dataclass
class DeletionServices:
    service: DeletionService
    holds: Holds
    suppressions: Suppressions
    journal: DeletionJournal
    graph: GraphPurger | None
    handler: DeletionExceptionHandler


def build_deletion(
    settings: Settings,
    engine: AsyncEngine,
    knowledge: KnowledgeServices,
    graph: GraphHandle | None,
    cipher: PayloadCipher,
    hasher: ContentHasher,
    writer: MemoryWriter,
) -> DeletionServices:
    directory = Path(settings.deletion_journal_dir) if settings.deletion_journal_dir else REPO_ROOT / ".local" / "deletion-journal"
    journal = DeletionJournal(directory, settings.content_hmac_secret.get_secret_value().encode())
    suppressions = Suppressions(hasher.ref)
    faults.install(lambda stage, point: job_faults.current().hit(stage, point))
    purger = GraphPurger(engine, graph, knowledge.projector, knowledge.lock) if graph is not None else None
    service = DeletionService(
        cipher=ManifestCipherAdapter(cipher),
        enqueue=enqueue_stage,
        suppressions=suppressions,
        journal=journal,
        graph=purger,
        backup_retention_days=settings.backup_retention_days,
        cooling_hours=settings.workspace_deletion_cooling_hours,
        enabled=settings.deletion_api_enabled,
    )
    knowledge.pipeline.set_ref_hmac(hasher.ref)
    handler = DeletionExceptionHandler(service, writer)
    knowledge.reviews.register_handler("deletion_exception", handler)
    return DeletionServices(service=service, holds=Holds(), suppressions=suppressions, journal=journal, graph=purger, handler=handler)


class NotHeld(Conflict):
    code, default_message = "not_held", "Only a legal-hold exception can be retained"


class DeletionExceptionHandler:
    """`redact_text` | `delete_item` | `retain_with_reason` | `retry_verification` (phase-8 plan §5.4.4)."""

    def __init__(self, service: DeletionService, writer: MemoryWriter):
        self._service, self._writer = service, writer

    async def decide(self, cc: CommandContext, review: Row[Any], body: DecisionIn, *, key: str) -> dict[str, Any]:
        conn, ws = cc.conn, cc.ctx.workspace_id
        request_id = review.deletion_request_id
        reason = review.reason_code
        if body.decision == "retry_verification":
            await self._service.reverify(conn, ws, request_id, cc.audit)
            return {}
        if body.decision == "retain_with_reason":
            if reason != "legal_hold":
                raise NotHeld()
            await cc.audit.record(
                "deletion.retained",
                resource_type="deletion_request",
                resource_id=request_id,
                metadata=AuditMetadata(request_id=request_id, reason_code="legal_hold", review_id=review.id),
            )
            return {}
        named = (review.detail or {}).get("memory_id")
        if named is None:
            raise ValidationFailed("this exception names no item to redact or delete", details={"fields": ["decision"]})
        mid = uuid.UUID(named)
        if body.decision == "redact_text":
            if not body.edited_content:
                raise ValidationFailed("redact_text needs the replacement text in edited_content", details={"fields": ["edited_content"]})
            grants = await ScopeGrants.for_principal(conn, cc.principal)
            w = WriteContext(conn, cc.ctx, cc.principal, cc.audit, grants, curator=True)
            row = (
                await conn.execute(
                    text("SELECT revision FROM curated_memories WHERE workspace_id = :ws AND id = :id"), {"ws": ws, "id": mid}
                )
            ).first()
            if row is None:
                raise ValidationFailed("the memory is gone", details={"fields": ["decision"]})
            await self._writer.supersede(
                w, mid, SupersedeIn(content=body.edited_content, reason_code="redaction"), f"{key}:redact", row.revision
            )
        await self._service.add_to_plan(conn, ws, request_id, memories=[mid], audit=cc.audit)
        return {"memory_ids": [mid]}


class RetentionSweeper:
    """The periodic retention task (phase-8 plan §5.6): `ops.retention_candidates()` names the workspaces with something expired (ids and counts, nothing else), and each
    class is carried out as a per-workspace job: content rows through the purge gate as a `retention` deletion request, Neo4j episode text blanked, model call rows removed,
    stale memories expired. Everything is idempotent, so a sweep that is interrupted is simply run again."""

    def __init__(self, engine: AsyncEngine, service: DeletionService, graph: GraphHandle | None):
        self._engine, self._service, self._graph = engine, service, graph

    async def sweep(self) -> dict[str, int]:
        from kg_db.session import system_session, tenant_session
        from kg_domain.principal import WorkspaceContext

        async with system_session(self._engine) as conn:
            todo = (await conn.execute(text("SELECT workspace_id, retention_class, n FROM ops.retention_candidates()"))).all()
        done: dict[str, int] = {}
        for row in todo:
            ctx = WorkspaceContext.for_system_task(row.workspace_id, reason="retention sweep")
            async with tenant_session(self._engine, ctx) as conn:
                settings = (
                    await conn.execute(
                        text("SELECT settings FROM workspace_settings WHERE workspace_id = :ws ORDER BY version DESC LIMIT 1"),
                        {"ws": ctx.workspace_id},
                    )
                ).scalar_one_or_none() or {}
            r = settings.get("retention", {})
            cls = row.retention_class
            n = 0
            if cls == "raw_content" and r.get("raw_content_days"):
                n = await self._service.run_retention(self._engine, ctx, retention_class="raw_content", days=int(r["raw_content_days"]))
            elif cls == "normalized_content" and r.get("normalized_content_days"):
                n = await self._service.run_retention(
                    self._engine, ctx, retention_class="normalized_content", days=int(r["normalized_content_days"])
                )
            elif cls == "episode_content" and r.get("episode_content_days") and self._graph is not None:
                n = await self._blank_episodes(ctx, int(r["episode_content_days"]))
            elif cls == "model_calls" and r.get("model_calls_days"):
                async with tenant_session(self._engine, ctx) as conn:
                    n = int(
                        (
                            await conn.execute(
                                text("SELECT ops.retention_purge_model_calls(:ws, :d)"),
                                {"ws": ctx.workspace_id, "d": int(r["model_calls_days"])},
                            )
                        ).scalar_one()
                    )
                if n:
                    from kg_obs import metrics

                    metrics.retention_deleted_total.labels(retention_class="model_calls").inc(n)
            elif cls == "memory_archive" and r.get("memory_archive_after_days"):
                n = await self._expire_memories(ctx, int(r["memory_archive_after_days"]))
            if n:
                done[cls] = done.get(cls, 0) + n
        return done

    async def _blank_episodes(self, ctx: Any, days: int) -> int:
        from kg_db.session import tenant_session

        assert self._graph is not None
        async with tenant_session(self._engine, ctx) as conn:
            uuids = [
                r.episode_uuid
                for r in (
                    await conn.execute(
                        text(
                            "SELECT episode_uuid FROM graph_episodes WHERE workspace_id = :ws AND created_at < now() - make_interval(days => :d)"
                        ),
                        {"ws": ctx.workspace_id, "d": days},
                    )
                ).all()
            ]
        n = await self._graph.scoped(ctx).blank_episode_content(uuids)
        if n:
            from kg_obs import metrics

            metrics.retention_deleted_total.labels(retention_class="episode_content").inc(n)
        return n

    async def _expire_memories(self, ctx: Any, days: int) -> int:
        from kg_db.session import tenant_session
        from kg_governance.audit import SystemAudit
        from kg_obs import metrics

        from kg_memory import store

        async with tenant_session(self._engine, ctx) as conn:
            ids = [
                r.id
                for r in (
                    await conn.execute(
                        text(
                            "SELECT id FROM curated_memories WHERE workspace_id = :ws AND status = 'active' AND type <> 'working' AND frozen_at IS NULL "
                            "AND COALESCE(last_confirmed_at, recorded_at) < now() - make_interval(days => :d) ORDER BY recorded_at LIMIT 500 FOR UPDATE"
                        ),
                        {"ws": ctx.workspace_id, "d": days},
                    )
                ).all()
            ]
            audit = SystemAudit(conn, ctx.workspace_id)
            for mid in ids:
                await store.set_status(conn, ctx.workspace_id, mid, "expired", close_validity=True)
                await audit.record(
                    "memory.expired",
                    resource_type="memory",
                    resource_id=mid,
                    metadata=AuditMetadata(memory_id=mid, memory_status="expired", reason_code="retention_policy"),
                )
        if ids:
            metrics.retention_deleted_total.labels(retention_class="memory_archive").inc(len(ids))
        return len(ids)
