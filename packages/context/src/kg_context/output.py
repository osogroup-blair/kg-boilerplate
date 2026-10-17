"""From packed candidates to the response (pure): the typed arrays, the citations of exactly what was packed, and the exclusion counts."""

from __future__ import annotations

from kg_context.models import Candidate, CitationOut, ContextOut, ItemOut
from kg_context.render import HandleBook, item_text


def item_out(c: Candidate) -> ItemOut:
    return ItemOut(
        handle=c.handle,
        kind=c.kind,
        id=c.id,
        section=c.section,
        tier=c.tier,
        text=item_text(c, "structured", 10**6),  # neutralised like the rendered line: a consumer may embed either
        citations=list(c.shown),
        supported=c.supported,
        label=c.label,
        evidence_status=c.evidence_status if c.evidence_status in ("verified", "unverified") else None,
        authority_class=c.authority_class,
        agent_generated=c.authority_class == "agent_generated",
        memory_type=c.memory_type,
        rel_type=c.rel_type,
        valid_from=c.valid_from,
        valid_to=c.valid_to,
        score=c.score,
        tokens=c.tokens,
    )


def fill(out: ContextOut, selected: list[Candidate], book: HandleBook) -> None:
    """Sorts the packed items into the blueprint's arrays and attaches the citations they actually use, in first-use order."""
    for c in selected:
        item = item_out(c)
        if c.kind == "fact":
            out.facts.append(item)
        elif c.kind == "memory" and c.memory_type == "procedural":
            out.instructions.append(item)
        elif c.kind == "memory":
            out.memories.append(item)
        elif c.kind == "unknown":
            out.unknowns.append(item)
        else:
            out.conflicts.append(item)
    seen: dict[str, CitationOut] = {}
    for c in selected:
        for h in c.shown:
            cite = book.citations.get(h)
            if cite is not None and h not in seen:
                seen[h] = cite
    out.citations = list(seen.values())


def exclusion_counts(candidates: list[Candidate]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for c in candidates:
        if c.exclusion is not None:
            counts[c.exclusion] = counts.get(c.exclusion, 0) + 1
    return dict(sorted(counts.items()))
