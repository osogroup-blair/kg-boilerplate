"""Repositories for the ledger tables. Every statement carries an explicit workspace predicate (defence in depth beside RLS)."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from kg_common.errors import UnknownEntity
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from kg_ingestion.crypto import ContentHasher, Sealed
from kg_ingestion.envelope import EntityRef, ExternalRef, SubjectRef


async def advisory_lock(conn: AsyncConnection, ws: uuid.UUID, namespace: str, key: str) -> None:
    """Serialises concurrent requests that carry the same key until the transaction ends."""
    await conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"{ws}:{namespace}:{key}"})


@dataclass(frozen=True)
class ResolvedRef:
    role: str
    ordinal: int
    entity_id: uuid.UUID | None
    ref_type: str | None
    ref_system: str | None
    ref_value_hmac: str | None

    @property
    def resolved(self) -> bool:
        return self.entity_id is not None


class SubjectResolver:
    """Resolves envelope subject / actor references against the workspace's entities. An entity_id that does not exist HERE is `unknown_entity`
    (identical for a foreign and a random id: no existence oracle). An external identifier that matches nothing stays unresolved for P4 identity."""

    def __init__(self, hasher: ContentHasher):
        self._hasher = hasher

    async def resolve(self, conn: AsyncConnection, ws: uuid.UUID, role: str, refs: list[SubjectRef]) -> list[ResolvedRef]:
        out: list[ResolvedRef] = []
        for ordinal, ref in enumerate(refs):
            if isinstance(ref, EntityRef):
                row = (
                    await conn.execute(
                        text("SELECT id FROM entities WHERE workspace_id = :ws AND id = :id"), {"ws": ws, "id": ref.entity_id}
                    )
                ).first()
                if row is None:
                    raise UnknownEntity()
                out.append(ResolvedRef(role, ordinal, row.id, None, None, None))
            elif isinstance(ref, ExternalRef):
                row = (
                    await conn.execute(
                        text(
                            "SELECT entity_id FROM entity_external_ids WHERE workspace_id = :ws AND type_key = :t AND system = :s AND value = :v"
                        ),
                        {"ws": ws, "t": ref.type, "s": ref.system, "v": ref.value},
                    )
                ).first()
                out.append(
                    ResolvedRef(
                        role,
                        ordinal,
                        row.entity_id if row else None,
                        ref.type,
                        ref.system,
                        self._hasher.ref(ws, ref.type, ref.system, ref.value),
                    )
                )
        return out


async def insert_interaction(conn: AsyncConnection, values: dict[str, Any]) -> None:
    await conn.execute(
        text(
            "INSERT INTO interactions (id, workspace_id, source_system, source_ref, idempotency_key, request_hmac, kind, content_type, occurred_at, "
            "occurred_at_unknown, submitted_by, classification, consent_external, pipeline_version, ontology_version_id, processing_status, byte_size, token_estimate) "
            "VALUES (:id, :ws, :ss, :sr, :key, :h, :kind, :ct, :occ, :unk, :by, :cls, :consent, :pv, :ov, 'accepted', :bytes, :tokens)"
        ),
        values,
    )


async def insert_content(
    conn: AsyncConnection, ws: uuid.UUID, interaction_id: uuid.UUID, form: str, sealed: Sealed, content_hmac: str, byte_size: int
) -> bool:
    """False when the form already exists (a re-run of a deterministic stage): the first write wins and the row is kept."""
    row = (
        await conn.execute(
            text(
                "INSERT INTO interaction_contents (interaction_id, workspace_id, form, kid, nonce, ciphertext, content_hmac, byte_size) "
                "VALUES (:i, :ws, :form, :kid, :nonce, :ct, :h, :n) ON CONFLICT (interaction_id, form) DO NOTHING RETURNING form"
            ),
            {
                "i": interaction_id,
                "ws": ws,
                "form": form,
                "kid": sealed.kid,
                "nonce": sealed.nonce,
                "ct": sealed.ciphertext,
                "h": content_hmac,
                "n": byte_size,
            },
        )
    ).first()
    return row is not None


async def read_content(conn: AsyncConnection, ws: uuid.UUID, interaction_id: uuid.UUID, form: str) -> Sealed | None:
    row = (
        await conn.execute(
            text(
                "SELECT kid, nonce, ciphertext FROM interaction_contents WHERE workspace_id = :ws AND interaction_id = :i AND form = :form"
            ),
            {"ws": ws, "i": interaction_id, "form": form},
        )
    ).first()
    return None if row is None else Sealed(row.kid, bytes(row.nonce), bytes(row.ciphertext))


async def insert_subjects(conn: AsyncConnection, ws: uuid.UUID, interaction_id: uuid.UUID, refs: list[ResolvedRef]) -> None:
    for r in refs:
        await conn.execute(
            text(
                "INSERT INTO interaction_subjects (interaction_id, workspace_id, role, ordinal, entity_id, ref_type, ref_system, ref_value_hmac, resolved) "
                "VALUES (:i, :ws, :role, :ord, :eid, :rt, :rs, :rh, :res)"
            ),
            {
                "i": interaction_id,
                "ws": ws,
                "role": r.role,
                "ord": r.ordinal,
                "eid": r.entity_id,
                "rt": r.ref_type,
                "rs": r.ref_system,
                "rh": r.ref_value_hmac,
                "res": r.resolved,
            },
        )


async def insert_stage(
    conn: AsyncConnection,
    ws: uuid.UUID,
    interaction_id: uuid.UUID,
    stage: str,
    status: str,
    pipeline_version: str,
    ontology_version_id: uuid.UUID | None,
) -> None:
    finished = status in ("skipped_by_policy", "skipped_disabled")
    await conn.execute(
        text(
            "INSERT INTO interaction_stages (interaction_id, workspace_id, stage, generation, status, pipeline_version, ontology_version_id, finished_at) "
            "VALUES (:i, :ws, :stage, 0, :status, :pv, :ov, CASE WHEN :fin THEN now() END)"
        ),
        {
            "i": interaction_id,
            "ws": ws,
            "stage": stage,
            "status": status,
            "pv": pipeline_version,
            "ov": ontology_version_id,
            "fin": finished,
        },
    )


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
