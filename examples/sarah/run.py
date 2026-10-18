"""`make demo-sarah`: the Sarah two-agent scenario in a fresh workspace, with a pass/fail report."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from examples._kit.env import ROOT, load_env
from examples._kit.report import Report
from examples._kit.workspace import fresh_workspace
from examples.sarah.steps import Scenario

ONTOLOGY = ROOT / "examples" / "ontologies" / "personal-knowledge-governance.yaml"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true", help="owner action: use the real router instead of the mock")
    ap.add_argument("--reports", type=Path, default=ROOT / "examples" / "sarah" / "reports")
    args = ap.parse_args()
    env = load_env(live=args.live)
    ws = fresh_workspace(env, "sarah", ontology=ONTOLOGY)
    print(f"workspace {ws.slug}")
    report = Report("Sarah two-agent scenario", "live" if env.live else "mock")
    Scenario(env, ws, report).run_all()
    import time

    report.write(args.reports, f"sarah-{time.strftime('%Y-%m-%d')}")
    print(f"\n{'PASS' if report.passed else 'FAIL'}: {sum(s.passed for s in report.steps)}/{len(report.steps)} steps")
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
