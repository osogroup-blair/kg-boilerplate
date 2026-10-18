"""The baseline runner (phase-9 WP4, DEC-11): `python -m eval.load.run up | seed | baseline | down`  (or `make perf-baseline`).

* `up`        the `kg-perf` compose project (its own ports and volumes) with the mock router's response-time model switched on
* `seed`      the S/M/L workspaces (+ small background workspaces) with bulk SQL; credentials to .local/perf/credentials.json
* `baseline`  the scenarios against the M workspace, the size curve over S/M/L, freshness, cost per event, storage per 1k events, worker throughput per stage; writes
              eval/reports/baseline-<date>.{json,md,svg}
* `down`      removes the project and its volumes

The latency model is PROVISIONAL until the live session captures the router's real one (WP1 step g); a report made with the provisional model says so on its first line. The
SLOs in the report are PROPOSALS derived from the measurements; the owner sets the values (DEC-11) in docs/release/decisions.md and the alert rules."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import psycopg

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from eval.load.data import SIZES, Seeded, save_credentials, seed  # noqa: E402
from eval.load.driver import Driver, pct, summarise  # noqa: E402

PROJECT = "kg-perf"
WORK = ROOT / ".local" / "perf"
PORTS = {
    "KG_PG_PORT": "5744",
    "KG_NEO4J_HTTP_PORT": "7675",
    "KG_NEO4J_BOLT_PORT": "7888",
    "KG_MOCK_PORT": "8982",
    "KG_API_PORT": "8200",
    "KG_API_METRICS_PORT": "9300",
    "KG_WORKER_METRICS_PORT": "9301",
}
API = f"http://127.0.0.1:{PORTS['KG_API_PORT']}"
OWNER = f"postgresql://kg_owner:kg_owner_dev@127.0.0.1:{PORTS['KG_PG_PORT']}/kg"
PROFILE_PATH = "/app/tools/mock_router/latency.provisional.json"
REPORTS = ROOT / "eval" / "reports"


def compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env_file = WORK / "perf.env"
    return subprocess.run(  # noqa: S603
        ["docker", "compose", "-p", PROJECT, "-f", str(ROOT / "deploy/compose/docker-compose.yml"), "--env-file", str(env_file), *args],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=check,
    )


def write_env(profile: str) -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / "faults").mkdir(exist_ok=True)
    (WORK / "faults").chmod(0o777)
    (WORK / "journal").mkdir(exist_ok=True)
    (WORK / "journal").chmod(0o777)
    base = (ROOT / ".env.example").read_text()
    lines = [ln for ln in base.splitlines() if not any(ln.startswith(k + "=") for k in PORTS)]
    lines += [f"{k}={v}" for k, v in PORTS.items()]
    lines += [
        f"KG_MOCK_LATENCY_PROFILE={profile}",
        f"KG_JOURNAL_HOST_DIR={(WORK / 'journal').resolve()}",
        f"KG_FAULTS_HOST_DIR={(WORK / 'faults').resolve()}",
        "RECONCILE_SCAN_HOURS=0",
    ]
    (WORK / "perf.env").write_text("\n".join(lines) + "\n")


def cmd_up(args: argparse.Namespace) -> int:
    write_env(PROFILE_PATH if args.latency == "provisional" else "")
    out = compose("up", "-d", "--build", "--wait")
    print(out.stderr[-300:])
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    if (WORK / "perf.env").exists():
        compose("down", "-v", "--remove-orphans", check=False)
    return 0


def settings_for_perf() -> Any:
    from tests.conftest import make_test_settings

    return make_test_settings(
        database_url_app=f"postgresql+psycopg://kg_app:kg_app_dev@127.0.0.1:{PORTS['KG_PG_PORT']}/kg",
        database_url_owner=f"postgresql+psycopg://kg_owner:kg_owner_dev@127.0.0.1:{PORTS['KG_PG_PORT']}/kg",
        auth_key_pepper="dev-only-pepper-change-me-32-bytes-min",
    )


async def cmd_seed_async(args: argparse.Namespace) -> int:
    settings = settings_for_perf()
    seeded: list[Seeded] = []
    sizes = [s for s in args.sizes.split(",") if s]
    for size in sizes:
        started = time.monotonic()
        s = await seed(settings, OWNER.replace("postgresql://", "postgresql+psycopg://"), f"perf-{size.lower()}", size)
        print(
            f"seeded {s.slug}: {s.entities} entities, {s.facts} facts, {s.memories} memories in {time.monotonic() - started:.0f}s",
            flush=True,
        )
        seeded.append(s)
    for i in range(args.background):  # many small tenants beside the big ones: the realistic shape of a shared database
        seeded.append(await seed(settings, OWNER.replace("postgresql://", "postgresql+psycopg://"), f"perf-bg-{i}", "S"))
    save_credentials(WORK / "credentials.json", seeded)
    return 0


def role_keys(keys: dict[str, str]) -> dict[str, str]:
    """'perf-m/agent@perf-m' -> 'agent'."""
    return {k.split("/")[-1].split("@")[0]: v for k, v in keys.items()}


def sql(query: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with psycopg.connect(OWNER, autocommit=True) as conn:
        return list(conn.execute(query, params).fetchall())  # type: ignore[arg-type]


def db_state() -> dict[str, Any]:
    size = sql("SELECT pg_database_size('kg')")[0][0]
    return {"pg_bytes": int(size), "interactions": int(sql("SELECT count(*) FROM interactions")[0][0]), "t": time.time()}


def cost_window(since: dt.datetime) -> dict[str, Any]:
    rows = sql(
        "SELECT purpose, count(*), COALESCE(sum(est_cost_usd), 0)::float8, COALESCE(sum(prompt_tokens), 0), COALESCE(sum(completion_tokens), 0) FROM model_calls WHERE created_at >= %s GROUP BY purpose",
        (since,),
    )
    return {
        r[0]: {"calls": int(r[1]), "usd": round(float(r[2]), 6), "prompt_tokens": int(r[3]), "completion_tokens": int(r[4])} for r in rows
    }


def stage_throughput(since: dt.datetime, seconds: float) -> dict[str, float]:
    rows = sql("SELECT stage, count(*) FROM outbox_jobs WHERE status = 'succeeded' AND finished_at >= %s GROUP BY stage", (since,))
    return {r[0]: round(int(r[1]) / max(1.0, seconds), 2) for r in rows}


async def drain_outbox(*, timeout: float) -> None:  # noqa: ASYNC109
    """Waits until no job is pending or leased (jobs that are only retrying later do not hold the curve back for more than `timeout`)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        busy = sql(
            "SELECT count(*) FROM outbox_jobs WHERE status IN ('pending', 'leased') AND available_at <= now() + interval '5 seconds'"
        )[0][0]
        if int(busy) == 0:
            await asyncio.sleep(5)  # let caches and autovacuum settle
            return
        await asyncio.sleep(5)


