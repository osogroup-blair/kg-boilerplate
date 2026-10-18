"""Waits with a timeout that always report the last state they saw."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from examples._kit.admin import get
from examples._kit.workspace import Workspace

TERMINAL = {"indexed", "partial", "failed", "needs_review", "skipped", "rejected", "cancelled", "dead_lettered"}


def wait_until(probe: Callable[[], tuple[bool, Any]], *, timeout: float = 90, interval: float = 0.4, what: str = "condition") -> Any:  # noqa: ASYNC109
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        done, last = probe()
        if done:
            return last
        time.sleep(interval)
    raise TimeoutError(f"{what} not reached in {timeout}s; last state: {last!r}")


def wait_interaction(ws: Workspace, event_id: str, *, key: str | None = None, timeout: float = 90) -> dict[str, Any]:  # noqa: ASYNC109
    def probe() -> tuple[bool, Any]:
        d = get(ws, f"/v1/interactions/{event_id}", key=key).json()
        return d.get("processing_status") in TERMINAL, d

    return dict(wait_until(probe, timeout=timeout, what=f"interaction {event_id} terminal"))
