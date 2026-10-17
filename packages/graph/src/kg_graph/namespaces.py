"""Which graph namespace is a workspace's ACTIVE one (phase-9 plan WP8, Flow F).

A workspace's graph lives under one Neo4j `group_id`. Normally that is `ws-<uuid>`; after a reindex cutover it is `ws-<uuid>-r<n>`. The registry holds ONLY the exceptions (the
workspaces whose active namespace is not the default), loaded from Postgres by a loader the composition root supplies (kg_graph imports no database code) and refreshed on a
`namespace` invalidation notification, at startup, and on a short TTL as the fallback. The process that performs a cutover calls `set` so it need not wait for its own notification.

Reads are fail-safe in one direction: if the table cannot be read at all the registry keeps what it had (never silently falling back to the default namespace for a workspace it
knew was elsewhere)."""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

from kg_domain.principal import WorkspaceContext

Loader = Callable[[], Awaitable[dict[uuid.UUID, str]]]


class NamespaceRegistry:
    def __init__(self, loader: Loader | None = None, *, ttl_seconds: float = 30.0):
        self._loader, self._ttl = loader, ttl_seconds
        self._overrides: dict[uuid.UUID, str] = {}
        self._loaded_at = float("-inf")

    def active(self, ctx: WorkspaceContext) -> str:
        return self._overrides.get(ctx.workspace_id, ctx.group_id)

    def set(self, workspace_id: uuid.UUID, namespace: str | None) -> None:
        """`None` (or the default) removes the override."""
        if namespace is None or namespace == f"ws-{workspace_id}":
            self._overrides.pop(workspace_id, None)
        else:
            self._overrides[workspace_id] = namespace

    @property
    def stale(self) -> bool:
        return self._loader is not None and time.monotonic() - self._loaded_at >= self._ttl

    async def refresh(self) -> None:
        if self._loader is None:
            return
        loaded = await self._loader()
        self._overrides = dict(loaded)
        self._loaded_at = time.monotonic()
