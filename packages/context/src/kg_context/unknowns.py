"""Expected-signal unknowns (phase-6 plan §5.6). Deterministic: no model decides what is missing.

For each requested subject and each `expected_signal` of the profile whose `for_types` match the subject's type: if no AUTHORISED, VERIFIED item satisfies one of its
`satisfied_by`, the package says `No verified <label> for <subject>`. Unknowns are required-tier items (CTX-05): a tight budget never removes one.

The inputs are everything the sources returned (before ranking and packing), so a signal is judged on what the system knows, not on what happened to fit."""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from kg_ontology.spec import ExpectedSignal, SignalSource

from kg_context.candidates import SubjectInfo, unknown_candidate
from kg_context.models import Candidate


def predicates_needed(signals: Iterable[ExpectedSignal]) -> list[str]:
    return sorted({s.memory_predicate for sig in signals for s in sig.satisfied_by if s.memory_predicate})


def unknowns_for(
    signals: Iterable[ExpectedSignal], subjects: list[SubjectInfo], facts: list[Candidate], memory_keys: set[tuple[uuid.UUID, str]]
) -> list[Candidate]:
    held = {(str(sid), pred) for sid, pred in memory_keys}
    out: list[Candidate] = []
    for sig in signals:
        for subject in subjects:
            if subject.type not in sig.for_types:
                continue
            if any(_satisfied(src, subject, facts, held) for src in sig.satisfied_by):
                continue
            out.append(unknown_candidate(sig.key, sig.label, subject))
    return out


def _satisfied(src: SignalSource, subject: SubjectInfo, facts: list[Candidate], held: set[tuple[str, str]]) -> bool:
    if src.relationship is not None:
        for f in facts:
            if f.kind != "fact" or f.rel_type != src.relationship or f.temporal != "current" or f.evidence_status != "verified":
                continue
            source_id, target_id = _ends(f)
            if (src.direction in ("out", "both") and source_id == subject.id) or (
                src.direction in ("in", "both") and target_id == subject.id
            ):
                return True
        return False
    if src.attribute is not None:
        value = subject.attributes.get(src.attribute)
        return value not in (None, "", [], {})
    return (subject.id, src.memory_predicate or "") in held


def _ends(f: Candidate) -> tuple[str, str]:
    """(source entity id, target entity id) of a fact candidate: the assertion key carries the source, `subject_ids` the other endpoint."""
    source_id = f.assertion[0] if f.assertion else ""
    others = [s for s in f.subject_ids if s != source_id]
    return source_id, (others[0] if others else source_id)
