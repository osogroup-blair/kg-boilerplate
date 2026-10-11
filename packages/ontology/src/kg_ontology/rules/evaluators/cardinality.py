"""Cardinality rules: how many active facts may exist for a source at a time. They run after succession, on the chain as it will be."""

from __future__ import annotations

import datetime as dt

from kg_ontology.rules.context import (
    NEG_INF,
    POS_INF,
    EvalState,
    FactSnap,
    ResolvedRule,
    RuleCommand,
    RuleContext,
    Violation,
    overlaps,
    violation,
)
from kg_ontology.spec import OntologySpec

STAGE = "temporal_rules"


def _candidates(cmd: RuleCommand, state: EvalState, *, same_target: bool) -> list[FactSnap]:
    assert cmd.source is not None and cmd.target is not None
    return [
        f
        for f in state.facts
        if f.status == "active"
        and f.rel_type == cmd.rel_type
        and f.source_id == cmd.source.id
        and (not same_target or f.target_id == cmd.target.id)
    ]


def _depth(cmd: RuleCommand, facts: list[FactSnap]) -> int:
    """Highest number of facts (including the new one) valid at the same instant inside the new fact's interval (sweep line over half-open intervals)."""
    n_start, n_end = cmd.valid_from or NEG_INF, cmd.valid_to or POS_INF
    events: list[tuple[dt.datetime, int]] = []
    for f in facts:
        if not overlaps(n_start, n_end, f.start, f.end):
            continue
        events.append((max(f.start, n_start), 1))
        events.append((min(f.end, n_end), -1))
    events.sort(key=lambda e: (e[0], e[1]))  # at equal instants an end (-1) sorts before a start (+1)
    best = depth = 0
    for _, delta in events:
        depth += delta
        best = max(best, depth)
    return best + 1


def unique_active_pair(rule: ResolvedRule, spec: OntologySpec, cmd: RuleCommand, ctx: RuleContext, state: EvalState) -> list[Violation]:
    if cmd.kind != "relationship.assert" or cmd.source is None or cmd.target is None:
        return []
    n_start, n_end = cmd.valid_from or NEG_INF, cmd.valid_to or POS_INF
    clash = [f for f in _candidates(cmd, state, same_target=True) if overlaps(n_start, n_end, f.start, f.end)]
    return [violation(rule, STAGE, "duplicate_active_pair", "target", queue_class="cardinality_violation")] if clash else []


def max_active_per_source(rule: ResolvedRule, spec: OntologySpec, cmd: RuleCommand, ctx: RuleContext, state: EvalState) -> list[Violation]:
    if cmd.kind != "relationship.assert" or cmd.source is None or cmd.target is None:
        return []
    params = rule.params
    facts = _candidates(cmd, state, same_target="target" in params.match)
    if _depth(cmd, facts) > params.n:
        return [violation(rule, STAGE, "max_active_exceeded", "source", queue_class="cardinality_violation")]
    return []
