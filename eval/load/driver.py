"""The load driver (phase-9 WP4): an asyncio, open-loop generator over httpx. Arrivals are Poisson at a target rate (so a slow server does not slow the offered load down, which is
what makes a latency number honest), bounded by a maximum number of requests in flight.

Scenarios: steady (70% context builds, 20% interaction ingests, 10% memory proposals), burst (10x the rate), search (memory search only) and long (long conversations for the
token-heavy normalisation and extraction path). Output per operation: count, errors, throughput, p50/p95/p99 latency; plus the freshness of every ingested event (accepted ->
terminal, polled)."""

from __future__ import annotations

import asyncio
import random
import statistics
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

TASKS = [
    "Help Sarah assess the implications of starting a business",
    "Where does Sarah work",
    "What should Sarah consider about licensing",
    "Summarise what we know about Sarah's preferences",
    "What did we decide about the planning workstream",
]
MIX = {"context": 0.70, "ingest": 0.20, "propose": 0.10}


@dataclass
class Sample:
    op: str
    ok: bool
    status: int
    seconds: float
    detail: str | None = None


@dataclass
class Run:
    scenario: str
    samples: list[Sample] = field(default_factory=list)
    events: list[tuple[str, float]] = field(default_factory=list)  # (event id, accepted at monotonic)
    started: float = 0.0
    finished: float = 0.0


