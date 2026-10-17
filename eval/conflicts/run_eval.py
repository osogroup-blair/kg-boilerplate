"""The conflicts and identity evaluation: runs every case of the frozen dataset through the PURE policy code (no stack, no router, deterministic).

    make eval-conflicts

Strict: `mismatches` must be 0 (every case, every authority pair, every overlap mode). Output: eval/reports/conflicts-mock-<date>.json."""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from kg_extraction.conflicts import FactView, Incoming, Invalidation, PolicyInputs, resolve  # noqa: E402
from kg_extraction.identity import conflicting_strong_ids, names_compatible  # noqa: E402

CASES = ROOT / "eval" / "datasets" / "conflicts" / "v1" / "cases.json"
REPORTS = ROOT / "eval" / "reports"
UTC = dt.UTC


def d(y: int, m: int, day: int) -> dt.datetime:
    return dt.datetime(y, m, day, tzinfo=UTC)


def authority_case(c: dict[str, Any]) -> dict[str, object]:
    mode = c["overlap"]
    if mode == "disjoint" or mode == "invalidated_disjoint":
        old = FactView("old", c["existing"], d(2023, 1, 1), d(2023, 12, 31))
        new = Incoming(c["incoming"], d(2024, 6, 1))
    elif mode == "invalidated_dated":
        old = FactView("old", c["existing"], d(2024, 1, 1), None)
        new = Incoming(c["incoming"], d(2024, 6, 1))
    else:  # rule_overlap (dated, but no invalidation: not a succession) and invalidated_undated (no dates at all)
        old = FactView("old", c["existing"], d(2024, 1, 1) if mode == "rule_overlap" else None, None)
        new = Incoming(c["incoming"], d(2024, 6, 1) if mode == "rule_overlap" else None)
    inputs = PolicyInputs(
        rule_conflict=mode in ("rule_overlap", "disjoint"), invalidated=(Invalidation("old"),) if mode.startswith("invalidated") else ()
    )
    res = resolve(new, [old], inputs)
    return {"status": res.status, "code": res.code, "review": res.review, "ends": len(res.end_facts)}


def run() -> dict[str, Any]:
    data = json.loads(CASES.read_text())
    mismatches: list[dict[str, Any]] = []
    for c in data["cases"]:
        if c["kind"] == "authority":
            got: dict[str, object] = authority_case(c)
        elif c["kind"] == "name":
            got = {"compatible": names_compatible(c["a"], c["b"])}
        else:
            got = {"conflict": conflicting_strong_ids(c["a"].items(), c["b"].items())}
        if got != c["expect"]:
            mismatches.append({"id": c["id"], "expected": c["expect"], "got": got})
    by_kind = {k: sum(1 for c in data["cases"] if c["kind"] == k) for k in ("authority", "name", "external_ids")}
    summary = {"cases": len(data["cases"]), "mismatches": len(mismatches), **{f"cases_{k}": v for k, v in by_kind.items()}}
    return {
        "mode": "mock",
        "date": dt.date.today().isoformat(),
        "summary": summary,
        "mismatches": mismatches,
        "problems": [f"{len(mismatches)} cases differ"] if mismatches else [],
    }


def main() -> int:
    report = run()
    REPORTS.mkdir(parents=True, exist_ok=True)
    base = REPORTS / f"conflicts-mock-{report['date']}"
    base.with_suffix(".json").write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(json.dumps(report["summary"]))
    for m in report["mismatches"][:10]:
        print("MISMATCH", m, file=sys.stderr)
    return 0 if not report["problems"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
