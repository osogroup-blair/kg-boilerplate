"""The inputs evaluators work on. The caller (the command pipeline, or the rule-test runner) fetches everything up front, so evaluators stay
pure: no clock, no database, no network."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Literal

CommandKind = Literal["entity.create", "entity.update", "relationship.assert", "relationship.end"]
ENTITY_COMMANDS = frozenset({"entity.create", "entity.update"})
RELATIONSHIP_COMMANDS = frozenset({"relationship.assert", "relationship.end"})

NEG_INF = dt.datetime.min.replace(tzinfo=dt.UTC)
POS_INF = dt.datetime.max.replace(tzinfo=dt.UTC)


@dataclass(frozen=True)
class EntityRef:
    """A resolved endpoint or the entity a command targets."""

    id: str
    type_key: str
    name: str = ""
    classification: str = "internal"


@dataclass(frozen=True)
class FactSnap:
    id: str
    rel_type: str
    source_id: str
    target_id: str
    valid_from: dt.datetime | None = None
    valid_to: dt.datetime | None = None
    valid_from_unknown: bool = False
    status: str = "active"

    @property
    def start(self) -> dt.datetime:
        return self.valid_from or NEG_INF

    @property
    def end(self) -> dt.datetime:
        return self.valid_to or POS_INF


@dataclass(frozen=True)
class RuleCommand:
    kind: CommandKind
    # entity commands
    entity_type: str | None = None
    name: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    external_ids: tuple[tuple[str, str], ...] = ()  # (system, value)
    # relationship commands
    rel_type: str | None = None
    source: EntityRef | None = None
    target: EntityRef | None = None
    valid_from: dt.datetime | None = None
    valid_to: dt.datetime | None = None
    valid_from_unknown: bool = False
    # provenance, common to all commands
    classification: str = "internal"
    authority: str = "user_asserted"
    source_system: str | None = None
    source_ref: str | None = None
    source_type: str = "command"
    # relationship.end: the fact being ended; entity.update: the entity being changed
    target_fact: FactSnap | None = None
    entity: EntityRef | None = None

    @property
    def domain(self) -> Literal["entity", "relationship"]:
        return "entity" if self.kind in ENTITY_COMMANDS else "relationship"


@dataclass(frozen=True)
class RuleContext:
    """`facts`: the ACTIVE facts of the command's relationship type that have the command's source entity as source (supplied by the caller,
    read under a row lock). `current_attributes`: the stored attributes of the entity an update targets."""

    facts: tuple[FactSnap, ...] = ()
    current_attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Violation:
    rule_kind: str
    code: str
    path: str
    action: str  # reject | queue_review | warn
    stage: str
    rule_id: str | None = None
    queue_class: str | None = None  # 'disallowed_endpoint' | 'cardinality_violation': which queue_if(when=...) may soften it


@dataclass(frozen=True)
class EndFact:
    """Effect of a succession: end the predecessor at the new fact's valid_from. The predecessor stays `active` (it is still a true historical fact)."""

    fact_id: str
    valid_to: dt.datetime


@dataclass
class EvalState:
    """Mutable accumulator for one evaluation. `facts` is the effective fact list: succession effects are applied as they are produced, so
    later cardinality rules see the chain as it will be after the command."""

    facts: list[FactSnap]
    effects: list[EndFact] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)  # normalised attribute values
    name: str | None = None


def overlaps(a_start: dt.datetime, a_end: dt.datetime, b_start: dt.datetime, b_end: dt.datetime) -> bool:
    """Half-open intervals [start, end): touching intervals do not overlap."""
    return a_start < b_end and b_start < a_end


@dataclass(frozen=True)
class ResolvedRule:
    """A rule instance ready to evaluate: kind, validated params, and the effective action (the declared override or the kind's default)."""

    kind: str
    params: Any  # the kind's params model instance
    action: str
    rule_id: str | None = None
    owner: str = "implicit"  # 'implicit' | 'workspace' | 'entity:<Type>' | 'relationship:<TYPE>'


def violation(
    rule: ResolvedRule, stage: str, code: str, path: str, *, action: str | None = None, queue_class: str | None = None
) -> Violation:
    return Violation(rule.kind, code, path, action or rule.action, stage, rule.rule_id, queue_class)
