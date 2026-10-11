"""Validation (ONT-01): structural + semantic checks, the Graphiti compile check (through a port) and the embedded rule tests.
Every problem is an Issue with a JSON path, so the designer can anchor it to a form field."""

from __future__ import annotations

from typing import Any, Literal

from kg_ontology.canonical import content_hash
from kg_ontology.ports import GraphCompilerPort
from kg_ontology.report import Issue, ValidationReport
from kg_ontology.rules.catalog import check_params, get_kind
from kg_ontology.spec import AttributeSpec, OntologySpec, RuleRef, load_text, parse_document
from kg_ontology.template import TemplateError, placeholders
from kg_ontology.testing import run_embedded_tests
from kg_ontology.values import default_fits


def _check_attributes(attrs: dict[str, AttributeSpec], base: str, errors: list[Issue], *, entity: bool) -> None:
    for key, attr in attrs.items():
        if not default_fits(attr):
            errors.append(
                Issue(
                    code="invalid_default", path=f"{base}.attributes.{key}.default", message="the default does not fit the attribute type"
                )
            )
        if entity and key == "name":
            if attr.sensitive:
                errors.append(
                    Issue(
                        code="name_cannot_be_sensitive",
                        path=f"{base}.attributes.name.sensitive",
                        message="the name is the node's own name and goes to the graph",
                    )
                )
            if attr.type != "string":
                errors.append(
                    Issue(code="name_must_be_string", path=f"{base}.attributes.name.type", message="the name attribute must be a string")
                )


def _check_rules(
    refs: list[RuleRef],
    base: str,
    scope: Literal["entity", "relationship", "workspace"],
    errors: list[Issue],
    seen_ids: set[str],
    *,
    temporal: bool = True,
) -> None:
    for i, ref in enumerate(refs):
        path = f"{base}[{i}]"
        kind = get_kind(ref.kind)
        if kind is None:
            errors.append(Issue(code="unknown_rule_kind", path=f"{path}.kind", message="not a rule kind in the catalog"))
            continue
        if kind.implicit:
            errors.append(Issue(code="implicit_rule", path=f"{path}.kind", message="this rule is built in and cannot be declared"))
            continue
        if scope not in kind.applies_to:
            errors.append(Issue(code="rule_not_applicable", path=f"{path}.kind", message=f"this rule cannot be declared on a {scope}"))
        _, param_issues = check_params(kind, ref.params, f"{path}.params")
        errors.extend(param_issues)
        if ref.action is not None and ref.action not in kind.allowed_actions:
            errors.append(Issue(code="invalid_action", path=f"{path}.action", message=f"this rule allows {sorted(kind.allowed_actions)}"))
        if scope == "relationship" and ref.kind in ("succession", "preserve_valid_time") and not temporal:
            errors.append(Issue(code="rule_requires_temporal", path=f"{path}.kind", message="this rule needs a temporal relationship type"))
        if ref.id is not None:
            if ref.id in seen_ids:
                errors.append(Issue(code="duplicate_rule_id", path=f"{path}.id", message="rule ids must be unique in an ontology"))
            seen_ids.add(ref.id)


def _check_template(name: str, rt: Any, base: str, errors: list[Issue]) -> None:
    if rt.fact_template is None:
        return
    path = f"{base}.fact_template"
    try:
        names = placeholders(rt.fact_template)
    except TemplateError as exc:
        errors.append(Issue(code="invalid_fact_template", path=path, message=str(exc)))
        return
    for ph in sorted(names):
        attr = rt.attributes.get(ph)
        if ph not in ("source", "target") and attr is None:
            errors.append(
                Issue(code="unknown_placeholder", path=path, message=f"'{ph}' is neither source, target nor a declared attribute")
            )
        elif attr is not None and attr.sensitive:
            errors.append(Issue(code="sensitive_in_template", path=path, message=f"'{ph}' is sensitive and must not appear in graph text"))


def validate_spec(spec: OntologySpec, compiler: GraphCompilerPort | None = None) -> ValidationReport:
    errors: list[Issue] = []
    warnings: list[Issue] = []
    seen_ids: set[str] = set()
    for tname, et in spec.entity_types.items():
        base = f"entity_types.{tname}"
        for alias in et.identity.aliases_from:
            if alias != "name" and alias not in et.attributes:
                errors.append(
                    Issue(
                        code="unknown_alias_source",
                        path=f"{base}.identity.aliases_from",
                        message=f"'{alias}' is not an attribute of {tname}",
                    )
                )
        if et.identity.key == "external_id" and not et.identity.external_id_systems:
            errors.append(
                Issue(
                    code="identity_requires_external_system",
                    path=f"{base}.identity.external_id_systems",
                    message="list at least one system",
                )
            )
        _check_attributes(et.attributes, base, errors, entity=True)
        _check_rules(et.rules, f"{base}.rules", "entity", errors, seen_ids)
    for rname, rt in spec.relationship_types.items():
        base = f"relationship_types.{rname}"
        for end in ("source", "target"):
            if getattr(rt, end) not in spec.entity_types:
                errors.append(
                    Issue(code="unknown_entity_type", path=f"{base}.{end}", message=f"'{getattr(rt, end)}' is not a declared entity type")
                )
        _check_attributes(rt.attributes, base, errors, entity=False)
        _check_template(rname, rt, base, errors)
        _check_rules(rt.rules, f"{base}.rules", "relationship", errors, seen_ids, temporal=rt.temporal)
    _check_rules(spec.rules, "rules", "workspace", errors, seen_ids)

    if compiler is not None and not errors:
        compile_errors, compile_warnings = compiler.check(spec)
        errors.extend(compile_errors)
        warnings.extend(compile_warnings)

    report = ValidationReport(errors=errors, warnings=warnings)
    if not errors:
        report.rule_tests = run_embedded_tests(spec)
        for result in report.rule_tests:
            if not result.passed:
                report.errors.append(
                    Issue(code="rule_test_failed", path=result.path, message=f"expected {result.expected}, got {result.actual}")
                )
        if report.ok:
            report.content_hash = content_hash(spec)
    return report


def validate_document(doc: Any, compiler: GraphCompilerPort | None = None) -> tuple[OntologySpec | None, ValidationReport]:
    spec, issues = parse_document(doc)
    if spec is None:
        return None, ValidationReport(errors=issues)
    return spec, validate_spec(spec, compiler)


def validate_text(
    text: str, fmt: Literal["yaml", "json"] = "yaml", compiler: GraphCompilerPort | None = None
) -> tuple[OntologySpec | None, Any, ValidationReport]:
    """Returns (spec or None, the parsed plain document or None, report). The document is what a draft stores when the spec is invalid."""
    doc, issues = load_text(text, fmt)
    if issues:
        return None, None, ValidationReport(errors=issues)
    spec, report = validate_document(doc, compiler)
    return spec, doc, report
