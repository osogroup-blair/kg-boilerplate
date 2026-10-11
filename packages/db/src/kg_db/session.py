"""The one way application code touches tenant data (L2).

A transaction is opened on a pooled connection and the workspace is set with a TRANSACTION-LOCAL setting as the first statement.
`conn.transaction()` on a non-autocommit psycopg connection would only be a SAVEPOINT and the setting would leak to the next tenant.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from kg_domain.principal import WorkspaceContext
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

_SET_TENANT = text("SELECT set_config('app.workspace_id', :ws, true), set_config('app.principal_id', :principal, true)")


@asynccontextmanager
async def tenant_session(
    engine: AsyncEngine, ctx: WorkspaceContext, principal_id: uuid.UUID | None = None
) -> AsyncIterator[AsyncConnection]:
    """Yields a connection inside one transaction with app.workspace_id set. Commits on success, rolls back on error."""
    async with engine.connect() as conn, conn.begin():
        await conn.execute(_SET_TENANT, {"ws": str(ctx.workspace_id), "principal": str(principal_id) if principal_id else ""})
        yield conn


@asynccontextmanager
async def system_session(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """No workspace set: tenant tables return nothing (fail closed). For the worker heartbeat and the SECURITY DEFINER auth functions."""
    async with engine.connect() as conn, conn.begin():
        yield conn
