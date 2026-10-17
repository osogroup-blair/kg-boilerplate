"""P7 WP0: `entity_types.<T>.access`. Old documents hash exactly as before, the field is validated, and tightening is a breaking change in the diff."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from kg_ontology import canonical_json, content_hash, diff_specs, parse_document, parse_text

from tests.support.ontology_base import BASE

pytestmark = pytest.mark.unit
EXAMPLES = Path(__file__).resolve().parents[3] / "examples" / "ontologies"


def spec_with(access: str | None):
    doc = copy.deepcopy(BASE)
    first = next(iter(doc["entity_types"]))
    if access is not None:
        doc["entity_types"][first]["access"] = access
    spec, issues = parse_document(doc)
    assert spec is not None, issues
    return spec, first


def test_the_default_is_workspace_and_is_not_serialised_so_old_hashes_do_not_change() -> None:
    absent, first = spec_with(None)
    explicit, _ = spec_with("workspace")
    assert absent.entity_types[first].access == "workspace"
    assert content_hash(absent) == content_hash(explicit)  # an explicit default means the same as an absent one
    assert '"access"' not in canonical_json(absent)


def test_subject_scoped_is_part_of_the_hash() -> None:
    plain, first = spec_with(None)
    scoped, _ = spec_with("subject_scoped")
    assert scoped.entity_types[first].access == "subject_scoped"
    assert content_hash(plain) != content_hash(scoped) and '"access":"subject_scoped"' in canonical_json(scoped)


def test_an_unknown_access_value_is_a_validation_error() -> None:
    doc = copy.deepcopy(BASE)
    doc["entity_types"][next(iter(doc["entity_types"]))]["access"] = "secret"
    spec, issues = parse_document(doc)
    assert spec is None and any("access" in i.path for i in issues)


def test_the_published_examples_still_parse_and_v140_scopes_person_only() -> None:
    for name in ("personal-knowledge", "personal-knowledge-memory", "personal-knowledge-context"):
        spec, issues = parse_text((EXAMPLES / f"{name}.yaml").read_text())
        assert spec is not None and not issues
        assert all(t.access == "workspace" for t in spec.entity_types.values()), name
    agents, issues = parse_text((EXAMPLES / "personal-knowledge-agents.yaml").read_text())
    assert agents is not None and not issues
    assert {k for k, t in agents.entity_types.items() if t.access == "subject_scoped"} == {"Person"}


def test_tightening_access_is_breaking_and_loosening_is_not() -> None:
    plain, first = spec_with(None)
    scoped, _ = spec_with("subject_scoped")
    tighten = diff_specs(plain, scoped)
    (change,) = [c for c in tighten.changes if c.path.endswith(".access")]
    assert (
        change.breaking
        and change.type_key == first
        and "tightened" in (change.reason or "")
        and change.detail == "workspace -> subject_scoped"
    )
    loosen = diff_specs(scoped, plain)
    (change,) = [c for c in loosen.changes if c.path.endswith(".access")]
    assert not change.breaking
    assert diff_specs(plain, plain).changes == []
