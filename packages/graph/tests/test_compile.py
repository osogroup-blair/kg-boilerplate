"""WP3: the Graphiti compile check (ported from GR-03). Reserved collisions are reported at the attribute, never as a publish-time crash."""

from __future__ import annotations

from pathlib import Path

import pytest
from graphiti_core.nodes import EntityNode
from graphiti_core.utils.ontology_utils.entity_types_utils import validate_entity_types
from kg_graph.compile import ENTITY_RESERVED, GraphCompiler, compile_spec, compiled_for
from kg_ontology import parse_document, validate_document, validate_text

from tests.support.ontology_base import base_with

pytestmark = pytest.mark.unit
EXAMPLE = Path(__file__).resolve().parents[3] / "examples" / "ontologies" / "personal-knowledge.yaml"


def test_the_example_ontology_compiles_and_graphiti_accepts_the_types() -> None:
    spec, _, report = validate_text(EXAMPLE.read_text(), compiler=GraphCompiler())
    assert spec is not None and report.ok, report.errors
    compiled = compile_spec(spec)
    assert validate_entity_types(compiled.entity_types)
    assert set(compiled.entity_types) == {"Person", "Organization", "Topic"}
    assert compiled.edge_type_map == {
        ("Person", "Organization"): ["WORKS_AT"],
        ("Person", "Topic"): ["CONSIDERING"],
        ("Person", "Person"): ["KNOWS"],
    }


def test_name_is_the_node_name_and_is_never_compiled_as_a_field() -> None:
    spec, _ = parse_document(base_with())
    assert spec is not None
    compiled = compile_spec(spec)
    assert "name" not in compiled.entity_types["Person"].model_fields
    assert compiled.dropped_fields["Person"] == ["name"]


def test_every_compiled_field_is_optional_so_required_is_the_platforms_job() -> None:
    spec, _ = parse_document(base_with())
    assert spec is not None
    for model in compile_spec(spec).entity_types.values():
        for field in model.model_fields.values():
            assert not field.is_required()
        model()  # an empty extraction result must validate


@pytest.mark.parametrize("reserved", sorted(set(EntityNode.model_fields) - {"name"}))
def test_each_reserved_node_field_is_reported_at_the_attribute(reserved: str) -> None:
    doc = base_with()
    doc["entity_types"]["Person"]["attributes"][reserved] = {"type": "string"}
    spec, report = validate_document(doc, GraphCompiler())
    assert spec is not None
    assert [(i.code, i.path) for i in report.errors] == [("reserved_graphiti_field", f"entity_types.Person.attributes.{reserved}")]


def test_the_blueprint_collision_is_reported_at_the_exact_path() -> None:
    doc = base_with()
    doc["entity_types"]["Person"]["attributes"]["labels"] = {"type": "string"}
    _, report = validate_document(doc, GraphCompiler())
    assert report.errors[0].path == "entity_types.Person.attributes.labels"


def test_reserved_edge_fields_are_reported_on_relationship_attributes() -> None:
    doc = base_with()
    doc["relationship_types"]["WORKS_AT"]["attributes"]["fact"] = {"type": "string"}
    _, report = validate_document(doc, GraphCompiler())
    assert [i.path for i in report.errors] == ["relationship_types.WORKS_AT.attributes.fact"]


def test_compile_is_cached_by_content_hash() -> None:
    spec, _ = parse_document(base_with())
    other, _ = parse_document(base_with())
    assert spec is not None and other is not None
    assert compiled_for(spec) is compiled_for(other)
    changed = base_with()
    changed["entity_types"]["Person"]["attributes"]["age"]["max"] = 99
    third, _ = parse_document(changed)
    assert third is not None and compiled_for(third) is not compiled_for(spec)


def test_compile_spec_refuses_a_colliding_spec() -> None:
    doc = base_with()
    doc["entity_types"]["Person"]["attributes"]["summary"] = {"type": "string"}
    spec, _ = parse_document(doc)
    assert spec is not None
    with pytest.raises(ValueError):
        compile_spec(spec)
    assert "summary" in ENTITY_RESERVED
