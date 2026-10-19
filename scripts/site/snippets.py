"""Static checks on the code in the docs, so a page cannot name a command or a route that does not exist.

* every `make <target>` in a fenced shell block must be a real target in the Makefile;
* every `curl` against `$KG/v1/...` (or `$KG_BASE_URL/v1/...`) must name a method and path the OpenAPI contract has;
* `uv run python -m <module>` and `python -m <module>` must name a module in the repository.
This is not a replacement for running them: the `stack`-tagged blocks run in CI's examples job."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from scripts.site.common import CONTENT, ROOT

FENCE = re.compile(r"^```([^\n]*)\n(.*?)^```", re.S | re.M)


def targets() -> set[str]:
    return {m.group(1) for line in (ROOT / "Makefile").read_text().splitlines() if (m := re.match(r"^([a-zA-Z0-9_-]+):", line))}


def routes() -> set[tuple[str, str]]:
    spec = json.loads((ROOT / "contracts" / "openapi.json").read_text())
    return {
        (m.upper(), re.sub(r"\{[^}]+\}", "{}", p))
        for p, ops in spec["paths"].items()
        for m in ops
        if m in ("get", "post", "put", "patch", "delete")
    }


def module_exists(mod: str) -> bool:
    rel = Path(*mod.split("."))
    return (ROOT / f"{rel}.py").exists() or (ROOT / rel / "__main__.py").exists() or (ROOT / rel / "__init__.py").exists()


def curl_calls(block: str) -> list[tuple[str, str]]:
    out = []
    for stmt in re.split(r"\n(?=curl )|\n\n", block.replace("\\\n", " ")):
        if "curl" not in stmt:
            continue
        url = re.search(r"\$(?:KG|KG_BASE_URL)(/v1/[^\s\"']*)", stmt) or re.search(r"http://localhost:8000(/v1/[^\s\"']*)", stmt)
        if not url:
            continue
        method = (re.search(r"-X\s+([A-Z]+)", stmt) or [None, "POST" if re.search(r"\s-d\s", stmt) else "GET"])[1]
        path = url.group(1).split("?")[0]
        path = re.sub(r"/\$[A-Z_]+|/\$\{[A-Z_]+\}|/<[^>]+>|/\{[^}]+\}|/[0-9a-f-]{36}", "/{}", path)
        out.append((method, path))
    return out


def main() -> int:
    known_targets, known_routes = targets(), routes()
    problems: list[str] = []
    blocks = 0
    files = [*CONTENT.rglob("*.md"), *CONTENT.rglob("*.mdx"), ROOT / "README.md"]
    for f in files:
        text = f.read_text()
        for m in FENCE.finditer(text):
            info, body = m.group(1), m.group(2)
            if not any(w in info.split() for w in ("bash", "sh", "shell")) and "curl" not in body and "make " not in body:
                continue
            blocks += 1
            where = f"{f.relative_to(ROOT)}"
            for line in body.splitlines():
                code = re.sub(r"\s#.*$", "", line.strip()).lstrip("$ ")
                for seg in re.split(r"&&|;|\|\|", code):
                    seg = seg.strip()
                    if not seg.startswith("make "):
                        continue
                    for word in seg.split()[1:]:
                        if "=" in word or word.startswith(("-", '"', "'", "<", "$")):
                            continue
                        if word not in known_targets:
                            problems.append(f"{where}: `make {word}` is not a Makefile target")
            for pm in re.finditer(r"python3? -m ([a-zA-Z0-9_.]+)", body):
                mod = pm.group(1)
                if mod.split(".")[0] in ("examples", "tools", "scripts", "apps") and not module_exists(mod):
                    problems.append(f"{where}: module {mod} does not exist")
            for method, path in curl_calls(body):
                norm = re.sub(r"/\{\}", "/{}", path)
                if (method, norm) not in known_routes:
                    problems.append(f"{where}: {method} {path} is not in the OpenAPI contract")
    for p in sorted(set(problems)):
        print("SNIPPET", p)
    print(f"snippets: {blocks} blocks checked in {len(files)} files, {len(set(problems))} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
