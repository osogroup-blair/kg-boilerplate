"""The live session (phase-9 WP1): the one run against the REAL model router that confirms every provisional number in P4-P8. An OWNER action: it spends model calls.

    make live-session                                   preflight, cost estimate, confirmation, steps (a)-(h), calibration, a proposal
    python -m tools.live_session preflight              checks only (no model calls beyond four 16-token probes)
    python -m tools.live_session run [--from STEP] [--yes]
    python -m tools.live_session propose                (re)build the calibration and the proposed diff from what the steps recorded
    python -m tools.live_session apply                  write the proposal (after the owner has read eval/reports/live/<date>/proposal.diff)
    python -m tools.live_session status

Environment (never printed or written): OSO_ROUTER_MODE=live, OSO_ROUTER_BASE_URL (or OSO_ROUTER_LIVE_URL) ending /v1, OSO_ROUTER_API_KEY (or OSO_ROUTER_API_KEY_API), a NON-ADMIN key.
Every step writes eval/reports/live/<date>/<step>.json and is resumable with --from."""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from tools.live_session import calibrate
from tools.verify_router.context import REQUIRED_ALIASES, Ctx, RouterUnreachable, check_reachable

ROOT = Path(__file__).resolve().parents[2]
REPORTS = ROOT / "eval" / "reports"
SPIKE_RESULTS = ROOT / "spikes" / "results"
PY = [sys.executable, "-m"]
# step -> (title, command, report glob in eval/reports, sample size for the cost estimate)
STEPS: list[tuple[str, str, list[str] | None, str | None]] = [
    ("a", "verify-router: LV-01..10, G-05, GR-11..13, TK-02", [*PY, "tools.verify_router"], None),
    ("b", "eval-extraction-live", [*PY, "eval.extraction.run_eval", "--mode", "live"], "extraction-live-*.json"),
    ("c", "eval-memory-live", [*PY, "eval.memory.run_eval", "--mode", "live"], "memory-live-*.json"),
    ("d", "eval-context-live", [*PY, "eval.context.run_eval", "--mode", "live"], "context-live-*.json"),
    ("e", "eval-consolidation-live", [*PY, "eval.consolidation.run_eval", "--mode", "live"], "consolidation-live-*.json"),
    ("f", "eval-simulation-live", [*PY, "eval.simulation.run_eval", "--mode", "live"], "simulation-live-*.json"),
    ("g", "latency profile capture (router p50/p95 per alias)", None, None),
    ("h", "fixture recording refresh (LV-10)", [*PY, "tools.verify_router", "--only", "LV-10"], None),
]
ORDER = [s[0] for s in STEPS]


def out_dir() -> Path:
    d = REPORTS / "live" / dt.date.today().isoformat()
    d.mkdir(parents=True, exist_ok=True)
    return d


def ctx_from_env() -> Ctx:
    env = os.environ
    base = (env.get("OSO_ROUTER_BASE_URL") or env.get("OSO_ROUTER_LIVE_URL") or "https://models.oso.group/v1").rstrip("/")
    key = env.get("OSO_ROUTER_API_KEY") or env.get("OSO_ROUTER_API_KEY_API") or ""
    if env.get("OSO_ROUTER_MODE") != "live":
        raise SystemExit("OSO_ROUTER_MODE is not 'live'.")
    if not key:
        raise SystemExit("No router key: export OSO_ROUTER_API_KEY (a non-admin key; it is never printed or written).")
    return Ctx(base, key, SPIKE_RESULTS)


# ---- cost estimate ------------------------------------------------------------------------------------------------------------------------------------------
def estimate_cost() -> dict[str, Any]:
    """tokens recorded by the latest MOCK reports (they count what the platform sends) x the alias's primary price, with a 3x safety factor (live prompts, retries, fallbacks)."""
    pricing = yaml.safe_load((ROOT / "compat" / "pricing.yaml").read_text())
    purposes = yaml.safe_load((ROOT / "compat" / "model-purposes.yaml").read_text())["model_purposes"]
    price = pricing["prices"][pricing["alias_primary"][purposes["graph_extraction"]["alias"]]]

    def latest(prefix: str) -> dict[str, Any]:
        files = sorted(REPORTS.glob(f"{prefix}-mock-*.json"))
        return json.loads(files[-1].read_text()) if files else {}

    ext, mem, ctx_r = latest("extraction"), latest("memory"), latest("context")
    episodes = len(ext.get("scenarios", [])) or 20
    tokens = {
        "extraction": episodes * float(ext.get("summary", {}).get("tokens_per_episode", 4200)),
        "memory": 40 * float(mem.get("summary", {}).get("tokens_per_event", 520)),
        "context": 16 * float(ctx_r.get("summary", {}).get("tokens_per_call", 240)) * 3,
        "consolidation": 12 * 1500.0,
        "simulation": 3 * 6000.0,
        "verify_router_and_calibration": 120 * 400.0,
    }
    total = sum(tokens.values())
    usd = total * (0.7 * price["input"] + 0.3 * price["output"]) / 1_000_000 * 3
    return {
        "tokens": {k: round(v) for k, v in tokens.items()},
        "total_tokens": round(total),
        "price_per_mtok": price,
        "safety_factor": 3,
        "estimated_usd": round(usd, 2),
    }


