"""Effective profiles (phase-6 plan §5.2): built-in -> ontology (same name, field by field) -> request narrowing. Pure.

A request or an agent override can only NARROW a profile: it may lower the caps, drop sections, and pick one of the compressions the profile allows. Asking for more is
a 422 `exceeds_profile` naming the fields, never a silent clamp, so a caller learns its assumption was wrong."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from kg_ontology.spec import ContextProfile, ExpectedSignal

from kg_context.errors import ExceedsProfile, UnknownProfile
from kg_context.models import INCLUDE_SECTIONS, ContextBuildIn

BUILTIN_PATH = Path(__file__).with_name("profiles_builtin.yaml")
DEFAULT_WEIGHTS = {
    "relevance": 1.0,
    "authority": 0.5,
    "importance": 0.3,
    "evidence": 0.3,
    "proximity": 0.5,
    "recency": 0.0,
    "diversity": 0.3,
}
FALLBACK = "quick_answer"  # a profile that only an ontology defines starts from this one's values


@dataclass(frozen=True)
class EffectiveProfile:
    name: str
    source: str  # builtin | ontology
    ontology_version_id: uuid.UUID | None
    max_tokens: int
    max_items: int
    include: frozenset[str]
    evidence_required: bool
    evidence_threshold: float
    compression: tuple[str, ...]  # allowed modes; the first is the default
    graph_enabled: bool
    graph_hops: int
    graph_limit: int
    memory_types: tuple[str, ...]
    limit_per_type: int
    weights: dict[str, float]
    section_budget: dict[str, float]
    history: str
    current_facts: str
    required_current_k: int
    required_procedural_k: int
    item_chars: int
    intent_terms: tuple[str, ...]
    expected_signals: tuple[ExpectedSignal, ...]
    # set by `narrow`
    compression_mode: str = "structured"

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source": self.source,
            "max_tokens": self.max_tokens,
            "max_items": self.max_items,
            "include": sorted(self.include),
            "compression": list(self.compression),
            "history": self.history,
        }


@lru_cache(maxsize=1)
def builtin_profiles() -> dict[str, ContextProfile]:
    raw = yaml.safe_load(BUILTIN_PATH.read_text())
    return {name: ContextProfile.model_validate(body) for name, body in raw.items()}


def _set_fields(profile: ContextProfile) -> dict[str, Any]:
    return {k: getattr(profile, k) for k in profile.model_fields_set if k in ContextProfile.model_fields}


def resolve(
    name: str, ontology_profiles: dict[str, ContextProfile] | None = None, *, ontology_version_id: uuid.UUID | None = None
) -> EffectiveProfile:
    """Built-in, overridden field by field by the ontology's profile of the same name. Unknown everywhere -> UnknownProfile."""
    builtin = builtin_profiles()
    onto = (ontology_profiles or {}).get(name)
    base_profile = builtin.get(name)
    if base_profile is None and onto is None:
        raise UnknownProfile()
    merged: dict[str, Any] = _set_fields(base_profile if base_profile is not None else builtin[FALLBACK])
    if onto is not None:
        merged.update(_set_fields(onto))
    graph = merged.get("graph")
    memory = merged.get("memory")
    weights = {**DEFAULT_WEIGHTS, **(merged.get("weights") or {})}
    include = [s for s in (merged.get("include") or []) if s in INCLUDE_SECTIONS]
    return EffectiveProfile(
        name=name,
        source="ontology" if onto is not None else "builtin",
        ontology_version_id=ontology_version_id if onto is not None else None,
        max_tokens=int(merged.get("max_tokens") or builtin[FALLBACK].max_tokens or 300),
        max_items=int(merged.get("max_items") or 12),
        include=frozenset(include or builtin[FALLBACK].include),
        evidence_required=bool(merged.get("evidence_required", False)),
        evidence_threshold=float(merged["evidence_threshold"]) if merged.get("evidence_threshold") is not None else 0.0,
        compression=tuple(merged.get("compression") or ("structured",)),
        graph_enabled=bool(graph.enabled) if graph is not None else True,
        graph_hops=int(graph.hops) if graph is not None else 1,
        graph_limit=int(graph.limit) if graph is not None else 10,
        memory_types=tuple(memory.types) if memory is not None else ("episodic", "semantic", "procedural"),
        limit_per_type=int(memory.limit_per_type) if memory is not None else 8,
        weights=weights,
        section_budget=dict(merged.get("section_budget") or {}),
        history=str(merged.get("history") or "when_relevant"),
        current_facts=str(merged.get("current_facts") or "relevant"),
        required_current_k=int(merged["required_current_k"]) if merged.get("required_current_k") is not None else 3,
        required_procedural_k=int(merged["required_procedural_k"]) if merged.get("required_procedural_k") is not None else 0,
        item_chars=int(merged.get("item_chars") or 320),
        intent_terms=tuple(merged.get("intent_terms") or ()),
        expected_signals=tuple(merged.get("expected_signals") or ()),
    )


def narrow(profile: EffectiveProfile, request: ContextBuildIn) -> tuple[EffectiveProfile, int, int]:
    """Apply the request to the profile. Returns (profile with the chosen compression and include, max_tokens, max_items). Widening raises ExceedsProfile."""
    over: list[str] = []
    max_tokens = profile.max_tokens
    if request.max_tokens is not None:
        if request.max_tokens > profile.max_tokens:
            over.append("max_tokens")
        else:
            max_tokens = request.max_tokens
    max_items = profile.max_items
    if request.max_items is not None:
        if request.max_items > profile.max_items:
            over.append("max_items")
        else:
            max_items = request.max_items
    include = profile.include
    if request.include is not None:
        asked = frozenset(request.include)
        if not asked <= profile.include:
            over.append("include")
        else:
            include = asked
    mode = profile.compression[0]
    if request.compression is not None:
        if request.compression not in profile.compression:
            over.append("compression")
        else:
            mode = request.compression
    if over:
        raise ExceedsProfile(over)
    return (
        replace(profile, include=include, compression_mode=mode, evidence_required=profile.evidence_required or request.evidence_required),
        max_tokens,
        max_items,
    )
