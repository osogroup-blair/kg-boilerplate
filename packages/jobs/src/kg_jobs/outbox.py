"""The transactional outbox write. `enqueue` runs on the CALLER's connection, so the job commits (or rolls back) with the state change that needs it.

The payload carries IDs and small tokens only (validated): job rows are operator-visible, so they must never hold content."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from kg_common.ids import uuid7
from kg_obs import context
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from kg_jobs.policy import policy_for

_KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_.:@=-]{1,64}$")  # UUIDs, enum names and short ids fit; sentences do not (no spaces)
SUBJECT_TYPES = ("interaction", "command", "memory")


def validate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if len(payload) > 8:
        raise ValueError("a job payload carries at most 8 keys")
    for k, v in payload.items():
        if not _KEY.match(k):
            raise ValueError(f"invalid payload key {k!r}")
        ok = v is None or isinstance(v, bool | int) or (isinstance(v, str) and _TOKEN.match(v)) or isinstance(v, uuid.UUID)
        if not ok:
            raise ValueError(f"payload value for {k!r} must be an id, a number or a short token (no free text)")
    return {k: str(v) if isinstance(v, uuid.UUID) else v for k, v in payload.items()}


_INSERT = text("""
    INSERT INTO outbox_jobs (id, workspace_id, stage, subject_type, subject_id, dedupe_key, payload, priority, max_attempts, available_at,
                             concurrency_key, pipeline_version, ontology_version_id, correlation_id)
    VALUES (:id, :ws, :stage, :stype, :sid, :dedupe, CAST(:payload AS jsonb), :prio, :max, now() + (:delay * interval '1 second'),
            :ckey, :pv, :ov, :corr)
    ON CONFLICT (workspace_id, dedupe_key) DO NOTHING
    RETURNING id""")


async def enqueue(
    conn: AsyncConnection,
    *,
    workspace_id: uuid.UUID,
    stage: str,
    subject_type: str,
    subject_id: uuid.UUID,
    dedupe_key: str,
    payload: dict[str, Any],
    pipeline_version: str,
    ontology_version_id: uuid.UUID | None = None,
    priority: int = 100,
    delay_seconds: float = 0.0,
    concurrency_key: str | None = None,
    correlation_id: str | None = None,
    max_attempts: int | None = None,
) -> uuid.UUID | None:
    """Returns the new job id, or None when a job with this dedupe key already exists (enqueue is idempotent)."""
    if subject_type not in SUBJECT_TYPES:
        raise ValueError(f"unknown subject type {subject_type!r}")
    clean = validate_payload(payload)
    row = (
        await conn.execute(
            _INSERT,
            {
                "id": uuid7(),
                "ws": workspace_id,
                "stage": stage,
                "stype": subject_type,
                "sid": subject_id,
                "dedupe": dedupe_key,
                "payload": json.dumps(clean),
                "prio": priority,
                "max": max_attempts or policy_for(stage).max_attempts,
                "delay": delay_seconds,
                "ckey": concurrency_key,
                "pv": pipeline_version,
                "ov": ontology_version_id,
                "corr": correlation_id or context.correlation_id_var.get() or str(uuid7()),
            },
        )
    ).first()
    return None if row is None else uuid.UUID(str(row.id))
