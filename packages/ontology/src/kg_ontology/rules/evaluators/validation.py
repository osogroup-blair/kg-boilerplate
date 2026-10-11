"""Validation rules: form only (blueprint: validity is not truth). Scalar types, required, constraints, and the valid-time interval."""

from __future__ import annotations

import datetime as dt
from typing import Any

from kg_ontology.rules.context import EvalState, ResolvedRule, RuleCommand, RuleContext, Violation, violation
from kg_ontology.spec import AttributeSpec, OntologySpec
from kg_ontology.values import MIN_YEAR, coerce_value

SCHEMA = "schema_validation"


def _scalar(value: Any) -> bool:
    return isinstance(value, str | int | float | bool)


def _check_name(
    rule: ResolvedRule, spec_attr: AttributeSpec | None, name: str | None, *, required: bool, out: list[Violation], state: EvalState
) -> None:
    if name is None:
        if required:
            out.append(violation(rule, SCHEMA, "required_attribute", "name"))
        return
    cleaned = name.strip()
    if not cleaned:
        out.append(violation(rule, SCHEMA, "required_attribute", "name"))
        return
    if len(cleaned) > 500:
        out.append(violation(rule, SCHEMA, "too_long", "name"))
        return
    if spec_attr is not None:
        _, err = coerce_value(spec_attr, cleaned)
        if err is not None:
            out.append(violation(rule, SCHEMA, err, "name"))
            return
    state.name = cleaned


def attribute_types(rule: ResolvedRule, spec: OntologySpec, cmd: RuleCommand, ctx: RuleContext, state: EvalState) -> list[Violation]:
    out: list[Violation] = []
    create = cmd.kind in ("entity.create", "relationship.assert")
    if cmd.domain == "entity":
        et = spec.entity_types.get(cmd.entity_type or "")
        if et is None:
            return out
        declared = et.attributes
        name_spec = declared.get("name")
        if cmd.kind == "entity.update" and name_spec is not None and not name_spec.editable and cmd.name is not None:
            out.append(violation(rule, SCHEMA, "attribute_not_editable", "name"))
        elif cmd.kind == "entity.create" or cmd.name is not None:
            _check_name(rule, name_spec, cmd.name, required=cmd.kind == "entity.create", out=out, state=state)
    elif cmd.kind == "relationship.assert":
        rt = spec.relationship_types.get(cmd.rel_type or "")
        if rt is None:
            return out
        declared = rt.attributes
    else:
        return out

    for key, attr in sorted(declared.items()):
        if key == "name" and cmd.domain == "entity":
            continue
        provided = key in cmd.attributes
        value = cmd.attributes.get(key)
        path = f"attributes.{key}"
        if provided and not attr.editable and cmd.kind == "entity.update":
            out.append(violation(rule, SCHEMA, "attribute_not_editable", path))
            continue
        if value is None:
            if create and attr.default is not None:
                norm, err = coerce_value(attr, attr.default)
                if err is None:
                    state.attributes[key] = norm
            elif attr.required and (create or provided):
                out.append(violation(rule, SCHEMA, "required_attribute", path))
            elif provided and cmd.kind == "entity.update":
                state.attributes[key] = None  # clear
            continue
        norm, err = coerce_value(attr, value)
        if err is not None:
            out.append(violation(rule, SCHEMA, err, path))
        else:
            state.attributes[key] = norm
    if spec.ontology.mode == "permissive":
        for key in sorted(set(cmd.attributes) - set(declared) - {"name"}):
            if _scalar(cmd.attributes[key]):
                state.attributes[key] = cmd.attributes[key]
            else:
                out.append(violation(rule, SCHEMA, "wrong_type", f"attributes.{key}"))
    return out


def _bad_year(value: dt.datetime | None) -> bool:
    return value is not None and value.year < MIN_YEAR


def valid_interval(rule: ResolvedRule, spec: OntologySpec, cmd: RuleCommand, ctx: RuleContext, state: EvalState) -> list[Violation]:
    if cmd.domain != "relationship":
        return []
    rt = spec.relationship_types.get(cmd.rel_type or "")
    if rt is None:
        return []
    out: list[Violation] = []
    if not rt.temporal:
        if cmd.valid_from is not None or cmd.valid_from_unknown:
            out.append(violation(rule, SCHEMA, "not_temporal", "valid_from"))
        if cmd.valid_to is not None:
            out.append(violation(rule, SCHEMA, "not_temporal", "valid_to"))
        return out
    if _bad_year(cmd.valid_from):
        out.append(violation(rule, SCHEMA, "date_out_of_range", "valid_from"))
    if _bad_year(cmd.valid_to):
        out.append(violation(rule, SCHEMA, "date_out_of_range", "valid_to"))
    if cmd.valid_from is not None and cmd.valid_from_unknown:
        out.append(violation(rule, SCHEMA, "valid_from_conflict", "valid_from_unknown"))
    if cmd.kind == "relationship.assert":
        if cmd.valid_from is not None and cmd.valid_to is not None and cmd.valid_from > cmd.valid_to:
            out.append(violation(rule, SCHEMA, "invalid_interval", "valid_to"))
        return out
    # relationship.end
    fact = cmd.target_fact
    if fact is not None and fact.status != "active":
        out.append(violation(rule, SCHEMA, "fact_not_active", "fact"))
        return out
    if cmd.valid_to is None:
        out.append(violation(rule, SCHEMA, "valid_to_required", "valid_to"))
    elif fact is not None:
        if fact.valid_to is not None:
            out.append(violation(rule, SCHEMA, "already_ended", "valid_to"))
        elif fact.valid_from is not None and cmd.valid_to < fact.valid_from:
            out.append(violation(rule, SCHEMA, "invalid_interval", "valid_to"))
    return out
