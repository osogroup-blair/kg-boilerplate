"""`make test-release` (phase-9 plan WP6, V10): the release sweep.

Runs the leak suites, the P1-P8 route walkers, the injection corpus, the egress matrix, revocation, the trigger gates, pooled reuse, the telemetry sentinels and the
acceptance-relevant integration suites ONCE, against TWO API replicas (`KG_REPLICAS=2`: two independent apps with their own pools, caches and invalidation listeners behind
a round-robin client, one shared Postgres-backed limiter), and writes a report keyed by SEC-01..06 and AT-01..18:

    eval/reports/<date>/release-sweep.json      machine-readable (per id: covering tests, passed, failed, status)
    eval/reports/<date>/release-sweep.md        the same, for people

Which ids a test covers comes from the test's own source (an `SEC-nn` / `AT-nn` token in its body or its module docstring) plus the explicit table EXTRA below. AT-18 is
satisfied by a passing restore rehearsal report from the release candidate's own backups (eval/reports/restore-*.md). Any failure, and any id with no covering test, fails the run.
Strict blockers (AT-01, AT-08, AT-10, AT-11, AT-15, AT-16 read block) are called out.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UV = os.environ.get("UV") or str(ROOT / ".tools" / "uvenv" / "bin" / "uv")
SUITES = [
    "tests/security",
    "tests/contract",
    "tests/integration/test_api_contract.py",
    "tests/integration/test_db.py",
    "tests/integration/test_db_p2.py",
    "tests/integration/test_db_p3.py",
    "tests/integration/test_db_p4.py",
    "tests/integration/test_db_p5.py",
    "tests/integration/test_db_p7.py",
    "tests/integration/test_db_p8.py",
    "tests/integration/test_p2_commands.py",
    "tests/integration/test_p3_interactions.py",
    "tests/integration/test_p4_extraction.py",
    "tests/integration/test_p4_reviews.py",
    "tests/integration/test_p5_memory.py",
    "tests/integration/test_p6_api.py",
    "tests/integration/test_p6_context.py",
    "tests/integration/test_p6_traces.py",
    "tests/integration/test_p7_publish.py",
    "tests/integration/test_p7_quotas.py",
    "tests/integration/test_p7_registration.py",
    "tests/integration/test_p7_revocation.py",
    "tests/integration/test_p8_corrections.py",
    "tests/integration/test_p8_simulation.py",
    "tests/integration/test_p8_deletion.py",
    "tests/integration/test_p8_deletion_more.py",
    "tests/integration/test_p8_graph_deletion.py",
    "tests/integration/test_p8_privacy_audit.py",
    "tests/integration/test_p8_settings.py",
    "tests/integration/test_p9_hardening.py",
    "tests/integration/test_p9_shared.py",
]
STRICT = ["AT-01", "AT-08", "AT-10", "AT-11", "AT-15", "AT-16"]
IDS = [f"SEC-{n:02d}" for n in range(1, 7)] + [f"AT-{n:02d}" for n in range(1, 19)]
TOKEN = re.compile(r"\b(SEC|AT)-(\d{2})\b")
NAMED = re.compile(r"(?:^|_)(sec|at)(\d{2})(?:_|$)")  # test_at06_the_aspiration_memory… names the id it covers
# ids a whole file covers although its tests do not name them (the file is the contract for that property)
EXTRA: dict[str, list[str]] = {
    "tests/security/test_isolation.py": ["SEC-01", "AT-11"],
    "tests/security/test_p2_isolation.py": ["SEC-01", "AT-11"],
    "tests/security/test_p3_isolation.py": ["SEC-01", "AT-11"],
    "tests/security/test_p4_isolation.py": ["SEC-01", "AT-11"],
    "tests/security/test_p5_isolation.py": ["SEC-01", "SEC-02", "AT-10"],
    "tests/security/test_p6_isolation.py": ["SEC-01", "SEC-02", "AT-10"],
    "tests/security/test_p7_isolation.py": ["SEC-01", "AT-10"],
    "tests/security/test_memory_leaks.py": ["SEC-02", "AT-10"],
    "tests/security/test_context_leaks.py": ["SEC-02", "AT-10"],
    "tests/security/test_fact_acl_leaks.py": ["SEC-02", "AT-10"],
    "tests/security/test_revocation.py": ["SEC-03"],
    "tests/integration/test_p7_revocation.py": ["SEC-03"],
    "tests/security/test_injection_corpus.py": ["SEC-04", "AT-15"],
    "tests/security/test_context_injection.py": ["SEC-04", "AT-15"],
    "tests/security/test_deletion_read_block.py": ["SEC-05", "AT-16"],
    "tests/security/test_deletion_residuals.py": ["SEC-05", "AT-16"],
    "tests/security/test_egress_matrix.py": ["SEC-06"],
    "tests/integration/test_p8_settings.py": ["SEC-06"],
    "tests/contract/test_context_budget.py": ["AT-08"],
    "tests/integration/test_p3_interactions.py": ["AT-01"],
}


def ids_by_test(path: Path) -> tuple[set[str], dict[str, set[str]]]:
    """(ids named by the module docstring and comments outside functions, {test name: ids named in its source})."""
    text = path.read_text()
    tree = ast.parse(text)
    lines = text.splitlines()
    per_test: dict[str, set[str]] = {}
    covered = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            seg = "\n".join(lines[node.lineno - 1 - len(node.decorator_list) : node.end_lineno])
            per_test[node.name] = {f"{k}-{n}" for k, n in TOKEN.findall(seg)} | {f"{k.upper()}-{n}" for k, n in NAMED.findall(node.name)}
            covered.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    module = {f"{k}-{n}" for k, n in TOKEN.findall(ast.get_docstring(tree) or "")}
    return module, per_test


def main() -> int:
    day = ROOT / "eval" / "reports" / dt.date.today().isoformat()
    day.mkdir(parents=True, exist_ok=True)
    junit = day / "release-sweep.junit.xml"
    env = {**os.environ, "KG_REPLICAS": "2", "KG_REQUIRE_STACK": "1"}
    only = [a for a in sys.argv[1:] if not a.startswith("-")]
    cmd = [UV, "run", "pytest", *(only or SUITES), "-q", "-p", "no:cacheprovider", f"--junitxml={junit}"]
    print("== release sweep against 2 API replicas:", " ".join(cmd[3:6]), "...", flush=True)
    proc = subprocess.run(cmd, cwd=ROOT, env=env, check=False)  # noqa: S603
    tree = ET.parse(junit).getroot()
    cache: dict[str, tuple[set[str], dict[str, set[str]]]] = {}
    per_id: dict[str, dict[str, list[str]]] = defaultdict(lambda: {"passed": [], "failed": [], "skipped": []})
    for case in tree.iter("testcase"):
        file = case.get("classname", "").replace(".", "/")
        # classname is dotted module path (+ maybe a class); find the longest prefix that is a file
        parts = file.split("/")
        rel = next((("/".join(parts[:i]) + ".py") for i in range(len(parts), 0, -1) if (ROOT / ("/".join(parts[:i]) + ".py")).exists()), "")
        name = (case.get("name") or "").split("[")[0]
        if not rel:
            continue
        if rel not in cache:
            cache[rel] = ids_by_test(ROOT / rel)
        module_ids, per_test = cache[rel]
        ids = set(per_test.get(name, set())) | module_ids | set(EXTRA.get(rel, []))
        outcome = (
            "failed"
            if case.find("failure") is not None or case.find("error") is not None
            else "skipped"
            if case.find("skipped") is not None
            else "passed"
        )
        for i in ids:
            per_id[i][outcome].append(f"{rel}::{case.get('name')}")
    rehearsals = sorted((ROOT / "eval" / "reports").glob("restore-*.md"))
    rehearsal_ok = bool(rehearsals) and "PASSED" in rehearsals[-1].read_text()[:400].upper()
    rows = []
    ok = proc.returncode == 0
    for i in IDS:
        if i == "AT-18":
            status = "pass" if rehearsal_ok else "missing"
            rows.append(
                {
                    "id": i,
                    "status": status,
                    "evidence": rehearsals[-1].name if rehearsals else None,
                    "passed": int(rehearsal_ok),
                    "failed": 0,
                    "strict": False,
                }
            )
            ok &= rehearsal_ok
            continue
        got = per_id.get(i, {"passed": [], "failed": [], "skipped": []})
        status = "fail" if got["failed"] else "pass" if got["passed"] else "uncovered"
        ok &= status == "pass"
        rows.append(
            {
                "id": i,
                "status": status,
                "passed": len(got["passed"]),
                "failed": len(got["failed"]),
                "skipped": len(got["skipped"]),
                "strict": i in STRICT,
                "failing": got["failed"][:10],
                "files": sorted({t.split("::")[0] for t in got["passed"] + got["failed"]}),
            }
        )
    totals = {"tests": len(list(tree.iter("testcase")))}
    report = {
        "date": dt.date.today().isoformat(),
        "replicas": 2,
        "pytest_exit": proc.returncode,
        "ok": ok,
        "strict_blockers": STRICT,
        "ids": rows,
        "totals": totals,
    }
    (day / "release-sweep.json").write_text(json.dumps(report, indent=2) + "\n")
    md = [
        f"# Release sweep {report['date']} - {'PASSED' if ok else 'FAILED'}",
        "",
        f"Two API replicas (`KG_REPLICAS=2`), pytest exit {proc.returncode}, {totals['tests']} tests.",
        "",
        "| id | status | passed | failed | skipped (single-process) | strict |",
        "|---|---|---|---|---|---|",
    ]
    md += [
        f"| {r['id']} | {r['status']} | {r['passed']} | {r['failed']} | {r.get('skipped', 0)} | {'yes' if r['strict'] else ''} |"
        for r in rows
    ]
    (day / "release-sweep.md").write_text("\n".join(md) + "\n")
    print(
        json.dumps(
            {
                "ok": ok,
                "uncovered": [r["id"] for r in rows if r["status"] in ("uncovered", "missing")],
                "failing": [r["id"] for r in rows if r["status"] == "fail"],
            }
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
