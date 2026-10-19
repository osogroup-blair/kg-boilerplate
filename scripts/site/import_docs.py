"""Imports the repository's Markdown into the Starlight content tree.

One source of truth: the files stay in `docs/`, `examples/` and the repository root. This script reads the allowlist in `apps/site/import-map.yaml`,
adds frontmatter, rewrites links (between imported files to site URLs, to everything else to GitHub), and strips `<!-- internal -->` blocks.
Nothing it writes is committed (see the generated block in apps/site/.gitignore)."""

from __future__ import annotations

import posixpath
import re
import sys
from pathlib import Path
from typing import Any

import yaml

from scripts.site.common import BLOB, CONTENT, GITHUB, ROOT, SITE, TREE, escape_stray_angles

LINK = re.compile(r"(?<!\!)\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
FENCE = re.compile(r"^(```|~~~)")
INTERNAL = re.compile(r"<!--\s*internal\s*-->.*?<!--\s*/internal\s*-->", re.S)


def load_map() -> tuple[list[dict[str, Any]], dict[str, str]]:
    spec = yaml.safe_load((SITE / "import-map.yaml").read_text())
    entries: list[dict[str, Any]] = []
    for e in spec["files"]:
        if (ROOT / e["src"]).exists():
            entries.append(dict(e))
        elif not e.get("optional"):
            raise SystemExit(f"import_docs: {e['src']} is in the map but missing")
    for g in spec["globs"]:
        for i, p in enumerate(sorted(ROOT.glob(g["src"]))):
            rel = p.relative_to(ROOT).as_posix()
            dest = g["dest"].format(stem=p.stem, stem_lower=p.stem.lower())
            entries.append({"src": rel, "dest": dest, "order": g.get("order_start", 1) + i, "title": None})
    urls = {e["src"]: f"/docs/{e['dest']}/" for e in entries}
    return entries, urls


def rewrite_links(text: str, src: str, urls: dict[str, str]) -> str:
    out: list[str] = []
    fenced = False
    for line in text.split("\n"):
        if FENCE.match(line.strip()):
            fenced = not fenced
        if fenced:
            out.append(line)
            continue

        def sub(m: re.Match[str]) -> str:
            label, target = m.group(1), m.group(2)
            if re.match(r"^(https?:|mailto:|#)", target):
                return m.group(0)
            path, _, anchor = target.partition("#")
            repo = posixpath.normpath(posixpath.join(posixpath.dirname(src), path))
            frag = f"#{anchor}" if anchor else ""
            if repo in urls:
                return f"[{label}]({urls[repo]}{frag})"
            if (ROOT / repo).is_dir():
                return f"[{label}]({TREE}/{repo}{frag})"
            if (ROOT / repo).exists():
                return f"[{label}]({BLOB}/{repo}{frag})"
            return f"[{label}]({target})"

        out.append(escape_stray_angles(LINK.sub(sub, line)))
    return "\n".join(out)


def split_title(text: str) -> tuple[str, str]:
    m = re.match(r"\s*#\s+(.+?)\s*\n", text)
    if not m:
        return "", text
    return m.group(1).strip(), text[m.end() :].lstrip("\n")


def describe(body: str) -> str:
    m = re.search(r"\*\*Status:\*\*\s*([^\n]+?)(?:\s{2,}\*\*|$)", body)
    if m:
        return f"Status: {m.group(1).strip()}"[:200]
    for para in re.split(r"\n\s*\n", body):
        p = para.strip()
        if p and not p.startswith(("#", "```", "|", "-", ">", "<", "!")):
            return re.sub(r"\s+", " ", re.sub(r"[`*_\[\]]", "", re.sub(r"\]\([^)]*\)", "]", p)))[:200]
    return ""


def yq(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def run() -> list[Path]:
    entries, urls = load_map()
    written: list[Path] = []
    for e in entries:
        raw = (ROOT / e["src"]).read_text()
        raw = INTERNAL.sub("", raw)
        title, body = split_title(raw)
        body = rewrite_links(body, e["src"], urls)
        title = e.get("title") or title or Path(e["src"]).stem
        desc = e.get("description") or describe(body)
        front = [
            "---",
            f"title: {yq(title)}",
            *([f"description: {yq(desc)}"] if desc else []),
            f"editUrl: {GITHUB}/edit/main/{e['src']}",
            "sidebar:",
            f"  order: {e['order']}",
            "---",
            "",
            f"> Imported from [`{e['src']}`]({BLOB}/{e['src']}) in the repository; edit it there.",
            "",
        ]
        dest = CONTENT / f"{e['dest']}.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("\n".join(front) + body.rstrip() + "\n")
        written.append(dest)
    return written


if __name__ == "__main__":
    files = run()
    print(f"import_docs: {len(files)} pages")
    sys.exit(0)
