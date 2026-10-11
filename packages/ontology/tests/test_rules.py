"""ONT-06: every rule kind has fixture cases (at least one pass and one non-pass), a conflict policy and pure evaluators."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
import yaml
from kg_ontology.rules.catalog import CATALOG, STAGES, explicit_kinds
from kg_ontology.rules.engine import evaluate, resolve_rules
from kg_ontology.spec import parse_document
from kg_ontology.testing import build, run_case
from kg_ontology.values import parse_instant

from tests.support.ontology_base import base_with

pytestmark = pytest.mark.unit
FIXTURES = Path(__file__).parent / "fixtures" / "rules"


def _load() -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    out = []
    for path in sorted(FIXTURES.glob("*.yaml")):
        fx = yaml.safe_load(path.read_text())
        for case in fx["cases"]:
            out.append((f"{fx['kind']}::{case['name']}", fx, case))
    return out


CASES = _load()


@pytest.mark.parametrize(("label", "fx", "case"), CASES, ids=[c[0] for c in CASES])
def test_fixture_case(label: str, fx: dict[str, Any], case: dict[str, Any]) -> None:
    doc = base_with(
        rel_rules=case.get("rel_rules", fx.get("rel_rules")),
        entity_rules=case.get("entity_rules", fx.get("entity_rules")),
        workspace_rules=case.get("workspace_rules", fx.get("workspace_rules")),
        mode=case.get("mode", fx.get("mode", "strict")),
    )
    spec, issues = parse_document(doc)
    assert spec is not None, issues
    ev = run_case(spec, case.get("given", {}), case["command"], default_entity_type="Person", default_rel_type="WORKS_AT")
    assert ev.outcome == case["expect"], [(v.code, v.action) for v in ev.violations]
    if "codes" in case:
        assert sorted({v.code for v in ev.violations if v.action != "warn"}) == sorted(case["codes"])
    if "warn_codes" in case:
        assert sorted({v.code for v in ev.violations if v.action == "warn"}) == sorted(case["warn_codes"])
    if "effects" in case:
        got = sorted((e.fact_id, e.valid_to) for e in ev.effects)
        want = sorted((e["fact"], parse_instant(e["valid_to"])) for e in case["effects"])
        assert got == want


def test_every_kind_has_fixtures_with_a_pass_and_a_non_pass_case_and_a_conflict_policy() -> None:
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for _, fx, case in CASES:
        by_kind.setdefault(fx["kind"], []).append(case)
    assert set(by_kind) == set(CATALOG), set(CATALOG) ^ set(by_kind)
    for name, kind in CATALOG.items():
        outcomes = {c["expect"] for c in by_kind[name]}
        assert "accept" in outcomes and outcomes - {"accept"}, f"{name} needs a pass case and a non-pass case"
        assert kind.conflict_policy.strip() and kind.description.strip(), name
        assert kind.stage in STAGES and kind.default_action in kind.allowed_actions, name


def test_the_declarable_kinds_are_exactly_the_documented_set() -> None:
    assert {k.kind for k in explicit_kinds()} == {
        "preserve_valid_time",
        "succession",
        "unique_active_pair",
        "max_active_per_source",
        "require_source",
        "min_authority",
        "require_review_if_inferred",
        "queue_if",
    }


def test_evaluation_is_deterministic_and_does_not_mutate_its_inputs() -> None:
    doc = base_with(rel_rules=[{"kind": "succession"}, "unique_active_pair", {"kind": "max_active_per_source", "params": {"n": 1}}])
    spec, _ = parse_document(doc)
    assert spec is not None
    given = {"facts": [{"id": "f1", "target": "t", "valid_from": "2020-01-01"}]}
    command = {"valid_from": "2022-01-01"}
    cmd, ctx = build(spec, given, command, default_rel_type="WORKS_AT")
    first, second = evaluate(spec, cmd, ctx), evaluate(spec, cmd, ctx)
    assert first.violations == second.violations and first.effects == second.effects and first.trace == second.trace
    assert ctx.facts[0].valid_to is None  # the caller's snapshot is untouched: effects are reported, not applied
    assert [e.fact_id for e in first.effects] == ["f1"]


def test_evaluators_do_no_io() -> None:
    """Pure by construction: the evaluator modules import neither I/O libraries nor a clock."""
    import kg_ontology.rules as pkg

    forbidden = (
        "import asyncio",
        "import socket",
        "import httpx",
        "import sqlalchemy",
        "import os",
        "import subprocess",
        "datetime.now",
        "utcnow",
        "time.time",
    )
    root = Path(pkg.__file__).parent
    for path in list(root.rglob("*.py")) + [root.parent / "values.py", root.parent / "spec.py", root.parent / "template.py"]:
        text = path.read_text()
        for needle in forbidden:
            assert needle not in text, f"{path.name} contains {needle}"


def test_resolution_orders_implicit_rules_first_then_declared_in_catalog_order() -> None:
    doc = base_with(rel_rules=["require_source", {"kind": "succession"}, "preserve_valid_time"], workspace_rules=["require_source"])
    spec, _ = parse_document(doc)
    assert spec is not None
    cmd, _ = build(spec, {}, {"valid_from": "2020-01-01"}, default_rel_type="WORKS_AT")
    kinds = [r.kind for r in resolve_rules(spec, cmd)]
    assert kinds[:5] == [
        "strict_types",
        "endpoint_types",
        "attribute_types",
        "valid_interval",
        "classification_allowed",
    ]  # identity_rules is entity-only
    assert kinds[5:] == ["preserve_valid_time", "succession", "require_source"]  # the duplicate require_source collapses


def test_severity_warning_downgrades_the_default_action_to_warn() -> None:
    doc = base_with(rel_rules=[{"kind": "require_source", "severity": "warning"}])
    spec, _ = parse_document(doc)
    assert spec is not None
    ev = run_case(spec, {}, {"valid_from": "2020-01-01"}, default_rel_type="WORKS_AT")
    assert ev.outcome == "accept" and {v.action for v in ev.violations} == {"warn"}


def test_a_rejecting_stage_stops_evaluation() -> None:
    doc = base_with(rel_rules=["require_source"])
    spec, _ = parse_document(doc)
    assert spec is not None
    ev = run_case(spec, {}, {"valid_from": "2022-01-01", "valid_to": "2021-01-01"}, default_rel_type="WORKS_AT")
    assert [t["stage"] for t in ev.trace] == ["schema_validation"]  # require_source (a later stage) never ran
    assert isinstance(dt.date.today(), dt.date)
