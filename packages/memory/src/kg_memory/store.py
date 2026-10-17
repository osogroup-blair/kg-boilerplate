"""Repositories for the memory tables. Every statement carries an explicit workspace predicate (defence in depth beside RLS); nothing here decides policy.

Content is plaintext (curated, minimised, scope-protected; ADR-029) so lexical search works. It is never logged: this module has no logger on purpose."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Any

from kg_governance.grants import SubjectFilter, entity_visible_sql
from kg_ingestion.crypto import ContentHasher
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

_SPACES = re.compile(r"\s+")
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def normalise_text(value: str) -> str:
    return _SPACES.sub(" ", unicodedata.normalize("NFC", value)).strip()


def derive_summary(content: str, limit: int = 160) -> str:
    """A summary is optional on input: the first sentence, cut to size. It is derived from the content, never from anything else."""
    flat = normalise_text(content)
    first = _SENTENCE.split(flat, maxsplit=1)[0]
    return first if len(first) <= limit else first[: limit - 1].rstrip() + "…"


def candidate_hmac(
    hasher: ContentHasher, ws: uuid.UUID, mem_type: str, content: str, subject_ids: list[uuid.UUID], scopes: list[tuple[str, str]]
) -> str:
    """Keyed hash of (type, normalised content, subjects, scopes). Scopes are part of it so that the same words written to a private scope and to a shared one
    are two memories, and so a duplicate check can never reveal a memory the writer could not already see."""
    doc = {
        "t": mem_type,
        "c": normalise_text(content).lower(),
        "s": sorted(str(s) for s in subject_ids),
        "sc": sorted(f"{k}:{r}" for k, r in scopes),
    }
    return hasher.digest(ws, json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in vector) + "]"


@dataclass(frozen=True)
class EvidenceRef:
    kind: str  # interaction | episode | fact | command | memory | external_ref
    interaction_id: uuid.UUID | None = None
    episode_uuid: str | None = None
    fact_id: uuid.UUID | None = None
    command_id: uuid.UUID | None = None
    memory_id: uuid.UUID | None = None
    external_ref: str | None = None


@dataclass
class NewMemory:
    """Everything needed to insert one memory. Built by the writers and by the extraction stage; inserted by `insert_new`."""

    type: str
    origin: str
    content: str
    summary: str
    status: str
    scopes: list[tuple[str, str]]
    created_by: uuid.UUID
    authority: str
    evidence_status: str
    classification: str
    importance: float
    pipeline_version: str
    subject_ids: list[uuid.UUID] = field(default_factory=list)
    topic_ids: list[uuid.UUID] = field(default_factory=list)
    evidence: list[EvidenceRef] = field(default_factory=list)
    lineage: list[tuple[uuid.UUID, str]] = field(default_factory=list)  # (related memory, relation): THIS memory `relation` the related one
    assertion: dict[str, Any] | None = None
    confidence: float | None = None
    occurred_at: dt.datetime | None = None
    effective_from: dt.datetime | None = None
    effective_until: dt.datetime | None = None
    ttl_seconds: float | None = None  # working memory: the DB clock adds this to now() (in `persist`, which sets `expires_at`)
    expires_at: dt.datetime | None = None
    run_id: str | None = None
    purpose: str | None = None
    derived_by_alias: str | None = None
    prompt_version: str | None = None
    embedding_status: str = "pending"
    hmac: str = ""


# ---- writes ----------------------------------------------------------------------------------------------------------------------------------

_INSERT_MEMORY = text("""
    INSERT INTO curated_memories (id, workspace_id, type, origin, content, summary, assertion, status, importance, confidence, authority_class, evidence_status,
        classification, created_by, command_id, derived_by_alias, pipeline_version, prompt_version, purpose, occurred_at, effective_from, effective_until,
        expires_at, embedding_status, candidate_hmac, run_id)
    VALUES (:id, :ws, :type, :origin, :content, :summary, CAST(:assertion AS jsonb), :status, :importance, :confidence, :authority, :evidence_status,
        :classification, :created_by, :command_id, :alias, :pv, :prompt, :purpose, :occurred_at, COALESCE(:effective_from, now()), :effective_until,
        :expires_at, :embedding_status, :hmac, :run_id)
    RETURNING recorded_at, effective_from, expires_at""")


async def insert_new(conn: AsyncConnection, ws: uuid.UUID, memory_id: uuid.UUID, command_id: uuid.UUID, nm: NewMemory) -> Row[Any]:
    row = (
        await conn.execute(
            _INSERT_MEMORY,
            {
                "id": memory_id,
                "ws": ws,
                "type": nm.type,
                "origin": nm.origin,
                "content": nm.content,
                "summary": nm.summary,
                "assertion": json.dumps(nm.assertion, default=str) if nm.assertion else None,
                "status": nm.status,
                "importance": nm.importance,
                "confidence": nm.confidence,
                "authority": nm.authority,
                "evidence_status": nm.evidence_status,
                "classification": nm.classification,
                "created_by": nm.created_by,
                "command_id": command_id,
                "alias": nm.derived_by_alias,
                "pv": nm.pipeline_version,
                "prompt": nm.prompt_version,
                "purpose": nm.purpose,
                "occurred_at": nm.occurred_at,
                "effective_from": nm.effective_from,
                "effective_until": nm.effective_until,
                "expires_at": nm.expires_at,
                "embedding_status": nm.embedding_status,
                "hmac": nm.hmac,
                "run_id": nm.run_id,
            },
        )
    ).one()
    for kind, ref in nm.scopes:
        await conn.execute(
            text(
                "INSERT INTO memory_scopes (memory_id, workspace_id, scope_kind, scope_ref) VALUES (:m, :ws, :k, :r) ON CONFLICT DO NOTHING"
            ),
            {"m": memory_id, "ws": ws, "k": kind, "r": ref},
        )
    for role, ids in (("subject", nm.subject_ids), ("topic", nm.topic_ids)):
        for entity_id in dict.fromkeys(ids):
            await conn.execute(
                text(
                    "INSERT INTO memory_subjects (memory_id, entity_id, workspace_id, role) VALUES (:m, :e, :ws, :role) ON CONFLICT DO NOTHING"
                ),
                {"m": memory_id, "e": entity_id, "ws": ws, "role": role},
            )
    for ev in nm.evidence:
        await add_evidence(conn, ws, memory_id, ev, nm.created_by)
    for related, relation in nm.lineage:
        await conn.execute(
            text(
                "INSERT INTO memory_lineage (memory_id, related_id, workspace_id, relation) VALUES (:m, :r, :ws, :rel) ON CONFLICT DO NOTHING"
            ),
            {"m": memory_id, "r": related, "ws": ws, "rel": relation},
        )
    return row


async def add_evidence(conn: AsyncConnection, ws: uuid.UUID, memory_id: uuid.UUID, ev: EvidenceRef, added_by: uuid.UUID | None) -> bool:
    """Adds one evidence row; True when it is new (an independent source), False when this memory already cites it."""
    row = (
        await conn.execute(
            text(
                "INSERT INTO memory_evidence (id, memory_id, workspace_id, source_kind, interaction_id, episode_uuid, fact_id, command_id, source_memory_id, "
                "external_ref, added_by) VALUES (gen_random_uuid(), :m, :ws, :k, :i, :ep, :f, :c, :sm, :x, :by) ON CONFLICT (memory_id, evidence_key) DO NOTHING RETURNING id"
            ),
            {
                "m": memory_id,
                "ws": ws,
                "k": ev.kind,
                "i": ev.interaction_id,
                "ep": ev.episode_uuid,
                "f": ev.fact_id,
                "c": ev.command_id,
                "sm": ev.memory_id,
                "x": ev.external_ref,
                "by": added_by,
            },
        )
    ).first()
    return row is not None


async def set_status(
    conn: AsyncConnection,
    ws: uuid.UUID,
    memory_id: uuid.UUID,
    status: str,
    *,
    close_validity: bool = False,
    confirm: bool = False,
) -> int:
    """Moves a memory to a new status and bumps its revision. `close_validity` ends its validity now (superseded / expired): never before it began."""
    row = (
        await conn.execute(
            text(
                "UPDATE curated_memories SET status = :st, revision = revision + 1, "
                "effective_until = CASE WHEN :close THEN GREATEST(effective_from, LEAST(COALESCE(effective_until, now()), now())) ELSE effective_until END, "
                "expires_at = CASE WHEN :close AND expires_at IS NOT NULL AND expires_at > now() THEN now() ELSE expires_at END, "
                "last_confirmed_at = CASE WHEN :confirm THEN now() ELSE last_confirmed_at END "
                "WHERE workspace_id = :ws AND id = :id RETURNING revision"
            ),
            {"st": status, "close": close_validity, "confirm": confirm, "ws": ws, "id": memory_id},
        )
    ).one()
    return int(row.revision)


# ---- reads -----------------------------------------------------------------------------------------------------------------------------------


async def load_row(conn: AsyncConnection, ws: uuid.UUID, memory_id: uuid.UUID, *, lock: bool = False) -> Row[Any] | None:
    return (
        await conn.execute(
            text(f"SELECT * FROM curated_memories WHERE workspace_id = :ws AND id = :id{' FOR UPDATE' if lock else ''}"),  # noqa: S608 (constant fragment)
            {"ws": ws, "id": memory_id},
        )
    ).first()


async def load_scopes(conn: AsyncConnection, ws: uuid.UUID, memory_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[tuple[str, str]]]:
    out: dict[uuid.UUID, list[tuple[str, str]]] = {m: [] for m in memory_ids}
    if not memory_ids:
        return out
    rows = (
        await conn.execute(
            text(
                "SELECT memory_id, scope_kind, scope_ref FROM memory_scopes WHERE workspace_id = :ws AND memory_id = ANY(:ids) ORDER BY scope_kind, scope_ref"
            ),
            {"ws": ws, "ids": memory_ids},
        )
    ).all()
    for r in rows:
        out[r.memory_id].append((r.scope_kind, r.scope_ref))
    return out


_SUBJECTS_HEAD = (
    "SELECT ms.memory_id, ms.entity_id, ms.role FROM memory_subjects ms JOIN entities e ON e.workspace_id = ms.workspace_id AND e.id = ms.entity_id "
    "WHERE ms.workspace_id = :ws AND ms.memory_id = ANY(:ids)"
)


async def load_subjects(
    conn: AsyncConnection, ws: uuid.UUID, memory_ids: list[uuid.UUID], *, subjects: SubjectFilter | None = None
) -> dict[uuid.UUID, dict[str, list[uuid.UUID]]]:
    """The entities each memory is about. With a `subjects` filter, entities the caller may not see are omitted (P7: the memory stays visible by its scopes; the id of a
    hidden person does not travel with it). Writers pass no filter: they need the true list."""
    out: dict[uuid.UUID, dict[str, list[uuid.UUID]]] = {m: {"subject": [], "topic": []} for m in memory_ids}
    if not memory_ids:
        return out
    acl, acl_params = entity_visible_sql(subjects, alias="e") if subjects is not None else ("TRUE", {})
    sql = f"{_SUBJECTS_HEAD} AND {acl} ORDER BY ms.entity_id"  # noqa: S608 (constant fragments; the filter is bound parameters)
    rows = (await conn.execute(text(sql), {"ws": ws, "ids": memory_ids, **acl_params})).all()
    for r in rows:
        out[r.memory_id][r.role].append(r.entity_id)
    return out


async def load_evidence(conn: AsyncConnection, ws: uuid.UUID, memory_id: uuid.UUID) -> list[Row[Any]]:
    return list(
        (
            await conn.execute(
                text("SELECT * FROM memory_evidence WHERE workspace_id = :ws AND memory_id = :m ORDER BY added_at, id"),
                {"ws": ws, "m": memory_id},
            )
        ).all()
    )


async def load_evidence_many(conn: AsyncConnection, ws: uuid.UUID, memory_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[Row[Any]]]:
    out: dict[uuid.UUID, list[Row[Any]]] = {m: [] for m in memory_ids}
    rows = (
        await conn.execute(
            text("SELECT * FROM memory_evidence WHERE workspace_id = :ws AND memory_id = ANY(:ids) ORDER BY added_at, id"),
            {"ws": ws, "ids": memory_ids},
        )
    ).all()
    for r in rows:
        out[r.memory_id].append(r)
    return out


async def evidence_count(conn: AsyncConnection, ws: uuid.UUID, memory_id: uuid.UUID) -> int:
    return int(
        (
            await conn.execute(
                text("SELECT count(*) FROM memory_evidence WHERE workspace_id = :ws AND memory_id = :m"), {"ws": ws, "m": memory_id}
            )
        ).scalar_one()
    )


def stable_id(*parts: str) -> uuid.UUID:
    """A deterministic id from a few strings (an extraction's memory id is derived from its interaction, generation and candidate, so a re-run finds its own work)."""
    digest = hashlib.sha256("|".join(parts).encode()).digest()[:16]
    return uuid.UUID(bytes=digest, version=5)
