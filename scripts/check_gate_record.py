"""CI rule (phase-9 plan WP2): a change that can change what the models do must come with a recorded LIVE gate run for that change.

    python scripts/check_gate_record.py [--base origin/main]

Protected: the model purposes and aliases, the price table, the version pins (Graphiti, the router contract), every prompt, and the ranker and packer. If none of them differs from
the base, the check passes. Otherwise it needs eval/reports/gate/<commit>-live.json with status `passed`, where <commit> is an ancestor of HEAD and NONE of the protected files
differ between that commit and HEAD (so the record covers exactly what is being merged). Make one with `make eval-gate-live` (it costs model calls) and commit the record."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROTECTED = [
    "compat/model-purposes.yaml",
    "compat/pricing.yaml",
    "compat/versions.lock.yaml",
    "packages/memory/src/kg_memory/prompts",
    "packages/extraction/src/kg_extraction/prompts",
    "packages/context/src/kg_context/rank.py",
    "packages/context/src/kg_context/pack.py",
    "packages/context/src/kg_context/compress.py",
    "packages/graph/src/kg_graph/compile.py",
]


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=check)  # noqa: S603, S607


def changed_files(base: str) -> list[str]:
    paths = [p for p in PROTECTED if (ROOT / p).exists()]
    out = git("diff", "--name-only", f"{base}...HEAD", "--", *paths, check=False)
    return [line for line in out.stdout.splitlines() if line]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="origin/main")
    args = ap.parse_args()
    if git("rev-parse", "--verify", args.base, check=False).returncode != 0:
        print(f"base {args.base} not found: nothing to compare with, the check is skipped")
        return 0
    changed = changed_files(args.base)
    if not changed:
        print("no model-affecting file changed: no live gate record needed")
        return 0
    paths = [p for p in PROTECTED if (ROOT / p).exists()]
    for record_path in sorted((ROOT / "eval" / "reports" / "gate").glob("*-live.json"), reverse=True):
        rec = json.loads(record_path.read_text())
        commit = rec.get("commit")
        if rec.get("status") != "passed" or rec.get("mode") != "live" or not commit:
            continue
        if git("merge-base", "--is-ancestor", commit, "HEAD", check=False).returncode != 0:
            continue
        if git("diff", "--quiet", commit, "HEAD", "--", *paths, check=False).returncode == 0:
            print(f"covered by the live gate record {record_path.name} ({len(changed)} protected files changed)")
            return 0
    print(
        "a model-affecting change needs a recorded live gate run for it (`make eval-gate-live`, then commit eval/reports/gate/*-live.json):",
        file=sys.stderr,
    )
    for c in changed:
        print(f"  changed: {c}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
