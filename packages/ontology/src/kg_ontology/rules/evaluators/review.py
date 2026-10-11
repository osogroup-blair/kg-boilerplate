"""Review rules. Commands are never inferred, so require_review_if_inferred passes in P2; it is stored and tested now so P4 can rely on it."""

from __future__ import annotations

from kg_ontology.rules.context import EvalState, ResolvedRule, RuleCommand, RuleContext, Violation, violation
from kg_ontology.spec import OntologySpec

STAGE = "review"


def require_review_if_inferred(
    rule: ResolvedRule, spec: OntologySpec, cmd: RuleCommand, ctx: RuleContext, state: EvalState
) -> list[Violation]:
    if cmd.source_type == "inferred":
        return [violation(rule, STAGE, "inferred_requires_review", "source_type")]
    return []
