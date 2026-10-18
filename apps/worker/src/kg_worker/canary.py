"""The synthetic canary (phase-9 WP9): every `CANARY_INTERVAL_SECONDS` (300) it builds a context in a canary workspace THROUGH THE PUBLIC API, exactly as an agent would, and checks
that the build answered, that its status is acceptable and that the package is inside its budget.

    python -m kg_worker.canary          (compose service `canary`; metrics on CANARY_METRICS_PORT, default 9103)

It is a client, not a worker: it holds the canary agent's API key (`CANARY_KEY` or `CANARY_KEY_FILE`) and the API's address, nothing else. Outcomes feed
`canary_builds_total{outcome}` and `canary_last_success_unixtime`; the alert rules fire on a failed build or on a success older than 15 minutes. The canary workspace is an ordinary
tenant: an operator creates it with `kg workspaces create` and registers an agent; its content is synthetic.

Outcomes: ok (complete) · degraded (partial: a source was unavailable, the package is still valid) · failed (no answer, 5xx, 401/403, bad JSON) · budget_violation (used tokens above
the budget: must never happen, and fires the same alert as `context_budget_violations_total`)."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from pathlib import Path

import httpx
from kg_obs import metrics
from kg_obs.logging import configure_logging, get_logger
from prometheus_client import start_http_server

log = get_logger("kg.worker.canary")
ACCEPTABLE = {"complete": "ok", "partial": "degraded"}


def read_key() -> str:
    path = os.environ.get("CANARY_KEY_FILE", "")
    if path:
        return Path(path).read_text().strip()
    return os.environ.get("CANARY_KEY", "").strip()


async def probe(client: httpx.AsyncClient, *, task: str, max_tokens: int | None = None, profile: str = "quick_answer") -> str:
    """One build; returns the outcome label and never raises."""
    try:
        body: dict[str, object] = {"task": task, "profile": profile, "allow_partial": True}
        if max_tokens:  # otherwise the profile's own budget applies (a profile can cap what a request may ask for)
            body["max_tokens"] = max_tokens
        resp = await client.post("/v1/context/build", json=body)
    except httpx.HTTPError as exc:
        log.warning("canary_transport_error", error_type=type(exc).__name__)
        return "failed"
    if resp.status_code != 200:
        log.warning("canary_http_status", status=resp.status_code)
        return "failed"
    try:
        answer = resp.json()
        budgets = answer["budgets"]
        status = str(answer["status"])
        used, limit = int(budgets["used_tokens"]), int(budgets["max_tokens"])
    except (ValueError, KeyError, TypeError):
        return "failed"
    if used > limit:
        log.error("canary_budget_violation", used=used, limit=limit)
        return "budget_violation"
    return ACCEPTABLE.get(status, "failed")


async def leak_probe(client: httpx.AsyncClient, *, marker: str) -> str:
    """The scheduled synthetic leakage check: the canary agent searches for text that exists ONLY in a private memory of another canary agent. Anything that comes back
    (in a memory search or in a built context) is a cross-agent leak. Returns "clean", "leak" or "error"; never raises."""
    if not marker:
        return "skipped"
    try:
        found = await client.post("/v1/memories/search", json={"query": marker, "limit": 20})
        built = await client.post(
            "/v1/context/build",
            json={"task": f"Tell me about {marker}", "max_tokens": 600, "profile": "quick_answer", "allow_partial": True},
        )
    except httpx.HTTPError:
        return "error"
    if found.status_code >= 500 or built.status_code >= 500:
        return "error"
    return "leak" if marker in found.text or marker in built.text else "clean"


async def run(stop: asyncio.Event) -> int:
    configure_logging(level=os.environ.get("LOG_LEVEL", "INFO"), service="kg-canary")
    base, key = os.environ.get("CANARY_API_URL", "http://api:8000"), read_key()
    if not key:
        log.error("canary_no_key")
        return 2
    interval = float(os.environ.get("CANARY_INTERVAL_SECONDS", "300"))
    task = os.environ.get("CANARY_TASK", "What is the canary project status?")
    max_tokens = int(os.environ.get("CANARY_MAX_TOKENS", "0")) or None
    profile = os.environ.get("CANARY_PROFILE", "quick_answer")
    host = os.environ.get(
        "METRICS_HOST", "127.0.0.1"
    )  # production compose sets 0.0.0.0 inside the internal network; nothing publishes this port
    start_http_server(int(os.environ.get("CANARY_METRICS_PORT", "9103")), addr=host, registry=metrics.REGISTRY)
    async with httpx.AsyncClient(
        base_url=base, headers={"authorization": f"Bearer {key}"}, timeout=httpx.Timeout(60.0, connect=5.0)
    ) as client:
        while not stop.is_set():
            outcome = await probe(client, task=task, max_tokens=max_tokens, profile=profile)
            metrics.canary_builds_total.labels(outcome=outcome).inc()
            if outcome in ("ok", "degraded"):
                metrics.canary_last_success_unixtime.set(time.time())
            log.info("canary_build", outcome=outcome)
            verdict = await leak_probe(client, marker=os.environ.get("CANARY_LEAK_MARKER", ""))
            if verdict != "skipped":
                metrics.leakage_checks_total.labels(outcome=verdict).inc()
                if verdict != "error":
                    metrics.leakage_check_failed.set(1.0 if verdict == "leak" else 0.0)
                if verdict == "leak":
                    log.error("canary_leak_detected")
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                pass
    return 0


def main() -> None:
    async def _go() -> int:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        return await run(stop)

    sys.exit(asyncio.run(_go()))


if __name__ == "__main__":
    main()
