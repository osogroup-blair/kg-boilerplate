"""Runtime ontology compiler (DEC-05 prototype): ontology YAML -> Graphiti entity/edge Pydantic types, built with create_model.

Rules learned from Graphiti 0.30.2 (see GR-03 evidence):
  * a field that shares a name with an EntityNode field (name, uuid, group_id, labels, created_at, name_embedding, summary, attributes)
    makes validate_entity_types raise. `name` is always the node's own name, so it is NOT emitted (the platform keeps it as the
    identity alias); any other reserved collision is a compile error the ontology author must fix.
  * every compiled field is Optional with default None: extraction is best-effort, `required` is enforced by the platform's validator
    (ONT rules), never by the Graphiti model (a required field would make a whole extraction fail validation).
  * type and relationship names are validated against a strict identifier pattern because Graphiti interpolates labels into Cypher.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import yaml
from graphiti_core.edges import EntityEdge
from graphiti_core.nodes import EntityNode
from pydantic import BaseModel, Field, create_model

IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
GRAPHITI_RESERVED_LABELS = {"Entity", "Episodic", "Community", "Saga"}
GRAPHITI_RESERVED_EDGES = {"MENTIONS", "HAS_EPISODE", "NEXT_EPISODE", "RELATES_TO", "HAS_MEMBER"}
PY_TYPES = {"string": str, "integer": int, "number": float, "boolean": bool, "date": dt.date, "datetime": dt.datetime}
ENTITY_RESERVED = set(EntityNode.model_fields)
EDGE_RESERVED = set(EntityEdge.model_fields)
ALIASED_TO_NODE_NAME = {"name"}


class OntologyCompileError(ValueError):
    pass


@dataclass
class Compiled:
    entity_types: dict[str, type[BaseModel]]
    edge_types: dict[str, type[BaseModel]]
    edge_type_map: dict[tuple[str, str], list[str]]
    dropped_fields: dict[str, list[str]] = field(default_factory=dict)      # type -> fields not emitted (aliased to node.name)
    warnings: list[str] = field(default_factory=list)


def _model(name: str, attrs: dict[str, Any], reserved: set[str], dropped: dict[str, list[str]]) -> type[BaseModel]:
    fields: dict[str, Any] = {}
    for attr, spec in (attrs or {}).items():
        if not IDENT.match(attr):
            raise OntologyCompileError(f"{name}.{attr}: attribute name is not a safe identifier")
        if attr in reserved:
            if attr in ALIASED_TO_NODE_NAME and reserved is ENTITY_RESERVED:
                dropped.setdefault(name, []).append(attr)
                continue
            raise OntologyCompileError(f"{name}.{attr}: collides with a reserved Graphiti field {sorted(reserved)}")
        py = PY_TYPES.get(spec.get("type", "string"))
        if py is None:
            raise OntologyCompileError(f"{name}.{attr}: unsupported type {spec.get('type')!r}")
        fields[attr] = (Optional[py], Field(default=None, description=spec.get("description", f"{attr} of the {name}")))
    return create_model(name, __base__=BaseModel, __doc__=f"{name} (compiled from ontology)", **fields)  # type: ignore[call-overload]


def compile_ontology(text: str) -> Compiled:
    doc = yaml.safe_load(text)
    entities, rels = doc.get("entity_types", {}), doc.get("relationship_types", {})
    out = Compiled({}, {}, {})
    for name, spec in entities.items():
        if not IDENT.match(name) or name in GRAPHITI_RESERVED_LABELS:
            raise OntologyCompileError(f"entity type {name!r}: unsafe or reserved label")
        out.entity_types[name] = _model(name, spec.get("attributes", {}), ENTITY_RESERVED, out.dropped_fields)
    for name, spec in rels.items():
        if not IDENT.match(name) or name in GRAPHITI_RESERVED_EDGES:
            raise OntologyCompileError(f"relationship type {name!r}: unsafe or reserved edge name")
        src, tgt = spec["source"], spec["target"]
        if src not in entities or tgt not in entities:
            raise OntologyCompileError(f"{name}: source/target must be declared entity types ({src}->{tgt})")
        out.edge_types[name] = _model(name, spec.get("attributes", {}), EDGE_RESERVED, {})
        out.edge_type_map.setdefault((src, tgt), []).append(name)
    return out
