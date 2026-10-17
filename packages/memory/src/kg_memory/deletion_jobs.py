"""The three job stages of a deletion on the outbox (phase-8 plan §5.4.1): `deletion_plan`, `deletion_purge`, `deletion_verify`.

Each handler is idempotent (the service steps are: items carry their own `done` marks), so a crash or a lease loss only repeats work. A step that keeps failing ends the job
`dead`, and `on_failure` then marks the request `failed` with a code an operator can read; `POST /v1/deletion-requests/{id}/retry` resumes it."""

from __future__ import annotations

import asyncio
import uuid

from kg_domain.principal import WorkspaceContext
from kg_governance.deletion.requests import STAGE_PLAN, STAGE_PURGE, STAGE_VERIFY, DeletionService
from kg_jobs.client import ClaimedJob
from kg_jobs.errors import Category, JobError
from kg_jobs.policy import Decision
from kg_obs.logging import get_logger
from sqlalchemy.ext.asyncio import AsyncEngine

log = get_logger("kg.deletion.job")


class DeletionStageJob:
    def __init__(self, engine: AsyncEngine, service: DeletionService, stage: str):
        assert stage in (STAGE_PLAN, STAGE_PURGE, STAGE_VERIFY)
        self._engine, self._service, self.stage = engine, service, stage

    @staticmethod
    def _ctx(job: ClaimedJob) -> WorkspaceContext:
        return WorkspaceContext.for_system_task(job.workspace_id, reason="deletion job")

    async def handle(self, job: ClaimedJob) -> None:
        ctx, request_id = self._ctx(job), uuid.UUID(str(job.payload["request_id"]))
        step = {STAGE_PLAN: self._service.run_plan, STAGE_PURGE: self._service.run_purge, STAGE_VERIFY: self._service.run_verify}[
            self.stage
        ]
        try:
            await step(self._engine, ctx, request_id)
        except (JobError, asyncio.CancelledError):
            raise
        except Exception as exc:
            log.warning("deletion_step_failed", stage=self.stage, error_type=type(exc).__name__)
            raise JobError(Category.RETRYABLE, "deletion_step_failed") from exc

    async def on_failure(self, job: ClaimedJob, decision: Decision) -> None:
        if decision.outcome == "dead":
            await self._service.fail(
                self._engine, self._ctx(job), uuid.UUID(str(job.payload["request_id"])), decision.code or "attempts_exhausted"
            )
