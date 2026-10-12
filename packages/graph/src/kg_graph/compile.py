"""Ontology -> Graphiti entity/edge types (ADR-014), promoted from the Phase 0 spike compiler.

* `name` on an entity type is the node's own name (M1): it is never emitted as a field (an informational warning says so). Any other collision with a
  Graphiti field is an error anchored at the attribute, so the author fixes it in the designer instead of hitting a publish-time crash.
* every compiled field is Optional with default None (M2): extraction is best-effort and `required` is enforced by the platform validator, never by
  the Graphiti model (a required field would make a whole extraction fail validation).
* the compiled types are cached by content hash; P4 extraction jobs pin an ontology version and reuse them.
* sensitive attributes are NEVER compiled (P4 Q6, P2 decision 3): they live in Postgres only, so Graphiti is never asked to extract a birth date
  (its prompt and its answer would both cross the router).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from graphiti_core.edges import EntityEdge
from graphiti_core.errors import EntityTypeValidationError
from graphiti_core.nodes import EntityNode
from graphiti_core.utils.ontology_utils.entity_types_utils import validate_entity_types
from kg_ontology import Issue, OntologySpec, content_hash
from kg_ontology.spec import AttributeSpec
from pydantic import BaseModel, Field, create_model

PY_TYPES: dict[str, type] = {"string": str, "integer": int, "number": float, "boolean": bool, "date": dt.date, "datetime": dt.datetime}
ENTITY_RESERVED = frozenset(EntityNode.model_fields)
EDGE_RESERVED = frozenset(EntityEdge.model_fields)
ALIASED_TO_NODE_NAME = "name"


@dataclass
class Compiled:
    entity_types: dict[str, type[BaseModel]]
    edge_types: dict[str, type[BaseModel]]
    edge_type_map: dict[tuple[str, str], list[str]]
    dropped_fields: dict[str, list[str]] = field(default_factory=dict)  # entity type -> fields not emitted (aliased to the node name)
    excluded_sensitive: dict[str, list[str]] = field(
        default_factory=dict
    )  # entity/edge type -> sensitive attributes kept out of the Graphiti types


def _model(name: str, attrs: dict[str, AttributeSpec]) -> type[BaseModel]:
    fields: dict[str, Any] = {}
    for attr, spec in attrs.items():
        if attr == ALIASED_TO_NODE_NAME or spec.sensitive:
            continue
        py = PY_TYPES[spec.type]
        fields[attr] = (py | None, Field(default=None, description=spec.description or f"{attr} of the {name}"))
    return create_model(name, __base__=BaseModel, __doc__=f"{name} (compiled from ontology)", **fields)


def _collisions(spec: OntologySpec) -> list[Issue]:
    errors: list[Issue] = []
    for tname, et in spec.entity_types.items():
        for attr in et.attributes:
            if attr != ALIASED_TO_NODE_NAME and attr in ENTITY_RESERVED:
                errors.append(
                    Issue(
                        code="reserved_graphiti_field",
                        path=f"entity_types.{tname}.attributes.{attr}",
                        message=f"'{attr}' is a reserved Graphiti node field; rename the attribute",
                    )
                )
    for rname, rt in spec.relationship_types.items():
        for attr in rt.attributes:
            if attr in EDGE_RESERVED:
                errors.append(
                    Issue(
                        code="reserved_graphiti_field",
                        path=f"relationship_types.{rname}.attributes.{attr}",
                        message=f"'{attr}' is a reserved Graphiti edge field; rename the attribute",
                    )
                )
    return errors


def compile_spec(spec: OntologySpec) -> Compiled:
    """Raises ValueError if the spec has reserved-field collisions (call `GraphCompiler.check` first for anchored issues)."""
    bad = _collisions(spec)
    if bad:
        raise ValueError(f"ontology does not compile: {[i.path for i in bad]}")
    out = Compiled({}, {}, {})
    # sorted: Graphiti hands the model positional type ids, and a spec read back from JSONB has no stable key order
    for name, et in sorted(spec.entity_types.items()):
        out.entity_types[name] = _model(name, et.attributes)
        if ALIASED_TO_NODE_NAME in et.attributes:
            out.dropped_fields[name] = [ALIASED_TO_NODE_NAME]
        if hidden := [a for a, x in et.attributes.items() if x.sensitive]:
            out.excluded_sensitive[name] = hidden
    for name, rt in sorted(spec.relationship_types.items()):
        out.edge_types[name] = _model(name, rt.attributes)
        if hidden := [a for a, x in rt.attributes.items() if x.sensitive]:
            out.excluded_sensitive[name] = hidden
        out.edge_type_map.setdefault((rt.source, rt.target), []).append(name)
    validate_entity_types(out.entity_types)  # Graphiti's own check
    return out


@lru_cache(maxsize=64)
def _compile_cached(_hash: str, spec_json: str) -> Compiled:
    return compile_spec(OntologySpec.model_validate_json(spec_json))


def compiled_for(spec: OntologySpec) -> Compiled:
    """Compiled types, cached by content hash (the hash covers the whole meaning of the spec)."""
    return _compile_cached(content_hash(spec), spec.model_dump_json())


class GraphCompiler:
    """Implements kg_ontology.GraphCompilerPort."""

    def check(self, spec: OntologySpec) -> tuple[list[Issue], list[Issue]]:
        errors = _collisions(spec)
        warnings: list[Issue] = []  # 'name' being the node's own name is expected, not a warning (M1)
        if errors:
            return errors, warnings
        try:
            compile_spec(spec)
        except (EntityTypeValidationError, ValueError, TypeError) as exc:
            errors.append(
                Issue(code="graph_compile_failed", path="", message=f"Graphiti rejected the compiled types: {type(exc).__name__}")
            )
        return errors, warnings
