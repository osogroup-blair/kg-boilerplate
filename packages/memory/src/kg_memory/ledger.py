"""Memory writes are ledger commands (`memory.*`, R10): immutable, idempotent by `Idempotency-Key`, audited. The ledger row carries ids, codes and counts only:
`/v1/commands` does not list memory commands at all (their visibility depends on the memory's scopes), and the row never holds content."""

from __future__ import annotations

import json
import uuid
from typing import Any

from kg_common.errors import IdempotencyMismatch
from kg_governance.audit import CommandType
from kg_knowledge.idempotency import lock_key, request_hash
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


def digest_of(command_type: CommandType, principal_id: uuid.UUID, body: Any) -> str:
    """The principal is part of the hash: another caller reusing a key is a mismatch (409), never a replay of someone else's answer."""
    return request_hash(command_type.value, str(principal_id), body)


async def replay_or_none(conn: AsyncConnection, ws: uuid.UUID, key: str, command_type: CommandType, digest: str) -> dict[str, Any] | None:
    """The stored `result` of an earlier command with this key and the same request, None when the key is new; 409 when it was used for something else."""
    await lock_key(conn, ws, "cmd", key)
    prior = (
        await conn.execute(
            text("SELECT command_type, request_hash, result FROM commands WHERE workspace_id = :ws AND idempotency_key = :k"),
            {"ws": ws, "k": key},
        )
    ).first()
    if prior is None:
        return None
    if prior.request_hash != digest or prior.command_type != command_type.value:
        raise IdempotencyMismatch()
    return dict(prior.result)


async def insert_command(
    conn: AsyncConnection,
    ws: uuid.UUID,
    *,
    command_id: uuid.UUID,
    command_type: CommandType,
    key: str,
    digest: str,
    actor_principal_id: uuid.UUID,
    authority: str,
    classification: str,
    ontology_version_id: uuid.UUID | None,
    payload: dict[str, Any],
    result: dict[str, Any],
    http_status: int,
    source_system: str | None = None,
    source_ref: str | None = None,
) -> None:
    await conn.execute(
        text(
            "INSERT INTO commands (id, workspace_id, idempotency_key, request_hash, command_type, actor_principal_id, authority_class, classification, "
            "source_system, source_ref, ontology_version_id, payload, status, decision, result, http_status, projection_status) "
            "VALUES (:id, :ws, :key, :h, :ct, :actor, :auth, :cls, :ss, :sr, :ver, CAST(:payload AS jsonb), 'applied', '[]'::jsonb, CAST(:result AS jsonb), "
            ":http, 'not_applicable')"
        ),
        {
            "id": command_id,
            "ws": ws,
            "key": key,
            "h": digest,
            "ct": command_type.value,
            "actor": actor_principal_id,
            "auth": authority,
            "cls": classification,
            "ss": source_system,
            "sr": source_ref,
            "ver": ontology_version_id,
            "payload": json.dumps(payload, default=str),
            "result": json.dumps(result, default=str),
            "http": http_status,
        },
    )
