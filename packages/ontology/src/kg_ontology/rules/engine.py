"""Rule evaluation in the blueprint's order. Pure: the caller supplies the command and a pre-fetched RuleContext."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from kg_ontology.rules.catalog import CATALOG, ORDER, STAGES, RuleKind, check_params
from kg_ontology.rules.context import EndFact, EvalState, ResolvedRule, RuleCommand, RuleContext, Violation
from kg_ontology.spec import OntologySpec, RuleRef

IMPLICIT_ORDER = ["strict_types", "endpoint_types", "identity_rules", "attribute_types", "valid_interval", "classification_allowed"]


@dataclass
class Evaluation:
    violations: list[Violation] = field(default_factory=list)
    effects: list[EndFact] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)
    name: str | None = None
    trace: list[dict[str, Any]] = field(default_factory=list)

    @property
    def outcome(self) -> str:
        actions = {v.action for v in self.violations}
        if "reject" in actions:
            return "reject"
        return "queue_review" if "queue_review" in actions else "accept"


def _effective_action(kind: RuleKind, ref: RuleRef) -> str:
    if ref.action is not None:
        return ref.action
    return "warn" if ref.severity == "warning" else kind.default_action


def declared_rules(spec: OntologySpec, cmd: RuleCommand) -> list[tuple[str, RuleRef]]:
    out: list[tuple[str, RuleRef]] = [("workspace", r) for r in spec.rules]
    if cmd.domain == "entity":
        et = spec.entity_types.get(cmd.entity_type or "")
        if et is not None:
            out += [(f"entity:{cmd.entity_type}", r) for r in et.rules]
    else:
        rt = spec.relationship_types.get(cmd.rel_type or "")
        if rt is not None:
            out += [(f"relationship:{cmd.rel_type}", r) for r in rt.rules]
    return out


def resolve_rules(spec: OntologySpec, cmd: RuleCommand) -> list[ResolvedRule]:
    """Implicit rules first (in a fixed order), then the declared ones that apply to this command, de-duplicated and in catalog order."""
    resolved: list[ResolvedRule] = []
    for name in IMPLICIT_ORDER:
        kind = CATALOG[name]
        if cmd.kind in kind.commands:
            resolved.append(ResolvedRule(kind.kind, kind.params_model(), kind.default_action, None, "implicit"))
    seen: set[tuple[str, str, str]] = set()
    declared: list[ResolvedRule] = []
    for owner, ref in declared_rules(spec, cmd):
        declared_kind = CATALOG.get(ref.kind)
        if declared_kind is None or declared_kind.implicit or cmd.kind not in declared_kind.commands:
            continue
        kind = declared_kind
        params, issues = check_params(kind, ref.params, "params")
        if params is None:
            raise ValueError(
                f"rule '{ref.kind}' has invalid params: {[i.code for i in issues]}"
            )  # a published spec is validated; this is a bug
        action = _effective_action(kind, ref)
        key = (ref.kind, json.dumps(params.model_dump(mode="json"), sort_keys=True), action)
        if key in seen:
            continue
        seen.add(key)
        declared.append(ResolvedRule(ref.kind, params, action, ref.id, owner))
    declared.sort(key=lambda r: (ORDER[r.kind], r.rule_id or "", json.dumps(r.params.model_dump(mode="json"), sort_keys=True)))
    return resolved + declared


def _soften(violations: list[Violation], queue_when: set[str]) -> list[Violation]:
    out: list[Violation] = []
    for v in violations:
        if v.action == "reject" and v.queue_class is not None and v.queue_class in queue_when:
            v = Violation(v.rule_kind, v.code, v.path, "queue_review", v.stage, v.rule_id, v.queue_class)
        out.append(v)
    return out


def evaluate(spec: OntologySpec, cmd: RuleCommand, ctx: RuleContext) -> Evaluation:
    rules = resolve_rules(spec, cmd)
    queue_when = {r.params.when for r in rules if r.kind == "queue_if"}
    state = EvalState(facts=list(ctx.facts))
    result = Evaluation()
    for stage in STAGES:
        found: list[Violation] = []
        for rule in rules:
            kind = CATALOG[rule.kind]
            if kind.stage != stage or kind.evaluator is None or cmd.kind not in kind.commands:
                continue
            found += kind.evaluator(rule, spec, cmd, ctx, state)
        found = _soften(found, queue_when)
        result.violations += found
        actions = {v.action for v in found}
        outcome = (
            "reject" if "reject" in actions else "queue_review" if "queue_review" in actions else "warn" if "warn" in actions else "pass"
        )
        result.trace.append(
            {
                "stage": stage,
                "outcome": outcome,
                "violations": [
                    {"rule_kind": v.rule_kind, "rule_id": v.rule_id, "code": v.code, "path": v.path, "action": v.action} for v in found
                ],
            }
        )
        if outcome == "reject":
            break  # later stages assume a structurally valid command
    if result.outcome != "reject":
        result.effects = list(state.effects)
    result.attributes, result.name = state.attributes, state.name
    return result
