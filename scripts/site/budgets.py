"""Page-weight budgets on the built site (a stand-in for Lighthouse CI, which needs a browser download): the JavaScript and CSS a page asks for up front, gzipped.

It reads each page's `<script src>`, `<link rel=stylesheet|modulepreload>` and the island entry points, then adds the chunks those modules import statically, so it is an
honest upper bound on what a visitor downloads before the page is interactive."""

from __future__ import annotations

import gzip
import re
import sys
from pathlib import Path

from scripts.site.common import SITE

DIST = SITE / "dist"
BUDGETS_KB = {
    "/": 150,
    "/see-it/sarah/": 260,
    "/docs/get-started/quickstart/": 200,
    "/docs/concepts/context-engine/": 260,
}  # JS + CSS gzipped; the landing page budget is the plan's 150 KB
HTML_KB = 120


def gz(path: Path) -> int:
    return len(gzip.compress(path.read_bytes(), 6))


def assets(html: str) -> set[str]:
    urls = set(re.findall(r'<script[^>]+src="([^"]+)"', html))
    urls |= set(re.findall(r'<link[^>]+rel="(?:stylesheet|modulepreload)"[^>]*href="([^"]+)"', html)) | set(
        re.findall(r'<link[^>]+href="([^"]+)"[^>]*rel="(?:stylesheet|modulepreload)"', html)
    )
    urls |= set(re.findall(r'(?:component-url|renderer-url)="([^"]+)"', html))
    return {u for u in urls if u.startswith("/") and not u.startswith("//")}


def closure(urls: set[str]) -> set[Path]:
    seen: set[Path] = set()
    todo = [DIST / u.lstrip("/").split("?")[0] for u in urls]
    while todo:
        p = todo.pop()
        if p in seen or not p.is_file():
            continue
        seen.add(p)
        if p.suffix == ".js":
            # static imports only (`import ... from "./x.js"` / `import"./x.js"`); dynamic import() chunks load on demand
            for m in re.finditer(
                r'(?:^|[;}\s])(?:import|export)\s*(?:[^;"\']*?from\s*)?["\'](\./[^"\']+\.js)["\']', p.read_text(errors="ignore")
            ):
                todo.append((p.parent / m.group(1)).resolve())
    return seen


def main() -> int:
    if not DIST.exists():
        print("budgets: dist/ is missing, build first")
        return 2
    bad = 0
    for page, budget in BUDGETS_KB.items():
        f = DIST / page.lstrip("/") / "index.html" if page != "/" else DIST / "index.html"
        html = f.read_text()
        files = closure(assets(html))
        kb = sum(gz(p) for p in files if p.suffix in (".js", ".css")) / 1024
        html_kb = len(gzip.compress(html.encode())) / 1024
        over = kb > budget or html_kb > HTML_KB
        bad += over
        print(
            f"  {'FAIL' if over else 'ok  '} {page:<34} js+css {kb:6.1f} KB gz (budget {budget})   html {html_kb:5.1f} KB gz (budget {HTML_KB})"
        )
    print(f"budgets: {'OK' if not bad else f'{bad} over budget'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
