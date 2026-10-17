"""Cache invalidation across processes (phase-9 plan §5 WP7, V4): LISTEN / NOTIFY on `kg_invalidate`, with no broker.

A trigger (migration 0009, `kg_notify_invalidate`) sends `<workspace uuid>:<kind>` after a change to settings, principals, grants, API-key revocation, the active ontology or the
graph namespaces. Every API and worker process holds one LISTEN connection and drops the entry of what changed. The per-process TTL caches stay as the fallback: a missed
notification (the connection was down) costs at most one TTL, and the listener drops EVERYTHING when it reconnects.

NOTIFY is delivered at commit, so a reader never sees the notification before the change. A callback must be cheap and must not raise; one that does is logged and skipped."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

import psycopg
from kg_obs import metrics
from kg_obs.logging import get_logger

log = get_logger("kg.invalidate")
CHANNEL = "kg_invalidate"
ALL = "*"  # registered for every kind; also the kind of the "reconnected, drop everything" call
Callback = Callable[[uuid.UUID | None, str], Awaitable[None] | None]


def sync_dsn(sqlalchemy_url: str) -> str:
    """`postgresql+psycopg://...` -> `postgresql://...` (psycopg's own connect string)."""
    return sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://", 1)


class Invalidator:
    def __init__(self, dsn: str, *, reconnect_seconds: float = 2.0):
        self._dsn, self._retry = dsn, reconnect_seconds
        self._callbacks: dict[str, list[Callback]] = defaultdict(list)
        self._task: asyncio.Task[None] | None = None
        self._periodic: list[tuple[float, Callable[[], Awaitable[None]]]] = []
        self._tickers: list[asyncio.Task[None]] = []
        self.connected = asyncio.Event()

    def on(self, kind: str, callback: Callback) -> None:
        self._callbacks[kind].append(callback)

    async def fire(self, workspace_id: uuid.UUID | None, kind: str) -> None:
        """Runs the callbacks for `kind` and for ALL. Used for notifications and by the process that made a change itself (so it need not wait for its own NOTIFY)."""
        for cb in (*self._callbacks.get(kind, ()), *self._callbacks.get(ALL, ())):
            try:
                result = cb(workspace_id, kind)
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                log.warning("invalidation_callback_failed", kind=kind, error_type=type(exc).__name__)

    async def handle(self, payload: str) -> None:
        ws_text, _, kind = payload.partition(":")
        try:
            ws: uuid.UUID | None = uuid.UUID(ws_text)
        except ValueError:
            ws, kind = None, payload  # unparseable: drop everything of that kind rather than nothing
        metrics.cache_invalidations_total.labels(kind=kind if kind in KINDS else "other").inc()
        await self.fire(ws, kind if kind else ALL)

    def every(self, seconds: float, refresh: Callable[[], Awaitable[None]]) -> None:
        """The TTL fallback for something the listener keeps fresh: `refresh` runs every `seconds` for as long as the invalidator runs."""
        self._periodic.append((seconds, refresh))

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="invalidation-listener")
            self._tickers = [asyncio.create_task(self._tick(sec, fn), name="invalidation-ttl") for sec, fn in self._periodic]

    async def _tick(self, seconds: float, refresh: Callable[[], Awaitable[None]]) -> None:
        while True:
            await asyncio.sleep(seconds)
            try:
                await refresh()
            except Exception as exc:
                log.warning("invalidation_refresh_failed", error_type=type(exc).__name__)

    async def aclose(self) -> None:
        if self._task is not None:
            self._task.cancel()
            for t in self._tickers:
                t.cancel()
            await asyncio.gather(self._task, *self._tickers, return_exceptions=True)
            self._task, self._tickers = None, []
        metrics.invalidation_listener_up.set(0)

    async def _run(self) -> None:
        first = True
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(self._dsn, autocommit=True) as conn:
                    await conn.execute(f"LISTEN {CHANNEL}")
                    metrics.invalidation_listener_up.set(1)
                    self.connected.set()
                    if not first:
                        await self.fire(None, "reconnected")  # notifications may have been missed while we were away
                    first = False
                    async for note in conn.notifies():
                        await self.handle(note.payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("invalidation_listener_down", error_type=type(exc).__name__)
            finally:
                metrics.invalidation_listener_up.set(0)
                self.connected.clear()
            first = False
            with contextlib.suppress(Exception):
                await asyncio.sleep(self._retry)


KINDS = frozenset({"settings", "principals", "grants", "revocation", "ontology", "namespace", "oidc", "reconnected"})


def wire(invalidator: Invalidator, **targets: Any) -> None:
    """Convenience for the composition roots: `wire(inv, egress=..., quotas=..., namespaces=...)` registers the standard reactions."""
    egress, quotas, namespaces = targets.get("egress"), targets.get("quotas"), targets.get("namespaces")
    if egress is not None:
        invalidator.on("settings", lambda ws, _k: egress.invalidate(ws))
        invalidator.on("reconnected", lambda _ws, _k: egress.invalidate(None))
    if quotas is not None:
        for kind in ("principals", "grants", "revocation"):
            invalidator.on(kind, lambda ws, _k: quotas.forget_workspace(ws))
        invalidator.on("reconnected", lambda _ws, _k: quotas.forget_workspace(None))
    if namespaces is not None:
        invalidator.on("namespace", lambda _ws, _k: namespaces.refresh())
        invalidator.on("reconnected", lambda _ws, _k: namespaces.refresh())
