"""Idempotency for non-command mutations (ontology draft/publish/activate). The record is written in the SAME transaction as the mutation, so a
replay either sees both or neither. Commands use their own ledger (commands.idempotency_key)."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from kg_common.errors import IdempotencyMismatch
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


@dataclass(frozen=True)
class Replay:
    status_code: int
    response: Any


def request_hash(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


async def lock_key(conn: AsyncConnection, ws: uuid.UUID, namespace: str, key: str) -> None:
    """Serialises concurrent requests that carry the same key (released at commit)."""
    await conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"{ws}:{namespace}:{key}"})


async def begin(conn: AsyncConnection, ws: uuid.UUID, key: str, route: str, digest: str) -> Replay | None:
    """Returns the stored response if this key was already used by the same request; raises 409 idempotency_mismatch if used by a different one."""
    await lock_key(conn, ws, "idem", key)
    await conn.execute(
        text("DELETE FROM idempotency_records WHERE workspace_id = :ws AND key = :k AND expires_at <= now()"), {"ws": ws, "k": key}
    )
    row = (
        await conn.execute(
            text("SELECT route, request_hash, status_code, response FROM idempotency_records WHERE workspace_id = :ws AND key = :k"),
            {"ws": ws, "k": key},
        )
    ).first()
    if row is None:
        return None
    if row.route != route or row.request_hash != digest:
        raise IdempotencyMismatch()
    return Replay(int(row.status_code), row.response)


async def record(conn: AsyncConnection, ws: uuid.UUID, key: str, route: str, digest: str, status_code: int, response: Any) -> None:
    await conn.execute(
        text(
            "INSERT INTO idempotency_records (workspace_id, key, route, request_hash, status_code, response) "
            "VALUES (:ws, :k, :route, :h, :s, CAST(:r AS jsonb))"
        ),
        {"ws": ws, "k": key, "route": route, "h": digest, "s": status_code, "r": json.dumps(response, default=str)},
    )
