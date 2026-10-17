"""Temporal reconciliation (phase-6 plan §5.5, CTX-04, RET-03). Pure.

A fact that is not true at the reference time (the `as_of`, or the database's now) is a stale default-state statement: it is dropped unless history is on, in which case
it is kept, labelled historical with its dates, and placed BELOW the current items. A claim that is only proposed is never shown as current; it becomes a conflict line.
A disputed fact is never presented as settled."""

from __future__ import annotations

from kg_context.models import Candidate


def reconcile(candidates: list[Candidate], *, history: bool) -> None:
    for c in candidates:
        if c.exclusion is not None or c.kind != "fact":
            continue
        if c.temporal == "historical" and not history:
            c.exclusion = "stale_default_state"
        elif c.temporal in ("future", "none"):
            c.exclusion = "stale_default_state"
        elif c.temporal in ("proposed", "disputed"):
            c.exclusion = "not_included"  # reported through its conflict line, never as a fact
