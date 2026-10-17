"""Generates sdk/python/src/kg_platform_sdk/models.py from contracts/openapi.json (phase-7 plan §5.7, decision 8).

Only the schemas the SDK's operations reach are generated (the request bodies and responses of the eight SDK methods, plus `list_profiles` / `usage`), so the file stays small
and a change elsewhere in the API does not churn it. `--check` regenerates into memory and exits 1 when the committed file differs: the drift test and `make check` use it.
"""

from __future__ import annotations

import argparse
import difflib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OPENAPI = ROOT / "contracts" / "openapi.json"
TARGET = ROOT / "sdk" / "python" / "src" / "kg_platform_sdk" / "models.py"
ROOTS = (
    "ContextBuildIn",
    "ContextOut",
    "InteractionIn",
    "AcceptedOut",
    "InteractionDetail",
    "ProposalIn",
    "MemorySaveIn",
    "MemoryOut",
    "MemoryPublishIn",
    "SearchIn",
    "SearchOut",
    "WriteOut",
    "EvidenceOut",
    "UsageOut",
    "ErrorEnvelope",
)


def reachable(schemas: dict[str, Any], roots: tuple[str, ...]) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
                name = ref.rsplit("/", 1)[1]
                if name not in seen:
                    seen[name] = schemas[name]
                    walk(schemas[name])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for r in roots:
        seen[r] = schemas[r]
        walk(schemas[r])
    return dict(sorted(seen.items()))


def generate() -> str:
    spec = json.loads(OPENAPI.read_text())
    trimmed = {
        "openapi": "3.1.0",
        "info": {"title": "sdk", "version": "0"},
        "paths": {},
        "components": {"schemas": reachable(spec["components"]["schemas"], ROOTS)},
    }
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp) / "openapi.json", Path(tmp) / "models.py"
        src.write_text(json.dumps(trimmed, indent=1, sort_keys=True))
        subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-m",
                "datamodel_code_generator",
                "--input",
                str(src),
                "--input-file-type",
                "openapi",
                "--output",
                str(out),
                "--output-model-type",
                "pydantic_v2.BaseModel",
                "--target-python-version",
                "3.12",
                "--use-annotated",
                "--use-standard-collections",
                "--use-union-operator",
                "--disable-timestamp",
                "--enum-field-as-literal",
                "all",
                "--reuse-model",
                "--field-constraints",
                "--collapse-root-models",
                "--formatters",
                "ruff-format",
            ],
            check=True,
            capture_output=True,
        )
        return out.read_text()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="exit 1 if the committed models.py differs from a fresh generation")
    args = ap.parse_args()
    fresh = generate()
    if args.check:
        current = TARGET.read_text() if TARGET.exists() else ""
        if current != fresh:
            sys.stdout.writelines(difflib.unified_diff(current.splitlines(True), fresh.splitlines(True), "committed", "generated", n=2))
            return 1
        return 0
    TARGET.write_text(fresh)
    print(f"wrote {TARGET.relative_to(ROOT)} ({len(fresh.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
