"""Tenant-scoped reads of the outbox (operations console, health). Plain SELECTs under row-level security: a job of another workspace is simply absent."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from kg_common.errors import NotFound
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection


def generation_of(payload: dict[str, Any] | None) -> int:
    return int((payload or {}).get("generation", 0))


def job_summary(r: Row[Any]) -> dict[str, Any]:
    return {
        "id": r.id,
        "stage": r.stage,
        "subject_type": r.subject_type,
        "subject_id": r.subject_id,
        "status": r.status,
        "attempts": r.attempts,
        "max_attempts": r.max_attempts,
        "generation": generation_of(r.payload),
        "available_at": r.available_at,
        "last_error_category": r.last_error_category,
        "last_error_code": r.last_error_code,
        "pipeline_version": r.pipeline_version,
        "ontology_version_id": r.ontology_version_id,
        "created_at": r.created_at,
        "finished_at": r.finished_at,
    }


async def list_jobs(
    conn: AsyncConnection,
    ws: uuid.UUID,
    *,
    status: str | None,
    stage: str | None,
    subject_type: str | None,
    subject_id: uuid.UUID | None,
    size: int,
    before: tuple[dt.datetime, uuid.UUID] | None,
) -> list[Row[Any]]:
    where, params = ["workspace_id = :ws"], {"ws": ws, "n": size + 1}
    for column, key, value in (
        ("status", "status", status),
        ("stage", "stage", stage),
        ("subject_type", "stype", subject_type),
        ("subject_id", "sid", subject_id),
    ):
        if value is not None:
            where.append(f"{column} = :{key}")
            params[key] = value
    if before:
        where.append("(created_at, id) < (:c_at, :c_id)")
        params["c_at"], params["c_id"] = before[0], before[1]
    sql = f"SELECT * FROM outbox_jobs WHERE {' AND '.join(where)} ORDER BY created_at DESC, id DESC LIMIT :n"  # noqa: S608 (constant fragments)
    return list((await conn.execute(text(sql), params)).all())


async def get_job(conn: AsyncConnection, ws: uuid.UUID, job_id: uuid.UUID) -> Row[Any]:
    row = (await conn.execute(text("SELECT * FROM outbox_jobs WHERE workspace_id = :ws AND id = :id"), {"ws": ws, "id": job_id})).first()
    if row is None:
        raise NotFound()
    return row


async def job_attempts(conn: AsyncConnection, ws: uuid.UUID, job_id: uuid.UUID) -> list[Row[Any]]:
    return list(
        (
            await conn.execute(
                text("SELECT * FROM job_attempts WHERE workspace_id = :ws AND job_id = :id ORDER BY attempt_no, started_at"),
                {"ws": ws, "id": job_id},
            )
        ).all()
    )


async def backlog(conn: AsyncConnection, ws: uuid.UUID) -> dict[str, Any]:
    cells = (
        await conn.execute(
            text(
                "SELECT stage, status, count(*)::int AS jobs, "
                "EXTRACT(EPOCH FROM now() - min(available_at) FILTER (WHERE status = 'pending' AND available_at <= now()))::float8 AS oldest "
                "FROM outbox_jobs WHERE workspace_id = :ws AND status IN ('pending','leased','dead') GROUP BY stage, status ORDER BY stage, status"
            ),
            {"ws": ws},
        )
    ).all()
    reclaims = (
        await conn.execute(
            text(
                "SELECT count(*) FILTER (WHERE ended_at > now() - interval '1 hour')::int AS hour, count(*)::int AS day FROM job_attempts "
                "WHERE workspace_id = :ws AND outcome = 'lease_expired' AND ended_at > now() - interval '24 hours'"
            ),
            {"ws": ws},
        )
    ).one()
    pending_ages = [c.oldest for c in cells if c.status == "pending" and c.oldest is not None]
    return {
        "cells": [{"stage": c.stage, "status": c.status, "jobs": c.jobs, "oldest_age_seconds": c.oldest} for c in cells],
        "dead": sum(c.jobs for c in cells if c.status == "dead"),
        "backlog": sum(c.jobs for c in cells if c.status in ("pending", "leased")),
        "oldest_pending_age_seconds": max(pending_ages) if pending_ages else None,
        "lease_reclaims_last_hour": reclaims.hour,
        "lease_reclaims_last_day": reclaims.day,
    }


async def stage_lag(conn: AsyncConnection, ws: uuid.UUID, stages: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """How far behind each pipeline stage is for ONE workspace, by the database clock (P6 source watermarks): the age of the oldest job still waiting (pending or
    leased), how many dead-lettered jobs are stuck, and when the newest job finished. Counts and ages only: no subject, payload or content."""
    rows = (
        await conn.execute(
            text(
                "SELECT stage, "
                "EXTRACT(EPOCH FROM now() - min(created_at) FILTER (WHERE status IN ('pending','leased')))::float8 AS waiting_age, "
                "count(*) FILTER (WHERE status = 'dead') AS dead, "
                "max(finished_at) FILTER (WHERE status IN ('succeeded','skipped')) AS last_finished "
                "FROM outbox_jobs WHERE workspace_id = :ws AND stage = ANY(CAST(:stages AS text[])) GROUP BY stage"
            ),
            {"ws": ws, "stages": list(stages)},
        )
    ).all()
    out: dict[str, dict[str, Any]] = {st: {"waiting_age_seconds": None, "dead": 0, "last_finished_at": None} for st in stages}
    for r in rows:
        out[r.stage] = {"waiting_age_seconds": r.waiting_age, "dead": int(r.dead), "last_finished_at": r.last_finished}
    return out
