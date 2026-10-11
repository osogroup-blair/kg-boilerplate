"""Python wrappers for the ops.* SECURITY DEFINER functions (migration 0003). They are the ONLY way application code changes outbox rows
(kg_app has no UPDATE grant), and they return routing metadata only: ids, stage, attempt, lease token, pinned versions. Never content."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from kg_db.session import system_session
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass(frozen=True)
class ClaimedJob:
    job_id: uuid.UUID
    workspace_id: uuid.UUID
    stage: str
    subject_type: str
    subject_id: uuid.UUID
    payload: dict[str, Any]
    attempts: int  # 1-based number of THIS attempt (the claim already counted it)
    max_attempts: int
    lease_token: uuid.UUID
    pipeline_version: str
    ontology_version_id: uuid.UUID | None
    correlation_id: str | None
    reclaimed: bool = False  # the previous lease had expired (a crashed or stalled worker)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def generation(self) -> int:
        return int(self.payload.get("generation", 0))


@dataclass(frozen=True)
class BacklogRow:
    stage: str
    status: str
    jobs: int
    oldest_age_seconds: float | None


class JobsBackend(Protocol):
    """What the dispatcher needs. OpsClient is the Postgres implementation; unit tests use an in-memory fake."""

    async def claim(self, worker_id: str, stages: list[str], limit: int, lease_seconds: int) -> list[ClaimedJob]: ...
    async def heartbeat(self, worker_id: str, tokens: list[uuid.UUID], lease_seconds: int) -> list[uuid.UUID]: ...
    async def complete(
        self,
        job_id: uuid.UUID,
        token: uuid.UUID,
        outcome: str,
        *,
        error_category: str | None = None,
        error_code: str | None = None,
        delay_seconds: float = 0.0,
    ) -> str: ...
    async def release(self, worker_id: str) -> int: ...
    async def backlog_stats(self) -> list[BacklogRow]: ...


class OpsClient:
    def __init__(self, engine: AsyncEngine):
        self._engine = engine

    async def claim(self, worker_id: str, stages: list[str], limit: int, lease_seconds: int) -> list[ClaimedJob]:
        async with system_session(self._engine) as conn:
            rows = (
                await conn.execute(
                    text("SELECT * FROM ops.claim_jobs(:w, CAST(:stages AS text[]), :n, :lease)"),
                    {"w": worker_id, "stages": stages, "n": limit, "lease": lease_seconds},
                )
            ).all()
        return [
            ClaimedJob(
                job_id=r.job_id,
                workspace_id=r.workspace_id,
                stage=r.stage,
                subject_type=r.subject_type,
                subject_id=r.subject_id,
                payload=dict(r.payload or {}),
                attempts=int(r.attempts),
                max_attempts=int(r.max_attempts),
                lease_token=r.lease_token,
                pipeline_version=r.pipeline_version,
                ontology_version_id=r.ontology_version_id,
                correlation_id=r.correlation_id,
                reclaimed=bool(r.reclaimed),
            )
            for r in rows
        ]

    async def heartbeat(self, worker_id: str, tokens: list[uuid.UUID], lease_seconds: int) -> list[uuid.UUID]:
        """Extends the leases this worker still owns; returns the tokens it LOST (reclaimed by someone else, completed, or cancelled)."""
        if not tokens:
            return []
        async with system_session(self._engine) as conn:
            rows = (
                await conn.execute(
                    text("SELECT * FROM ops.heartbeat_jobs(:w, CAST(:tokens AS uuid[]), :lease)"),
                    {"w": worker_id, "tokens": tokens, "lease": lease_seconds},
                )
            ).all()
        return [r[0] for r in rows]

    async def complete(
        self,
        job_id: uuid.UUID,
        token: uuid.UUID,
        outcome: str,
        *,
        error_category: str | None = None,
        error_code: str | None = None,
        delay_seconds: float = 0.0,
    ) -> str:
        """Fenced: 'ok', or 'stale' when the lease token no longer matches (someone reclaimed the job). A stale completion changes nothing."""
        async with system_session(self._engine) as conn:
            row = (
                await conn.execute(
                    text("SELECT ops.complete_job(:j, :t, :o, :cat, :code, :delay)"),
                    {"j": job_id, "t": token, "o": outcome, "cat": error_category, "code": error_code, "delay": delay_seconds},
                )
            ).one()
        return str(row[0])

    async def release(self, worker_id: str) -> int:
        async with system_session(self._engine) as conn:
            row = (await conn.execute(text("SELECT ops.release_jobs(:w)"), {"w": worker_id})).one()
        return int(row[0])

    async def backlog_stats(self) -> list[BacklogRow]:
        async with system_session(self._engine) as conn:
            rows = (await conn.execute(text("SELECT * FROM ops.backlog_stats()"))).all()
        return [
            BacklogRow(r.stage, r.status, int(r.jobs), None if r.oldest_age_seconds is None else float(r.oldest_age_seconds)) for r in rows
        ]
