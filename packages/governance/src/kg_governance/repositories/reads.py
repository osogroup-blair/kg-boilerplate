"""Tenant reads. Every query carries an explicit workspace_id predicate (the primary control); RLS is the backstop (T-DB-03).

A resource that exists in another workspace is indistinguishable from one that does not exist: both are NotFound (SEC-01).
"""

from __future__ import annotations

import uuid
from typing import Any

from kg_common.errors import NotFound
from kg_common.pagination import clamp_limit, decode_cursor, encode_cursor
from kg_domain.api import AuditEventOut, KeyInfo, PrincipalOut, WorkerStatus, WorkspaceOut
from kg_domain.enums import PrincipalKind, PrincipalStatus, Role, WorkspaceStatus
from kg_domain.envelope import Page
from kg_domain.principal import WorkspaceContext
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection


async def get_workspace(conn: AsyncConnection, ctx: WorkspaceContext, workspace_id: uuid.UUID | None = None) -> WorkspaceOut:
    row = (
        await conn.execute(
            text("SELECT id, slug, display_name, status FROM workspaces WHERE id = :ws AND id = :requested"),
            {"ws": ctx.workspace_id, "requested": workspace_id or ctx.workspace_id},
        )
    ).first()
    if row is None:
        raise NotFound()
    return WorkspaceOut(id=row.id, slug=row.slug, display_name=row.display_name, status=WorkspaceStatus(row.status))


_PRINCIPAL_SELECT = """
    SELECT p.id, p.handle, p.display_name, p.kind, p.status, p.created_at, p.revoked_at, p.description, p.owner_principal_id, p.trusted_source,
           p.model_disclosure, p.allowed_profiles, p.quota_requests_per_minute, p.quota_context_tokens_per_day, p.quota_interactions_per_day,
           p.revoke_reason, p.revoked_by, p.updated_at, p.revision,
           COALESCE((SELECT array_agg(r.role ORDER BY r.role) FROM role_assignments r
                      WHERE r.principal_id = p.id AND r.workspace_id = p.workspace_id), ARRAY[]::text[]) AS roles,
           COALESCE((SELECT jsonb_agg(jsonb_build_object('prefix', k.prefix, 'id', k.id, 'name', k.name, 'created_at', k.created_at, 'expires_at', k.expires_at,
                                                         'revoked_at', k.revoked_at, 'last_used_at', k.last_used_at) ORDER BY k.created_at)
                       FROM api_keys k WHERE k.principal_id = p.id AND k.workspace_id = p.workspace_id), '[]'::jsonb) AS keys
    FROM principals p
"""


def _principal_out(row: Row[Any]) -> PrincipalOut:
    return PrincipalOut(
        id=row.id,
        handle=row.handle,
        display_name=row.display_name,
        kind=PrincipalKind(row.kind),
        status=PrincipalStatus(row.status),
        roles=[Role(r) for r in row.roles],
        keys=[KeyInfo.model_validate(k) for k in row.keys],
        created_at=row.created_at,
        revoked_at=row.revoked_at,
        description=row.description,
        owner_principal_id=row.owner_principal_id,
        trusted_source=row.trusted_source,
        model_disclosure=row.model_disclosure,
        allowed_profiles=list(row.allowed_profiles),
        quota_requests_per_minute=row.quota_requests_per_minute,
        quota_context_tokens_per_day=row.quota_context_tokens_per_day,
        quota_interactions_per_day=row.quota_interactions_per_day,
        revoke_reason=row.revoke_reason,
        revoked_by=row.revoked_by,
        updated_at=row.updated_at,
        revision=row.revision,
    )


