"""A small ontology the rule fixtures run against. Fixture files add rules and flip the mode; they never redefine the types."""

from __future__ import annotations

import copy
from typing import Any

BASE: dict[str, Any] = {
    "ontology": {"key": "fixture", "version": "1.0.0", "mode": "strict"},
    "entity_types": {
        "Person": {
            "identity": {"key": "platform_uuid", "external_id_systems": ["hr"]},
            "allowed_classifications": ["internal", "confidential", "restricted"],
            "attributes": {
                "name": {"type": "string", "required": True, "max_length": 100},
                "age": {"type": "integer", "min": 0, "max": 150},
                "birth_date": {"type": "date", "sensitive": True},
                "badge": {"type": "string", "pattern": r"^B-\d+$", "editable": False},
                "nickname": {"type": "string", "max_length": 10},
                "employee_level": {"type": "integer", "required": True, "default": 1},
            },
        },
        "Organization": {
            "identity": {"key": "external_id", "external_id_systems": ["hr"]},
            "allowed_classifications": ["internal", "confidential", "restricted", "local_only"],
            "attributes": {"name": {"type": "string", "required": True}},
        },
        "Topic": {
            "allowed_classifications": ["internal", "confidential", "restricted", "local_only"],
            "attributes": {"name": {"type": "string", "required": True}},
        },
    },
    "relationship_types": {
        "WORKS_AT": {"source": "Person", "target": "Organization", "temporal": True, "attributes": {"role": {"type": "string"}}},
        "KNOWS_NO_TIME": {"source": "Person", "target": "Person", "temporal": False},
    },
}


def base_with(
    *,
    rel_rules: list[Any] | None = None,
    entity_rules: list[Any] | None = None,
    workspace_rules: list[Any] | None = None,
    mode: str = "strict",
) -> dict[str, Any]:
    doc = copy.deepcopy(BASE)
    doc["ontology"]["mode"] = mode
    doc["relationship_types"]["WORKS_AT"]["rules"] = copy.deepcopy(rel_rules or [])
    doc["entity_types"]["Person"]["rules"] = copy.deepcopy(entity_rules or [])
    doc["rules"] = copy.deepcopy(workspace_rules or [])
    return doc
