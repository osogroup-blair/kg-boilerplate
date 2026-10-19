"""Executes notebooks top to bottom against the local stack and stops at the first failing cell.

python -m examples.notebooks.run [01_ingest_and_ledger ...]        run (all by default); outputs are discarded
python -m examples.notebooks.run --write                           also save executed copies to examples/notebooks/out/ (git-ignored) to read with outputs"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import nbformat
from nbclient import NotebookClient
from nbclient.exceptions import CellExecutionError

HERE = Path(__file__).parent
ROOT = HERE.parents[1]


def execute(path: Path, *, write: bool = False, timeout: int = 600) -> float:
    nb = nbformat.read(path, as_version=4)
    client = NotebookClient(nb, timeout=timeout, kernel_name="python3", resources={"metadata": {"path": str(ROOT)}})
    t0 = time.monotonic()
    client.execute()
    if write:
        out = HERE / "out"
        out.mkdir(exist_ok=True)
        nbformat.write(nb, out / path.name)
    return time.monotonic() - t0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("names", nargs="*")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    paths = [HERE / f"{n}.ipynb" for n in args.names] or sorted(HERE.glob("[0-9][0-9]_*.ipynb"))
    failed = 0
    for p in paths:
        try:
            print(f"{p.name}: ok in {execute(p, write=args.write):.0f}s")
        except CellExecutionError as exc:
            failed += 1
            print(f"{p.name}: FAILED\n{str(exc)[-1800:]}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