# ---- preflight ----------------------------------------------------------------------------------------------------------------------------------------------
def preflight(ctx: Ctx) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    try:
        check_reachable(ctx)
        checks["router_reachable"] = {"ok": True, "url": ctx.root_url}
    except RouterUnreachable as exc:
        checks["router_reachable"] = {"ok": False, "error": str(exc)[:200]}
        return {"ok": False, "checks": checks}
    models = ctx.client.get(f"{ctx.base_url}/models", headers=ctx.headers())
    ids = {d["id"] for d in models.json().get("data", [])} if models.status_code == 200 else set()
    checks["aliases"] = {"ok": REQUIRED_ALIASES <= ids, "missing": sorted(REQUIRED_ALIASES - ids)}
    probe = ctx.chat("oso/fast")
    admin = sorted(h for h in ("x-gateway-provider", "x-gateway-model") if h in probe.headers)
    checks["key_not_admin"] = {"ok": probe.status_code == 200 and not admin, "status": probe.status_code, "admin_headers": admin}
    ceiling = None
    for classification in ("restricted", "confidential", "internal"):
        r = ctx.chat("oso/fast", "ok", max_tokens=8, routing={"data_classification": classification})
        if r.status_code == 200:
            ceiling = classification
            break
    checks["classification_ceiling"] = {
        "ok": ceiling in ("restricted", "confidential"),
        "highest_accepted": ceiling,
        "note": "the test data is `internal`; `restricted` is what the egress policy allows",
    }
    for name, cmd in (
        ("postgres", ["docker", "compose", "exec", "-T", "postgres", "pg_isready", "-U", "postgres"]),
        (
            "neo4j",
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "neo4j",
                "cypher-shell",
                "-u",
                "neo4j",
                "-p",
                os.environ.get("NEO4J_PASSWORD", "kg-neo4j-dev"),
                "RETURN 1",
            ],
        ),
    ):
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)  # noqa: S603
        checks[name] = {"ok": r.returncode == 0}
    checks["cost_estimate"] = {"ok": True, **estimate_cost()}
    return {"ok": all(c["ok"] for c in checks.values()), "checks": checks}


# ---- steps --------------------------------------------------------------------------------------------------------------------------------------------------
def run_command(step: str, cmd: list[str], report_glob: str | None, d: Path) -> dict[str, Any]:
    started = time.time()
    before = set(REPORTS.glob(report_glob)) if report_glob else set()
    proc = subprocess.run(cmd, cwd=ROOT, env={**os.environ, "OSO_ROUTER_MODE": "live"}, check=False)  # noqa: S603
    rec: dict[str, Any] = {
        "step": step,
        "command": " ".join(cmd[2:]),
        "exit_code": proc.returncode,
        "seconds": round(time.time() - started, 1),
    }
    if report_glob:
        fresh = (
            sorted(set(REPORTS.glob(report_glob)) - before, key=lambda p: p.stat().st_mtime)
            or sorted(REPORTS.glob(report_glob), key=lambda p: p.stat().st_mtime)[-1:]
        )
        if fresh:
            rec["report"] = json.loads(fresh[-1].read_text())
            rec["report_file"] = fresh[-1].name
            shutil.copy(fresh[-1], d / f"{step}.report.json")
            md = fresh[-1].with_suffix(".md")
            if md.exists():
                shutil.copy(md, d / f"{step}.report.md")
    if step == "a":
        rec["spike_results"] = {p.stem: json.loads(p.read_text()) for p in sorted(SPIKE_RESULTS.glob("live-*.json"))}
    return rec


