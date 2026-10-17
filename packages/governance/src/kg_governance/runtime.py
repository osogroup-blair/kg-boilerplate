"""Composition helpers shared by the API and the worker for the P9 cross-process pieces: the graph namespace loader and the invalidation listener."""

from __future__ import annotations

import uuid
from typing import Any

from kg_common.settings import Settings
from kg_db.session import system_session
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from kg_governance.notify import Invalidator, sync_dsn, wire


async def namespace_overrides(engine: AsyncEngine) -> dict[uuid.UUID, str]:
    """The workspaces whose ACTIVE graph namespace is not `ws-<uuid>` (ops.graph_namespace_overrides: workspace ids and namespaces, nothing else)."""
    async with system_session(engine) as conn:
        rows = (await conn.execute(text("SELECT workspace_id, namespace FROM ops.graph_namespace_overrides()"))).all()
    return {r.workspace_id: str(r.namespace) for r in rows}


def build_invalidator(
    settings: Settings, *, egress: Any = None, quotas: Any = None, namespaces: Any = None, ttl_seconds: float = 30.0
) -> Invalidator | None:
    """None when the listener is switched off (INVALIDATION_LISTENER=false): the TTL caches then carry on alone, as before P9."""
    if not settings.invalidation_listener:
        return None
    inv = Invalidator(sync_dsn(settings.database_url_app.get_secret_value()))
    wire(inv, egress=egress, quotas=quotas, namespaces=namespaces)
    if namespaces is not None:
        inv.every(ttl_seconds, namespaces.refresh)
    return inv
