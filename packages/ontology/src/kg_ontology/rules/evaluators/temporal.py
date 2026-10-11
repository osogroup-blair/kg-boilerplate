"""Temporal rules. The platform never defaults valid time to 'now' (blueprint, KGR-03), and a new fact in a chain ENDS its predecessor rather than
superseding it: the old employment is still a true historical fact (ADR-023)."""

from __future__ import annotations

from kg_ontology.rules.context import (
    NEG_INF,
    POS_INF,
    EndFact,
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


def preserve_valid_time(rule: ResolvedRule, spec: OntologySpec, cmd: RuleCommand, ctx: RuleContext, state: EvalState) -> list[Violation]:
    if cmd.kind != "relationship.assert" or not rule.params.require_valid_from:
        return []
    if cmd.valid_from is None and not cmd.valid_from_unknown:
        return [violation(rule, STAGE, "valid_from_required", "valid_from")]
    return []


def succession(rule: ResolvedRule, spec: OntologySpec, cmd: RuleCommand, ctx: RuleContext, state: EvalState) -> list[Violation]:
    """A new fact whose valid_from falls inside an existing matching fact's interval ends that fact at the new valid_from.

    * existing starts earlier and the new fact reaches at least as far -> predecessor, ended at the new start (effect)
    * existing starts earlier but the new fact would sit inside it       -> succession_conflict (we do not split a fact in two)
    * both start at the same instant                                      -> succession_conflict (ambiguous order)
    * existing starts later and the new interval reaches into it          -> out_of_order_succession
    * no overlap (the new fact fits a gap or lies entirely before)       -> nothing to do
    """
    if cmd.kind != "relationship.assert" or cmd.source is None or cmd.target is None:
        return []
    same_target = "target" in rule.params.match
    n_start, n_end = cmd.valid_from or NEG_INF, cmd.valid_to or POS_INF
    out: list[Violation] = []
    new_facts: list[FactSnap] = []
    ended: dict[str, EndFact] = {}
    for f in sorted(state.facts, key=lambda x: (x.start, x.id)):
        if f.status != "active" or f.rel_type != cmd.rel_type or f.source_id != cmd.source.id:
            continue
        if same_target and f.target_id != cmd.target.id:
            continue
        if not overlaps(n_start, n_end, f.start, f.end):
            continue
        if f.start == n_start:
            out.append(violation(rule, STAGE, "succession_conflict", "valid_from"))
        elif f.start > n_start:
            out.append(violation(rule, STAGE, "out_of_order_succession", "valid_from"))
        elif n_end < f.end:
            out.append(violation(rule, STAGE, "succession_conflict", "valid_to"))
        else:
            ended[f.id] = EndFact(f.id, n_start)
    if out:
        return out
    for f in state.facts:
        if f.id in ended:
            f = FactSnap(f.id, f.rel_type, f.source_id, f.target_id, f.valid_from, ended[f.id].valid_to, f.valid_from_unknown, f.status)
        new_facts.append(f)
    state.facts[:] = new_facts
    state.effects.extend(ended.values())
    return []
