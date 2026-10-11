"""Regenerate catalog.json from the router's seed script so the mock cannot silently drift.

Usage: python -m tools.mock_router.sync_catalog [path/to/seed-logical-router.ts]
Default seed path: ~/Documents/oso_model_router/scripts/seed-logical-router.ts
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

DEFAULT_SEED = Path.home() / "Documents/oso_model_router/scripts/seed-logical-router.ts"
OUT = Path(__file__).with_name("catalog.json")


def _block(text: str, start_marker: str, open_ch: str, close_ch: str) -> str:
    i = text.index(start_marker)
    j = text.index(open_ch, text.index("=", i))
    depth = 0
    for k in range(j, len(text)):
        if text[k] == open_ch:
            depth += 1
        elif text[k] == close_ch:
            depth -= 1
            if depth == 0:
                return text[j + 1 : k]
    raise ValueError(f"unterminated block after {start_marker}")


def _objects(body: str) -> list[str]:
    out, depth, start = [], 0, None
    for i, ch in enumerate(body):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                out.append(body[start : i + 1])
    return out


def _str(obj: str, key: str) -> str | None:
    m = re.search(rf'\b{key}:\s*"([^"]*)"', obj)
    return m.group(1) if m else None


def _num(obj: str, key: str) -> float | None:
    m = re.search(rf"\b{key}:\s*([0-9_.]+)", obj)
    return float(m.group(1).replace("_", "")) if m else None


def parse(seed_text: str) -> dict:
    weights = {_str(o, "slug"): _str(o, "weights") for o in _objects(_block(seed_text, "const CANONICAL_MODELS", "[", "]"))}
    models = {}
    for o in _objects(_block(seed_text, "const DEPLOYMENTS", "[", "]")):
        slug = _str(o, "slug")
        caps = re.search(r"capabilities:\s*\[([^\]]*)\]", o).group(1)
        ctx = int(_num(o, "contextWindow"))
        models[slug] = {
            "weights": weights.get(slug, "closed"),
            "provider": _str(o, "providerKey"),
            "hosting_boundary": _str(o, "hostingBoundary"),
            "region": _str(o, "region") or "global",
            "context_window": ctx,
            "max_output_tokens": int(_num(o, "maxOutputTokens")),
            "capabilities": re.findall(r'"([^"]+)"', caps),
            "input_per_mtok": _num(o, "inputPrice"),
            "output_per_mtok": _num(o, "outputPrice"),
        }
    aliases = {}
    aliases_src = seed_text[seed_text.index("const ALIASES") :]
    for m in re.finditer(r'"(oso/[a-z-]+)":\s*\[([^\]]*)\]', aliases_src):
        aliases[m.group(1)] = re.findall(r'"([^"]+)"', m.group(2))
    return {"source": "oso_model_router/scripts/seed-logical-router.ts", "aliases": aliases, "models": models}


def main() -> None:
    seed = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SEED
    catalog = parse(seed.read_text())
    OUT.write_text(json.dumps(catalog, indent=2, sort_keys=True) + "\n")
    print(f"wrote {OUT} ({len(catalog['aliases'])} aliases, {len(catalog['models'])} models)")


if __name__ == "__main__":
    main()
