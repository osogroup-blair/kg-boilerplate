"""The restore rehearsal (phase-9 plan WP5, AT-18): restore the latest backup into a SEPARATE environment, replay the deletion journal, reconcile, verify against a fixed checklist,
and report how long it took (RTO) and how much a disaster at the backup's age would have cost (RPO)."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from tools.backup import probe, record, stores
from tools.backup.config import REPO, Config
from tools.backup.restore import Target, bring_up, cli, decrypt_set, fresh_target, restore_neo4j, restore_postgres, tear_down
from tools.backup.tooling import ToolError, log, run, sha256_file

REPORTS = REPO / "eval" / "reports"


class Check:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def add(self, name: str, ok: bool | None, detail: str = "") -> None:
        """ok=None: not verifiable here (reported as skipped, never as passed)."""
        self.rows.append({"check": name, "status": "skipped" if ok is None else ("passed" if ok else "failed"), "detail": detail})
        log(f"{'SKIP' if ok is None else ('ok  ' if ok else 'FAIL')} {name}{(': ' + detail) if detail else ''}")

    @property
    def failed(self) -> list[dict[str, Any]]:
        return [r for r in self.rows if r["status"] == "failed"]


def _http(url: str, method: str = "GET", body: bytes | None = None) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, method=method, headers={"content-type": "application/json"})  # noqa: S310 (loopback)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def _journal_dir(cfg: Config) -> Path:
    import os

    return Path(os.environ.get("DELETION_JOURNAL_DIR", str(REPO / ".local" / "deletion-journal")))


def rehearse(cfg: Config, identity: Path, *, setdir: Path | None = None, keep: bool = False) -> dict[str, Any]:
    started = time.monotonic()
    started_at = dt.datetime.now(dt.UTC)
    chosen = setdir or (stores.sets(cfg) or [None])[-1]  # type: ignore[list-item]
    if chosen is None:
        raise SystemExit("no backup set to rehearse from")
    manifest = stores.load(chosen)
    created = dt.datetime.fromisoformat(manifest["created_at"])
    checks = Check()
    problems = stores.verify_set(chosen)
    checks.add("backup set is intact (sha256 of every artifact equals the manifest)", not problems, "; ".join(problems))
    if problems:
        return _finish(cfg, chosen, manifest, checks, started, started_at, created, {})
    t = fresh_target(cfg)
    scratch = t.work / "decrypted"
    details: dict[str, Any] = {"project": t.project, "ports": t.ports, "backup_set": chosen.name, "git": manifest.get("git")}
    try:
        files = decrypt_set(cfg, chosen, identity, scratch)
        t.compose(cfg, "up", "-d", "--wait", "postgres")
        pg_art = next(a for a in manifest["artifacts"] if a["store"] == "postgres")
        db = pg_art["database"]
        t0 = time.monotonic()
        restore_postgres(cfg, t, files[f"postgres-{db}.dump"], db)
        details["postgres_restore_seconds"] = round(time.monotonic() - t0, 2)
        graph_art = next((a for a in manifest["artifacts"] if a["store"] == "neo4j"), None)
        if graph_art is not None:
            t0 = time.monotonic()
            restore_neo4j(cfg, t, files["neo4j.dump"])
            details["neo4j_restore_seconds"] = round(time.monotonic() - t0, 2)
        shutil.rmtree(scratch, ignore_errors=True)  # the decrypted dumps do not outlive the restore

        counts = {n: int(c) for n, c in t.exec_psql(cfg, db, stores.COUNT_SQL)}
        diff = {n: (c, counts.get(n)) for n, c in pg_art["row_counts"].items() if counts.get(n) != c}
        checks.add("postgres row counts equal the manifest (immediately after restore)", not diff, json.dumps(diff)[:300])
        restored_structure = {key: sorted({r[0] for r in t.exec_psql(cfg, db, sql)}) for key, sql in stores.STRUCTURE_SQL.items()}
        for key, want in pg_art.get("structure", {}).items():
            missing = sorted(set(want) - set(restored_structure.get(key, [])))
            checks.add(f"every {key.replace('_', ' ')} of the source schema is present", not missing, ", ".join(missing[:8]))

        bring_up(cfg, t)
        api = f"http://127.0.0.1:{t.ports['KG_API_PORT']}"
        if graph_art is not None:
            counts_g = _neo4j_counts(cfg, t)
            want_g = graph_art.get("counts", {})
            checks.add("neo4j node and relationship counts equal the manifest", not want_g or counts_g == want_g, f"{counts_g} vs {want_g}")
            names = _neo4j_names(cfg, t, "CONSTRAINTS")
            wanted = graph_art.get("structure", {}).get("constraints", [])
            checks.add(
                "the Graphiti and uniqueness constraints are present",
                set(wanted) <= set(names),
                ", ".join(sorted(set(wanted) - set(names))),
            )

        # read-only: reads and builds work, other writes are refused (503) before they reach a handler
        status, _ = _http(f"{api}/health/ready")
        checks.add("the restored API is ready", status == 200, f"/health/ready {status}")
        status, body = _http(f"{api}/v1/interactions", "POST", b"{}")
        checks.add(
            "the restored API refuses writes (MAINTENANCE_MODE=read_only)", status == 503 and "maintenance_read_only" in body, f"{status}"
        )

        # tenant isolation survives the restore: no workspace set -> nothing; one transaction-local workspace -> only its rows, and the next transaction sees nothing again
        slugs = [r[0] for r in t.exec_psql(cfg, db, "SELECT slug FROM workspaces WHERE status = 'active' ORDER BY created_at LIMIT 50")]
        rls_ok, rls_detail = _rls_check(cfg, t, db)
        checks.add("row-level security fails closed and a pooled connection does not carry a tenant over", rls_ok, rls_detail)

        # the deletion journal: a COPY (the original is out of band and read-only to this rehearsal)
        src = _journal_dir(cfg) / "deletions.jsonl"
        journal_sha = None
        if src.exists():
            shutil.copy(src, t.journal_dir / "deletions.jsonl")
            journal_sha = sha256_file(t.journal_dir / "deletions.jsonl")
        since = created.isoformat()
        code, out = cli(cfg, t, "deletions", "reapply", "--since", since, check=False)
        results = [json.loads(ln) for ln in out.splitlines() if ln.startswith("{") and '"outcome"' in ln]
        checks.add(
            "kg deletions reapply: the journal verifies and every line since the backup is applied",
            code == 0,
            f"exit {code}, {len(results)} lines: " + ", ".join(sorted({r["outcome"] for r in results})),
        )
        code2, out2 = cli(cfg, t, "deletions", "reapply", "--since", since, check=False)
        again = [json.loads(ln)["outcome"] for ln in out2.splitlines() if ln.startswith("{") and '"outcome"' in ln]
        checks.add(
            "a second reapply is idempotent (nothing left to do)",
            code2 == 0 and all(o in ("already_applied", "workspace_absent") for o in again),
            f"{again}",
        )
        checks.add(
            "the journal was not changed by the rehearsal",
            journal_sha is None or sha256_file(t.journal_dir / "deletions.jsonl") == journal_sha,
        )

        reconcile: dict[str, Any] = {}
        residuals = 0
        for slug in slugs:
            code, out = cli(cfg, t, "reconcile", "--workspace", slug, "--scope", "all", "--repair", check=False)
            line = next((ln for ln in out.splitlines() if ln.startswith("{") and '"run_id"' in ln), None)
            res = json.loads(line) if line else {"status": "no_output", "drift": {}}
            reconcile[slug] = {
                "status": res.get("status"),
                "drift": res.get("drift"),
                "repaired": res.get("repaired"),
                "needs_confirmation": res.get("needs_confirmation"),
                "reingest_preview": res.get("reingest_preview"),
            }
            residuals += int((res.get("drift") or {}).get("deletions.residual", 0))
        failing = {s: r for s, r in reconcile.items() if r["status"] != "completed"}
        checks.add("kg reconcile --repair ran for every workspace", not failing, json.dumps(failing)[:300])
        checks.add("the residual scan of every journal entry found 0 residuals", residuals == 0, f"{residuals}")
        left = {s: r["drift"] for s, r in reconcile.items() if r["drift"] and set(r["drift"]) - set(r["needs_confirmation"] or {})}
        checks.add(
            "reconcile left no drift except classes that need a cost confirmation (listed, not executed)", not left, json.dumps(left)[:300]
        )
        details["reconcile"] = reconcile

        wanted_probe = json.loads((chosen / "probe.json").read_text()) if (chosen / "probe.json").exists() else {"available": False}
        if wanted_probe.get("available"):
            got = probe.capture(api)
            same = got.get("available") and [(b["status"], b["items"]) for b in got["builds"]] == [
                (b["status"], b["items"]) for b in wanted_probe["builds"]
            ]
            detail = ""
            if not same:
                pairs = zip(wanted_probe["builds"], got.get("builds", []), strict=False)
                detail = "; ".join(
                    f"{w['max_tokens']}: {w['status']} -> {g['status']}, only before {len(set(w['items']) - set(g['items']))}, only after {len(set(g['items']) - set(w['items']))}"
                    for w, g in pairs
                ) or json.dumps({k: v for k, v in got.items() if k != "builds"})
            checks.add("the context probe on the Sarah world matches its pre-backup report (status and items)", bool(same), detail)
        else:
            checks.add(
                "the context probe on the Sarah world matches its pre-backup report (status and items)",
                None,
                "no probe was captured with this backup (needs `make demo-p6` data and dev credentials)",
            )
        return _finish(cfg, chosen, manifest, checks, started, started_at, created, details)
    finally:
        if not keep:
            tear_down(cfg, t)
            shutil.rmtree(t.work, ignore_errors=True)


def _neo4j_counts(cfg: Config, t: Target) -> dict[str, int]:
    import os

    user, password = os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "kg-neo4j-dev")
    proc = t.compose(
        cfg,
        "exec",
        "-T",
        "neo4j",
        "cypher-shell",
        "-u",
        user,
        "-p",
        password,
        "--format",
        "plain",
        "MATCH (n) WITH count(n) AS nodes OPTIONAL MATCH ()-[r]->() RETURN nodes, count(r) AS relationships",
        check=False,
    )
    lines = [ln for ln in proc.stdout.decode().splitlines() if ln.strip()]
    if len(lines) < 2:
        return {}
    nodes, rels = (int(x) for x in lines[1].split(","))
    return {"nodes": nodes, "relationships": rels}


def _neo4j_names(cfg: Config, t: Target, what: str) -> list[str]:
    import os

    user, password = os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "kg-neo4j-dev")
    proc = t.compose(
        cfg,
        "exec",
        "-T",
        "neo4j",
        "cypher-shell",
        "-u",
        user,
        "-p",
        password,
        "--format",
        "plain",
        f"SHOW {what} YIELD name RETURN name ORDER BY name",
        check=False,
    )
    return [ln.strip().strip('"') for ln in proc.stdout.decode().splitlines()[1:] if ln.strip()]


def _rls_check(cfg: Config, t: Target, db: str) -> tuple[bool, str]:
    """As kg_app (no BYPASSRLS): without a workspace nothing is visible; with one transaction-local workspace only that workspace's rows; and the NEXT transaction on the same
    connection sees nothing again."""
    rows = t.exec_psql(cfg, db, "SELECT workspace_id FROM entities GROUP BY 1 ORDER BY count(*) DESC LIMIT 1")
    if not rows:
        return True, "no entities to probe with (skipped)"
    ws = rows[0][0]
    script = (
        "SELECT 'none', count(*) FROM entities;\n"
        f"BEGIN; SELECT set_config('app.workspace_id', '{ws}', true); SELECT 'own', count(*) FROM entities; COMMIT;\n"
        "SELECT 'after', count(*) FROM entities;\n"
    )
    import os

    pw = os.environ.get("KG_APP_PASSWORD", "kg_app_dev")
    proc = t.compose(
        cfg,
        "exec",
        "-T",
        "-e",
        f"PGPASSWORD={pw}",
        "postgres",
        "psql",
        "-h",
        "127.0.0.1",
        "-U",
        "kg_app",
        "-d",
        db,
        "-At",
        "-F",
        "|",
        input=script.encode(),
        check=False,
    )
    seen = [ln.split("|") for ln in proc.stdout.decode().splitlines() if "|" in ln]
    counts = {k: int(v) for k, v in seen if k in ("none", "own", "after")}
    ok = counts.get("none") == 0 and counts.get("own", 0) > 0 and counts.get("after") == 0
    return ok, str(counts)


def _finish(
    cfg: Config,
    chosen: Path,
    manifest: dict[str, Any],
    checks: Check,
    started: float,
    started_at: dt.datetime,
    created: dt.datetime,
    details: dict[str, Any],
) -> dict[str, Any]:
    elapsed = time.monotonic() - started
    status = "failed" if checks.failed else "completed"
    report: dict[str, Any] = {
        "kind": "restore_rehearsal",
        "status": "passed" if status == "completed" else "failed",
        "started_at": started_at.isoformat(),
        "backup_created_at": created.isoformat(),
        "rto_seconds": round(elapsed, 1),  # restore + replay + reconcile + verification, wall clock
        "rpo_seconds_at_rehearsal": round(
            (started_at - created).total_seconds(), 1
        ),  # what a disaster at this moment would have lost (the age of the newest backup)
        "checks": checks.rows,
        **details,
    }
    REPORTS.mkdir(parents=True, exist_ok=True)
    day = started_at.strftime("%Y-%m-%d")
    (REPORTS / f"restore-{day}.json").write_text(json.dumps(report, indent=2, sort_keys=True, default=str))
    lines = [
        f"# Restore rehearsal {day}",
        "",
        f"Backup set `{chosen.name}` (taken {created.isoformat()}, git `{(manifest.get('git') or {}).get('describe')}`).",
        "",
        f"- **Result:** {report['status'].upper()}",
        f"- **RTO (measured):** {report['rto_seconds']} s",
        f"- **RPO at rehearsal (age of the backup):** {report['rpo_seconds_at_rehearsal']} s",
        "",
        "| Check | Result | Detail |",
        "|---|---|---|",
    ]
    lines += [f"| {r['check']} | {r['status']} | {str(r['detail']).replace('|', '/')[:120]} |" for r in checks.rows]
    (REPORTS / f"restore-{day}.md").write_text("\n".join(lines) + "\n")
    record.rehearsal(
        cfg,
        "completed" if report["status"] == "passed" else "failed",
        {
            "rto_seconds": report["rto_seconds"],
            "rpo_seconds": report["rpo_seconds_at_rehearsal"],
            "set": chosen.name,
            "failed": [c["check"] for c in checks.failed],
        },
    )
    log(f"rehearsal {report['status']} in {report['rto_seconds']}s; report: eval/reports/restore-{day}.md")
    return report


_ = (hashlib, ToolError, run)
