"""`python -m tools.backup <command>`: keygen · build-image · backup · verify · prune · status · rehearse."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tools.backup import probe, rehearse, stores, tooling
from tools.backup.config import Config


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m tools.backup")
    sub = p.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen", help="write a new age identity (the recovery key) and print its public key")
    k.add_argument("identity", type=Path)
    sub.add_parser("build-image", help="build the pinned tool image (pg_dump, age)")
    b = sub.add_parser("backup", help="make a backup set")
    b.add_argument("--stores", default="postgres,neo4j,config,keys", help="comma list of postgres,neo4j,config,keys")
    b.add_argument("--probe-api", default="http://127.0.0.1:8000", help="capture a context probe from this API into the set (empty: skip)")
    v = sub.add_parser("verify", help="check a set's hashes against its manifest")
    v.add_argument("--set", type=Path)
    sub.add_parser("prune", help="remove sets older than BACKUP_RETENTION_DAYS (always keeping the newest few)")
    sub.add_parser("status", help="list the sets")
    r = sub.add_parser("rehearse", help="restore the latest set into the kg-restore project and verify it (AT-18)")
    r.add_argument("--identity", type=Path, required=True)
    r.add_argument("--set", type=Path)
    r.add_argument("--keep", action="store_true", help="leave the restored environment running")
    args = p.parse_args(argv)
    cfg = Config.from_env()
    if args.cmd == "keygen":
        print(tooling.keygen(args.identity))
        return 0
    if args.cmd == "build-image":
        tooling.build_image()
        return 0
    if args.cmd == "backup":
        wanted = [s for s in args.stores.split(",") if s]
        baseline = (
            probe.capture(args.probe_api) if args.probe_api else None
        )  # BEFORE the dump: Neo4j is down for a moment during it, and a build then is `partial`
        manifest = stores.run_backup(cfg, wanted)
        if baseline is not None:
            (cfg.backup_dir / manifest["id"] / "probe.json").write_text(json.dumps(baseline, indent=2, sort_keys=True))
        print(
            json.dumps(
                {"id": manifest["id"], "artifacts": [{k: a[k] for k in ("store", "name", "size_bytes")} for a in manifest["artifacts"]]}
            )
        )
        return 0
    if args.cmd == "verify":
        setdir = args.set or (stores.sets(cfg) or [None])[-1]
        if setdir is None:
            print("no backup sets", file=sys.stderr)
            return 1
        problems = stores.verify_set(setdir)
        print(json.dumps({"set": setdir.name, "problems": problems}))
        return 1 if problems else 0
    if args.cmd == "prune":
        print(json.dumps({"removed": stores.prune(cfg)}))
        return 0
    if args.cmd == "status":
        for s in stores.sets(cfg):
            m = stores.load(s)
            print(
                f"{s.name}  {m['created_at']}  {sum(a['size_bytes'] for a in m['artifacts'])} bytes  {','.join(sorted({a['store'] for a in m['artifacts']}))}"
            )
        return 0
    if args.cmd == "rehearse":
        report = rehearse.rehearse(cfg, args.identity, setdir=args.set, keep=args.keep)
        print(json.dumps({k: report[k] for k in ("status", "rto_seconds", "rpo_seconds_at_rehearsal")}))
        return 0 if report["status"] == "passed" else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
