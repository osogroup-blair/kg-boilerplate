"""The retrieval plan (phase-6 plan §5.3 step 2). Pure and model-free: it decides WHAT to ask the sources, from the request and the effective profile alone.

The plan is stored in the trace (its shape, never the task text): which profile and mode, which sections and memory types, whether history is on and why, the query's
term count. The retrieval query itself is derived from the task and is not stored (S8)."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

from kg_context.models import SECTION_INCLUDE, ContextBuildIn
from kg_context.profiles import EffectiveProfile
from kg_context.text import name_stems, query_of, terms

_HISTORY_WORDS = re.compile(
    r"\b(previous|previously|formerly|before|earlier|used to|history|historical|past|prior|back then|originally|last year|years ago)\b",
    re.IGNORECASE,
)
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")


@dataclass(frozen=True)
class Plan:
    profile: EffectiveProfile
    query: str  # what the sources are asked (not stored)
    memory_query: str  # the query plus the profile's intent terms: what the MEMORY search is asked
    query_terms: tuple[str, ...]  # stems, for the lexical half of relevance
    drop_stems: frozenset[str]  # the subjects' name stems (they carry no topical signal)
    history: bool
    history_reason: str  # none | as_of | profile | task
    sections: frozenset[str]  # render sections that may appear
    memory_types: tuple[str, ...]
    use_graph: bool
    use_memory: bool
    as_of: dt.datetime | None
    known_at: dt.datetime | None

    def shape(self) -> dict[str, object]:
        """The metadata-only form for the trace."""
        return {
            "profile": self.profile.name,
            "compression": self.profile.compression_mode,
            "history": self.history,
            "history_reason": self.history_reason,
            "sections": sorted(self.sections),
            "memory_types": list(self.memory_types),
            "graph": self.use_graph,
            "memory": self.use_memory,
            "query_terms": len(self.query_terms),
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "known_at": self.known_at.isoformat() if self.known_at else None,
        }


def history_intent(task: str, as_of: dt.datetime | None) -> str:
    """Why history would be wanted: an explicit `as_of`, or a task that talks about the past ("previous employer", "in 2023")."""
    if as_of is not None:
        return "as_of"
    if _HISTORY_WORDS.search(task) or _YEAR.search(task):
        return "task"
    return "none"


def make_plan(request: ContextBuildIn, profile: EffectiveProfile, subject_names: list[str]) -> Plan:
    intent = history_intent(request.task, request.as_of)
    if profile.history == "never":
        history, reason = False, "none"
    elif profile.history == "always":
        history, reason = True, "profile"
    else:
        history, reason = intent != "none", intent
    sections = frozenset(s for s, inc in SECTION_INCLUDE.items() if inc in profile.include and (s != "history" or history))
    if history and "history" not in profile.include:  # the profile must name history for it to render, whatever the task says
        history, reason = False, "none"
    text = request.query or request.task
    query = query_of(text, subject_names)
    drop = name_stems(subject_names)
    memory_types = tuple(t for t in profile.memory_types if t in profile.include)
    return Plan(
        profile=profile,
        query=query,
        memory_query=(query + " " + " ".join(profile.intent_terms)).strip(),
        query_terms=tuple(terms(query, drop=drop)),
        drop_stems=drop,
        history=history,
        history_reason=reason,
        sections=sections,
        memory_types=memory_types,
        use_graph=profile.graph_enabled and bool(sections & {"current_facts", "unverified_facts", "relevant_facts", "history"}),
        use_memory=bool(memory_types),
        as_of=request.as_of,
        known_at=request.known_at,
    )