async def list_principals(
    conn: AsyncConnection, ctx: WorkspaceContext, *, limit: int | None = None, cursor: str | None = None
) -> Page[PrincipalOut]:
    size, after = clamp_limit(limit), decode_cursor(cursor)
    params: dict[str, object] = {"ws": ctx.workspace_id, "n": size + 1}
    keyset = ""
    if after:
        keyset, params["c_at"], params["c_id"] = "AND (p.created_at, p.id) > (:c_at, :c_id)", after[0], after[1]
    rows = (
        await conn.execute(text(f"{_PRINCIPAL_SELECT} WHERE p.workspace_id = :ws {keyset} ORDER BY p.created_at, p.id LIMIT :n"), params)
    ).all()  # noqa: S608 (keyset is a constant fragment)
    page, more = rows[:size], len(rows) > size
    return Page[PrincipalOut](
        items=[_principal_out(r) for r in page], next_cursor=encode_cursor(page[-1].created_at, page[-1].id) if more else None
    )


async def get_principal(conn: AsyncConnection, ctx: WorkspaceContext, principal_id: uuid.UUID) -> PrincipalOut:
    row = (
        await conn.execute(
            text(f"{_PRINCIPAL_SELECT} WHERE p.workspace_id = :ws AND p.id = :pid"), {"ws": ctx.workspace_id, "pid": principal_id}
        )
    ).first()
    if row is None:
        raise NotFound()
    return _principal_out(row)


async def principal_counts(conn: AsyncConnection, ctx: WorkspaceContext) -> dict[str, int]:
    rows = (
        await conn.execute(
            text("SELECT kind, count(*) AS n FROM principals WHERE workspace_id = :ws AND status = 'active' GROUP BY kind"),
            {"ws": ctx.workspace_id},
        )
    ).all()
    return {r.kind: int(r.n) for r in rows}


async def list_audit(
    conn: AsyncConnection, ctx: WorkspaceContext, *, limit: int | None = None, cursor: str | None = None, action: str | None = None
) -> Page[AuditEventOut]:
    """Newest first. The cursor keys on (created_at, id) descending. `action` narrows to one audit action (for example `interaction.replayed`)."""
    size, before = clamp_limit(limit), decode_cursor(cursor)
    params: dict[str, object] = {"ws": ctx.workspace_id, "n": size + 1}
    keyset = ""
    if action:
        keyset, params["action"] = "AND action = :action", action
    if before:
        keyset, params["c_at"], params["c_id"] = f"{keyset} AND (created_at, id) < (:c_at, :c_id)", before[0], before[1]
    sql = f"""
        SELECT id, action, outcome, actor_principal_id, resource_type, resource_id, request_id, created_at, metadata
        FROM audit_log WHERE workspace_id = :ws {keyset} ORDER BY created_at DESC, id DESC LIMIT :n"""  # noqa: S608 (keyset is a constant fragment)
    rows = (await conn.execute(text(sql), params)).all()  # noqa: S608 (keyset is a constant fragment)
    page, more = rows[:size], len(rows) > size
    items = [
        AuditEventOut(
            id=r.id,
            action=r.action,
            outcome=r.outcome,
            actor_principal_id=r.actor_principal_id,
            resource_type=r.resource_type,
            resource_id=r.resource_id,
            request_id=r.request_id,
            created_at=r.created_at,
            metadata=r.metadata,
        )
        for r in page
    ]
    return Page[AuditEventOut](items=items, next_cursor=encode_cursor(page[-1].created_at, page[-1].id) if more else None)


async def worker_status(conn: AsyncConnection, stale_after_s: float) -> WorkerStatus:
    """Worker heartbeats are platform-level (not tenant data). Age is measured by the database clock (the beat is stamped with now())."""
    rows = (
        await conn.execute(
            text("SELECT EXTRACT(EPOCH FROM now() - last_beat_at)::float8 AS age FROM worker_heartbeats WHERE status = 'running'")
        )
    ).all()
    if not rows:
        return WorkerStatus(status="none", last_beat_age_seconds=None, count=0)
    age = max(0.0, min(float(r.age) for r in rows))
    return WorkerStatus(status="ok" if age <= stale_after_s else "stale", last_beat_age_seconds=round(age, 1), count=len(rows))
