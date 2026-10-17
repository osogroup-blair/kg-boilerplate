"""Scheduling of the consolidation job (phase-8 plan §5.3): called by the memory writers after a write, so it imports nothing from them."""

from __future__ import annotations

import hashlib
import json
import uuid

from kg_common.versions import PIPELINE_VERSION
from kg_jobs.outbox import enqueue
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from kg_memory.policy import EffectivePolicy

STAGE = "memory_consolidate"
CONSOLIDATABLE = ("episodic", "semantic")  # procedural guidance is reviewed by hand, working notes expire on their own


def key_hash(mem_type: str, scopes: list[tuple[str, str]], subjects: list[uuid.UUID]) -> str:
    doc = {"t": mem_type, "sc": sorted(f"{k}:{r}" for k, r in scopes), "su": sorted(str(s) for s in subjects)}
    return hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


_MATCHING = (
    "FROM curated_memories m WHERE m.workspace_id = :ws AND m.type = :t AND m.status = 'active' AND m.frozen_at IS NULL "
    "AND COALESCE((SELECT array_agg(s.scope_kind || ':' || s.scope_ref ORDER BY s.scope_kind, s.scope_ref) FROM memory_scopes s "
    "WHERE s.workspace_id = m.workspace_id AND s.memory_id = m.id), ARRAY[]::text[]) = CAST(:scopes AS text[]) "
    "AND COALESCE((SELECT array_agg(x.entity_id::text ORDER BY x.entity_id::text) FROM memory_subjects x "
    "WHERE x.workspace_id = m.workspace_id AND x.memory_id = m.id AND x.role = 'subject'), ARRAY[]::text[]) = CAST(:subjects AS text[])"
)


async def schedule(
    conn: AsyncConnection,
    ws: uuid.UUID,
    *,
    mem_type: str,
    scopes: list[tuple[str, str]],
    subjects: list[uuid.UUID],
    policy: EffectivePolicy,
    debounce_minutes: float,
    correlation_id: str | None = None,
) -> bool:
    """Enqueues the consolidation of ONE (type, scope set, subject set) when a write leaves at least `consolidation_min_group` active memories under it. Delayed, and one job
    per key per debounce window (the dedupe key carries the window), so a burst of writes costs one job."""
    if mem_type not in CONSOLIDATABLE or policy.consolidation == "disabled":
        return False
    scope_rows = sorted(f"{k}:{r}" for k, r in scopes)
    subject_rows = sorted(str(s) for s in subjects)
    n = (
        await conn.execute(text(f"SELECT count(*) {_MATCHING}"), {"ws": ws, "t": mem_type, "scopes": scope_rows, "subjects": subject_rows})  # noqa: S608 (constant fragment)
    ).scalar_one()
    if int(n) < policy.consolidation_min_group:
        return False
    key = key_hash(mem_type, scopes, subjects)
    window = max(1.0, debounce_minutes * 60.0)
    now = (await conn.execute(text("SELECT extract(epoch FROM now())"))).scalar_one()
    bucket = int((float(now) + window) // window)
    job = await enqueue(
        conn,
        workspace_id=ws,
        stage=STAGE,
        subject_type="memory",
        subject_id=uuid.UUID(key[:32]),
        dedupe_key=f"consolidate:{key}:{bucket}",
        payload={
            "type": mem_type,
            "key": key,
            "generation": 0,
        },  # a job payload holds tokens only: the job finds the group again from the key
        pipeline_version=PIPELINE_VERSION,
        delay_seconds=window,
        correlation_id=correlation_id,
    )
    return job is not None
