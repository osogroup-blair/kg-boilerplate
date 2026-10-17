"""`make eval-gate`: compare an `eval-all` run with the release baseline (eval/baselines/release.json) and block regressions.

    python -m eval.gate [--report eval/reports/<date>/all.json] [--write-baseline]

* STRICT metrics must EQUAL their required value (leakage 0, budget adherence 1.0, attribution 1.0, every strict counter 0, every policy mismatch 0 ...). No tolerance.
* PROPOSED metrics may not regress beyond tolerance: relevance -0.02 (higher is better) / +0.02 (lower is better), cost +10%, p95 latency +15% (and at least 0.25 s of slack:
  the mock is fast, so a few milliseconds of noise must not fail a release).
* The datasets must be the frozen ones: a file that defines a suite changed since its freeze, or a baseline hash that differs, is a failure until the owner re-freezes and
  re-baselines on purpose (`--write-baseline`, which is an explicit act and prints what it replaced).

`--live` marks a gate run made against the real router (`make eval-gate-live`); its report is the record the CI rule requires for a change to a model alias, a price, a prompt, a
ranker or packer version, or a Graphiti pin (scripts/check_gate_record.py)."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "eval" / "baselines" / "release.json"
GATE_DIR = ROOT / "eval" / "reports" / "gate"
TOLERANCE = {"relevance": 0.02, "cost": 0.10, "p95": 0.15, "p95_slack_seconds": 0.25}

STRICT: dict[str, float] = {
    "context.leakage": 0,
    "context.budget_adherence": 1.0,
    "context.attribution": 1.0,
    "memory.escalation_violations": 0,
    "memory.unmapped_subject_leaks": 0,
    "memory.procedural_auto_activations": 0,
    "memory.scope_violations": 0,
    "extraction.missed_reverts": 0,
    "extraction.cross_workspace_links": 0,
    "extraction.unquarantined_unparseable": 0,
    "consolidation.merged_distinct": 0,
    "consolidation.crossed_scope": 0,
    "consolidation.missed_duplicates": 0,
    "consolidation.bad_accepted": 0,
    "consolidation.good_refused": 0,
    "consolidation.merged_distinct_escaped": 0,
    "consolidation.escalations_escaped": 0,
    "conflicts.mismatches": 0,
    "authorization.policy_mismatches": 0,
    "authorization.leakage": 0,
    "authorization.server_errors": 0,
    "authorization.foreign_mutations": 0,
    "authorization.frozen_leaks": 0,
    "authorization.deleted_leaks": 0,
    "injection.failures": 0,
    # the suites' own pass/fail booleans (1.0 = held)
    "context.status_ok": 1.0,
    "context.structure_intact": 1.0,
    "context.required_stable": 1.0,
    "context.unavailable_ok": 1.0,
    "extraction.entities_ok": 1.0,
    "extraction.reviews_ok": 1.0,
    "extraction.reverts_ok": 1.0,
    "extraction.stage_outcomes_ok": 1.0,
    "memory.drop_reasons_ok": 1.0,
    "memory.reviews_ok": 1.0,
    "memory.stage_outcomes_ok": 1.0,
    "consolidation.ok": 1.0,
}
HIGHER = [
    "context.evidence_recall_cases_1_8",
    "context.temporal_correctness",
    "extraction.fact_precision",
    "extraction.fact_recall",
    "extraction.status_correct",
    "extraction.valid_time_correct",
    "extraction.identity_correct",
    "memory.candidate_precision",
    "memory.candidate_recall",
    "memory.recall_at_3",
]
LOWER = ["context.irrelevant_inclusion", "context.wrongly_included", "memory.distractor_in_top3"]
COST = [
    "extraction.chat_calls_per_episode",
    "extraction.tokens_per_episode",
    "context.tokens_per_call",
    "memory.model_calls_per_event",
    "memory.tokens_per_event",
]
LATENCY = ["extraction.latency_p95", "context.latency_p95", "memory.latency_p95"]


def latest_report() -> Path:
    days = sorted(p for p in (ROOT / "eval" / "reports").iterdir() if p.is_dir() and (p / "all.json").exists())
    if not days:
        raise SystemExit("no eval-all report: run `make eval-all` first")
    return days[-1] / "all.json"


def compare(current: dict[str, Any], baseline: dict[str, Any]) -> list[str]:
    metrics: dict[str, float] = current["metrics"]
    tol = {**TOLERANCE, **baseline.get("tolerances", {})}
    problems: list[str] = []
    for name, want in baseline["strict"].items():
        got = metrics.get(name)
        if got is None:
            problems.append(f"STRICT {name}: not measured (the suite did not run)")
        elif abs(got - want) > 1e-9:
            problems.append(f"STRICT {name}: {got} (required {want})")
    for name, base in baseline["proposed"].items():
        got = metrics.get(name)
        if got is None:
            problems.append(f"{name}: not measured")
            continue
        if name in HIGHER and got < base - tol["relevance"]:
            problems.append(f"{name}: {got} regressed from {base} (tolerance -{tol['relevance']})")
        elif name in LOWER and got > base + tol["relevance"]:
            problems.append(f"{name}: {got} regressed from {base} (tolerance +{tol['relevance']})")
        elif name in COST and got > base * (1 + tol["cost"]) + 1e-9:
            problems.append(f"{name}: {got} is more than {int(tol['cost'] * 100)}% above {base}")
        elif name in LATENCY and got > max(base * (1 + tol["p95"]), base + tol["p95_slack_seconds"]):
            problems.append(f"{name}: {got}s is more than {int(tol['p95'] * 100)}% above {base}s")
    frozen = baseline.get("datasets", {})
    for problem in current["datasets"]["problems"]:
        problems.append(f"DATASET {problem}")
    for suite, hashes in frozen.items():
        manifest = ROOT / "eval" / "datasets" / suite / "v1" / "manifest.json"
        if manifest.exists() and json.loads(manifest.read_text())["sources"] != hashes:
            problems.append(f"DATASET {suite}: the frozen files differ from the release baseline (re-freeze and re-baseline on purpose)")
    failed = [s for s, v in current["suites"].items() if v["exit"] != 0]
    problems += [f"SUITE {s}: exited non-zero" for s in failed]
    return problems


def write_baseline(current: dict[str, Any]) -> dict[str, Any]:
    metrics = current["metrics"]
    datasets = {}
    for suite in {n.split(".")[0] for n in metrics} | {"conflicts", "authorization", "injection"}:
        manifest = ROOT / "eval" / "datasets" / suite / "v1" / "manifest.json"
        if manifest.exists():
            datasets[suite] = json.loads(manifest.read_text())["sources"]
    return {
        "version": 1,
        "created": dt.date.today().isoformat(),
        "git": current["git"],
        "strict": {k: v for k, v in STRICT.items() if k in metrics},
        "proposed": {k: metrics[k] for k in [*HIGHER, *LOWER, *COST, *LATENCY] if k in metrics},
        "tolerances": TOLERANCE,
        "datasets": datasets,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", type=Path)
    ap.add_argument("--write-baseline", action="store_true")
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args()
    path = args.report or latest_report()
    current = json.loads(path.read_text())
    if args.write_baseline:
        new = write_baseline(current)
        old = json.loads(BASELINE.read_text()) if BASELINE.exists() else None
        BASELINE.parent.mkdir(parents=True, exist_ok=True)
        BASELINE.write_text(json.dumps(new, indent=2, sort_keys=True) + "\n")
        print(f"baseline written from {path.relative_to(ROOT)}" + ("" if old is None else f" (replacing the one of {old['created']})"))
        return 0
    baseline = json.loads(BASELINE.read_text())
    problems = compare(current, baseline)
    record = {
        "kind": "eval_gate_live" if args.live else "eval_gate",
        "mode": "live" if args.live else "mock",
        "date": dt.date.today().isoformat(),
        "commit": current["git"]["commit"],
        "dirty": current["git"]["dirty"],
        "status": "failed" if problems else "passed",
        "report": str(path.relative_to(ROOT)),
        "problems": problems,
    }
    GATE_DIR.mkdir(parents=True, exist_ok=True)
    (GATE_DIR / f"{current['git']['commit'][:12]}-{record['mode']}.json").write_text(json.dumps(record, indent=2) + "\n")
    for p in problems:
        print("GATE:", p, file=sys.stderr)
    print(
        json.dumps(
            {"status": record["status"], "problems": len(problems), "metrics": len(current["metrics"]), "baseline": baseline["created"]}
        )
    )
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
