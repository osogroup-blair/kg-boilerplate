"""The two memory jobs on the outbox (R9, R11): `memory_embed` (the vector of a new memory) and `memory_expire` (a working note's deadline).

Payloads are `{memory_id, generation}` only. Both are idempotent: a re-run finds the vector already stored, or the memory already expired, and does nothing.
The expiry is a DELAYED job (`available_at` = the deadline), enqueued with the memory in the same transaction: no sweeper and no cross-workspace scan."""

from __future__ import annotations

import uuid

from kg_common.ids import is_uuid, uuid7_str
from kg_common.settings import Settings
from kg_db.session import tenant_session
from kg_domain.principal import WorkspaceContext
from kg_governance.audit import AuditMetadata, SystemAudit
from kg_jobs.client import ClaimedJob
from kg_jobs.errors import Category, JobError
from kg_jobs.policy import Decision
from kg_modelgw.config import ModelConfig
from kg_modelgw.gateway import Correlation, ModelGateway
from kg_obs import metrics
from kg_obs.logging import get_logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from kg_memory import store
from kg_memory.errors import job_error_for
from kg_memory.store import vector_literal

log = get_logger("kg.memory.jobs")
DEAD_STATUSES = ("rejected", "deleted")


def embed_text(summary: str, content: str) -> str:
    """What a memory is embedded as: its content, plus the summary when the summary adds words the content lacks (an extracted memory's does)."""
    return content if summary.strip() and summary.strip() in content else f"{summary}\n{content}"


class MemoryEmbedJob:
    stage = "memory_embed"

    def __init__(self, engine: AsyncEngine, gateway: ModelGateway, config: ModelConfig, settings: Settings, *, service: str = "kg-worker"):
        self._engine, self._gateway, self._config, self._settings, self._service = engine, gateway, config, settings, service

    async def handle(self, job: ClaimedJob) -> None:
        ws, mid = job.workspace_id, uuid.UUID(str(job.payload.get("memory_id", job.subject_id)))
        ctx = WorkspaceContext.for_system_task(ws, reason="memory_embed stage")
        version = self._settings.memory_embedding_version
        async with tenant_session(self._engine, ctx) as conn:
            row = (
                await conn.execute(
                    text("SELECT summary, content, classification, status FROM curated_memories WHERE workspace_id = :ws AND id = :id"),
                    {"ws": ws, "id": mid},
                )
            ).first()
            if row is None:
                raise JobError(Category.CONFIG_TERMINAL, "memory_missing")
            if row.classification == "local_only" or not self._config.egress_allows(row.classification) or row.status in DEAD_STATUSES:
                await self._mark(
                    conn, ws, mid, "skipped_by_policy"
                )  # ING-06 for the memory store: nothing leaves, and a local_only memory stays lexical-only
                metrics.memory_embed_total.labels(outcome="skipped_by_policy").inc()
                return
            have = (
                await conn.execute(
                    text("SELECT 1 FROM memory_embeddings WHERE workspace_id = :ws AND memory_id = :id AND embedding_version = :v"),
                    {"ws": ws, "id": mid, "v": version},
                )
            ).first()
            if have is not None:
                await self._mark(conn, ws, mid, "ready")
                metrics.memory_embed_total.labels(outcome="already_embedded").inc()
                return
        try:
            result = await self._gateway.embed(
                "embedding",
                [embed_text(row.summary, row.content)],
                kind="passage",
                classification=row.classification,
                correlation=Correlation(
                    job.correlation_id if job.correlation_id and is_uuid(job.correlation_id) else uuid7_str(), str(ws), "", self._service
                ),
            )
        except Exception as exc:
            if type(exc).__name__ == "CancelledError":
                raise
            metrics.memory_embed_total.labels(outcome="error").inc()
            raise job_error_for(exc, attempt=job.attempts) from exc
        async with tenant_session(self._engine, ctx) as conn:
            emb = self._config.embedding
            await conn.execute(
                text(
                    "INSERT INTO memory_embeddings (memory_id, embedding_version, workspace_id, embedding, embedding_model_id, embedding_dim) "
                    "VALUES (:m, :v, :ws, CAST(:e AS vector), :model, :dim) ON CONFLICT (memory_id, embedding_version) DO UPDATE "
                    "SET embedding = EXCLUDED.embedding, embedding_model_id = EXCLUDED.embedding_model_id, embedding_dim = EXCLUDED.embedding_dim, embedded_at = now()"
                ),
                {"m": mid, "v": version, "ws": ws, "e": vector_literal(result.vectors[0]), "model": emb.embedding_model_id, "dim": emb.dim},
            )
            await self._mark(conn, ws, mid, "ready")
        metrics.memory_embed_total.labels(outcome="embedded").inc()

    @staticmethod
    async def _mark(conn, ws: uuid.UUID, mid: uuid.UUID, status: str) -> None:  # type: ignore[no-untyped-def]
        await conn.execute(
            text("UPDATE curated_memories SET embedding_status = :s WHERE workspace_id = :ws AND id = :id"),
            {"s": status, "ws": ws, "id": mid},
        )

    async def on_failure(self, job: ClaimedJob, decision: Decision) -> None:
        """A dead embed leaves the memory lexical-only and says so (`failed`); a retry is still `pending`."""
        status = {"dead": "failed", "skipped": "skipped_by_policy"}.get(decision.outcome)
        if status is None:
            return
        ctx = WorkspaceContext.for_system_task(job.workspace_id, reason="memory_embed stage failure")
        mid = uuid.UUID(str(job.payload.get("memory_id", job.subject_id)))
        async with tenant_session(self._engine, ctx) as conn:
            await self._mark(conn, job.workspace_id, mid, status)
        metrics.memory_embed_total.labels(outcome=status).inc()
        log.info("memory_embed_failed", outcome=decision.outcome, error_code=decision.code, attempt=job.attempts)


class MemoryExpireJob:
    stage = "memory_expire"

    def __init__(self, engine: AsyncEngine):
        self._engine = engine

    async def handle(self, job: ClaimedJob) -> None:
        ws, mid = job.workspace_id, uuid.UUID(str(job.payload.get("memory_id", job.subject_id)))
        ctx = WorkspaceContext.for_system_task(ws, reason="memory_expire stage")
        async with tenant_session(self._engine, ctx) as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT status, type, (expires_at <= now()) AS due, EXTRACT(EPOCH FROM (expires_at - now()))::float8 AS remaining "
                        "FROM curated_memories WHERE workspace_id = :ws AND id = :id AND expires_at IS NOT NULL FOR UPDATE"
                    ),
                    {"ws": ws, "id": mid},
                )
            ).first()
            if row is None or row.status not in ("active", "proposed"):
                return  # gone, already expired, superseded or rejected: nothing to do (idempotent)
            if not row.due:
                raise JobError(
                    Category.RETRYABLE, "not_yet_due", retry_after=max(float(row.remaining) + 0.25, 0.5)
                )  # the job beat the DB clock by a hair
            await store.set_status(conn, ws, mid, "expired", close_validity=True)
            await SystemAudit(conn, ws, correlation_id=job.correlation_id).record(
                "memory.expired",
                resource_type="memory",
                resource_id=mid,
                metadata=AuditMetadata(memory_id=mid, memory_type=row.type, memory_status="expired", reason_code="ttl"),
            )
        metrics.memory_expired_total.inc()
