"""Builds the .ipynb files from examples/notebooks/src/*.py ("percent" format: `# %%` code cells, `# %% [markdown]` text cells), with no outputs.

python -m examples.notebooks.build           write the notebooks
python -m examples.notebooks.build --check   fail if a notebook differs from its source (used by the tests)"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import nbformat

HERE = Path(__file__).parent
SRC = HERE / "src"


def cells_of(text: str) -> list[nbformat.NotebookNode]:
    out: list[nbformat.NotebookNode] = []
    kind: str = "code"
    buf: list[str] = []

    def flush() -> None:
        body = "\n".join(buf).strip("\n")
        if body:
            if kind == "markdown":
                body = "\n".join(ln[2:] if ln.startswith("# ") else ln.lstrip("#") for ln in body.splitlines())
                out.append(nbformat.v4.new_markdown_cell(body))
            else:
                out.append(nbformat.v4.new_code_cell(body))

    for line in text.splitlines():
        if line.startswith("# %%"):
            flush()
            kind, buf = ("markdown" if "[markdown]" in line else "code"), []
        else:
            buf.append(line)
    flush()
    return out


def build(src: Path) -> str:
    nb = nbformat.v4.new_notebook(cells=cells_of(src.read_text()))
    nb.metadata["kernelspec"] = {"display_name": "Python 3", "language": "python", "name": "python3"}
    for i, c in enumerate(nb.cells):
        c["id"] = f"c{i:02d}"
    nbformat.validate(nb)
    return nbformat.writes(nb, version=4) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    stale = []
    for src in sorted(SRC.glob("*.py")):
        target = HERE / (src.stem + ".ipynb")
        text = build(src)
        if args.check:
            if not target.exists() or target.read_text() != text:
                stale.append(target.name)
        else:
            target.write_text(text)
    if stale:
        print("out of date (run python -m examples.notebooks.build):", ", ".join(stale))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
