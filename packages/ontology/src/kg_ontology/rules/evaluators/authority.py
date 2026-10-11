"""Authority rules (blueprint evaluation stage 7): who may assert what, and from which source."""

from __future__ import annotations

from kg_ontology.rules.context import EvalState, ResolvedRule, RuleCommand, RuleContext, Violation, violation
from kg_ontology.spec import AUTHORITY_RANK, OntologySpec

STAGE = "mutation_authorization"


def require_source(rule: ResolvedRule, spec: OntologySpec, cmd: RuleCommand, ctx: RuleContext, state: EvalState) -> list[Violation]:
    out: list[Violation] = []
    if not (cmd.source_system or "").strip():
        out.append(violation(rule, STAGE, "source_required", "source_system"))
    if not (cmd.source_ref or "").strip():
        out.append(violation(rule, STAGE, "source_required", "source_ref"))
    return out


def min_authority(rule: ResolvedRule, spec: OntologySpec, cmd: RuleCommand, ctx: RuleContext, state: EvalState) -> list[Violation]:
    if AUTHORITY_RANK.get(cmd.authority, 0) < AUTHORITY_RANK[rule.params.level]:
        return [violation(rule, STAGE, "insufficient_authority", "authority")]
    return []
