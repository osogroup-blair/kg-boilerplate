"""Deterministic ranking, `ctx-rank.v1` (phase-6 plan §5.7, ADR-033). Pure and versioned: same candidates, same weights, same order, byte for byte (CTX-06).

    score = sum_f weight_f * feature_f  -  diversity_weight * (the largest term overlap with an already chosen item of the same section)

Features (each in [0, 1]):  relevance (the retrievers' rank and term overlap, computed in `candidates`), authority (ADR-027 scaled), evidence (log of the independent
evidence count), importance (a memory's own; 0.5 for a fact), proximity (1.0 for a requested subject's own item, 0.5 otherwise), recency (only when the profile's weight is
above zero: recency is not validity). The diversity penalty is MMR-style, so a section is not filled with five phrasings of one thing.

Ties break by (section order, kind, id) so the order never depends on set iteration or arrival order. `make eval-context --ablate <feature>` zeroes one weight."""

from __future__ import annotations

import math

from kg_memory.models import AUTHORITY_RANK

from kg_context.models import RANKER_VERSION, SECTION_RANK, Candidate
from kg_context.profiles import EffectiveProfile
from kg_context.text import jaccard, terms

__all__ = ["RANKER_VERSION", "assign_tiers", "rank", "features_of"]
KIND_ORDER = {"fact": 0, "memory": 1, "conflict": 2, "unknown": 3}
EVIDENCE_SATURATION = math.log(4.0)  # three independent pieces of evidence saturate the feature


def features_of(c: Candidate, profile: EffectiveProfile) -> dict[str, float]:
    authority = AUTHORITY_RANK.get(c.authority_class or "", 3) / 5.0
    recency = math.exp(-c.age_days / 90.0) if (profile.weights.get("recency", 0.0) > 0 and c.age_days is not None) else 0.0
    return {
        "relevance": min(1.0, max(0.0, c.relevance)),
        "authority": authority,
        "evidence": min(1.0, math.log1p(max(0, c.evidence_n)) / EVIDENCE_SATURATION),
        "importance": min(1.0, max(0.0, c.importance)),
        "proximity": min(1.0, max(0.0, c.proximity)),
        "recency": recency,
    }


def _tie(c: Candidate) -> tuple[int, int, str]:
    return (SECTION_RANK.get(c.section, 99), KIND_ORDER.get(c.kind, 9), c.id or c.key)


def rank(candidates: list[Candidate], profile: EffectiveProfile, *, ablate: str | None = None) -> list[Candidate]:
    """Scores every candidate and returns them in selection order. Sets `features` (including `diversity`) and `score`."""
    weights = dict(profile.weights)
    if ablate is not None:
        weights[ablate] = 0.0
    div_w = weights.get("diversity", 0.0)
    term_cache = {c.key: set(terms(f"{c.text} {c.raw_text}")) for c in candidates}
    base: dict[str, float] = {}
    for c in candidates:
        c.features = features_of(c, profile)
        base[c.key] = sum(weights.get(name, 0.0) * value for name, value in c.features.items())
    remaining = sorted(candidates, key=lambda c: (-base[c.key], *_tie(c)))
    chosen: list[Candidate] = []
    per_section: dict[str, list[Candidate]] = {}
    while remaining:
        best, best_adj, best_pen = None, -math.inf, 0.0
        for c in remaining:
            same = per_section.get(c.section, ())
            pen = div_w * max((jaccard(term_cache[c.key], term_cache[o.key]) for o in same), default=0.0)
            adj = round(base[c.key] - pen, 9)
            if best is None or adj > best_adj or (adj == best_adj and _tie(c) < _tie(best)):
                best, best_adj, best_pen = c, adj, pen
        assert best is not None
        best.score = best_adj
        best.features = {**best.features, "diversity_penalty": round(best_pen, 9)}
        chosen.append(best)
        per_section.setdefault(best.section, []).append(best)
        remaining.remove(best)
    return chosen


def assign_tiers(ranked: list[Candidate], profile: EffectiveProfile) -> None:
    """Packing priority (blueprint 02-domain/05): conflicts and unknowns and the subjects' critical current facts and the required guidance are REQUIRED; relevant
    experience and related facts are HIGH; background, history and further knowledge are OPTIONAL. `ranked` is in selection order, so "top k" means best first."""
    current_seen: dict[str, int] = {}
    guidance_seen = 0
    for c in ranked:
        if c.exclusion is not None:
            continue
        if c.kind in ("unknown", "conflict"):
            c.tier = "required"
        elif c.section == "current_facts":
            owner = c.subject_ids[0] if c.subject_ids else ""
            n = current_seen.get(owner, 0)
            current_seen[owner] = n + 1
            c.tier = "required" if n < profile.required_current_k else "high"
        elif c.section == "procedural":
            c.tier = "required" if guidance_seen < profile.required_procedural_k else "high"
            guidance_seen += 1
        elif c.section in ("unverified_facts", "relevant_facts", "episodic", "working"):
            c.tier = "high"
        else:
            c.tier = "optional"


def plain_score(c: Candidate, profile: EffectiveProfile) -> None:
    """Features and the weighted sum (no diversity) for a candidate that was excluded before ranking, so the trace can still show WHY it did not make it."""
    c.features = features_of(c, profile)
    c.score = round(sum(profile.weights.get(name, 0.0) * value for name, value in c.features.items()), 9)
