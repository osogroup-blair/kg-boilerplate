"""The authority-aware conflict policy for facts that do not come from a trusted command (ADR-027, blueprint 03-runtime/02 'fact conflict policy').

A model's output is a PROPOSAL. When it collides with something the platform already holds, the platform decides by authority and by time, never by
which statement arrived last:

    authoritative_system > admin_asserted > user_asserted > conversation_report > agent_generated

* no conflict                               -> active (labelled unverified by the caller when the authority is below admin_asserted)
* a conflicting fact of HIGHER authority    -> the new fact is `proposed` and a contradiction review is opened; the existing fact stays active (ING-04, AT-05)
* a conflicting fact of EQUAL authority     -> both are `disputed` and a review is opened (KGR-06), unless Graphiti's own invalidation comes with a dated
                                               succession (the new fact starts after the old one): then the old fact ENDS at the new start (ADR-023)
* a conflicting fact of LOWER authority     -> `proposed` with `requires_correction`: displacing a recorded fact is a correction (P8), not an extraction
* an endpoint whose identity is unresolved                  -> `proposed` with no review of its own (the identity review is the review)
* a rule that wants a human (inferred, authority, queue_if) -> `proposed` with a policy review

The module is pure (no database, graph or clock) and has a fixture table under packages/ontology/tests/fixtures/conflicts.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from kg_ontology.rules.context import NEG_INF, POS_INF, overlaps
from kg_ontology.spec import AUTHORITY_RANK

FactStatus = Literal["active", "proposed", "disputed", "rejected"]
ReviewKind = Literal["contradiction", "policy_violation"]


def rank(authority: str) -> int:
    return AUTHORITY_RANK.get(authority, 0)


@dataclass(frozen=True)
class FactView:
    """An existing fact as the policy sees it."""

    id: str
    authority: str
    valid_from: dt.datetime | None = None
    valid_to: dt.datetime | None = None
    status: str = "active"

    @property
    def start(self) -> dt.datetime:
        return self.valid_from or NEG_INF

    @property
    def end(self) -> dt.datetime:
        return self.valid_to or POS_INF


@dataclass(frozen=True)
class Incoming:
    authority: str
    valid_from: dt.datetime | None = None
    valid_to: dt.datetime | None = None

    @property
    def start(self) -> dt.datetime:
        return self.valid_from or NEG_INF

    @property
    def end(self) -> dt.datetime:
        return self.valid_to or POS_INF


@dataclass(frozen=True)
class EndEffect:
    """End an existing fact at `valid_to` (historical succession: the old fact stays a true historical fact)."""

    fact_id: str
    valid_to: dt.datetime


@dataclass(frozen=True)
class Invalidation:
    """Graphiti's contradiction step expired or invalidated this existing fact because of the incoming one."""

    fact_id: str
    invalid_at: dt.datetime | None = None


@dataclass(frozen=True)
class PolicyInputs:
    hard_reject_code: str | None = None  # structural: out_of_ontology, disallowed_endpoint, required_attribute ...
    review_codes: tuple[str, ...] = ()  # rules that ask for a human: inferred_requires_review, insufficient_authority ...
    hold_code: str | None = (
        None  # an endpoint's identity is unresolved: the fact waits as `proposed` WITHOUT its own review (the identity item is the review)
    )
    rule_conflict: bool = (
        False  # the catalog's temporal/cardinality rules reported a violation (succession_conflict, max_active_exceeded ...)
    )
    succession_effects: tuple[EndEffect, ...] = ()  # predecessors the catalog's succession rule would end
    invalidated: tuple[Invalidation, ...] = ()


@dataclass(frozen=True)
class Resolution:
    status: FactStatus
    code: str
    review: ReviewKind | None = None
    competing: tuple[str, ...] = ()  # existing facts the review is about
    disputed: tuple[str, ...] = ()  # existing facts that also become `disputed`
    end_facts: tuple[EndEffect, ...] = field(default_factory=tuple)  # predecessors to end when the fact becomes active

    @property
    def needs_review(self) -> bool:
        return self.review is not None


def succession_evidence(new: Incoming, old: FactView) -> bool:
    """A DATED succession: both facts say when they began, the new one began strictly later, and the old one was still open at that moment."""
    if new.valid_from is None or old.valid_from is None:
        return False
    return new.valid_from > old.valid_from and (old.valid_to is None or old.valid_to >= new.valid_from)


def resolve(new: Incoming, existing: Sequence[FactView], inputs: PolicyInputs = PolicyInputs()) -> Resolution:
    if inputs.hard_reject_code:
        return Resolution("rejected", inputs.hard_reject_code)
    active = {f.id: f for f in existing if f.status == "active"}
    r_new = rank(new.authority)

    conflicting: dict[str, str] = {}  # fact id -> 'overlap' (a catalog rule) | 'invalidated' (Graphiti's contradiction step)
    if inputs.rule_conflict:
        for f in active.values():
            if overlaps(new.start, new.end, f.start, f.end):
                conflicting[f.id] = "overlap"
    for inv in inputs.invalidated:
        hit = active.get(inv.fact_id)
        if hit is not None and overlaps(
            new.start, new.end, hit.start, hit.end
        ):  # a fact that ended before the new one began is no contradiction
            conflicting.setdefault(hit.id, "invalidated")

    end_facts: list[EndEffect] = []
    higher: list[str] = []
    equal: list[str] = []
    lower: list[str] = []
    for effect in inputs.succession_effects:  # the catalog found a same-target predecessor to end: allowed only with enough authority
        old = active.get(effect.fact_id)
        if old is None:
            continue
        if rank(old.authority) > r_new:
            higher.append(old.id)
        else:
            end_facts.append(effect)
    for fact_id, kind in conflicting.items():
        old = active[fact_id]
        r_old = rank(old.authority)
        dated = succession_evidence(new, old) and new.valid_from is not None
        if r_old > r_new:
            higher.append(fact_id)
        elif dated and kind == "invalidated":
            assert new.valid_from is not None
            end_facts.append(EndEffect(fact_id, new.valid_from))
        elif r_old == r_new:
            equal.append(fact_id)
        else:
            lower.append(fact_id)

    if higher:
        return Resolution("proposed", "authority_conflict", "contradiction", competing=tuple(dict.fromkeys(higher)))
    if equal:
        ids = tuple(dict.fromkeys(equal))
        return Resolution("disputed", "equal_authority_conflict", "contradiction", competing=ids, disputed=ids)
    if lower:
        return Resolution("proposed", "requires_correction", "contradiction", competing=tuple(dict.fromkeys(lower)))
    if inputs.hold_code:
        return Resolution("proposed", inputs.hold_code)
    if inputs.review_codes:
        return Resolution("proposed", inputs.review_codes[0], "policy_violation")
    return Resolution("active", "no_conflict", end_facts=tuple({e.fact_id: e for e in end_facts}.values()))


def evidence_status(authority: str) -> Literal["verified", "unverified"]:
    """A fact is `verified` when a trusted source asserted it (admin or system); everything else, including a user's own statement in a conversation, is
    evidence of a statement, not of truth (02-domain/03)."""
    return "verified" if rank(authority) >= rank("admin_asserted") else "unverified"
