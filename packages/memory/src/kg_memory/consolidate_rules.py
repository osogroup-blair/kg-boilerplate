"""Consolidation rules, pure (phase-8 plan §5.3, ADR-043): which memories are the same claim, and whether a model's synthesis may be believed.

Nothing here touches the database or a model, so the gold set and the unit tests pin every decision.

    reinforce   exact duplicates (same normalised words) and near duplicates (token Jaccard AND cosine both over their thresholds) of ONE scope set, with no assertion that
                disagrees. The survivor keeps its words; the others are folded into it. Distinct claims are never touched.
    synthesize  a model's summary is checked sentence by sentence: every statement cites sources of THIS group, adds no name, number or date the sources do not contain,
                asserts no decision the sources never made, repeats no instruction, and a statement that merges sources may only merge sources that say the same thing.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from kg_memory.extract import asserts_decision
from kg_memory.models import AUTHORITY_RANK

_WORD = re.compile(r"[a-z0-9]+")  # the same cut as the embedder: "trade-offs" and "trade offs" are one claim
_NUMBER = re.compile(r"\d[\d.,:/-]*")
_STOP = frozenset(
    "a an the and or but of to in on at for with from by as is are was were be been being it its this that these those he she they his her their them you your i we our us "
    "not no do does did has have had will would can could should may might must about over into than then so if when while who whom which what also very just".split()
)
# the vocabulary of instructions a summary must not carry out of its sources (an echo of an injected command fails validation: SEC-04)
_INSTRUCTION = re.compile(
    r"(ignore|disregard|forget)\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier)|system prompt|\byou must\b|\bas an ai\b|reveal\s+(all|any|the)|"
    r"from now on|new instructions?",
    re.IGNORECASE,
)


def normalise(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).casefold().split())


def tokens(value: str) -> list[str]:
    return _WORD.findall(normalise(value))


def content_words(value: str) -> frozenset[str]:
    """Words that carry the claim: stop words out, a trailing plural/verb 's' folded, so "likes" and "like" are one word."""
    out = set()
    for w in tokens(value):
        if w in _STOP:
            continue
        out.add(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w)
    return frozenset(out)


def jaccard(a: str, b: str) -> float:
    x, y = content_words(a), content_words(b)
    if not x and not y:
        return 1.0 if normalise(a) == normalise(b) else 0.0
    return len(x & y) / len(x | y)


@dataclass(frozen=True)
class Mem:
    """What the rules need to know about one memory."""

    id: str
    type: str
    content: str
    authority: str
    recorded_at: Any  # a comparable timestamp
    classification: str = "internal"
    assertion: Mapping[str, Any] | None = None
    scopes: tuple[tuple[str, str], ...] = ()
    subjects: tuple[str, ...] = ()
    occurred_at: Any = None
    embedding: tuple[float, ...] | None = None


def assertions_disagree(a: Mem, b: Mem) -> bool:
    """Same subject and predicate, a different object: two claims about one thing that cannot both stand (they are never folded into each other)."""
    if not a.assertion or not b.assertion:
        return False
    same = a.assertion.get("subject_id") == b.assertion.get("subject_id") and a.assertion.get("predicate") == b.assertion.get("predicate")
    return same and a.assertion.get("object") != b.assertion.get("object")


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na, nb = sum(x * x for x in a) ** 0.5, sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def are_duplicates(a: Mem, b: Mem, *, jaccard_min: float, cosine_min: float) -> bool:
    if a.type != b.type or a.classification != b.classification or assertions_disagree(a, b):
        return False
    if normalise(a.content) == normalise(b.content):
        return True
    if jaccard(a.content, b.content) < jaccard_min:
        return False
    # a near duplicate needs the vectors to agree too; without both vectors only an exact duplicate is certain enough to fold
    return a.embedding is not None and b.embedding is not None and cosine(a.embedding, b.embedding) >= cosine_min


def survivor_of(group: Sequence[Mem]) -> Mem:
    """Highest authority, then the earliest recorded, then the smallest id (so a re-run picks the same one)."""
    return min(group, key=lambda m: (-AUTHORITY_RANK.get(m.authority, 0), m.recorded_at, m.id))


def clusters(memories: Sequence[Mem], *, jaccard_min: float, cosine_min: float) -> list[list[Mem]]:
    """Connected components of the duplicate relation, within one scope set. Only components of two or more are returned."""
    parent = {m.id: m.id for m in memories}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(memories):
        for b in memories[i + 1 :]:
            if a.scopes == b.scopes and a.subjects == b.subjects and are_duplicates(a, b, jaccard_min=jaccard_min, cosine_min=cosine_min):
                parent[find(a.id)] = find(b.id)
    by_root: dict[str, list[Mem]] = {}
    for m in memories:
        by_root.setdefault(find(m.id), []).append(m)
    return [sorted(g, key=lambda m: (m.recorded_at, m.id)) for g in by_root.values() if len(g) > 1]


def buckets(memories: Sequence[Mem], *, window_days: int) -> list[list[Mem]]:
    """Episodic memories within `window_days` of each other (by when they happened, else when they were recorded) form one bucket for synthesis."""
    ordered = sorted(memories, key=lambda m: (m.occurred_at or m.recorded_at, m.id))
    out: list[list[Mem]] = []
    last: Any = None
    for m in ordered:
        at = m.occurred_at or m.recorded_at
        if out and last is not None and (at - last).total_seconds() <= window_days * 86400:
            out[-1].append(m)
        else:
            out.append([m])
        last = at
    return out


# ---- synthesis validation -------------------------------------------------------------------------------------------------------------------------


@dataclass
class Statement:
    text: str
    sources: list[str]


@dataclass
class Proposal:
    sources: list[str]
    summary: str
    statements: list[Statement]
    keep_separate: list[tuple[list[str], str]] = field(default_factory=list)


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str = ""  # a stable token: it is the label of the drop metric and the reason in the run record


def _novel(text: str, vocabulary: set[str]) -> bool:
    """A number, a date or a name in `text` that no source contains. A name is a capitalised word that does not open its sentence (the first word of a sentence is
    capitalised for being first; the sources almost always name their subject, so an invented subject is caught by the numbers and by every later name)."""
    for m in _NUMBER.finditer(text):
        token = m.group().strip(".,:/-").casefold()
        if token and token not in vocabulary:
            return True
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        for w in re.findall(r"[A-Za-z][A-Za-z0-9'’-]*", sentence)[1:]:
            if w[0].isupper() and len(w) > 2 and w.casefold() not in _STOP and w.casefold().strip("'’") not in vocabulary:
                return True
    return False


def validate(p: Proposal, group: Mapping[str, Mem], *, same_claim_min: float = 0.3, source_text_decides: bool | None = None) -> Verdict:
    """`group` maps the model-facing handle (M1…) to the memory. The first failing rule names the drop; a proposal is all or nothing."""
    handles = set(group)
    if len(set(p.sources)) < 2 or not set(p.sources) <= handles:
        return Verdict(False, "bad_sources")
    if not p.statements or not p.summary.strip():
        return Verdict(False, "empty")
    vocabulary = {w for m in group.values() for w in tokens(m.content)} | {w.rstrip("s") for m in group.values() for w in tokens(m.content)}
    texts = [p.summary, *[s.text for s in p.statements]]
    for s in p.statements:
        cited = set(s.sources)
        if not cited:
            return Verdict(False, "missing_citation")
        if not cited <= set(p.sources):
            return Verdict(False, "foreign_citation")
        mems = [group[h] for h in cited]
        for i, a in enumerate(mems):
            for b in mems[i + 1 :]:
                if assertions_disagree(a, b):
                    return Verdict(False, "merged_conflicting_claims")
                if jaccard(a.content, b.content) < same_claim_min and normalise(a.content) != normalise(b.content):
                    return Verdict(
                        False, "merged_distinct_claims"
                    )  # "dislikes commute" and "wants remote work" are two memories, whatever the model says
    sources_decide = source_text_decides if source_text_decides is not None else any(asserts_decision(m.content) for m in group.values())
    for t in texts:
        if _INSTRUCTION.search(t):
            return Verdict(False, "echoed_instruction")
        if _novel(t, vocabulary):
            return Verdict(False, "invented_detail")
        if asserts_decision(t) and not sources_decide:
            return Verdict(False, "escalated_claim")
    covered = {h for s in p.statements for h in s.sources}
    if not covered <= set(p.sources):
        return Verdict(False, "foreign_citation")
    return Verdict(True)
