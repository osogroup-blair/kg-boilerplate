"""Runs rule scenarios: the fixtures under tests/fixtures/rules and the `tests:` embedded in an ontology (blueprint 'Rule … test cases').

A scenario is `{given: {facts: [...], current_attributes: {...}}, command: {...}, expect: accept|reject|queue_review}`. Missing endpoint details are filled
from the relationship declaration so scenarios stay short."""

from __future__ import annotations

from typing import Any

from kg_ontology.report import RuleTestResult
from kg_ontology.rules.context import CommandKind, EntityRef, FactSnap, RuleCommand, RuleContext
from kg_ontology.rules.engine import Evaluation, evaluate
from kg_ontology.spec import OntologySpec, RuleRef
from kg_ontology.values import parse_instant


def _instant(value: Any) -> Any:
    if value is None:
        return None
    parsed = parse_instant(value)
    if parsed is None:
        raise ValueError(f"not a valid instant: {value!r}")
    return parsed


def _ref(raw: dict[str, Any] | None, type_key: str, default_id: str) -> EntityRef:
    raw = raw or {}
    return EntityRef(
        str(raw.get("id", default_id)), str(raw.get("type", type_key)), str(raw.get("name", "")), str(raw.get("classification", "internal"))
    )


def _fact(raw: dict[str, Any], rel_type: str | None, source_id: str) -> FactSnap:
    return FactSnap(
        id=str(raw["id"]),
        rel_type=str(raw.get("rel_type", rel_type)),
        source_id=str(raw.get("source", source_id)),
        target_id=str(raw.get("target", "t")),
        valid_from=_instant(raw.get("valid_from")),
        valid_to=_instant(raw.get("valid_to")),
        valid_from_unknown=bool(raw.get("valid_from_unknown", False)),
        status=str(raw.get("status", "active")),
    )


def build(
    spec: OntologySpec,
    given: dict[str, Any],
    command: dict[str, Any],
    *,
    default_entity_type: str | None = None,
    default_rel_type: str | None = None,
) -> tuple[RuleCommand, RuleContext]:
    kind: CommandKind = command.get("kind") or ("entity.create" if default_entity_type and not default_rel_type else "relationship.assert")
    common: dict[str, Any] = {
        "classification": command.get("classification", "internal"),
        "authority": command.get("authority", "authoritative_system"),
        "source_system": command.get("source_system"),
        "source_ref": command.get("source_ref"),
        "source_type": command.get("source_type", "command"),
    }
    if kind.startswith("entity."):
        etype = command.get("entity_type", default_entity_type)
        cmd = RuleCommand(
            kind=kind,
            entity_type=etype,
            name=command.get("name"),
            attributes=dict(command.get("attributes") or {}),
            external_ids=tuple((str(x["system"]), str(x["value"])) for x in command.get("external_ids") or []),
            entity=_ref(command.get("entity"), etype or "", "e") if kind == "entity.update" else None,
            **common,
        )
        return cmd, RuleContext(current_attributes=dict(given.get("current_attributes") or {}))
    rel_type = command.get("rel_type", default_rel_type)
    rt = spec.relationship_types.get(rel_type or "")
    source = _ref(command.get("source"), rt.source if rt else "", "s")
    target = _ref(command.get("target"), rt.target if rt else "", "t")
    target_fact = _fact(command["fact"], rel_type, source.id) if kind == "relationship.end" and command.get("fact") else None
    cmd = RuleCommand(
        kind=kind,
        rel_type=rel_type,
        attributes=dict(command.get("attributes") or {}),
        source=source,
        target=target,
        valid_from=_instant(command.get("valid_from")),
        valid_to=_instant(command.get("valid_to")),
        valid_from_unknown=bool(command.get("valid_from_unknown", False)),
        target_fact=target_fact,
        **common,
    )
    facts = tuple(_fact(f, rel_type, source.id) for f in given.get("facts") or [])
    return cmd, RuleContext(facts=facts)


def run_case(
    spec: OntologySpec,
    given: dict[str, Any],
    command: dict[str, Any],
    *,
    default_entity_type: str | None = None,
    default_rel_type: str | None = None,
) -> Evaluation:
    cmd, ctx = build(spec, given, command, default_entity_type=default_entity_type, default_rel_type=default_rel_type)
    return evaluate(spec, cmd, ctx)


def _rule_lists(spec: OntologySpec) -> list[tuple[str, list[RuleRef], str | None, str | None]]:
    """(path prefix, rules, default entity type, default relationship type)"""
    out: list[tuple[str, list[RuleRef], str | None, str | None]] = [("rules", spec.rules, None, None)]
    out += [(f"entity_types.{n}.rules", t.rules, n, None) for n, t in spec.entity_types.items()]
    out += [(f"relationship_types.{n}.rules", t.rules, None, n) for n, t in spec.relationship_types.items()]
    return out


def run_embedded_tests(spec: OntologySpec) -> list[RuleTestResult]:
    results: list[RuleTestResult] = []
    for prefix, rules, etype, rtype in _rule_lists(spec):
        for i, rule in enumerate(rules):
            for j, test in enumerate(rule.tests):
                path = f"{prefix}[{i}].tests[{j}]"
                try:
                    ev = run_case(spec, test.given, test.command, default_entity_type=etype, default_rel_type=rtype)
                except (ValueError, KeyError, TypeError) as exc:
                    results.append(
                        RuleTestResult(path=path, name=test.name, expected=test.expect, actual=f"error:{type(exc).__name__}", passed=False)
                    )
                    continue
                codes = sorted({v.code for v in ev.violations})
                results.append(
                    RuleTestResult(
                        path=path, name=test.name, expected=test.expect, actual=ev.outcome, passed=ev.outcome == test.expect, codes=codes
                    )
                )
    return results
