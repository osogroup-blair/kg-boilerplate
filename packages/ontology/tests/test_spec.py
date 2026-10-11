"""WP1: spec model, loader, canonical hash."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml
from kg_ontology import canonical_json, content_hash, parse_document, parse_text, validate_text
from kg_ontology.spec import GRAPHITI_RESERVED_EDGES, GRAPHITI_RESERVED_LABELS

from tests.support.ontology_base import BASE, base_with

pytestmark = pytest.mark.unit
EXAMPLE = Path(__file__).resolve().parents[3] / "examples" / "ontologies" / "personal-knowledge.yaml"


def test_the_example_ontology_loads_and_has_a_stable_hash() -> None:
    spec, issues = parse_text(EXAMPLE.read_text())
    assert spec is not None and not issues
    assert spec.key == "personal-knowledge" and spec.version == "1.0.0"
    assert set(spec.entity_types) == {"Person", "Organization", "Topic"} and set(spec.relationship_types) == {
        "WORKS_AT",
        "CONSIDERING",
        "KNOWS",
    }
    birth = spec.entity_types["Person"].attributes["birth_date"]
    assert birth.sensitive and birth.sensitivity == "restricted"  # `sensitivity: restricted` alone implies sensitive
    assert spec.entity_types["Person"].identity.aliases_from == ["name"]  # blueprint `aliases:` accepted
    assert content_hash(spec) == content_hash(parse_text(EXAMPLE.read_text())[0])  # type: ignore[arg-type]


def test_round_trip_through_canonical_json_is_a_fixed_point() -> None:
    spec, _ = parse_text(EXAMPLE.read_text())
    assert spec is not None
    again, issues = parse_document(json.loads(canonical_json(spec)))
    assert again is not None and not issues
    assert canonical_json(again) == canonical_json(spec) and content_hash(again) == content_hash(spec)


def test_hash_ignores_key_order_rule_shorthand_and_defaults() -> None:
    a = base_with(rel_rules=["require_source", {"kind": "succession"}])
    b = copy.deepcopy(a)
    b["relationship_types"]["WORKS_AT"]["rules"] = [
        {"kind": "succession", "params": {"match": ["target", "source"]}, "action": "reject", "severity": "error"},
        {"kind": "require_source", "params": {}},
    ]
    b["entity_types"] = dict(reversed(list(b["entity_types"].items())))
    b["entity_types"]["Person"]["allowed_classifications"] = ["restricted", "internal", "confidential"]
    sa, _ = parse_document(a)
    sb, _ = parse_document(b)
    assert sa is not None and sb is not None
    assert content_hash(sa) == content_hash(sb)


def test_hash_changes_when_meaning_changes() -> None:
    a, b = base_with(), base_with()
    b["entity_types"]["Person"]["attributes"]["age"]["max"] = 120
    assert content_hash(parse_document(a)[0]) != content_hash(parse_document(b)[0])  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["1bad", "has space", "semi;colon", "a" * 65, "", "back`tick", "x}) DETACH DELETE (n"])
def test_unsafe_entity_type_keys_are_rejected(bad: str) -> None:
    doc = base_with()
    doc["entity_types"][bad] = {"attributes": {}}
    spec, issues = parse_document(doc)
    assert spec is None and any(i.path.startswith("entity_types") for i in issues)


@pytest.mark.parametrize("bad", ["works_at", "Works_At", "WORKS-AT", "1WORKS", "A" * 65])
def test_relationship_keys_must_be_upper_snake(bad: str) -> None:
    doc = base_with()
    doc["relationship_types"][bad] = {"source": "Person", "target": "Topic"}
    spec, issues = parse_document(doc)
    assert spec is None and any(i.path.startswith("relationship_types") for i in issues)


@pytest.mark.parametrize("name", sorted(GRAPHITI_RESERVED_LABELS))
def test_graphiti_reserved_labels_are_rejected(name: str) -> None:
    doc = base_with()
    doc["entity_types"][name] = {}
    spec, issues = parse_document(doc)
    assert spec is None and any(i.code == "reserved_label" for i in issues)


@pytest.mark.parametrize("name", sorted(GRAPHITI_RESERVED_EDGES))
def test_graphiti_reserved_edge_names_are_rejected(name: str) -> None:
    doc = base_with()
    doc["relationship_types"][name] = {"source": "Person", "target": "Topic"}
    spec, issues = parse_document(doc)
    assert spec is None and any(i.code == "reserved_edge_name" for i in issues)


def test_unsupported_attribute_types_are_rejected_with_a_path() -> None:
    doc = base_with()
    doc["entity_types"]["Person"]["attributes"]["tags"] = {"type": "list"}
    spec, issues = parse_document(doc)
    assert spec is None
    assert any(i.path == "entity_types.Person.attributes.tags.type" for i in issues)


def test_platform_owned_property_names_cannot_be_attributes() -> None:
    doc = base_with()
    doc["entity_types"]["Person"]["attributes"]["platform_entity_id"] = {"type": "string"}
    spec, issues = parse_document(doc)
    assert spec is None and any(i.code == "reserved_attribute_name" for i in issues)


def test_constraints_must_fit_the_attribute_type() -> None:
    doc = base_with()
    doc["entity_types"]["Person"]["attributes"]["x"] = {"type": "integer", "pattern": "a"}
    doc["entity_types"]["Person"]["attributes"]["y"] = {"type": "string", "min": 1}
    doc["entity_types"]["Person"]["attributes"]["z"] = {"type": "string", "pattern": "("}
    _, issues = parse_document(doc)
    assert {i.path for i in issues} >= {
        "entity_types.Person.attributes.x",
        "entity_types.Person.attributes.y",
        "entity_types.Person.attributes.z",
    }


def test_unknown_keys_are_rejected_everywhere_the_model_is_strict() -> None:
    doc = base_with()
    doc["entity_types"]["Person"]["colour"] = "blue"
    spec, issues = parse_document(doc)
    assert spec is None and any(i.path == "entity_types.Person.colour" for i in issues)


def test_yaml_syntax_errors_are_reported_not_raised() -> None:
    spec, _, report = validate_text("ontology: [unclosed")
    assert spec is None and report.errors[0].code == "yaml_syntax"
    spec, _, report = validate_text("- just\n- a list\n")
    assert spec is None and report.errors[0].code == "document_not_mapping"


def test_the_input_document_is_not_mutated_by_loading() -> None:
    doc = yaml.safe_load(EXAMPLE.read_text())
    before = copy.deepcopy(doc)
    parse_document(doc)
    assert doc == before


def test_base_fixture_is_valid() -> None:
    spec, issues = parse_document(copy.deepcopy(BASE))
    assert spec is not None, issues
