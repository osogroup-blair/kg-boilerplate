"""Structural rules (blueprint evaluation order, stages 3-4): types exist, endpoints match the declaration, identity and classification are allowed."""

from __future__ import annotations

from kg_ontology.rules.context import EvalState, ResolvedRule, RuleCommand, RuleContext, Violation, violation
from kg_ontology.spec import OntologySpec

SCHEMA = "schema_validation"
CLASSIFICATION = "source_classification"


def strict_types(rule: ResolvedRule, spec: OntologySpec, cmd: RuleCommand, ctx: RuleContext, state: EvalState) -> list[Violation]:
    out: list[Violation] = []
    if cmd.domain == "entity":
        et = spec.entity_types.get(cmd.entity_type or "")
        if et is None:
            return [violation(rule, SCHEMA, "unknown_entity_type", "entity_type")]
        declared = set(et.attributes)
    else:
        rt = spec.relationship_types.get(cmd.rel_type or "")
        if rt is None:
            return [violation(rule, SCHEMA, "unknown_relationship_type", "rel_type")]
        declared = set(rt.attributes)
    strict = spec.ontology.mode == "strict"
    for key in sorted(cmd.attributes):
        if cmd.domain == "entity" and key == "name":
            out.append(
                violation(rule, SCHEMA, "attribute_is_node_name", "attributes.name")
            )  # the name is the node's own name (M1): send it as `name`
        elif key not in declared:
            out.append(violation(rule, SCHEMA, "unknown_attribute", f"attributes.{key}", action=None if strict else "warn"))
    return out


def endpoint_types(rule: ResolvedRule, spec: OntologySpec, cmd: RuleCommand, ctx: RuleContext, state: EvalState) -> list[Violation]:
    if cmd.kind != "relationship.assert" or cmd.source is None or cmd.target is None:
        return []
    rt = spec.relationship_types.get(cmd.rel_type or "")
    if rt is None:
        return []
    out: list[Violation] = []
    if cmd.source.type_key != rt.source:
        out.append(violation(rule, SCHEMA, "disallowed_endpoint", "source", queue_class="disallowed_endpoint"))
    if cmd.target.type_key != rt.target:
        out.append(violation(rule, SCHEMA, "disallowed_endpoint", "target", queue_class="disallowed_endpoint"))
    return out


def identity_rules(rule: ResolvedRule, spec: OntologySpec, cmd: RuleCommand, ctx: RuleContext, state: EvalState) -> list[Violation]:
    """External identifiers: only declared systems are accepted, and a type whose identity key is `external_id` needs one at creation (KGR-01)."""
    if cmd.domain != "entity":
        return []
    et = spec.entity_types.get(cmd.entity_type or "")
    if et is None:
        return []
    out = [
        violation(rule, SCHEMA, "unknown_external_id_system", f"external_ids[{i}].system")
        for i, (system, _) in enumerate(cmd.external_ids)
        if system not in et.identity.external_id_systems
    ]
    if cmd.kind == "entity.create" and et.identity.key == "external_id" and not cmd.external_ids:
        out.append(violation(rule, SCHEMA, "external_id_required", "external_ids"))
    return out


def classification_allowed(rule: ResolvedRule, spec: OntologySpec, cmd: RuleCommand, ctx: RuleContext, state: EvalState) -> list[Violation]:
    """Stage 4: the classification must be one the type allows. A fact must be allowed by BOTH endpoint types."""
    if cmd.kind == "relationship.end":
        return []
    if cmd.domain == "entity":
        et = spec.entity_types.get(cmd.entity_type or "")
        allowed = set(et.allowed_classifications) if et else set()
        return (
            []
            if et is None or cmd.classification in allowed
            else [violation(rule, CLASSIFICATION, "classification_not_allowed", "classification")]
        )
    types = [spec.entity_types.get(e.type_key) for e in (cmd.source, cmd.target) if e is not None]
    if any(t is not None and cmd.classification not in t.allowed_classifications for t in types):
        return [violation(rule, CLASSIFICATION, "classification_not_allowed", "classification")]
    return []
