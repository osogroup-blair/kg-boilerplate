"""Live router verification kit.  Usage:

    OSO_ROUTER_MODE=live OSO_ROUTER_BASE_URL=http://127.0.0.1:8780/v1 OSO_ROUTER_API_KEY=... make verify-router
    python -m tools.verify_router --only LV-01,LV-05          # subset
    python -m tools.verify_router --self-test                  # run the kit against the in-process MOCK to prove the kit itself works

Exit codes: 0 all pass, 1 some check failed, 2 router not reachable / bad configuration. The key is read from the environment only and is never printed or written.
"""
from __future__ import annotations

import argparse
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

from .checks import ALL
from .context import ROOT, Ctx, RouterUnreachable, check_reachable, from_env


def run(ctx: Ctx, only: set[str] | None) -> int:
    check_reachable(ctx)
    ctx.say(f"router reachable at {ctx.root_url}; running live checks (results -> {ctx.results_dir})")
    failed = 0
    for cid, fn in ALL:
        if only and cid not in only:
            continue
        ctx.say(f"{cid} ...")
        try:
            fn(ctx)
        except RouterUnreachable:
            raise
        except Exception as e:  # a crashing check is a failed check, never a crashed kit
            ctx.record(cid, "fail", {"exception": f"{type(e).__name__}: {e}"}, "check raised an exception")
        rec = ctx.results_dir / f"live-{cid}.json"
        if rec.exists() and '"status": "fail"' in rec.read_text():
            failed += 1
    ctx.say(f"done: {failed} failed. Rebuild the report with `make spike-report`.")
    return 1 if failed else 0


def self_test(only: set[str] | None) -> int:
    import os

    import uvicorn

    from tools.mock_router.app import MockConfig, create_app
    key = "selftest-SECRET-key-0123456789"
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    server = uvicorn.Server(uvicorn.Config(create_app(MockConfig(api_key=key, retry_after_seconds=0)), host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    time.sleep(1.0)
    os.environ.update(OSO_ROUTER_MODE="live", OSO_ROUTER_BASE_URL=f"http://127.0.0.1:{port}/v1", OSO_ROUTER_API_KEY=key)
    out = Path(tempfile.mkdtemp(prefix="verify-selftest-"))
    ctx = from_env(out)
    only = only or {cid for cid, _ in ALL if cid not in ("LV-09", "LV-10")}      # LV-09/10 need Neo4j + the real router
    rc = run(ctx, only)
    leaked = [f.name for f in out.glob("*.json") if key in f.read_text()] + (["stdout"] if any(key in line for line in ctx.log) else [])
    wrote = {f.name for f in out.glob("live-*.json")}
    missing = [f"live-{cid}.json" for cid in only if f"live-{cid}.json" not in wrote]
    print(f"self-test: results in {out}; missing={missing}; key_leaked_in={leaked}; kit_exit={rc}")
    server.should_exit = True
    return 0 if not missing and not leaked else 1


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma separated ids, e.g. LV-01,LV-05")
    ap.add_argument("--results-dir", default=str(ROOT / "spikes/results"))
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    only = set(a.only.split(",")) if a.only else None
    if a.self_test:
        sys.exit(self_test(only))
    try:
        sys.exit(run(from_env(Path(a.results_dir)), only))
    except RouterUnreachable as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