async def timed(driver: Driver, op: str, n: int) -> dict[str, float]:
    lat = []
    for _ in range(n):
        while True:
            started = time.perf_counter()
            resp = await getattr(driver, op)()
            elapsed = (time.perf_counter() - started) * 1000
            if resp.status_code == 429:  # the platform's own limiter: wait out the window (not part of the measurement) and measure again
                await asyncio.sleep(max(1.0, float(resp.json().get("error", {}).get("retry_after") or 1)))
                continue
            break
        assert resp.status_code < 400, resp.text
        lat.append(elapsed)
    lat.sort()
    return {"n": n, "p50_ms": round(statistics.median(lat), 1), "p95_ms": round(pct(lat, 0.95), 1), "p99_ms": round(pct(lat, 0.99), 1)}


async def cmd_baseline_async(args: argparse.Namespace) -> int:
    creds = json.loads((WORK / "credentials.json").read_text())
    main = creds[f"perf-{args.size.lower()}"]
    d = Driver(API, role_keys(main["keys"]), main["sarah"])
    report: dict[str, Any] = {
        "date": dt.date.today().isoformat(),
        "latency_model": "provisional (not measured)" if args.latency == "provisional" else "live profile",
        "dataset": {"size": args.size, **dict(zip(("entities", "facts", "memories"), SIZES[args.size], strict=True))},
        "scenarios": {},
    }
    try:
        for name, rps, seconds in (
            ("steady", args.rps, args.seconds),
            ("burst", args.rps * 10, args.burst_seconds),
            ("search", args.rps * 2, 40),
            ("long", max(0.5, args.rps / 4), 40),
        ):
            since_dt, before = dt.datetime.now(dt.UTC), db_state()
            print(f"== {name} at {rps:g}/s for {seconds}s", flush=True)
            run = await d.run(name, rps, seconds)
            summary = summarise(run)
            summary["target_rps"] = rps
            if run.events:
                summary["freshness"] = await d.freshness(run, timeout=args.settle)
            after = db_state()
            events = max(1, len(run.events))
            summary["storage"] = (
                {"postgres_bytes_per_1k_events": int((after["pg_bytes"] - before["pg_bytes"]) / events * 1000)} if run.events else {}
            )
            summary["cost"] = cost_window(since_dt)
            summary["cost_per_event_usd"] = round(sum(v["usd"] for v in summary["cost"].values()) / events, 6) if run.events else None
            summary["worker_throughput_per_s"] = stage_throughput(since_dt, time.time() - before["t"])
            report["scenarios"][name] = summary
        await drain_outbox(
            timeout=600.0
        )  # the size curve measures an idle platform: the scenarios' own backlog (extraction, embedding) must be done first
        curve = {}
        for size in ("S", "M", "L"):
            if f"perf-{size.lower()}" not in creds:
                continue
            c = creds[f"perf-{size.lower()}"]
            dd = Driver(API, role_keys(c["keys"]), c["sarah"])
            try:
                curve[size] = {
                    "memories": c["memories"],
                    "search": await timed(dd, "search", 40),
                    "context": await timed(dd, "context", 30),
                }
            finally:
                await dd.aclose()
        report["size_curve"] = curve
    finally:
        await d.aclose()
    report["ann_trigger"] = ann_check(report["size_curve"])
    report["proposed_slos"] = propose_slos(report)
    write_reports(report)
    return 0


