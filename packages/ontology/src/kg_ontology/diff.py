"""Spec diff with breaking-change classification (ONT-05, partial). Impact counts (entities and facts per affected type) are added by kg_knowledge,
which can count rows."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel

from kg_ontology.canonical import canonical_dict
from kg_ontology.spec import CARDINALITY_STRICTNESS, OntologySpec


class Change(BaseModel):
    kind: Literal["added", "removed", "changed"]
    scope: str  # header | entity_type | attribute | relationship_type | relationship_attribute | rule | profile | policy
    path: str
    type_key: str | None = None  # the entity type or relationship type this change affects (for impact counts)
    detail: str = ""
    breaking: bool = False
    reason: str | None = None


class ImpactCount(BaseModel):
    type_key: str
    kind: Literal["entity", "relationship"]
    active: int


class DiffResult(BaseModel):
    from_version: str | None
    to_version: str
    changes: list[Change]
    impact: list[ImpactCount] = []

    @property
    def breaking_count(self) -> int:
        return sum(1 for c in self.changes if c.breaking)


def _rule_key(rule: dict[str, Any]) -> str:
    return json.dumps({k: rule.get(k) for k in ("kind", "id", "params", "action", "severity")}, sort_keys=True)


def _diff_rules(old: list[dict[str, Any]], new: list[dict[str, Any]], path: str, type_key: str | None, out: list[Change]) -> None:
    before, after = {_rule_key(r): r for r in old}, {_rule_key(r): r for r in new}
    for key, rule in before.items():
        if key not in after:
            out.append(Change(kind="removed", scope="rule", path=path, type_key=type_key, detail=rule["kind"]))
    for key, rule in after.items():
        if key not in before:
            out.append(Change(kind="added", scope="rule", path=path, type_key=type_key, detail=rule["kind"]))


def _diff_attrs(old: dict[str, Any], new: dict[str, Any], base: str, scope: str, type_key: str, out: list[Change]) -> None:
    for name in sorted(old.keys() | new.keys()):
        path = f"{base}.attributes.{name}"
        if name not in new:
            out.append(
                Change(
                    kind="removed",
                    scope=scope,
                    path=path,
                    type_key=type_key,
                    breaking=True,
                    reason="attribute removed; stored values lose their definition",
                )
            )
        elif name not in old:
            required_no_default = new[name]["required"] and new[name]["default"] is None
            out.append(
                Change(
                    kind="added",
                    scope=scope,
                    path=path,
                    type_key=type_key,
                    breaking=required_no_default,
                    reason="required attribute without a default: existing records would be invalid" if required_no_default else None,
                )
            )
        elif old[name] != new[name]:
            a, b = old[name], new[name]
            reasons: list[str] = []
            if a["type"] != b["type"]:
                reasons.append("attribute type changed")
            if not a["required"] and b["required"] and b["default"] is None:
                reasons.append("required added without a default")
            if a["sensitive"] and not b["sensitive"]:
                reasons.append("attribute is no longer sensitive: it would now reach the graph")
            changed = sorted(k for k in b if a.get(k) != b[k])
            out.append(
                Change(
                    kind="changed",
                    scope=scope,
                    path=path,
                    type_key=type_key,
                    detail=",".join(changed),
                    breaking=bool(reasons),
                    reason="; ".join(reasons) or None,
                )
            )


def diff_specs(old: OntologySpec | None, new: OntologySpec) -> DiffResult:
    a = canonical_dict(old) if old is not None else None
    b = canonical_dict(new)
    out: list[Change] = []
    if a is None:
        for name in sorted(b["entity_types"]):
            out.append(Change(kind="added", scope="entity_type", path=f"entity_types.{name}", type_key=name))
        for name in sorted(b["relationship_types"]):
            out.append(Change(kind="added", scope="relationship_type", path=f"relationship_types.{name}", type_key=name))
        return DiffResult(from_version=None, to_version=new.version, changes=out)

    if a["ontology"]["mode"] != b["ontology"]["mode"]:
        tightened = b["ontology"]["mode"] == "strict"
        out.append(
            Change(
                kind="changed",
                scope="header",
                path="ontology.mode",
                detail=f"{a['ontology']['mode']} -> {b['ontology']['mode']}",
                breaking=tightened,
                reason="strict mode rejects attributes that were accepted before" if tightened else None,
            )
        )
    for name in sorted(a["entity_types"].keys() | b["entity_types"].keys()):
        path = f"entity_types.{name}"
        if name not in b["entity_types"]:
            out.append(Change(kind="removed", scope="entity_type", path=path, type_key=name, breaking=True, reason="entity type removed"))
        elif name not in a["entity_types"]:
            out.append(Change(kind="added", scope="entity_type", path=path, type_key=name))
        else:
            x, y = a["entity_types"][name], b["entity_types"][name]
            _diff_attrs(x["attributes"], y["attributes"], path, "attribute", name, out)
            _diff_rules(x["rules"], y["rules"], f"{path}.rules", name, out)
            if x["identity"]["key"] != y["identity"]["key"]:
                out.append(
                    Change(
                        kind="changed",
                        scope="entity_type",
                        path=f"{path}.identity.key",
                        type_key=name,
                        breaking=True,
                        reason="identity strategy changed",
                    )
                )
            removed_cls = sorted(set(x["allowed_classifications"]) - set(y["allowed_classifications"]))
            if removed_cls:
                out.append(
                    Change(
                        kind="changed",
                        scope="entity_type",
                        path=f"{path}.allowed_classifications",
                        type_key=name,
                        breaking=True,
                        detail=",".join(removed_cls),
                        reason="classifications no longer allowed",
                    )
                )
            if x["description"] != y["description"]:
                out.append(Change(kind="changed", scope="entity_type", path=f"{path}.description", type_key=name))
    for name in sorted(a["relationship_types"].keys() | b["relationship_types"].keys()):
        path = f"relationship_types.{name}"
        if name not in b["relationship_types"]:
            out.append(
                Change(
                    kind="removed", scope="relationship_type", path=path, type_key=name, breaking=True, reason="relationship type removed"
                )
            )
        elif name not in a["relationship_types"]:
            out.append(Change(kind="added", scope="relationship_type", path=path, type_key=name))
        else:
            x, y = a["relationship_types"][name], b["relationship_types"][name]
            for end in ("source", "target"):
                if x[end] != y[end]:
                    out.append(
                        Change(
                            kind="changed",
                            scope="relationship_type",
                            path=f"{path}.{end}",
                            type_key=name,
                            breaking=True,
                            reason="endpoint type changed",
                        )
                    )
            if x["temporal"] != y["temporal"]:
                out.append(
                    Change(
                        kind="changed",
                        scope="relationship_type",
                        path=f"{path}.temporal",
                        type_key=name,
                        breaking=True,
                        reason="validity semantics changed",
                    )
                )
            if x["cardinality"] != y["cardinality"]:
                tighter = CARDINALITY_STRICTNESS[y["cardinality"]] > CARDINALITY_STRICTNESS[x["cardinality"]]
                out.append(
                    Change(
                        kind="changed",
                        scope="relationship_type",
                        path=f"{path}.cardinality",
                        type_key=name,
                        detail=f"{x['cardinality']} -> {y['cardinality']}",
                        breaking=tighter,
                        reason="cardinality tightened" if tighter else None,
                    )
                )
            for field in ("fact_template", "description"):
                if x[field] != y[field]:
                    out.append(Change(kind="changed", scope="relationship_type", path=f"{path}.{field}", type_key=name))
            _diff_attrs(x["attributes"], y["attributes"], path, "relationship_attribute", name, out)
            _diff_rules(x["rules"], y["rules"], f"{path}.rules", name, out)
    _diff_rules(a["rules"], b["rules"], "rules", None, out)
    for section, scope in (("context_profiles", "profile"), ("memory_policies", "policy")):
        for name in sorted(a[section].keys() | b[section].keys()):
            if a[section].get(name) != b[section].get(name):
                kind = "added" if name not in a[section] else "removed" if name not in b[section] else "changed"
                out.append(Change(kind=kind, scope=scope, path=f"{section}.{name}"))
    return DiffResult(from_version=old.version if old else None, to_version=new.version, changes=out)
