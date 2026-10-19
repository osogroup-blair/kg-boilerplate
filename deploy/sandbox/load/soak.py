"""A soak test for the DEPLOYED sandbox: N synthetic visitors each start a session and run the guided tour in a loop for a while, so the caps, the pool, the reaper
and the search path are exercised together.

    python deploy/sandbox/load/soak.py https://sandbox.kg.example.com --visitors 40 --minutes 30

It needs the broker's per-address limit lifted for the address you run it from (set SANDBOX_IP_PER_HOUR high for the soak only) and Turnstile off. It prints the failure
count and p50/p95 of each call; a run is clean when there are no 5xx and p95 context builds stay inside the proposed SLO."""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from collections import defaultdict

import httpx

samples: dict[str, list[float]] = defaultdict(list)
failures: list[str] = []


async def timed(name: str, coro) -> httpx.Response:  # type: ignore[no-untyped-def]
    t = time.perf_counter()
    r = await coro
    samples[name].append(time.perf_counter() - t)
    if r.status_code >= 500:
        failures.append(f"{name}: {r.status_code}")
    return r


async def visitor(base: str, deadline: float, n: int) -> None:
    async with httpx.AsyncClient(base_url=base, timeout=120) as c:
        r = await timed("start", c.post("/sandbox/sessions", json={"scenario": "sarah"}))
        if r.status_code != 201:
            failures.append(f"visitor {n} could not start: {r.status_code} {r.text[:80]}")
            return
        s = r.json()
        admin = {"authorization": f"Bearer {s['keys']['admin']}"}
        plan = {"authorization": f"Bearer {s['keys']['planning_agent']}"}
        body = {"task": "Help Sarah assess the implications of starting a business", "subject_ids": [s["ids"]["sarah"]], "profile": "planning", "project": "newco-plan"}
        while time.time() < deadline:
            await timed("context600", c.post("/v1/context/build", headers=plan, json={**body, "max_tokens": 600}))
            await timed("context150", c.post("/v1/context/build", headers=plan, json={**body, "max_tokens": 150}))
            await timed("as_of", c.get(f"/v1/entities/{s['ids']['sarah']}/relationships", params={"as_of": "2027-03-01T00:00:00Z"}, headers=admin))
            await timed("search", c.post("/v1/memories/search", headers=plan, json={"query": "state licence", "limit": 5}))
            await timed("tour", c.get(f"/sandbox/sessions/{s['session_id']}", headers=admin))
            await asyncio.sleep(2)
        await c.delete(f"/sandbox/sessions/{s['session_id']}", headers=admin)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("base")
    ap.add_argument("--visitors", type=int, default=40)
    ap.add_argument("--minutes", type=float, default=30)
    args = ap.parse_args()
    deadline = time.time() + args.minutes * 60
    await asyncio.gather(*(visitor(args.base.rstrip("/"), deadline, i) for i in range(args.visitors)))
    print(f"{'call':<12}{'n':>7}{'p50 ms':>9}{'p95 ms':>9}")
    for name, xs in sorted(samples.items()):
        xs.sort()
        print(f"{name:<12}{len(xs):>7}{statistics.median(xs) * 1000:>9.0f}{xs[int(0.95 * (len(xs) - 1))] * 1000:>9.0f}")
    print(f"failures: {len(failures)}")
    for f in failures[:20]:
        print("  ", f)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
