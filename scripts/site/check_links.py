"""Internal link check over the built site: every href and src that points inside the site must exist in dist/ (and every #fragment must have its anchor).
External links are counted, not fetched (CI would flake on other people's sites)."""

from __future__ import annotations

import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlparse

from scripts.site.common import SITE

DIST = SITE / "dist"


class Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if a.get("id"):
            self.ids.add(a["id"] or "")
        if a.get("name") and tag == "a":
            self.ids.add(a["name"] or "")
        for key in ("href", "src"):
            v = a.get(key)
            if v and tag in ("a", "link", "script", "img", "source") and not v.startswith(("mailto:", "tel:", "data:", "javascript:")):
                self.links.append(v)


def target(url: str, here: Path) -> Path | None:
    p = urlparse(url)
    if p.scheme in ("http", "https") or p.netloc:
        return None
    path = unquote(p.path)
    if not path:
        return here
    base = DIST / path.lstrip("/") if path.startswith("/") else (here.parent / path)
    for cand in (base, base / "index.html", base.with_suffix(".html")):
        if cand.is_file():
            return cand
    return base  # missing


def main() -> int:
    if not DIST.exists():
        print("check_links: dist/ is missing, build first")
        return 2
    pages: dict[Path, Page] = {}
    for f in DIST.rglob("*.html"):
        pg = Page()
        pg.feed(f.read_text(errors="ignore"))
        pages[f] = pg
    broken: list[str] = []
    external = 0
    for f, pg in pages.items():
        for link in pg.links:
            if re.match(r"https?://", link) or link.startswith("//"):
                external += 1
                continue
            t = target(link, f)
            if t is None:
                continue
            if not t.is_file():
                broken.append(f"{f.relative_to(DIST)}: {link}")
                continue
            frag = urlparse(link).fragment
            if frag and t.suffix == ".html" and t in pages and frag not in pages[t].ids and not frag.startswith(":~:"):
                broken.append(f"{f.relative_to(DIST)}: {link} (no anchor #{frag})")
    for b in sorted(set(broken)):
        print("BROKEN", b)
    print(f"check_links: {len(pages)} pages, {external} external links not fetched, {len(set(broken))} broken")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