def ann_check(curve: dict[str, Any]) -> dict[str, Any]:
    """Memory-search p95 against workspace size: the ANN trigger is >= 200k memories in a workspace or p95 > 100 ms (ADR-019, ADR-031)."""
    points = [(v["memories"], v["search"]["p95_ms"]) for v in curve.values()]
    out: dict[str, Any] = {"trigger": "200000 memories in one workspace, or a memory-search p95 above 100 ms", "points": points}
    if len(points) >= 2:
        n = len(points)
        sx, sy = sum(p[0] for p in points), sum(p[1] for p in points)
        sxx, sxy = sum(p[0] ** 2 for p in points), sum(p[0] * p[1] for p in points)
        denom = n * sxx - sx * sx
        slope = (n * sxy - sx * sy) / denom if denom else 0.0
        intercept = (sy - slope * sx) / n
        out["fit_ms"] = {"intercept": round(intercept, 2), "per_100k_memories": round(slope * 100_000, 2)}
        out["memories_at_100ms"] = int((100 - intercept) / slope) if slope > 0 else None
    biggest = max(points) if points else None
    out["needed_before_L"] = bool(biggest and biggest[1] > 100)
    return out


def round_up(x: float, step: float) -> float:
    return float(-(-x // step) * step)


def propose_slos(r: dict[str, Any]) -> dict[str, Any]:
    steady = r["scenarios"].get("steady", {})
    ctx = steady.get("operations", {}).get("context", {})
    fresh = steady.get("freshness", {})
    slos: dict[str, Any] = {}
    if ctx:
        slos["context_build_p95_ms"] = {
            "measured": ctx["p95_ms"],
            "proposed": round_up(ctx["p95_ms"] * 1.5, 50),
            "at": f"{r['dataset']['size']} dataset, {steady['target_rps']:g} req/s mixed",
        }
    if fresh:
        slos["freshness_p95_s"] = {"measured": fresh["p95_s"], "proposed": round_up(fresh["p95_s"] * 2, 5)}
    errs = [op["error_rate"] for sc in r["scenarios"].values() for op in sc.get("operations", {}).values()]
    slos["api_error_rate"] = {"measured_max": max(errs) if errs else None, "proposed": 0.001}
    slos["events_terminal"] = {"proposed": "99% of events indexed or needs_review within the freshness SLO; zero silent loss"}
    return slos


def svg(report: dict[str, Any]) -> str:
    rows = [
        (f"{sc}/{op}", v["p50_ms"], v["p95_ms"], v["p99_ms"])
        for sc, s in report["scenarios"].items()
        for op, v in s.get("operations", {}).items()
    ]
    width, bar_h, left = 900, 18, 220
    scale = (width - left - 60) / max(1.0, max((r[3] for r in rows), default=1.0))
    height = 30 + len(rows) * (bar_h * 3 + 14)
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" font-family="sans-serif" font-size="11">',
        f'<text x="10" y="16" font-weight="bold">Latency by scenario/operation (ms): p50 / p95 / p99 ({report["latency_model"]})</text>',
    ]
    y = 30
    for label, p50, p95, p99 in rows:
        out.append(f'<text x="10" y="{y + bar_h}">{label}</text>')
        for i, (v, colour) in enumerate(((p50, "#4c78a8"), (p95, "#f58518"), (p99, "#e45756"))):
            out.append(
                f'<rect x="{left}" y="{y + i * bar_h}" width="{max(1.0, v * scale):.1f}" height="{bar_h - 3}" fill="{colour}"/><text x="{left + v * scale + 4:.1f}" y="{y + i * bar_h + 11}">{v}</text>'
            )
        y += bar_h * 3 + 14
    out.append("</svg>")
    return "\n".join(out)


def markdown(r: dict[str, Any]) -> str:
    lines = [
        f"# Baseline {r['date']}",
        "",
        f"**Latency model: {r['latency_model']}.**"
        + (
            " These numbers describe the platform plus a MODELLED router; they are provisional until the live profile is captured."
            if r["latency_model"].startswith("provisional")
            else ""
        ),
        "",
        f"Dataset: {r['dataset']['size']} ({r['dataset']['entities']} entities, {r['dataset']['facts']} facts, {r['dataset']['memories']} memories) in a workspace beside the others of the shared database.",
        "",
        "![latency](baseline-" + r["date"] + ".svg)",
        "",
        "| Scenario | Operation | n | errors | /s | p50 ms | p95 ms | p99 ms |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for sc, s in r["scenarios"].items():
        for op, v in s["operations"].items():
            lines.append(
                f"| {sc} ({s['target_rps']:g}/s) | {op} | {v['count']} | {v['errors']} | {v['throughput_per_s']} | {v['p50_ms']} | {v['p95_ms']} | {v['p99_ms']} |"
            )
    lines += [
        "",
        "## Freshness (accepted to terminal)",
        "",
        "| Scenario | events | settled | p50 s | p95 s | max s |",
        "|---|---|---|---|---|---|",
    ]
    for sc, s in r["scenarios"].items():
        if "freshness" in s:
            f = s["freshness"]
            lines.append(f"| {sc} | {f['events']} | {f['settled']} | {f['p50_s']} | {f['p95_s']} | {f['max_s']} |")
    lines += [
        "",
        "## Cost and storage",
        "",
        "| Scenario | cost per event (USD) | Postgres bytes per 1k events | worker throughput (jobs/s by stage) |",
        "|---|---|---|---|",
    ]
    for sc, s in r["scenarios"].items():
        lines.append(
            f"| {sc} | {s.get('cost_per_event_usd')} | {s.get('storage', {}).get('postgres_bytes_per_1k_events')} | {json.dumps(s.get('worker_throughput_per_s'))} |"
        )
    lines += [
        "",
        "## Size curve",
        "",
        "| Size | memories | search p50 | search p95 | context p50 | context p95 |",
        "|---|---|---|---|---|---|",
    ]
    for size, v in r["size_curve"].items():
        lines.append(
            f"| {size} | {v['memories']} | {v['search']['p50_ms']} | {v['search']['p95_ms']} | {v['context']['p50_ms']} | {v['context']['p95_ms']} |"
        )
    a = r["ann_trigger"]
    lines += [
        "",
        "## ANN trigger",
        "",
        f"Trigger: {a['trigger']}. Fit: {a.get('fit_ms')}. Memories at 100 ms: {a.get('memories_at_100ms')}. **ANN needed before L: {a['needed_before_L']}.**",
        "",
        "## Proposed SLOs (the owner sets the values)",
        "",
    ]
    for k, v in r["proposed_slos"].items():
        lines.append(f"- **{k}**: {json.dumps(v)}")
    return "\n".join(lines) + "\n"


def write_reports(report: dict[str, Any]) -> None:
    REPORTS.mkdir(parents=True, exist_ok=True)
    base = REPORTS / f"baseline-{report['date']}"
    base.with_suffix(".json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    base.with_suffix(".md").write_text(markdown(report))
    base.with_suffix(".svg").write_text(svg(report))
    print(markdown(report))
    print(f"report: {base.with_suffix('.md').relative_to(ROOT)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("up")
    up.add_argument("--latency", choices=["provisional", "off"], default="provisional")
    sub.add_parser("down")
    sd = sub.add_parser("seed")
    sd.add_argument("--sizes", default="S,M")
    sd.add_argument("--background", type=int, default=5)
    bl = sub.add_parser("baseline")
    bl.add_argument("--size", default="M")
    bl.add_argument("--rps", type=float, default=4.0)
    bl.add_argument("--seconds", type=float, default=90.0)
    bl.add_argument("--burst-seconds", type=float, default=30.0)
    bl.add_argument("--settle", type=float, default=240.0)
    bl.add_argument("--latency", choices=["provisional", "off"], default="provisional")
    args = ap.parse_args()
    if args.cmd == "up":
        return cmd_up(args)
    if args.cmd == "down":
        return cmd_down(args)
    if args.cmd == "seed":
        return asyncio.run(cmd_seed_async(args))
    return asyncio.run(cmd_baseline_async(args))


if __name__ == "__main__":
    raise SystemExit(main())

_ = os
