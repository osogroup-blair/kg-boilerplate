"""`make release-candidate` (phase-9 WP12): every gate, in order, from a clean tree, with one report that links the evidence.

    python scripts/release_candidate.py [--allow-dirty] [--fail-fast] [--skip STEP ...] [--only STEP ...]

Steps (each a `make` target, so a failure reproduces by hand): reset-and-demos · check · test-release · test-resilience · test-e2e · examples · eval · scan · obs-test · prod-smoke · backup ·
restore-rehearsal · baseline. It never stops at the first failure unless --fail-fast: the owner needs the whole picture. Writes docs/release/rc-report-<date>.md (status per step, the
duration, the artefacts to open) and exits non-zero if any step failed. It does NOT tag: the owner signs docs/release/checklist.md first, then runs `git tag v1.0.0-rc1`.

`reset-and-demos` drops the development volumes (`make reset`): do not run this against a stack you want to keep."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TODAY = dt.date.today().isoformat()
DEMOS = [f"demo-p{n}" for n in range(1, 9)]
STEPS: list[tuple[str, list[str], str]] = [
    ("reset-and-demos", ["reset", "up", "seed", *DEMOS], "the eight phase demos pass from a clean reset"),
    ("check", ["check"], "ruff, mypy strict, import-linter, unit/integration/security/contract tests, admin Vitest"),
    ("test-release", ["test-release"], f"eval/reports/{TODAY}/release-sweep.md (SEC-01..06, AT-01..18, two API replicas)"),
    ("test-resilience", ["test-resilience"], "FI-01..23 (tests/resilience); FI-20 and FI-22 are emulated"),
    ("test-e2e", ["test-e2e"], "Playwright operator flows"),
    (
        "examples",
        ["examples-install", "examples", "test-examples"],
        "examples/*/reports (Sarah 11/11, CRM benchmark, agents 5/5) and the seven notebooks run top to bottom",
    ),
    ("eval", ["eval-all", "eval-gate"], "eval/reports/gate/ (strict metrics exact, proposed inside tolerance)"),
    ("scan", ["scan"], f"eval/reports/{TODAY}/scan.json, SBOMs in eval/reports/sbom/"),
    ("obs-test", ["obs-test"], "deploy/observability/rules_test.yml"),
    ("prod-smoke", ["prod-smoke"], "docker-compose.prod.yml under kg-prod-smoke"),
    ("backup", ["backup", "backup-verify"], "an encrypted set, verified against its manifest"),
    (
        "restore-rehearsal",
        ["restore-rehearsal"],
        f"eval/reports/restore-{TODAY}.md (AT-18: journal re-applied, residuals 0, reconcile clean; RTO/RPO)",
    ),
    ("baseline", ["perf-down", "perf-up", "perf-seed", "perf-baseline"], f"eval/reports/baseline-{TODAY}.md (M size, mock latency model)"),
]


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False).stdout.strip()  # noqa: S603,S607


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--allow-dirty", action="store_true")
    ap.add_argument("--fail-fast", action="store_true")
    ap.add_argument("--skip", nargs="*", default=[])
    ap.add_argument("--only", nargs="*", default=[])
    args = ap.parse_args()
    dirty = bool(git("status", "--porcelain"))
    if dirty and not args.allow_dirty:
        print("the working tree is not clean: commit first (a release candidate is a tagged commit), or pass --allow-dirty for a dry run")
        return 2
    results: list[dict[str, object]] = []
    for name, targets, evidence in STEPS:
        if (args.only and name not in args.only) or name in args.skip:
            results.append({"step": name, "status": "skipped", "seconds": 0, "evidence": evidence})
            continue
        print(f"\n===== {name}: make {' '.join(targets)}", flush=True)
        started = time.time()
        rc = subprocess.run(["make", *targets], cwd=ROOT, check=False).returncode  # noqa: S603,S607
        results.append(
            {
                "step": name,
                "status": "passed" if rc == 0 else "FAILED",
                "seconds": round(time.time() - started),
                "evidence": evidence,
                "exit": rc,
            }
        )
        if rc != 0 and args.fail_fast:
            break
    live = sorted((ROOT / "eval" / "reports" / "live").glob("*/summary.md")) if (ROOT / "eval" / "reports" / "live").exists() else []
    failed = [r["step"] for r in results if r["status"] == "FAILED"]
    lines = [
        f"# Release candidate report {TODAY}",
        "",
        f"Commit `{git('rev-parse', '--short', 'HEAD')}` (`{git('describe', '--tags', '--always')}`), working tree {'DIRTY (dry run)' if dirty else 'clean'}. Result: **{'FAILED: ' + ', '.join(map(str, failed)) if failed else 'every executed step passed'}**.",
        "",
        "| Step | Status | Time | Evidence |",
        "|---|---|---|---|",
        *[f"| {r['step']} | {r['status']} | {r['seconds']} s | {r['evidence']} |" for r in results],
        "",
        "## Not automated (owner actions)",
        f"- Live session: {'latest ' + str(live[-1].relative_to(ROOT)) if live else '**not run yet** (`make live-session`, review, `make live-session-apply`)'}",
        "- Owner decisions (SLOs, retention): `docs/release/decisions.md`",
        "- Threat review sign-off: `docs/security/threat-model.md` §5",
        "- Sign `docs/release/checklist.md`, then `git tag v1.0.0-rc1`.",
        "",
    ]
    out = ROOT / "docs" / "release" / f"rc-report-{TODAY}.md"
    out.write_text("\n".join(lines))
    print(f"\nreport: {out.relative_to(ROOT)}")
    print(json.dumps({"failed": failed}))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
