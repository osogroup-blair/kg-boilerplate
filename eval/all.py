"""`make eval-all`: every evaluation suite against the strict mock (deterministic, offline), one aggregated report with a flat metric table.

    python -m eval.all [--suite NAME ...] [--mode mock]

Writes eval/reports/<date>/all.json:
  {"date", "git", "datasets": {problems}, "suites": {name: {exit, seconds, report}}, "metrics": {"suite.metric": number}, "ok"}
`make eval-gate` compares that file with the release baseline. A suite that cannot run (a stack that is down) is a FAILED suite, never a missing one."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "eval" / "reports"
SUITES: dict[str, list[str]] = {
    "extraction": ["-m", "eval.extraction.run_eval", "--mode", "mock"],
    "memory": ["-m", "eval.memory.run_eval", "--mode", "mock"],
    "context": ["-m", "eval.context.run_eval", "--mode", "mock"],
    "consolidation": ["-m", "eval.consolidation.run_eval", "--mode", "mock"],
    "conflicts": ["-m", "eval.conflicts.run_eval"],
    "authorization": ["-m", "eval.authorization.run_eval"],
    "injection": ["-m", "eval.injection.run_eval"],
}


def git_state() -> dict[str, Any]:
    def run(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False).stdout.strip()  # noqa: S603, S607

    return {
        "commit": run("rev-parse", "HEAD"),
        "dirty": bool(run("status", "--porcelain")),
        "describe": run("describe", "--tags", "--always"),
    }


def flatten(suite: str, report: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    summary = report.get("summary") or report.get("strict") or {}
    for k, v in summary.items():
        if isinstance(v, bool):
            out[f"{suite}.{k}"] = 1.0 if v else 0.0
        elif isinstance(v, int | float):
            out[f"{suite}.{k}"] = float(v)
        elif isinstance(v, dict) and k == "latency_seconds" and "p95" in v:
            out[f"{suite}.latency_p95"] = float(v["p95"])
    if suite == "consolidation":
        out["consolidation.ok"] = 1.0 if report.get("ok") else 0.0
    return out


def newest(suite: str, day: str) -> Path | None:
    for p in sorted(REPORTS.glob(f"{suite}-mock-*.json"), reverse=True):
        if p.stem.endswith(day):
            return p
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", action="append")
    ap.add_argument("--mode", default="mock", choices=["mock"])
    args = ap.parse_args()
    names = args.suite or list(SUITES)
    day = dt.date.today().isoformat()
    from eval.datasets.__main__ import verify

    result: dict[str, Any] = {
        "date": day,
        "git": git_state(),
        "mode": args.mode,
        "datasets": {"problems": verify([n for n in names if n != "simulation"])},
        "suites": {},
        "metrics": {},
    }
    for name in names:
        started = time.monotonic()
        print(f"== {name}", flush=True)
        proc = subprocess.run([sys.executable, *SUITES[name]], cwd=ROOT, check=False)  # noqa: S603
        path = newest(name, day)
        report = json.loads(path.read_text()) if path and path.stat().st_mtime >= started - 1 else None
        result["suites"][name] = {
            "exit": proc.returncode,
            "seconds": round(time.monotonic() - started, 1),
            "report": str(path.relative_to(ROOT)) if path else None,
        }
        if report is not None:
            result["metrics"].update(flatten(name, report))
    result["ok"] = not result["datasets"]["problems"] and all(s["exit"] == 0 for s in result["suites"].values())
    out = REPORTS / day / "all.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps(
            {
                "suites": {k: v["exit"] for k, v in result["suites"].items()},
                "datasets": len(result["datasets"]["problems"]),
                "ok": result["ok"],
                "report": str(out.relative_to(ROOT)),
            }
        )
    )
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
