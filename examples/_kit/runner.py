"""Plumbing shared by the scenario examples: recorded steps, SDK clients, context builds that retry a `partial`, and a wait for the outbox to drain."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx
from kg_platform_sdk import KGClientSync

from examples._kit import admin, waits
from examples._kit.env import Env
from examples._kit.report import Report, Step
from examples._kit.workspace import Workspace

SCHEDULED_STAGES = {"memory_expire"}  # jobs scheduled for later on purpose (a working memory expires after its TTL); they are not backlog


class Runner:
    def __init__(self, env: Env, ws: Workspace, report: Report, out: Callable[[str], None] = print) -> None:
        self.env, self.ws, self.report, self.say = env, ws, report, out
        self.ids: dict[str, str] = {}
        self.data: dict[str, Any] = {}

    def step(self, sid: str, title: str, at: list[str], fn: Callable[[], dict[str, Any]]) -> None:
        self.say(f"\n[{sid}] {title}")
        try:
            evidence = fn()
            self.report.add(Step(sid, title, at, True, evidence))
            self.say(f"     pass  {evidence}")
        except Exception as exc:  # a failing step must not hide the later ones
            self.report.add(Step(sid, title, at, False, error=f"{type(exc).__name__}: {str(exc)[:900]}"))
            self.say(f"     FAIL  {type(exc).__name__}: {str(exc)[:900]}")

    def sdk(self, handle: str) -> KGClientSync:
        return KGClientSync(self.env.base_url, self.ws.keys[handle])

    def mock_requests(self) -> int:
        return len(httpx.get(f"{self.env.mock_url}/__mock/requests", timeout=10).json())

    def build(self, handle: str, **kw: Any) -> dict[str, Any]:
        with self.sdk(handle) as kg:
            for _ in range(4):  # the first graph search after a restart can outlast its source timeout (`partial`, by design): ask again
                out: dict[str, Any] = kg.build_context(**kw).model_dump(mode="json")
                if out["status"] != "partial":
                    break
                time.sleep(3)
        return out

    def settle(self) -> None:
        waits.wait_until(lambda: (not self._backlog(), self._backlog()), timeout=180, what="the outbox to drain")

    def _backlog(self) -> dict[str, int]:
        """Jobs still pending or leased, by stage and status (empty when the outbox is quiet)."""
        r = admin.get(self.ws, "/v1/operations/backlog")
        r.raise_for_status()
        return {
            f"{c['stage']}/{c['status']}": int(c["jobs"])
            for c in r.json()["cells"]
            if c["status"] in ("pending", "leased") and int(c["jobs"]) > 0 and c["stage"] not in SCHEDULED_STAGES
        }

    def ingest(self, source_ref: str, content: str, subjects: list[str], occurred_at: str, *, key: str | None = None) -> str:
        r = admin.post(
            self.ws,
            "/v1/interactions",
            {
                "source_system": "chat",
                "source_ref": source_ref,
                "kind": "message",
                "content": content,
                "occurred_at": occurred_at,
                "subject_refs": [{"entity_id": s} for s in subjects],
            },
            key=key or self.ws.keys["conversation-app"],
        )
        return str(r["event_id"])