def pct(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    return sorted_values[min(len(sorted_values) - 1, max(0, int(round(q * len(sorted_values))) - 1))]


def summarise(run: Run) -> dict[str, Any]:
    elapsed = max(1e-9, run.finished - run.started)
    out: dict[str, Any] = {"scenario": run.scenario, "seconds": round(elapsed, 1), "operations": {}}
    for op in sorted({s.op for s in run.samples}):
        rows = [s for s in run.samples if s.op == op]
        lat = sorted(s.seconds * 1000 for s in rows)
        errors = [s for s in rows if not s.ok]
        out["operations"][op] = {
            "count": len(rows),
            "errors": len(errors),
            "error_rate": round(len(errors) / len(rows), 4),
            "throughput_per_s": round(len(rows) / elapsed, 2),
            "p50_ms": round(pct(lat, 0.50), 1),
            "p95_ms": round(pct(lat, 0.95), 1),
            "p99_ms": round(pct(lat, 0.99), 1),
            "mean_ms": round(statistics.mean(lat), 1) if lat else 0.0,
            "statuses": {str(k): sum(1 for s in rows if s.status == k) for k in sorted({s.status for s in rows})},
        }
    return out


class Driver:
    def __init__(self, base_url: str, keys: dict[str, str], sarah: str, *, max_in_flight: int = 64, seed: int = 7):
        self.base, self.keys, self.sarah, self.max = base_url.rstrip("/"), keys, sarah, max_in_flight
        self.rng = random.Random(seed)  # noqa: S311 (a seeded workload generator, not a secret)
        self._evidence: str | None = None
        self.client = httpx.AsyncClient(
            base_url=self.base, timeout=httpx.Timeout(120.0, connect=10.0), limits=httpx.Limits(max_connections=max_in_flight + 8)
        )

    async def aclose(self) -> None:
        await self.client.aclose()

    def _h(self, who: str) -> dict[str, str]:
        return {"authorization": f"Bearer {self.keys[who]}"}

    # ---- operations ----------------------------------------------------------------------------------------------------------------------
    async def _op(self, run: Run, op: str, request: Any) -> None:
        started = time.perf_counter()
        try:
            resp = await request
            ok = resp.status_code < 400
            denied = ok and op == "context" and resp.json().get("status") == "denied"  # HTTP 200 with nothing in it: fast, and not a build
            run.samples.append(Sample(op, ok and not denied, resp.status_code, time.perf_counter() - started, "denied" if denied else None))
            if ok and op == "ingest":
                run.events.append((resp.json()["event_id"], time.monotonic()))
        except Exception as exc:
            run.samples.append(Sample(op, False, 0, time.perf_counter() - started, type(exc).__name__))

    def context(self) -> Any:
        return self.client.post(
            "/v1/context/build",
            headers=self._h("agent"),
            json={"task": self.rng.choice(TASKS), "subject_ids": [self.sarah], "project": "load-plan"},
        )

    def ingest(self, long: bool = False) -> Any:
        text = "Sarah Johnson: " + " ".join(
            f"Note {i}: we discussed planning, licensing and the NewCo timeline in detail." for i in range(60 if long else 1)
        )
        return self.client.post(
            "/v1/interactions",
            headers={**self._h("app"), "idempotency-key": str(uuid.uuid4())},
            json={
                "source_system": "load",
                "source_ref": f"m-{uuid.uuid4().hex[:10]}",
                "kind": "message",
                "content": text,
                "occurred_at": "2026-10-10T09:00:00Z",
                "subject_refs": [{"entity_id": self.sarah}],
            },
        )

    async def propose(self) -> Any:
        if (
            self._evidence is None
        ):  # a proposal must cite evidence (ProposalIn); one memory the agent can already see is enough for a load test
            found = await self.client.post("/v1/memories/search", headers=self._h("agent"), json={"query": "licensing", "limit": 1})
            found.raise_for_status()
            self._evidence = found.json()["hits"][0]["memory_id"]
        return await self.client.post(
            "/v1/memories/proposals",
            headers={**self._h("agent"), "idempotency-key": str(uuid.uuid4())},
            json={
                "type": "semantic",
                "content": f"Load note {uuid.uuid4().hex[:8]}: the licensing review is a project risk.",
                "purpose": "load",
                "scopes": [{"kind": "project", "ref": "load-plan"}],
                "memory_ids": [self._evidence],
            },
        )

    def search(self) -> Any:
        return self.client.post("/v1/memories/search", headers=self._h("admin"), json={"query": self.rng.choice(TASKS), "limit": 10})

    # ---- scenarios -----------------------------------------------------------------------------------------------------------------------
    async def run(self, scenario: str, rps: float, seconds: float) -> Run:
        run = Run(scenario)
        gate = asyncio.Semaphore(self.max)
        tasks: set[asyncio.Task[None]] = set()

        async def one(op: str) -> None:
            async with gate:
                req = {
                    "context": self.context,
                    "ingest": lambda: self.ingest(scenario == "long"),
                    "propose": self.propose,
                    "search": self.search,
                }[op]()
                await self._op(run, op, req)

        run.started = time.perf_counter()
        deadline = run.started + seconds
        while time.perf_counter() < deadline:
            if scenario == "search":
                op = "search"
            elif scenario == "long":
                op = "ingest"
            else:
                op = self.rng.choices(list(MIX), weights=list(MIX.values()))[0]
            task = asyncio.create_task(one(op))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
            await asyncio.sleep(self.rng.expovariate(rps))
        await asyncio.gather(*tasks, return_exceptions=True)
        run.finished = time.perf_counter()
        return run

    async def freshness(self, run: Run, *, timeout: float = 300.0) -> dict[str, Any]:  # noqa: ASYNC109
        """Accepted -> terminal for every ingested event of the run (polled each second, so the resolution is a second)."""
        pending = dict(run.events)
        lags: list[float] = []
        giveup = time.monotonic() + timeout
        while pending and time.monotonic() < giveup:
            for eid in list(pending):
                try:
                    resp = await self.client.get(f"/v1/interactions/{eid}", headers=self._h("admin"))
                except httpx.TransportError:
                    continue  # a keep-alive connection the server closed between polls: the next second's poll opens another
                if resp.status_code == 200 and resp.json().get("processing_status") in ("indexed", "partial", "failed"):
                    lags.append(time.monotonic() - pending.pop(eid))
            await asyncio.sleep(1.0)
        lags.sort()
        return {
            "events": len(run.events),
            "settled": len(lags),
            "unsettled": len(pending),
            "p50_s": round(pct(lags, 0.5), 1),
            "p95_s": round(pct(lags, 0.95), 1),
            "max_s": round(lags[-1], 1) if lags else 0.0,
        }
