"""P5 WP0: the typed memory policy in the ontology (ADR-029/030): defaults fall through, the P2 keys still parse (R13), unknown keys fail with a path."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from kg_ontology import canonical_dict, content_hash, parse_document, parse_text, validate_text
from kg_ontology.spec import MemoryPolicy, OntologySpec

from tests.support.ontology_base import BASE, base_with

pytestmark = pytest.mark.unit
EXAMPLES = Path(__file__).resolve().parents[3] / "examples" / "ontologies"
V1 = EXAMPLES / "personal-knowledge.yaml"
V12 = EXAMPLES / "personal-knowledge-memory.yaml"


def with_policies(policies: dict) -> dict:
    doc = copy.deepcopy(BASE)
    doc["memory_policies"] = policies
    return doc


def test_the_p2_keys_still_parse_and_map_onto_the_typed_ones() -> None:
    spec, issues = parse_text(V1.read_text())
    assert spec is not None and not issues
    default = spec.memory_policies["default"]
    assert default.procedural_requires_review is True  # inferred_shared_procedural: review_required
    assert default.archive_after_days is None


@pytest.mark.parametrize(("legacy", "expected"), [("review_required", True), ("allowed", False)])
def test_inferred_shared_procedural_is_an_alias_of_procedural_requires_review(legacy: str, expected: bool) -> None:
    spec, issues = parse_document(with_policies({"default": {"inferred_shared_procedural": legacy}}))
    assert spec is not None and not issues
    assert spec.memory_policies["default"].procedural_requires_review is expected


def test_an_explicit_typed_key_wins_over_the_legacy_alias() -> None:
    spec, _ = parse_document(
        with_policies({"default": {"inferred_shared_procedural": "review_required", "procedural_requires_review": False}})
    )
    assert spec is not None and spec.memory_policies["default"].procedural_requires_review is False


def test_the_v12_example_uses_the_typed_keys() -> None:
    spec, issues = parse_text(V12.read_text())
    assert spec is not None and not issues
    assert spec.key == "personal-knowledge" and spec.version == "1.2.0"
    d = spec.memory_policies["default"]
    assert (d.extraction, d.extraction_min_importance, d.max_memories_per_interaction) == ("enabled", 0.5, 3)
    assert d.auto_activate == ["episodic", "semantic"] and d.excluded_classifications == ["local_only"]
    assert validate_text(V12.read_text())[2].ok


def test_unknown_policy_keys_and_values_are_validation_errors_with_a_path() -> None:
    spec, issues = parse_document(with_policies({"default": {"auto_activate": ["working"], "no_such_key": 1}}))
    assert spec is None
    paths = {i.path for i in issues}
    assert "memory_policies.default.no_such_key" in paths and any(p.startswith("memory_policies.default.auto_activate") for p in paths)
    _, issues = parse_document(with_policies({"default": {"extraction_min_importance": 1.5}}))
    assert [i.path for i in issues] == ["memory_policies.default.extraction_min_importance"]
    _, issues = parse_document(with_policies({"nonsense_type": {"extraction": "enabled"}}))
    assert issues  # only default and the four memory types can carry a policy


def test_local_only_can_never_be_removed_from_the_excluded_classifications() -> None:
    _, issues = parse_document(with_policies({"default": {"excluded_classifications": ["restricted"]}}))
    assert any(i.code == "local_only_not_excludable" for i in issues)
    spec, issues = parse_document(with_policies({"default": {"excluded_classifications": ["local_only", "restricted"]}}))
    assert spec is not None and not issues


def test_procedural_auto_activation_needs_the_review_requirement_lifted_explicitly() -> None:
    _, issues = parse_document(with_policies({"default": {"auto_activate": ["procedural"]}}))
    assert any(i.code == "procedural_needs_review" for i in issues)
    spec, issues = parse_document(with_policies({"default": {"auto_activate": ["procedural"], "procedural_requires_review": False}}))
    assert spec is not None and not issues


def test_unset_fields_stay_unset_so_they_fall_through_to_the_default_layer() -> None:
    spec, _ = parse_document(with_policies({"procedural": {"working_ttl_hours": 2}}))
    assert spec is not None
    p = spec.memory_policies["procedural"]
    assert p.working_ttl_hours == 2 and p.extraction is None and p.auto_activate is None and p.model_fields_set == {"working_ttl_hours"}


def test_the_canonical_form_round_trips_with_a_typed_policy() -> None:
    spec, _ = parse_text(V12.read_text())
    assert spec is not None
    again = OntologySpec.model_validate(json.loads(json.dumps(canonical_dict(spec))))
    assert content_hash(again) == content_hash(spec)


def test_a_version_published_before_p5_still_loads_from_its_stored_form() -> None:
    """R13: the stored `spec` of v1.0.0 (canonical form at P4: the policy was a free dict) parses, and nothing re-derives its stored hash."""
    spec, _ = parse_text(V1.read_text())
    assert spec is not None
    stored = canonical_dict(spec)
    stored["memory_policies"] = {
        "default": {"inferred_shared_procedural": "review_required", "archive_after_days": None}
    }  # the P4-era shape
    old_hash = hashlib.sha256(json.dumps(stored, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    loaded = OntologySpec.model_validate(stored)  # what SpecCache does with a stored row
    assert loaded.memory_policies["default"].procedural_requires_review is True
    assert len(old_hash) == 64 and old_hash != content_hash(
        loaded
    )  # the typed dump hashes differently, which is exactly why hashes are never recomputed


def test_memory_policy_is_a_strict_model() -> None:
    assert MemoryPolicy().model_fields_set == set()
    assert base_with() is not None
