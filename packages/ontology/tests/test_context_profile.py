"""P6 WP0: the typed ContextProfile is backward compatible. A version published before P6 parses and serialises EXACTLY as it did (so its stored content hash never changes and a
re-publish of the same document still dedupes), and the new fields validate."""

from __future__ import annotations

from pathlib import Path

import pytest
from kg_ontology.canonical import canonical_dict, canonical_json, content_hash
from kg_ontology.spec import ContextProfile, parse_document, parse_text

pytestmark = pytest.mark.unit
EXAMPLES = Path(__file__).resolve().parents[3] / "examples" / "ontologies"
OLD_FORM = {
    "planning": {
        "max_tokens": 1200,
        "include": ["current_facts", "episodic", "semantic", "procedural", "uncertainty"],
        "evidence_required": True,
    }
}


@pytest.mark.parametrize("name", ["personal-knowledge.yaml", "personal-knowledge-memory.yaml"])
def test_published_examples_serialise_their_context_profiles_exactly_as_before_p6(name: str) -> None:
    spec, issues = parse_text((EXAMPLES / name).read_text())
    assert issues == [] and spec is not None
    assert canonical_dict(spec)["context_profiles"] == OLD_FORM  # no new key appears, so no stored hash changes


def test_the_hash_is_stable_across_a_round_trip_and_key_order() -> None:
    spec, _ = parse_text((EXAMPLES / "personal-knowledge-context.yaml").read_text())
    assert spec is not None
    again, issues = parse_document(__import__("json").loads(canonical_json(spec)))
    assert issues == [] and again is not None and content_hash(again) == content_hash(spec)


def test_the_context_example_adds_signals_and_keeps_the_rest_of_the_policy() -> None:
    spec, issues = parse_text((EXAMPLES / "personal-knowledge-context.yaml").read_text())
    assert issues == [] and spec is not None and spec.version == "1.3.0"
    planning = spec.context_profiles["planning"]
    assert [s.key for s in planning.expected_signals] == ["current_employer", "financial_runway"]
    assert (
        planning.graph is not None
        and planning.graph.hops == 1
        and planning.history == "when_relevant"
        and planning.current_facts == "always"
    )
    assert spec.memory_policies["default"].extraction == "enabled"


def test_unknown_keys_are_still_kept_and_unset_fields_are_not_serialised() -> None:
    p = ContextProfile.model_validate({"max_tokens": 500, "future_key": {"x": 1}})
    assert p.model_dump(mode="json") == {"max_tokens": 500, "include": [], "evidence_required": False, "future_key": {"x": 1}}


@pytest.mark.parametrize(
    "bad",
    [
        {"weights": {"relevance": 9}},
        {"weights": {"nonsense": 1}},
        {"section_budget": {"current_facts": 1.5}},
        {"history": "sometimes"},
        {
            "expected_signals": [
                {"key": "a", "label": "A", "for_types": ["Person"], "satisfied_by": [{"relationship": "WORKS_AT", "attribute": "x"}]}
            ]
        },
        {"expected_signals": [{"key": "A!", "label": "A", "for_types": ["Person"], "satisfied_by": [{"attribute": "x"}]}]},
        {"graph": {"hops": 5}},
        {"compression": ["telepathy"]},
    ],
)
def test_the_new_fields_are_validated(bad: dict) -> None:  # type: ignore[type-arg]
    with pytest.raises(ValueError):
        ContextProfile.model_validate(bad)


def test_a_profile_without_a_known_section_still_parses() -> None:
    assert ContextProfile.model_validate({"include": ["a_section_from_the_future"]}).include == ["a_section_from_the_future"]
