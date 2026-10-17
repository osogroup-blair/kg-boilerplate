"""The privacy audit view (phase-8 plan §5.8): what was revealed, read in private, debugged, acted as, deleted, held, granted, overridden or sent to a model.

A timeline over `audit_log` (whose rows are ids, codes and counts by construction) plus an aggregate of the model call log for the egress decisions. Nothing here reads content,
and nothing it returns could: there is none in the tables it reads."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from kg_common.pagination import clamp_limit, decode_cursor, encode_cursor
from kg_domain.envelope import Page
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

CATEGORIES: dict[str, tuple[str, ...]] = {
    "reveals": ("interaction.content_read", "review.evidence_read"),
    "private_reads": ("memory.private_read",),
    "debug_reads": ("context.debug_read", "context.debug_snapshot_created"),
    "act_as": ("context.lab_build",),
    "deletions": (
        "deletion.requested",
        "deletion.planned",
        "deletion.purged",
        "deletion.completed",
        "deletion.failed",
        "deletion.cancelled",
        "deletion.retained",
        "deletion.resumed",
        "deletion.extended",
        "deletion.reverify",
        "deletion.retried",
    ),
    "holds": ("legal_hold.created", "legal_hold.released", "legal_hold.release_applied", "suppression.lifted"),
    "grants": (
        "grant.added",
        "grant.removed",
        "scope.granted",
        "scope.revoked",
        "principal.revoked",
        "principal.registered",
        "principal.updated",
    ),
    "overrides": ("correction.authoritative_override", "correction.applied", "entity.split"),
    "settings": ("settings.updated",),
    "simulations": ("ontology.simulated",),
}
_CATEGORY_OF = {a: c for c, actions in CATEGORIES.items() for a in actions}


class PrivacyItem(BaseModel):
    id: uuid.UUID
    at: dt.datetime
    actor_principal_id: uuid.UUID | None
    action: str
    category: str
    resource_type: str | None
    resource_id: str | None
    outcome: str
    metadata: dict[str, Any]


class EgressRow(BaseModel):
    purpose: str
    classification: str
    outcome: str
    count: int


async def timeline(
    conn: AsyncConnection,
    ws: uuid.UUID,
    *,
    categories: list[str] | None,
    since: dt.datetime | None,
    until: dt.datetime | None,
    actor: uuid.UUID | None,
    limit: int | None,
    cursor: str | None,
) -> Page[PrivacyItem]:
    chosen = categories or list(CATEGORIES)
    actions = sorted({a for c in chosen for a in CATEGORIES.get(c, ())})
    size, before = clamp_limit(limit), decode_cursor(cursor)
    where = ["workspace_id = :ws", "action = ANY(:actions)"]
    params: dict[str, Any] = {"ws": ws, "actions": actions, "n": size + 1}
    if since:
        where.append("created_at >= :since")
        params["since"] = since
    if until:
        where.append("created_at < :until")
        params["until"] = until
    if actor:
        where.append("actor_principal_id = :actor")
        params["actor"] = actor
    if before:
        where.append("(created_at, id) < (:c_at, :c_id)")
        params["c_at"], params["c_id"] = before[0], before[1]
    rows = (
        await conn.execute(
            text(
                f"SELECT id, created_at, actor_principal_id, action, resource_type, resource_id, outcome, metadata FROM audit_log WHERE {' AND '.join(where)} ORDER BY created_at DESC, id DESC LIMIT :n"  # noqa: S608
            ),
            params,
        )
    ).all()  # noqa: S608
    page, more = rows[:size], len(rows) > size
    items = [
        PrivacyItem(
            id=r.id,
            at=r.created_at,
            actor_principal_id=r.actor_principal_id,
            action=r.action,
            category=_CATEGORY_OF.get(r.action, "other"),
            resource_type=r.resource_type,
            resource_id=r.resource_id,
            outcome=r.outcome,
            metadata=r.metadata or {},
        )
        for r in page
    ]
    return Page[PrivacyItem](items=items, next_cursor=encode_cursor(page[-1].created_at, page[-1].id) if more else None)


async def egress_summary(conn: AsyncConnection, ws: uuid.UUID, *, since: dt.datetime | None, until: dt.datetime | None) -> list[EgressRow]:
    """Calls to a model by purpose, classification and outcome (an `egress_denied` row is a request the policy, the consent or the workspace refused)."""
    rows = (
        await conn.execute(
            text(
                "SELECT purpose, classification, outcome, count(*) AS n FROM model_calls WHERE workspace_id = :ws AND (CAST(:since AS timestamptz) IS NULL OR created_at >= :since) "
                "AND (CAST(:until AS timestamptz) IS NULL OR created_at < :until) GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"
            ),
            {"ws": ws, "since": since, "until": until},
        )
    ).all()
    return [EgressRow(purpose=r.purpose, classification=r.classification, outcome=r.outcome, count=int(r.n)) for r in rows]
