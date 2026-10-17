"""The injection evaluation: the SEC-04 / AT-15 suites run over the frozen corpus (tests/fixtures/injection/corpus.yaml), counted.

    make eval-injection

STRICT: `failures` must be 0 - not one payload may change a role, a grant, an authority class, a review decision, a policy, a deletion plan or the purpose of a model call, and each
must stay inside the delimiters of a context. The assertions are the security tests' own; this runner makes their outcome a number the release gate can hold.
Output: eval/reports/injection-mock-<date>.json."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
REPORTS = ROOT / "eval" / "reports"
TESTS = ["tests/security/test_injection_corpus.py", "tests/security/test_context_injection.py"]


def main() -> int:
    payloads = len(yaml.safe_load((ROOT / "tests/fixtures/injection/corpus.yaml").read_text())["payloads"])
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", *TESTS, "-q", "-p", "no:cacheprovider", "--tb=line"],
        cwd=ROOT,
        env={**os.environ, "KG_REQUIRE_STACK": "1"},
        capture_output=True,
        text=True,
        check=False,
    )
    tail = (proc.stdout or "").strip().splitlines()[-1:] or [""]
    passed = int(m.group(1) if (m := re.search(r"(\d+) passed", tail[0])) else 0)
    failed = int(m.group(1) if (m := re.search(r"(\d+) failed", tail[0])) else 0) + int(
        m.group(1) if (m := re.search(r"(\d+) error", tail[0])) else 0
    )
    summary = {"payloads": payloads, "tests_passed": passed, "failures": failed if passed or failed else 1}
    problems = [f"{summary['failures']} injection checks failed"] if summary["failures"] else []
    report = {"mode": "mock", "date": dt.date.today().isoformat(), "summary": summary, "pytest": tail[0], "problems": problems}
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / f"injection-mock-{report['date']}.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(summary))
    if problems:
        print(proc.stdout[-2000:], file=sys.stderr)
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