def capture_latency(ctx: Ctx, n: int = 25) -> dict[str, Any]:
    samples: dict[str, dict[str, list[float]]] = {"chat": {}, "embedding": {}}
    for alias in ("oso/fast", "oso/general", "oso/code"):
        for _ in range(n):
            t = time.perf_counter()
            r = ctx.chat(alias, "Summarise in one sentence: the quarterly planning meeting moved to Thursday.", max_tokens=48)
            if r.status_code == 200:
                samples["chat"].setdefault(alias, []).append((time.perf_counter() - t) * 1000)
    for _ in range(n):
        t = time.perf_counter()
        r = ctx.embed(["passage: Sarah Johnson works at Acme."])
        if r.status_code == 200:
            samples["embedding"].setdefault("oso/embedding", []).append((time.perf_counter() - t) * 1000)
    return {
        "step": "g",
        "exit_code": 0,
        "profile": calibrate.latency_profile(samples),
        "samples": {k: {a: len(v) for a, v in d.items()} for k, d in samples.items()},
    }


def cmd_run(args: argparse.Namespace) -> int:
    ctx = ctx_from_env()
    pre = preflight(ctx)
    d = out_dir()
    (d / "preflight.json").write_text(json.dumps(ctx.redact(pre), indent=2) + "\n")
    est = pre["checks"].get("cost_estimate", {})
    print(f"preflight: {'OK' if pre['ok'] else 'FAILED'}  (details: {d / 'preflight.json'})")
    for k, v in pre["checks"].items():
        print(f"  {k}: {'ok' if v['ok'] else 'FAIL'}")
    if not pre["ok"]:
        return 2
    print(f"estimated cost: about ${est['estimated_usd']} ({est['total_tokens']:,} tokens, x{est['safety_factor']} safety)")
    if not args.yes and input("Run the live session? type 'yes' to spend model calls: ").strip().lower() != "yes":
        print("cancelled; nothing was run")
        return 1
    start = ORDER.index(args.from_step) if args.from_step else 0
    failed = []
    for step, title, cmd, glob in STEPS[start:]:
        print(f"== step ({step}) {title}", flush=True)
        rec = capture_latency(ctx) if step == "g" else run_command(step, cmd or [], glob, d)
        (d / f"{step}.json").write_text(json.dumps(ctx.redact(rec), indent=2, default=str) + "\n")
        if rec["exit_code"] != 0:
            failed.append(step)
            print(f"   step ({step}) did not pass (exit {rec['exit_code']}); continuing so the whole picture is recorded")
    propose()
    print(f"summary: {d / 'summary.md'}; proposal: {d / 'proposal.diff'}")
    return 1 if failed else 0


# ---- calibration, proposal, apply --------------------------------------------------------------------------------------------------------------------------
def load_step(d: Path, step: str) -> dict[str, Any] | None:
    p = d / f"{step}.json"
    return json.loads(p.read_text()) if p.exists() else None


def calibrations(d: Path) -> dict[str, Any]:
    out: dict[str, Any] = {}
    a = load_step(d, "a") or {}
    tk = (a.get("spike_results", {}).get("live-TK-02") or {}).get("evidence", {})
    if tk.get("ratios"):
        out["CONTEXT_TOKEN_MARGIN"] = calibrate.token_margin(tk["ratios"])
    gr13 = (a.get("spike_results", {}).get("live-GR-13") or {}).get("evidence", {})
    if gr13:
        out["SEMAPHORE_LIMIT"] = calibrate.semaphore(gr13)
    emb = ((load_step(d, "b") or {}).get("report") or {}).get("embeddings", {})
    if emb.get("related", {}).get("cosine") and emb.get("unrelated", {}).get("cosine"):
        out["IDENTITY_COS_MIN"] = calibrate.separating_floor(emb["related"]["cosine"], emb["unrelated"]["cosine"])
    mem = ((load_step(d, "c") or {}).get("report") or {}).get("retrieval", {}).get("cosine", {})
    if mem.get("gold", {}).get("values") and mem.get("distractor", {}).get("values"):
        out["MEMORY_VEC_MIN_COSINE"] = calibrate.separating_floor(
            mem["gold"]["values"], mem["distractor"]["values"] + mem.get("filler", {}).get("values", [])
        )
    g = load_step(d, "g")
    if g and g.get("profile"):
        out["latency_profile"] = g["profile"]
    return out


def set_env(text: str, key: str, value: str) -> str:
    if re.search(rf"^{key}=", text, re.M):
        return re.sub(rf"^{key}=.*$", f"{key}={value}", text, flags=re.M)
    return text.rstrip("\n") + f"\n{key}={value}\n"


