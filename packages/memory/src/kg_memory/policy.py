"""Memory policy: the effective settings for a memory type, and the pure decision that turns "who wrote what, where" into a status (ADR-029/030).

`resolve_policy` merges, field by field, `memory_policies.<type>` over `memory_policies.default` over the platform defaults. `decide` is a pure function
with no I/O, tested from a YAML table (every origin x type x actor combination must have a case). The rules it encodes:

* private memory (agent / session scopes only) is the writer's own business: it is active at once;
* an agent can NEVER activate a shared procedural memory (AT-14), and its shared proposals go to review while `agent_shared_requires_review` holds. When the policy lets
  an agent's finding through, it becomes active only if the agent also holds the `publish` privilege on every scope it targets (P7); otherwise it waits for review;
* extracted and proposed memories of an auto-activating type become active but UNVERIFIED; procedural ones are always proposed;
* a conflict with a higher-authority memory makes the new one `proposed` with a `memory_conflict` review, never a silent override (ADR-027).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any, Literal

from kg_ontology.spec import MemoryPolicy

from kg_memory.models import AUTHORITY_RANK, MEMORY_TYPES

Actor = Literal["agent", "app", "admin", "system"]
Outcome = Literal["active", "proposed", "rejected"]
ReviewKind = Literal["memory_proposal", "memory_conflict", "consolidation_proposal"]
ACTORS: tuple[str, ...] = ("agent", "app", "admin", "system")


@dataclass(frozen=True)
class EffectivePolicy:
    extraction: str = "enabled"
    extraction_min_importance: float = 0.5
    max_memories_per_interaction: int = 3
    auto_activate: tuple[str, ...] = ("episodic", "semantic")
    procedural_requires_review: bool = True
    agent_shared_requires_review: bool = True
    working_ttl_hours: float = 24.0
    max_content_chars: int = 2000
    default_extracted_scope: str = "subject"
    excluded_classifications: tuple[str, ...] = ("local_only",)
    archive_after_days: int | None = None
    # P8 (ADR-043): consolidation. `reinforce` is deterministic and automatic; `synthesize` writes a summary and ALWAYS waits for a reviewer.
    consolidation: str = "reinforce"
    consolidation_min_group: int = 3
    consolidation_auto_supersede: bool = True
    near_duplicate_jaccard: float = 0.9
    near_duplicate_cosine: float = 0.95
    episodic_window_days: int = 7


PLATFORM_DEFAULTS = EffectivePolicy()


def resolve_policy(policies: Mapping[Any, MemoryPolicy] | None, memory_type: str) -> EffectivePolicy:
    """Type override -> default -> platform defaults, per field (an unset field falls through). Never raises: a workspace with no ontology still has a policy."""
    values = {f.name: getattr(PLATFORM_DEFAULTS, f.name) for f in fields(EffectivePolicy)}
    for key in ("default", memory_type):
        layer = (policies or {}).get(key)
        if layer is None:
            continue
        for name in layer.model_fields_set:
            value = getattr(layer, name)
            if value is None:
                continue  # unset (or an explicit null, which for `archive_after_days` means the same as the platform default)
            values[name] = tuple(value) if isinstance(value, list) else value
    return EffectivePolicy(**values)


@dataclass(frozen=True)
class Decision:
    status: Outcome
    reason: str  # a stable token: ends up in the audit trail, the review item and the stage result
    review: ReviewKind | None = None


def authority_for(origin: str, actor: Actor, *, submitter_is_agent: bool = False) -> str:
    """What a memory's authority class is, from how it was made (ADR-027): a conversation is a report of what was said, an agent's own output is the weakest."""
    if origin == "extracted":
        return "agent_generated" if submitter_is_agent else "conversation_report"
    if actor == "admin":
        return "admin_asserted"
    if actor == "agent":
        return "agent_generated"
    return "user_asserted"


def evidence_status_for(origin: str, status: str, actor: Actor) -> str:
    """An explicit, trusted save by an app or an admin is verified; everything a model or an agent produced stays unverified until a person vouches
    (blueprint: confidence is not truth)."""
    return "verified" if origin == "explicit" and status == "active" and actor in ("app", "admin") else "unverified"


def outranks(existing_authority: str, new_authority: str) -> bool:
    return AUTHORITY_RANK[existing_authority] > AUTHORITY_RANK[new_authority]


def decide(
    *, origin: str, memory_type: str, actor: Actor, shared: bool, conflict: bool, policy: EffectivePolicy, can_publish: bool = False
) -> Decision:
    """`can_publish`: the writer holds the `publish` privilege on EVERY shared scope it targets (P7, ADR-038). Only an agent's activation depends on it."""
    if memory_type not in MEMORY_TYPES:
        return Decision("rejected", "unknown_type")
    if memory_type == "working":
        if origin == "extracted":
            return Decision("rejected", "working_not_extractable")
        if shared:
            return Decision("rejected", "working_must_be_private")
        return Decision("active", "working_private")
    if not shared:
        return Decision("active", "private_scope")  # the writer's own notes: no one else's behaviour or knowledge is affected
    if conflict:
        return Decision("proposed", "higher_authority_conflict", "memory_conflict")
    if memory_type == "procedural":
        trusted_writer = actor == "admin" and origin in ("explicit", "proposal")
        policy_allows = (
            not policy.procedural_requires_review
            and "procedural" in policy.auto_activate
            and actor in ("app",)
            and origin in ("explicit", "proposal")
        )
        if trusted_writer or policy_allows:
            return Decision("active", "trusted_procedural")
        return Decision(
            "proposed", "procedural_requires_review", "memory_proposal"
        )  # AT-14: an agent, a model or a policy that says review never activates it
    if (
        actor == "agent"
    ):  # whatever the path, an agent's shared memory is a proposal unless the policy AND its publish privilege both say otherwise
        if policy.agent_shared_requires_review or memory_type not in policy.auto_activate:
            return Decision("proposed", "agent_shared_requires_review", "memory_proposal")
        if not can_publish:
            return Decision("proposed", "publish_privilege_required", "memory_proposal")
        return Decision(
            "active", "agent_auto_activate", "memory_proposal"
        )  # active, but unverified: the item is how a reviewer verifies (or withdraws) the finding
    if origin == "explicit":
        return Decision("active", "explicit_save")
    if actor == "admin":
        return Decision("active", "admin_proposal")
    if memory_type in policy.auto_activate:
        return Decision("active", "auto_activate")
    return Decision("proposed", "not_auto_activated", "memory_proposal")


def default_expiry(policy: EffectivePolicy, ttl_hours: float | None) -> dt.timedelta:
    """Working-memory time to live: the caller may ask for LESS than the policy allows, never more."""
    hours = policy.working_ttl_hours if ttl_hours is None else min(ttl_hours, policy.working_ttl_hours)
    return dt.timedelta(hours=hours)
