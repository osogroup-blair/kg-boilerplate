"""Shared paths for the site generators."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SITE = ROOT / "apps" / "site"
CONTENT = SITE / "src" / "content" / "docs" / "docs"
GENERATED = SITE / "src" / "data" / "generated"
GITHUB = "https://github.com/osogroup-blair/kg-boilerplate"
BLOB = f"{GITHUB}/blob/main"
TREE = f"{GITHUB}/tree/main"


import re  # noqa: E402

KNOWN_HTML = "details|summary|br|b|i|em|strong|code|pre|p|a|img|sup|sub|kbd|table|thead|tbody|tr|td|th|ul|ol|li|div|span|hr|h[1-6]|picture|source|section|small|figure|figcaption|mark|blockquote"
_CODE = re.compile(r"(`[^`\n]*`)")
_STRAY = re.compile(rf"<(?!/?(?:{KNOWN_HTML})\b|https?:|mailto:|!--)(?=[A-Za-z])")


def escape_stray_angles(line: str) -> str:
    """A placeholder such as `<date>` or `<utc time>` outside code is not HTML: in a Markdown page the browser would swallow it. Escape the `<` (code spans are left alone)."""
    return "".join(part if part.startswith("`") else _STRAY.sub("&lt;", part) for part in _CODE.split(line))
