"""Workspace egress narrowing (phase-8 plan §5.6, SEC-06, ADR-047): a workspace can make the platform's egress policy STRICTER for its own data, never looser.

The platform policy (`compat/model-purposes.yaml`) says which classifications may leave at all. A workspace setting adds classifications it does not want sent, and the transport
refuses a request when EITHER layer denies it. The lookup is `ops.workspace_egress_denials(workspace)` through the system session (the transport has no tenant session), cached for
a short time. If the lookup fails and nothing is cached the request is refused: an unreadable policy is a stricter one."""

from __future__ import annotations

import time
import uuid

from kg_db.session import system_session
from kg_obs.logging import get_logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

log = get_logger("kg.modelgw.narrowing")


class PolicyUnavailable(Exception):
    pass


class WorkspaceEgress:
    def __init__(self, engine: AsyncEngine, *, ttl_seconds: float = 30.0):
        self._engine, self._ttl = engine, ttl_seconds
        self._cache: dict[str, tuple[float, frozenset[str]]] = {}

    def invalidate(self, workspace_id: str | uuid.UUID | None = None) -> None:
        """Called by the process that applied a settings change; other processes catch up when their entry expires."""
        if workspace_id is None:
            self._cache.clear()
        else:
            self._cache.pop(str(workspace_id), None)

    async def denied(self, workspace_id: str) -> frozenset[str]:
        now = time.monotonic()
        hit = self._cache.get(workspace_id)
        if hit is not None and now - hit[0] < self._ttl:
            return hit[1]
        try:
            async with system_session(self._engine) as conn:
                rows = (await conn.execute(text("SELECT ops.workspace_egress_denials(:ws)"), {"ws": uuid.UUID(workspace_id)})).scalar_one()
            value = frozenset(rows or [])
        except Exception as exc:
            log.warning("egress_policy_lookup_failed", error_type=type(exc).__name__)
            if hit is not None:
                return hit[1]
            raise PolicyUnavailable from exc
        self._cache[workspace_id] = (now, value)
        return value
