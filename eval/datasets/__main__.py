"""`python -m eval.datasets freeze|verify|list`. Freezing writes eval/datasets/<suite>/<version>/manifest.json (case ids, sha256 of every defining file, provenance);
verifying recomputes the hashes. Datasets are SYNTHETIC only (no real person's data), generated with a fixed seed where they are generated."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys

from eval.datasets.spec import DATASETS, ROOT, SUITES, VERSION


def sha256(path: str) -> str:
    return hashlib.sha256((ROOT / path).read_bytes()).hexdigest()


def manifest_of(suite: str) -> dict[str, object]:
    spec = SUITES[suite]
    sources = spec["sources"]()
    return {
        "suite": suite,
        "version": VERSION,
        "frozen_on": dt.date.today().isoformat(),
        "provenance": "synthetic only: no real person's data",
        "seed": 7,
        "ontology": spec["ontology"],
        "note": spec["note"],
        "cases": spec["ids"](),
        "sources": {p: sha256(p) for p in sources},
    }


def freeze(suites: list[str]) -> None:
    for suite in suites:
        m = manifest_of(suite)
        out = DATASETS / suite / VERSION
        out.mkdir(parents=True, exist_ok=True)
        (out / "manifest.json").write_text(json.dumps(m, indent=2, sort_keys=True) + "\n")
        print(f"froze {suite}/{VERSION}: {len(m['cases'])} cases, {len(m['sources'])} files")  # type: ignore[arg-type]


def verify(suites: list[str]) -> list[str]:
    problems: list[str] = []
    for suite in suites:
        path = DATASETS / suite / VERSION / "manifest.json"
        if not path.exists():
            problems.append(f"{suite}: not frozen (run `python -m eval.datasets freeze`)")
            continue
        frozen = json.loads(path.read_text())
        now = manifest_of(suite)
        changed = sorted(p for p in set(frozen["sources"]) | set(now["sources"]) if frozen["sources"].get(p) != now["sources"].get(p))  # type: ignore[attr-defined]
        if changed:
            problems.append(f"{suite}: files changed since the freeze: {', '.join(changed[:6])}")
        if frozen["cases"] != now["cases"]:
            added, removed = set(now["cases"]) - set(frozen["cases"]), set(frozen["cases"]) - set(now["cases"])  # type: ignore[call-overload]
            problems.append(f"{suite}: case ids differ (added {sorted(added)[:4]}, removed {sorted(removed)[:4]})")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["freeze", "verify", "list"])
    ap.add_argument("--suite", action="append")
    args = ap.parse_args()
    suites = args.suite or list(SUITES)
    if args.cmd == "freeze":
        freeze(suites)
        return 0
    if args.cmd == "list":
        for s in suites:
            m = manifest_of(s)
            print(f"{s:<14} {len(m['cases']):>4} cases  {len(m['sources'])} files  {m['ontology']}")  # type: ignore[arg-type]
        return 0
    problems = verify(suites)
    for p in problems:
        print("DATASET:", p, file=sys.stderr)
    print(json.dumps({"datasets": len(suites), "problems": len(problems)}))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
