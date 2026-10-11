"""Outbox visibility and repair for operators (ADM-06): the job list, one job with its attempts, backlog, and audited retry / cancel."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from kg_common.pagination import clamp_limit, decode_cursor, encode_cursor
from kg_domain.envelope import Page
from kg_governance.authz.policy import Action, requires
from kg_ingestion.models import AttemptOut, BacklogCell, BacklogOut, JobActionIn, JobActionOut, JobDetail, JobStatus, JobSummary
from kg_jobs import reads as job_reads

from kg_api.deps import AuditDep, DbDep, StateDep, WorkspaceDep
from kg_api.http import IdempotencyKeyHeader, require_key, run_idempotent

router = APIRouter(prefix="/v1/operations", tags=["operations"])
ACTION_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"description": "Not found (also for a job of another workspace)"},
    409: {"description": "job_not_dead / job_not_cancellable / already_replayed"},
}


@router.get("/jobs", response_model=Page[JobSummary])
@requires(Action.OPERATIONS_READ)
async def list_jobs(
    ws: WorkspaceDep,
    db: DbDep,
    status: JobStatus | None = None,
    stage: Annotated[str | None, Query(max_length=32, pattern=r"^[a-z][a-z_]*$")] = None,
    subject_type: Literal["interaction", "command"] | None = None,
    subject_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: str | None = None,
) -> Page[JobSummary]:
    size = clamp_limit(limit)
    rows = await job_reads.list_jobs(
        db,
        ws.workspace_id,
        status=status,
        stage=stage,
        subject_type=subject_type,
        subject_id=subject_id,
        size=size,
        before=decode_cursor(cursor),
    )
    page, more = rows[:size], len(rows) > size
    return Page[JobSummary](
        items=[JobSummary(**job_reads.job_summary(r)) for r in page],
        next_cursor=encode_cursor(page[-1].created_at, page[-1].id) if more else None,
    )


@router.get("/backlog", response_model=BacklogOut)
@requires(Action.OPERATIONS_READ)
async def backlog(ws: WorkspaceDep, db: DbDep) -> BacklogOut:
    data = await job_reads.backlog(db, ws.workspace_id)
    return BacklogOut(
        cells=[BacklogCell(**c) for c in data["cells"]],
        dead=data["dead"],
        oldest_pending_age_seconds=data["oldest_pending_age_seconds"],
        lease_reclaims_last_hour=data["lease_reclaims_last_hour"],
        lease_reclaims_last_day=data["lease_reclaims_last_day"],
    )


@router.get("/jobs/{job_id}", response_model=JobDetail)
@requires(Action.OPERATIONS_READ)
async def get_job(job_id: uuid.UUID, ws: WorkspaceDep, db: DbDep) -> JobDetail:
    row = await job_reads.get_job(db, ws.workspace_id, job_id)
    attempts = await job_reads.job_attempts(db, ws.workspace_id, job_id)
    return JobDetail(
        **job_reads.job_summary(row),
        correlation_id=row.correlation_id,
        attempt_log=[
            AttemptOut(
                attempt_no=a.attempt_no,
                worker_id=a.worker_id,
                started_at=a.started_at,
                ended_at=a.ended_at,
                outcome=a.outcome,
                error_category=a.error_category,
                error_code=a.error_code,
                duration_ms=a.duration_ms,
            )
            for a in attempts
        ],
    )


@router.post("/jobs/{job_id}/retry", response_model=None, responses=ACTION_RESPONSES)
@requires(Action.OPERATIONS_REPAIR)
async def retry_job(
    job_id: uuid.UUID,
    body: JobActionIn,
    state: StateDep,
    ws: WorkspaceDep,
    db: DbDep,
    audit: AuditDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    key = require_key(idempotency_key)

    async def produce() -> tuple[int, JobActionOut, dict[str, str]]:
        return 202, await state.ingestion.operations.retry_job(db, ws, audit, job_id, reason_code=body.reason_code), {}

    return await run_idempotent(db, ws.workspace_id, key, f"POST /v1/operations/jobs/{job_id}/retry", body.model_dump(mode="json"), produce)


@router.post("/jobs/{job_id}/cancel", response_model=None, responses=ACTION_RESPONSES)
@requires(Action.OPERATIONS_REPAIR)
async def cancel_job(
    job_id: uuid.UUID,
    body: JobActionIn,
    state: StateDep,
    ws: WorkspaceDep,
    db: DbDep,
    audit: AuditDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    key = require_key(idempotency_key)

    async def produce() -> tuple[int, JobActionOut, dict[str, str]]:
        return 200, await state.ingestion.operations.cancel_job(db, ws, audit, job_id, reason_code=body.reason_code), {}

    return await run_idempotent(
        db, ws.workspace_id, key, f"POST /v1/operations/jobs/{job_id}/cancel", body.model_dump(mode="json"), produce
    )
