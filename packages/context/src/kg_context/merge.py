"""Merge and deduplicate (phase-6 plan §5.4). Pure: marks candidates, never deletes them, so the trace can say why something is not in the package.

* The same fact from two retrievers is ONE candidate (the best features of each).
* A memory that merely restates a fact the package already holds loses to the fact (`duplicate_of_fact`); an EPISODIC memory linked to a fact is kept, because it adds
  the circumstances, and is marked `supports` that fact. Guidance (procedural) is never a restatement.
* Near-identical memories of one type collapse to the best-ranked (`near_duplicate`), recording which one won.
* Different statements are never collapsed: two claims that disagree both stay, and the conflict is surfaced elsewhere."""

from __future__ import annotations

from kg_context.models import Candidate
from kg_context.text import jaccard, terms

NEAR_DUPLICATE = 0.8


def merge_facts(found: list[Candidate]) -> list[Candidate]:
    """One candidate per fact id: the first seen is kept, with the better relevance, rank hint and evidence of the duplicates."""
    by_id: dict[str, Candidate] = {}
    for c in found:
        if c.kind != "fact":
            continue
        kept = by_id.get(c.key)
        if kept is None:
            by_id[c.key] = c
            continue
        kept.relevance = max(kept.relevance, c.relevance)
        kept.rank_hint = max(kept.rank_hint, c.rank_hint)
        kept.incident = kept.incident or c.incident
        if c.evidence_n > kept.evidence_n:
            kept.evidence, kept.evidence_n = c.evidence, c.evidence_n
    return list(by_id.values())


def dedupe(candidates: list[Candidate]) -> None:
    """Marks `exclusion` / `supports` in place. Facts first (they win), then memories in descending relevance (the better one stays)."""
    facts = [c for c in candidates if c.kind == "fact" and c.exclusion is None]
    fact_ids = {c.id for c in facts if c.id}
    by_assertion = {c.assertion: c for c in facts if c.assertion}
    memories = sorted((c for c in candidates if c.kind == "memory" and c.exclusion is None), key=lambda c: (-c.relevance, c.key))
    for m in memories:
        linked = [f for f in m.related_fact_ids if f in fact_ids]
        if m.assertion is not None and m.assertion in by_assertion:
            twin = by_assertion[m.assertion]
            if twin.id:
                linked.append(twin.id)
        if not linked:
            continue
        if m.memory_type == "episodic":
            m.supports = linked[0]
        elif m.memory_type in ("semantic", "working"):
            m.exclusion, m.duplicate_of = "duplicate_of_fact", linked[0]
    kept_terms: dict[str, list[tuple[Candidate, set[str]]]] = {}
    for m in memories:
        if m.exclusion is not None:
            continue
        mine = set(terms(f"{m.text} {m.raw_text}"))
        group = kept_terms.setdefault(m.memory_type or "", [])
        winner = next((k for k, t in group if jaccard(mine, t) >= NEAR_DUPLICATE), None)
        if winner is not None:
            m.exclusion, m.duplicate_of = "near_duplicate", winner.id
        else:
            group.append((m, mine))
