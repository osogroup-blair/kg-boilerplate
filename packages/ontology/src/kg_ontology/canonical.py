"""Canonical JSON and the content hash. Two documents that mean the same thing (key order, rule shorthand, filled defaults) hash the same;
a published version is identified by this hash and never changes (ONT-02)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from kg_ontology.rules.catalog import CATALOG, check_params
from kg_ontology.spec import CLASSIFICATIONS, OntologySpec


def _canon_rule(rule: dict[str, Any]) -> dict[str, Any]:
    kind = CATALOG.get(rule["kind"])
    if kind is not None:
        params, issues = check_params(kind, rule.get("params") or {}, "params")
        if params is not None and not issues:
            rule["params"] = params.model_dump(mode="json")
        effective = rule.get("action") or ("warn" if rule.get("severity") == "warning" else kind.default_action)
        rule["action"], rule["severity"] = effective, rule.get("severity") or "error"
    return rule


def _canon_rules(rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expanded = [_canon_rule(dict(r)) for r in rules]
    return sorted(expanded, key=lambda r: (r["kind"], r.get("id") or "", json.dumps(r.get("params"), sort_keys=True)))


def canonical_dict(spec: OntologySpec) -> dict[str, Any]:
    data: dict[str, Any] = spec.model_dump(mode="json")
    data["rules"] = _canon_rules(data["rules"])
    for et in data["entity_types"].values():
        et["rules"] = _canon_rules(et["rules"])
        et["allowed_classifications"] = sorted(set(et["allowed_classifications"]), key=CLASSIFICATIONS.index)
        et["identity"]["external_id_systems"] = sorted(set(et["identity"]["external_id_systems"]))
        et["identity"]["aliases_from"] = sorted(set(et["identity"]["aliases_from"]))
    for rt in data["relationship_types"].values():
        rt["rules"] = _canon_rules(rt["rules"])
    return data


def canonical_json(spec: OntologySpec) -> str:
    return json.dumps(canonical_dict(spec), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(spec: OntologySpec) -> str:
    return hashlib.sha256(canonical_json(spec).encode()).hexdigest()
