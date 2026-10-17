"""Context Lab act-as (phase-6 plan §5.13, ADR-035, ADM-04/05).

The console builds a context AS an agent or app principal of the same workspace, through the SAME `ContextService.build`, so what an operator sees is exactly what the agent
would get: the target's roles, scope grants and classification ceiling, rebuilt from Postgres, never the operator's. This is a way to look through someone else's permissions,
so it is narrow and loud:

    caller        must hold `context.lab` (workspace admins only)
    target        must be in the caller's workspace (anything else is the same 404 as a missing id), of kind `agent` or `app`, and active; never a user, a service or an admin
    audit         `context.lab_build` (actor = the admin, target = the principal), written in the admin's own transaction before the response leaves
    trace         records `act_as_by`, and the trace inspector shows the TARGET's candidates, never the admin's wider view"""

from __future__ import annotations

import uuid

from kg_common.errors import Forbidden, NotFound
from kg_db.session import tenant_session
from kg_domain.enums import PrincipalKind, Role
from kg_domain.principal import Principal, WorkspaceContext
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

ACTABLE_KINDS = (PrincipalKind.AGENT, PrincipalKind.APP)


async def resolve_target(engine: AsyncEngine, caller: Principal, target_id: uuid.UUID) -> Principal:
    """The target as a `Principal` (no key, so it can never be used to authenticate), or NotFound / Forbidden."""
    async with tenant_session(engine, WorkspaceContext.of(caller), caller.principal_id) as conn:
        row = (
            await conn.execute(
                text(
                    "SELECT p.id, p.kind, p.handle, p.status, "
                    "COALESCE((SELECT array_agg(r.role) FROM role_assignments r WHERE r.workspace_id = p.workspace_id AND r.principal_id = p.id), ARRAY[]::text[]) AS roles "
                    "FROM principals p WHERE p.workspace_id = :ws AND p.id = :id"
                ),
                {"ws": caller.workspace_id, "id": target_id},
            )
        ).first()
    if row is None:
        raise NotFound()  # another workspace's principal is invisible under RLS: the same answer as an id that never existed
    if PrincipalKind(row.kind) not in ACTABLE_KINDS or row.status != "active" or Role.WORKSPACE_ADMIN.value in row.roles:
        raise Forbidden("That principal cannot be acted as", details={"reason": "not_actable"})
    return Principal(
        principal_id=row.id,
        workspace_id=caller.workspace_id,
        kind=PrincipalKind(row.kind),
        handle=row.handle,
        roles=frozenset(Role(r) for r in row.roles),
    )