def proposed_files(cal: dict[str, Any]) -> dict[str, str]:
    files: dict[str, str] = {}
    env = (ROOT / ".env.example").read_text()
    for key, field in (
        ("CONTEXT_TOKEN_MARGIN", "margin"),
        ("SEMAPHORE_LIMIT", "semaphore_limit"),
        ("IDENTITY_COS_MIN", "floor"),
        ("MEMORY_VEC_MIN_COSINE", "floor"),
    ):
        if key in cal:
            env = set_env(env, key, str(cal[key][field]))
    files[".env.example"] = env
    purposes = (ROOT / "compat" / "model-purposes.yaml").read_text()
    files["compat/model-purposes.yaml"] = re.sub(r"^status: provisional$", "status: accepted", purposes, flags=re.M)
    for adr, new in (
        ("ADR-012-model-gateway-and-router-contract.md", "Accepted (live session {d})"),
        ("ADR-013-embeddings.md", "Accepted (live session {d})"),
        (
            "ADR-020-tokenizer-auth-networking.md",
            "Accepted for dev auth and networking; tokenizer margin calibrated by TK-02 (live session {d})",
        ),
    ):
        p = ROOT / "docs" / "adr" / adr
        files[f"docs/adr/{adr}"] = re.sub(
            r"^(\*\*Status:\*\*) [^\n]*?(  \*\*Decision refs)",
            rf"\1 {new.format(d=dt.date.today().isoformat())}\2",
            p.read_text(),
            count=1,
            flags=re.M,
        )
    if "latency_profile" in cal:
        files["tools/mock_router/latency.live.json"] = json.dumps(cal["latency_profile"], indent=2) + "\n"
    return files


def verdicts(d: Path) -> list[tuple[str, str]]:
    out = []
    for step, title, _cmd, _glob in STEPS:
        rec = load_step(d, step)
        out.append(
            (f"({step}) {title}", "not run" if rec is None else "PASS" if rec["exit_code"] == 0 else f"FAIL (exit {rec['exit_code']})")
        )
    return out


def propose() -> int:
    d = out_dir()
    cal = calibrations(d)
    files = proposed_files(cal)
    (d / "proposal.json").write_text(json.dumps(files, indent=1) + "\n")
    diff = []
    for rel, new in files.items():
        old = (ROOT / rel).read_text() if (ROOT / rel).exists() else ""
        diff += difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{rel}", f"b/{rel}")
    (d / "proposal.diff").write_text("".join(diff))
    lines = [f"# Live session {dt.date.today().isoformat()}", "", "## Steps", ""] + [f"- {t}: **{v}**" for t, v in verdicts(d)]
    lines += ["", "## Calibration (proposals; nothing is applied until `python -m tools.live_session apply`)", ""]
    for key, val in cal.items():
        if key != "latency_profile":
            lines.append(f"- `{key}`: {json.dumps(val)}")
    if "latency_profile" in cal:
        lines.append(
            "- latency profile: written to `tools/mock_router/latency.live.json` on apply (use it with `MOCK_ROUTER_LATENCY_PROFILE`)"
        )
    lines += [
        "",
        "After applying: re-run `make eval-context` with the calibrated margin; budget adherence must stay at 100%. Failing thresholds are release blockers or exceptions the owner accepts (docs/release/decisions.md).",
        "",
    ]
    (d / "summary.md").write_text("\n".join(lines))
    return 0


def cmd_apply(_: argparse.Namespace) -> int:
    d = out_dir()
    if not (d / "proposal.json").exists():
        print("no proposal for today; run `propose` first")
        return 1
    for rel, content in json.loads((d / "proposal.json").read_text()).items():
        (ROOT / rel).write_text(content)
        print(f"wrote {rel}")
    print("applied. Re-run `make eval-context` and commit the diff.")
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    d = out_dir()
    for t, v in verdicts(d):
        print(f"{t}: {v}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="live_session")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("preflight")
    run = sub.add_parser("run")
    run.add_argument("--from", dest="from_step", choices=ORDER)
    run.add_argument("--yes", action="store_true", help="skip the confirmation prompt (the cost estimate is still printed)")
    sub.add_parser("propose")
    sub.add_parser("apply")
    sub.add_parser("status")
    args = ap.parse_args()
    if args.cmd == "preflight":
        ctx = ctx_from_env()
        res = preflight(ctx)
        print(json.dumps(ctx.redact(res), indent=2))
        return 0 if res["ok"] else 2
    if args.cmd == "run":
        return cmd_run(args)
    if args.cmd == "propose":
        return propose()
    if args.cmd == "apply":
        return cmd_apply(args)
    return cmd_status(args)


if __name__ == "__main__":
    raise SystemExit(main())
