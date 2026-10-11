"""Privileged writes used by the CLI / seed (and tests). Each takes a connection that is already inside a tenant_session for the workspace.

The workspace of a new row always comes from the WorkspaceContext, never from an argument that could differ from the session setting.
"""

from __future__ import annotations

import uuid

from kg_common.errors import Conflict, NotFound
from kg_common.ids import uuid7
from kg_domain.enums import PrincipalKind, Role
from kg_domain.principal import WorkspaceContext
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from kg_governance.authn.keys import MintedKey, mint_key


async def ensure_workspace(conn: AsyncConnection, ctx: WorkspaceContext, *, slug: str, display_name: str) -> bool:
    """Idempotent. Returns True when it was created."""
    res = await conn.execute(
        text("INSERT INTO workspaces (id, slug, display_name) VALUES (:id, :slug, :name) ON CONFLICT (id) DO NOTHING"),
        {"id": ctx.workspace_id, "slug": slug, "name": display_name},
    )
    return bool(res.rowcount)


async def ensure_principal(
    conn: AsyncConnection,
    ctx: WorkspaceContext,
    *,
    handle: str,
    kind: PrincipalKind,
    display_name: str,
    roles: list[Role],
    deterministic_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, bool]:
    existing = (
        await conn.execute(
            text("SELECT id FROM principals WHERE workspace_id = :ws AND handle = :h"), {"ws": ctx.workspace_id, "h": handle}
        )
    ).first()
    if existing:
        return existing.id, False
    pid = deterministic_id or uuid7()
    await conn.execute(
        text("INSERT INTO principals (id, workspace_id, kind, handle, display_name) VALUES (:id, :ws, :kind, :h, :name)"),
        {"id": pid, "ws": ctx.workspace_id, "kind": kind.value, "h": handle, "name": display_name},
    )
    for role in roles:
        await conn.execute(
            text("INSERT INTO role_assignments (principal_id, workspace_id, role) VALUES (:p, :ws, :r) ON CONFLICT DO NOTHING"),
            {"p": pid, "ws": ctx.workspace_id, "r": role.value},
        )
    return pid, True


async def principal_id_by_handle(conn: AsyncConnection, ctx: WorkspaceContext, handle: str) -> uuid.UUID:
    row = (
        await conn.execute(
            text("SELECT id FROM principals WHERE workspace_id = :ws AND handle = :h"), {"ws": ctx.workspace_id, "h": handle}
        )
    ).first()
    if row is None:
        raise NotFound(f"principal '{handle}' not found in this workspace")
    return row.id  # type: ignore[no-any-return]


async def create_key(
    conn: AsyncConnection,
    ctx: WorkspaceContext,
    *,
    principal_id: uuid.UUID,
    pepper: str,
    created_by: uuid.UUID | None = None,
    expires_in_seconds: int | None = None,
) -> tuple[uuid.UUID, MintedKey]:
    """Mints and stores a key; the plaintext is in the return value only (shown once by the caller)."""
    minted = mint_key(pepper)
    key_id = uuid7()
    await conn.execute(
        text("""
        INSERT INTO api_keys (id, workspace_id, principal_id, prefix, key_hash, created_by, expires_at)
        VALUES (:id, :ws, :p, :prefix, :hash, :by, CASE WHEN CAST(:ttl AS integer) IS NULL THEN NULL ELSE now() + make_interval(secs => :ttl) END)"""),
        {
            "id": key_id,
            "ws": ctx.workspace_id,
            "p": principal_id,
            "prefix": minted.prefix,
            "hash": minted.key_hash,
            "by": created_by,
            "ttl": expires_in_seconds,
        },
    )
    return key_id, minted


async def revoke_key(conn: AsyncConnection, ctx: WorkspaceContext, *, prefix: str) -> bool:
    res = await conn.execute(
        text("UPDATE api_keys SET revoked_at = now() WHERE workspace_id = :ws AND prefix = :p AND revoked_at IS NULL"),
        {"ws": ctx.workspace_id, "p": prefix},
    )
    return bool(res.rowcount)


async def revoke_principal(conn: AsyncConnection, ctx: WorkspaceContext, *, principal_id: uuid.UUID) -> None:
    res = await conn.execute(
        text("UPDATE principals SET status = 'revoked', revoked_at = now() WHERE workspace_id = :ws AND id = :p"),
        {"ws": ctx.workspace_id, "p": principal_id},
    )
    if not res.rowcount:
        raise Conflict("principal not found")


async def set_workspace_status(conn: AsyncConnection, ctx: WorkspaceContext, *, status: str) -> None:
    await conn.execute(text("UPDATE workspaces SET status = :s WHERE id = :ws"), {"s": status, "ws": ctx.workspace_id})
