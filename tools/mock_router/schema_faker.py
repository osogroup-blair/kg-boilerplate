"""Smallest schema-valid instance of a JSON Schema (used when no fixture matches a json_schema request)."""
from __future__ import annotations

from typing import Any


def fake_from_schema(schema: dict[str, Any], root: dict[str, Any] | None = None, depth: int = 0) -> Any:
    root = root or schema
    if depth > 8:
        return None
    if "$ref" in schema:
        ref = schema["$ref"]
        node: Any = root
        for part in ref.lstrip("#/").split("/"):
            node = node[part]
        return fake_from_schema(node, root, depth + 1)
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    for key in ("anyOf", "oneOf"):
        if key in schema:
            options = [o for o in schema[key] if o.get("type") != "null"] or schema[key]
            return fake_from_schema(options[0], root, depth + 1)
    if "allOf" in schema:
        merged: dict[str, Any] = {}
        for sub in schema["allOf"]:
            v = fake_from_schema(sub, root, depth + 1)
            if isinstance(v, dict):
                merged.update(v)
        return merged
    t = schema.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x != "null"), t[0])
    if t == "object" or "properties" in schema:
        props = schema.get("properties", {})
        required = schema.get("required", [])
        return {k: fake_from_schema(v, root, depth + 1) for k, v in props.items() if k in required}
    if t == "array":
        n = schema.get("minItems", 0)
        return [fake_from_schema(schema.get("items", {}), root, depth + 1) for _ in range(n)]
    if t == "string":
        fmt = schema.get("format")
        return {"date-time": "2026-01-01T00:00:00Z", "date": "2026-01-01", "uuid": "00000000-0000-0000-0000-000000000000"}.get(fmt, "sample")
    if t == "integer":
        return int(schema.get("minimum", 0))
    if t == "number":
        return float(schema.get("minimum", 0))
    if t == "boolean":
        return False
    if t == "null":
        return None
    return {}
