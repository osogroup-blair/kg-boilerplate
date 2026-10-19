"""`make tapes-check`: the committed tapes are valid, carry no secrets, match their narration, and (with --rerecord) still match what the platform does today.

--rerecord records into a scratch directory and compares the SHAPE of each tape (steps, methods, paths, statuses), not bytes: polling counts and timings vary.
It needs the stack (`make up seed`)."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from tools.replay_capture.record import ROOT

TAPES = ROOT / "apps" / "site" / "public" / "tapes"
SCHEMA = json.loads((Path(__file__).parent / "tape.schema.json").read_text())
KEYS = re.compile(r"\bkgk?_(?!EXAMPLE)[A-Za-z0-9_\-]{8,}")
JWT = re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")
UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")


def shape(tape: dict[str, Any]) -> list[Any]:
    out = []
    for s in tape["steps"]:
        calls = [(c["method"], re.sub(r"id_\d+|[a-z_]+_\d+", "{id}", c["path"]), c["status"]) for c in s["calls"] if not c.get("snapshot") and c["status"] != 429]
        dedup = [c for i, c in enumerate(calls) if i == 0 or c != calls[i - 1]]  # polling repeats
        out.append((s["id"], sorted(set(dedup))))
    return out


def check_committed() -> list[str]:
    problems: list[str] = []
    files = sorted(TAPES.glob("*.json"))
    if not files:
        return [f"no tapes in {TAPES}: run make tapes"]
    for f in files:
        text = f.read_text()
        try:
            tape = json.loads(text)
            jsonschema.validate(tape, SCHEMA)
        except Exception as exc:
            problems.append(f"{f.name}: {str(exc).splitlines()[0]}")
            continue
        if KEYS.search(text) or JWT.search(text):
            problems.append(f"{f.name}: contains something key-shaped")
        if UUID.search(text):
            problems.append(f"{f.name}: a raw UUID survived normalisation")
        if len(text.encode()) > 600_000:
            problems.append(f"{f.name}: {len(text) // 1024} KB is over the 600 KB budget")
    sarah = json.loads((TAPES / "sarah.json").read_text()) if (TAPES / "sarah.json").exists() else None
    narration = yaml.safe_load((Path(__file__).parent / "narration" / "sarah.yaml").read_text())
    if sarah:
        have = {s["id"] for s in sarah["steps"]}
        problems += [f"narration names step {k} but the sarah tape has none" for k in narration if k not in have]
        problems += [f"sarah step {k} has no narration" for k in have if k not in narration]
    return problems


def rerecord() -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "raw"
        out = Path(tmp) / "tapes"
        env_py = sys.executable
        for cmd in ([env_py, "-m", "tools.replay_capture.record", "--out", str(raw)], [env_py, "-m", "tools.replay_capture.normalize", str(raw), str(out)]):
            proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=False)  # noqa: S603
            if proc.returncode != 0:
                return [f"{' '.join(cmd[2:4])} failed:\n{proc.stdout[-1500:]}{proc.stderr[-800:]}"]
        problems = []
        for f in sorted(TAPES.glob("*.json")):
            new = out / f.name
            if not new.exists():
                problems.append(f"{f.name}: not produced by a fresh recording")
                continue
            old_shape, new_shape = shape(json.loads(f.read_text())), shape(json.loads(new.read_text()))
            if old_shape != new_shape:
                diffs = []
                for (sid, a), (sid2, b) in zip(old_shape, new_shape, strict=False):
                    if (sid, a) != (sid2, b):
                        diffs.append(f"step {sid}: only committed {sorted(set(a) - set(b))[:3]} only fresh {sorted(set(b) - set(a))[:3]}")
                if len(old_shape) != len(new_shape):
                    diffs.append(f"{len(old_shape)} steps committed, {len(new_shape)} fresh")
                problems.append(f"{f.name}: the platform now behaves differently (shape differs); re-record with make tapes\n    " + "\n    ".join(diffs[:6]))
        return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rerecord", action="store_true")
    args = ap.parse_args()
    problems = check_committed()
    if args.rerecord and not problems:
        problems = rerecord()
    for p in problems:
        print(f"tapes-check: {p}")
    print("tapes-check: OK" if not problems else f"tapes-check: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
