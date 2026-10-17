"""Text helpers for the pure core: term overlap (the lexical half of relevance) and the neutraliser (CTX-08).

Retrieved content is DATA. Whatever a memory or a fact says, it must stay inside its own bullet of the rendered package: no newline, no heading, no bracket that could
open a citation or close the envelope, no role marker that reads like a turn boundary. `neutralize` is the only way item text reaches the renderer."""

from __future__ import annotations

import re
import unicodedata

from kg_memory.search import STOPWORDS

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_ROLE = re.compile(r"^\s*(?:system|assistant|user|human|developer|tool|ai|model|instruction|instructions)\s*[:：>\-]", re.IGNORECASE)
_BAD_CATEGORIES = frozenset(
    {"Cc", "Cf", "Co", "Cs", "Zl", "Zp"}
)  # controls, format (zero-width, bidi), private use, surrogates, line / paragraph separators
_SUFFIXES = ("ing", "ed", "es", "s")
_NAME_STOP = frozenset({"mr", "mrs", "ms", "dr", "the", "of"})


def stem(word: str) -> str:
    """A crude, language-agnostic stem: one common suffix off, then the first six characters. `works` and `working` meet, so do `employer` and `employment`.
    It is only used to MEASURE overlap; nothing is ever shown stemmed."""
    w = word.lower()
    for suffix in _SUFFIXES:
        if w.endswith(suffix) and len(w) - len(suffix) >= 3:
            w = w[: -len(suffix)]
            break
    return w[:6]


def terms(text: str, *, drop: frozenset[str] = frozenset()) -> list[str]:
    """Distinct stems of the topical words of `text`, in order; function words and the stems in `drop` (a subject's own name) are ignored."""
    seen: dict[str, None] = {}
    for raw in _WORD.findall(text):
        w = raw.lower()
        if len(w) < 2 or w in STOPWORDS:
            continue
        s = stem(w)
        if s and s not in drop:
            seen[s] = None
    return list(seen)


def name_stems(names: list[str]) -> frozenset[str]:
    out: set[str] = set()
    for name in names:
        for raw in _WORD.findall(name):
            w = raw.lower()
            if len(w) >= 2 and w not in _NAME_STOP:
                out.add(stem(w))
    return frozenset(out)


def coverage(query_terms: list[str], text_terms: set[str] | list[str]) -> float:
    """The share of the query's topical terms the text contains, in [0, 1] (0 when the query has no topical terms)."""
    if not query_terms:
        return 0.0
    have = set(text_terms)
    return sum(1 for t in query_terms if t in have) / len(query_terms)


def jaccard(a: set[str] | list[str], b: set[str] | list[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def query_of(task: str, names: list[str]) -> str:
    """The retrieval text: the task with the subjects' own names removed (a subject filter already says WHO; the name only matches everything about them). Falls
    back to the task itself if nothing topical is left."""
    drop = name_stems(names)
    kept = [w for w in _WORD.findall(task) if stem(w.lower()) not in drop]
    topical = [w for w in kept if w.lower() not in STOPWORDS and len(w) > 1]
    return " ".join(kept) if topical else task


def neutralize(value: str, limit: int = 400) -> str:
    """One line of inert text: NFKC-folded, no control or invisible characters, no brackets, backticks or `<|...|>` tokens, no leading role marker."""
    s = unicodedata.normalize("NFKC", value)
    s = "".join(" " if ch in "\n\r\t\v\f" else ("" if unicodedata.category(ch) in _BAD_CATEGORIES else ch) for ch in s)
    s = s.replace("[", "(").replace("]", ")").replace("`", "'").replace("<|", "< |").replace("|>", "| >")
    s = " ".join(s.split())  # (a bullet is always rendered after "- ", so a leading `#` or `>` can never start a heading or a quote)
    if _ROLE.match(s):
        s = "quoted: " + s
    if len(s) > limit:
        s = s[: max(1, limit - 1)].rstrip() + "…"
    return s
