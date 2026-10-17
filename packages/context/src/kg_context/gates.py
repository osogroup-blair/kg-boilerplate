"""The gates between temporal reconciliation and merging (phase-6 plan §5.3 step 5-6). Pure: they only ever MARK a candidate with an exclusion reason (never delete), so the trace can say
why an authorised candidate is not in the package.

    not_included     the profile (or the request's narrowing) does not include that section
    unverified_agent_finding   the profile (or the request) requires evidence and the item is an agent's finding that no reviewer has verified
    unsupported      the profile requires evidence (`evidence_required`) and the item has no evidence pointer
    below_threshold  the item is not relevant to the task: zero relevance, or under the profile's `evidence_threshold`
In `relevant` mode (a short answer) a conflict or an unknown is held to the same standard: it is material only if the task touches it (its words, or its signal's label)."""

from __future__ import annotations

from collections.abc import Callable

from kg_context.models import Candidate
from kg_context.planner import Plan
from kg_context.profiles import EffectiveProfile
from kg_context.text import coverage, terms

NOTICE_KEY = "unknown:no_authorized_records"


def apply_gates(
    candidates: list[Candidate], *, profile: EffectiveProfile, plan: Plan, signal_terms: Callable[[Candidate], set[str]] = lambda c: set()
) -> None:
    for c in candidates:
        if c.exclusion is not None or c.kind not in ("fact", "memory"):
            continue
        if c.section not in plan.sections:
            c.exclusion = "not_included"
        elif profile.evidence_required and c.agent_finding and c.evidence_status != "verified":
            c.exclusion = (
                "unverified_agent_finding"  # P7 (AGT-01): an agent's finding is usable as evidence only once a reviewer has verified it
            )
        elif profile.evidence_required and not c.supported:
            c.exclusion = "unsupported"
        elif c.relevance <= 0.0 or c.relevance < profile.evidence_threshold:
            c.exclusion = "below_threshold"
    if profile.current_facts != "relevant":
        return
    for c in candidates:
        if c.exclusion is None and c.kind in ("conflict", "unknown") and c.key != NOTICE_KEY:
            c.relevance = coverage(list(plan.query_terms), set(terms(c.text)) | signal_terms(c))
            if c.relevance <= 0.0:
                c.exclusion = "below_threshold"
