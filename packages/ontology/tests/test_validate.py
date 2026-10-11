"""WP3: semantic validation (ONT-01), the compile-check port, embedded rule tests and diff classification."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from kg_ontology import Issue, diff_specs, parse_document, validate_document, validate_spec
from kg_ontology.template import TemplateError, placeholders, render_fact

from tests.support.ontology_base import base_with

pytestmark = pytest.mark.unit


def check(doc: dict[str, Any], compiler: Any = None):  # type: ignore[no-untyped-def]
    spec, report = validate_document(doc, compiler)
    return spec, report


def paths(report) -> dict[str, str]:  # type: ignore[no-untyped-def]
    return {i.path: i.code for i in report.errors}


def test_a_clean_ontology_validates_and_gets_a_hash() -> None:
    _, report = check(base_with())
    assert report.ok and report.content_hash and len(report.content_hash) == 64


def test_missing_endpoint_is_reported_at_the_field() -> None:
    doc = base_with()
    doc["relationship_types"]["WORKS_AT"]["target"] = "Company"
    _, report = check(doc)
    assert paths(report) == {"relationship_types.WORKS_AT.target": "unknown_entity_type"}


def test_bad_rule_params_are_anchored_at_the_param() -> None:
    doc = base_with(
        rel_rules=[{"kind": "max_active_per_source", "params": {"n": 0}}, {"kind": "min_authority", "params": {"level": "god"}}]
    )
    _, report = check(doc)
    got = paths(report)
    assert "relationship_types.WORKS_AT.rules[0].params.n" in got and "relationship_types.WORKS_AT.rules[1].params.level" in got


def test_unknown_implicit_and_misplaced_rules() -> None:
    doc = base_with(rel_rules=["nonsense", "strict_types"], entity_rules=["succession"])
    _, report = check(doc)
    got = paths(report)
    assert got["relationship_types.WORKS_AT.rules[0].kind"] == "unknown_rule_kind"
    assert got["relationship_types.WORKS_AT.rules[1].kind"] == "implicit_rule"
    assert got["entity_types.Person.rules[0].kind"] == "rule_not_applicable"


def test_succession_needs_a_temporal_relationship() -> None:
    doc = base_with()
    doc["relationship_types"]["KNOWS_NO_TIME"]["rules"] = ["succession"]
    _, report = check(doc)
    assert paths(report) == {"relationship_types.KNOWS_NO_TIME.rules[0].kind": "rule_requires_temporal"}


def test_action_must_be_allowed_for_the_kind() -> None:
    doc = base_with(rel_rules=[{"kind": "queue_if", "params": {"when": "disallowed_endpoint"}, "action": "reject"}])
    _, report = check(doc)
    assert paths(report) == {"relationship_types.WORKS_AT.rules[0].action": "invalid_action"}


def test_alias_source_and_identity_consistency() -> None:
    doc = base_with()
    doc["entity_types"]["Person"]["identity"]["aliases_from"] = ["nope"]
    doc["entity_types"]["Topic"]["identity"] = {"key": "external_id"}
    _, report = check(doc)
    got = paths(report)
    assert got["entity_types.Person.identity.aliases_from"] == "unknown_alias_source"
    assert got["entity_types.Topic.identity.external_id_systems"] == "identity_requires_external_system"


def test_defaults_must_fit_their_type_and_the_name_cannot_be_sensitive() -> None:
    doc = base_with()
    doc["entity_types"]["Person"]["attributes"]["age"]["default"] = "forty"
    doc["entity_types"]["Topic"]["attributes"]["name"]["sensitive"] = True
    _, report = check(doc)
    got = paths(report)
    assert got["entity_types.Person.attributes.age.default"] == "invalid_default"
    assert got["entity_types.Topic.attributes.name.sensitive"] == "name_cannot_be_sensitive"


@pytest.mark.parametrize(
    ("template", "code"),
    [
        ("{source} works at {target}{role?: as {role}}", None),
        ("{source} {nope} {target}", "unknown_placeholder"),
        ("{source} works at {target", "invalid_fact_template"),
        ("{source} } {target}", "invalid_fact_template"),
    ],
)
def test_fact_templates(template: str, code: str | None) -> None:
    doc = base_with()
    doc["relationship_types"]["WORKS_AT"]["fact_template"] = template
    _, report = check(doc)
    assert (paths(report).get("relationship_types.WORKS_AT.fact_template")) == code


def test_a_sensitive_attribute_cannot_appear_in_graph_text() -> None:
    doc = base_with()
    doc["relationship_types"]["WORKS_AT"]["attributes"]["salary"] = {"type": "integer", "sensitive": True}
    doc["relationship_types"]["WORKS_AT"]["fact_template"] = "{source} earns {salary} at {target}"
    _, report = check(doc)
    assert paths(report) == {"relationship_types.WORKS_AT.fact_template": "sensitive_in_template"}


def test_template_rendering() -> None:
    t = "{source} works at {target}{role?: as {role}}"
    assert render_fact(t, "WORKS_AT", "Sarah", "Acme", {"role": "Engineering Manager"}) == "Sarah works at Acme as Engineering Manager"
    assert render_fact(t, "WORKS_AT", "Sarah", "Acme", {}) == "Sarah works at Acme"
    assert render_fact(t, "WORKS_AT", "Sarah", "Acme", {"role": ""}) == "Sarah works at Acme"
    assert render_fact(None, "WORKS_AT", "Sarah", "Acme", {}) == "Sarah works at Acme"
    assert render_fact(None, "IS_FRIEND_OF", "A", "B", {}) == "A is friend of B"
    assert placeholders(t) == {"source", "target", "role"}
    with pytest.raises(TemplateError):
        placeholders("{a")


class FakeCompiler:
    def check(self, spec: Any) -> tuple[list[Issue], list[Issue]]:
        return [Issue(code="reserved_graphiti_field", path="entity_types.Person.attributes.labels", message="collides")], [
            Issue(code="aliased_to_node_name", path="entity_types.Person.attributes.name", message="not compiled")
        ]


def test_the_compile_check_runs_through_the_port_and_is_anchored_at_the_field() -> None:
    _, report = check(base_with(), FakeCompiler())
    assert paths(report) == {"entity_types.Person.attributes.labels": "reserved_graphiti_field"}
    assert [w.code for w in report.warnings] == ["aliased_to_node_name"]
    assert report.content_hash is None  # a report with errors has no publishable hash


def test_embedded_rule_tests_run_and_a_failing_one_blocks() -> None:
    good = {"name": "ok", "given": {}, "command": {"valid_from": "2020-01-01"}, "expect": "accept"}
    bad = {
        "name": "wrong expectation",
        "given": {},
        "command": {"valid_from": "2020-01-01"},
        "expect": "accept",
    }  # no source: require_source rejects
    doc = base_with(rel_rules=[{"kind": "require_source", "tests": [good, bad]}])
    doc["relationship_types"]["WORKS_AT"]["rules"][0]["tests"][0]["command"].update({"source_system": "hr", "source_ref": "r"})
    _, report = check(doc)
    assert [(r.path, r.passed) for r in report.rule_tests] == [
        ("relationship_types.WORKS_AT.rules[0].tests[0]", True),
        ("relationship_types.WORKS_AT.rules[0].tests[1]", False),
    ]
    assert paths(report) == {"relationship_types.WORKS_AT.rules[0].tests[1]": "rule_test_failed"}


def test_a_malformed_rule_test_fails_instead_of_crashing() -> None:
    doc = base_with(
        rel_rules=[{"kind": "require_source", "tests": [{"given": {}, "command": {"valid_from": "not a date"}, "expect": "accept"}]}]
    )
    _, report = check(doc)
    assert paths(report) == {"relationship_types.WORKS_AT.rules[0].tests[0]": "rule_test_failed"}


def test_validate_spec_is_deterministic() -> None:
    spec, _ = parse_document(base_with())
    assert spec is not None
    assert validate_spec(spec).model_dump() == validate_spec(spec).model_dump()


# ---- diff ---------------------------------------------------------------------------------------------------------------------------------


def specs(mutate: Any) -> tuple[Any, Any]:
    old = base_with()
    new = copy.deepcopy(old)
    mutate(new)
    return parse_document(old)[0], parse_document(new)[0]


def classify(mutate: Any) -> dict[tuple[str, str], bool]:
    old, new = specs(mutate)
    return {(c.kind, c.path): c.breaking for c in diff_specs(old, new).changes}


@pytest.mark.parametrize(
    ("label", "mutate", "path", "breaking"),
    [
        ("type removed", lambda d: d["relationship_types"].pop("KNOWS_NO_TIME"), "relationship_types.KNOWS_NO_TIME", True),
        ("entity type removed", lambda d: (d["relationship_types"].clear(), d["entity_types"].pop("Topic")), "entity_types.Topic", True),
        (
            "attribute removed",
            lambda d: d["entity_types"]["Person"]["attributes"].pop("nickname"),
            "entity_types.Person.attributes.nickname",
            True,
        ),
        (
            "attribute type changed",
            lambda d: d["entity_types"]["Person"]["attributes"]["age"].update(type="number"),
            "entity_types.Person.attributes.age",
            True,
        ),
        (
            "required added without default",
            lambda d: d["entity_types"]["Person"]["attributes"]["nickname"].update(required=True),
            "entity_types.Person.attributes.nickname",
            True,
        ),
        (
            "required added WITH default",
            lambda d: d["entity_types"]["Person"]["attributes"]["nickname"].update(required=True, default="x"),
            "entity_types.Person.attributes.nickname",
            False,
        ),
        (
            "optional attribute added",
            lambda d: d["entity_types"]["Person"]["attributes"].update(city={"type": "string"}),
            "entity_types.Person.attributes.city",
            False,
        ),
        (
            "required attribute added without default",
            lambda d: d["entity_types"]["Person"]["attributes"].update(city={"type": "string", "required": True}),
            "entity_types.Person.attributes.city",
            True,
        ),
        (
            "cardinality tightened",
            lambda d: d["relationship_types"]["WORKS_AT"].update(cardinality="one_to_one"),
            "relationship_types.WORKS_AT.cardinality",
            True,
        ),
        (
            "endpoint changed",
            lambda d: d["relationship_types"]["WORKS_AT"].update(target="Topic"),
            "relationship_types.WORKS_AT.target",
            True,
        ),
        ("description changed", lambda d: d["entity_types"]["Person"].update(description="x"), "entity_types.Person.description", False),
        (
            "sensitive turned off",
            lambda d: d["entity_types"]["Person"]["attributes"]["birth_date"].update(sensitive=False, sensitivity=None),
            "entity_types.Person.attributes.birth_date",
            True,
        ),
        ("new entity type", lambda d: d["entity_types"].update(Place={}), "entity_types.Place", False),
    ],
)
def test_diff_classification_table(label: str, mutate: Any, path: str, breaking: bool) -> None:
    result = classify(mutate)
    hits = {k: v for k, v in result.items() if k[1] == path}
    assert hits, (label, result)
    assert any(v == breaking for v in hits.values()), (label, hits)


def test_strict_toggled_on_is_breaking_and_off_is_not() -> None:
    loose = base_with(mode="permissive")
    strict = base_with(mode="strict")
    a, b = parse_document(loose)[0], parse_document(strict)[0]
    assert diff_specs(a, b).breaking_count == 1
    assert diff_specs(b, a).breaking_count == 0


def test_cardinality_loosening_is_not_breaking() -> None:
    old = base_with()
    old["relationship_types"]["WORKS_AT"]["cardinality"] = "one_to_one"
    new = base_with()
    result = diff_specs(parse_document(old)[0], parse_document(new)[0])  # type: ignore[arg-type]
    assert [c.breaking for c in result.changes] == [False]


def test_diff_against_nothing_lists_additions_and_rule_changes_are_not_breaking() -> None:
    spec, _ = parse_document(base_with())
    first = diff_specs(None, spec)  # type: ignore[arg-type]
    assert first.from_version is None and {c.kind for c in first.changes} == {"added"} and first.breaking_count == 0
    old, new = specs(lambda d: d["relationship_types"]["WORKS_AT"].update(rules=["require_source"]))
    r = diff_specs(old, new)
    assert [(c.kind, c.scope, c.breaking) for c in r.changes] == [("added", "rule", False)]
