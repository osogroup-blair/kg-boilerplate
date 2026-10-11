"""The rule catalog (ONT-06): a FIXED set of rule kinds. A new kind needs code here, fixtures under tests/fixtures/rules and a conflict policy
(a meta-test enforces all three). There are no expressions and no user code."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from kg_ontology.report import Issue
from kg_ontology.rules.context import EvalState, ResolvedRule, RuleCommand, RuleContext, Violation
from kg_ontology.rules.evaluators import authority, cardinality, review, structural, temporal, validation
from kg_ontology.spec import Authority, OntologySpec

Evaluator = Callable[[ResolvedRule, OntologySpec, RuleCommand, RuleContext, EvalState], list[Violation]]
ALL_COMMANDS = frozenset({"entity.create", "entity.update", "relationship.assert", "relationship.end"})
ASSERT_ONLY = frozenset({"relationship.assert"})

# Blueprint evaluation order (02-domain/01), minus identity resolution which needs the database and therefore lives in the pipeline.
STAGES = ["schema_validation", "source_classification", "temporal_rules", "mutation_authorization", "review"]


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoParams(_Params):
    pass


def _src() -> list[Literal["source", "target"]]:
    return ["source"]


def _src_tgt() -> list[Literal["source", "target"]]:
    return ["source", "target"]


def _normal_match(value: list[str]) -> list[str]:
    out = sorted(set(value))
    if "source" not in out:
        raise ValueError("match must include 'source'")
    return out


class MaxActiveParams(_Params):
    n: int = Field(ge=1, le=1000, description="Facts allowed at the same valid time")
    match: list[Literal["source", "target"]] = Field(default_factory=_src, description="Count per source, or per source+target pair")

    @field_validator("match")
    @classmethod
    def _norm(cls, value: list[str]) -> list[str]:
        return _normal_match(value)


class SuccessionParams(_Params):
    match: list[Literal["source", "target"]] = Field(default_factory=_src_tgt, description="Which existing facts a new fact succeeds")

    @field_validator("match")
    @classmethod
    def _norm(cls, value: list[str]) -> list[str]:
        return _normal_match(value)


class PreserveValidTimeParams(_Params):
    require_valid_from: bool = True


class MinAuthorityParams(_Params):
    level: Authority


class QueueIfParams(_Params):
    when: Literal["disallowed_endpoint", "cardinality_violation"]


@dataclass(frozen=True)
class RuleKind:
    kind: str
    family: str  # structural | validation | cardinality | temporal | authority | review
    stage: str
    commands: frozenset[str]  # command kinds the evaluator runs for
    applies_to: frozenset[str]  # where it may be declared: entity | relationship | workspace
    params_model: type[_Params]
    default_action: str
    allowed_actions: frozenset[str]
    description: str
    conflict_policy: str
    evaluator: Evaluator | None  # None: a modifier applied by the engine (queue_if)
    implicit: bool = False


_REJECT_WARN = frozenset({"reject", "warn"})
_ANY = frozenset({"reject", "queue_review", "warn"})

_KINDS: list[RuleKind] = [
    RuleKind(
        "strict_types",
        "structural",
        "schema_validation",
        ALL_COMMANDS,
        frozenset(),
        NoParams,
        "reject",
        _REJECT_WARN,
        "Entity and relationship types must be declared; attributes must be declared (reject in strict mode, warn in permissive mode).",
        "Always evaluated first. A rejection at this stage stops evaluation: later stages need a known type.",
        structural.strict_types,
        implicit=True,
    ),
    RuleKind(
        "endpoint_types",
        "structural",
        "schema_validation",
        ASSERT_ONLY,
        frozenset(),
        NoParams,
        "reject",
        _ANY,
        "The source and target entity types must match the relationship declaration.",
        "A queue_if(disallowed_endpoint) rule turns the rejection into a review item; nothing is written either way.",
        structural.endpoint_types,
        implicit=True,
    ),
    RuleKind(
        "identity_rules",
        "structural",
        "schema_validation",
        frozenset({"entity.create", "entity.update"}),
        frozenset(),
        NoParams,
        "reject",
        _REJECT_WARN,
        "External identifiers must come from a system the type declares; a type keyed by external_id needs one at creation.",
        "Names are never an identity (KGR-02): only declared external identifiers match deterministically.",
        structural.identity_rules,
        implicit=True,
    ),
    RuleKind(
        "attribute_types",
        "validation",
        "schema_validation",
        frozenset({"entity.create", "entity.update", "relationship.assert"}),
        frozenset(),
        NoParams,
        "reject",
        _REJECT_WARN,
        "Scalar type, required, default, editable, pattern, min/max and max_length. Checks form only: validity is not truth.",
        "A required attribute missing from a create is a rejection; an update may never clear a required attribute.",
        validation.attribute_types,
        implicit=True,
    ),
    RuleKind(
        "valid_interval",
        "validation",
        "schema_validation",
        frozenset({"relationship.assert", "relationship.end"}),
        frozenset(),
        NoParams,
        "reject",
        _REJECT_WARN,
        "valid_from <= valid_to, dates not before 1900, and valid time only on temporal relationship types.",
        "Evaluated together with the relationship's temporal flag; ending a fact needs an open fact and a valid_to not before its valid_from.",
        validation.valid_interval,
        implicit=True,
    ),
    RuleKind(
        "classification_allowed",
        "structural",
        "source_classification",
        frozenset({"entity.create", "entity.update", "relationship.assert"}),
        frozenset(),
        NoParams,
        "reject",
        _ANY,
        "The classification must be allowed by the entity type (a fact: by both endpoint types).",
        "The intersection of the endpoint types' allowed classifications applies to a fact.",
        structural.classification_allowed,
        implicit=True,
    ),
    RuleKind(
        "preserve_valid_time",
        "temporal",
        "temporal_rules",
        ASSERT_ONLY,
        frozenset({"relationship", "workspace"}),
        PreserveValidTimeParams,
        "reject",
        _ANY,
        "A temporal relationship needs valid_from (or an explicit valid_from_unknown). The platform never defaults valid time to 'now'.",
        "Independent of succession: it only checks that the command states when the fact became true.",
        temporal.preserve_valid_time,
    ),
    RuleKind(
        "succession",
        "temporal",
        "temporal_rules",
        ASSERT_ONLY,
        frozenset({"relationship", "workspace"}),
        SuccessionParams,
        "reject",
        frozenset({"reject", "queue_review"}),
        "A new fact whose valid_from falls inside an existing matching fact ENDS that fact at the new valid_from (historical succession, not supersession).",
        "Runs before the cardinality rules, which see the chain after the predecessor is ended. A new fact before an existing one is rejected "
        "(out_of_order_succession) unless it fits a gap; a fact that would sit inside another is rejected (succession_conflict).",
        temporal.succession,
    ),
    RuleKind(
        "unique_active_pair",
        "cardinality",
        "temporal_rules",
        ASSERT_ONLY,
        frozenset({"relationship", "workspace"}),
        NoParams,
        "reject",
        _ANY,
        "No two active facts of this type for the same source and target may overlap in valid time.",
        "A succession rule on the same type resolves the overlap first by ending the predecessor.",
        cardinality.unique_active_pair,
    ),
    RuleKind(
        "max_active_per_source",
        "cardinality",
        "temporal_rules",
        ASSERT_ONLY,
        frozenset({"relationship", "workspace"}),
        MaxActiveParams,
        "reject",
        _ANY,
        "At most n active facts of this type per source (or source+target pair) at any valid time.",
        "Counts the chain after succession effects. queue_if(cardinality_violation) turns the rejection into a review item.",
        cardinality.max_active_per_source,
    ),
    RuleKind(
        "require_source",
        "authority",
        "mutation_authorization",
        ALL_COMMANDS,
        frozenset({"entity", "relationship", "workspace"}),
        NoParams,
        "reject",
        _ANY,
        "The command must carry source_system and source_ref (provenance, KGR-05).",
        "Independent of authority level: a trusted app still has to say where the fact came from.",
        authority.require_source,
    ),
    RuleKind(
        "min_authority",
        "authority",
        "mutation_authorization",
        ALL_COMMANDS,
        frozenset({"entity", "relationship", "workspace"}),
        MinAuthorityParams,
        "reject",
        _ANY,
        "The command's authority class must be at or above the level. A request may lower its authority, never raise it.",
        "authoritative_system > admin_asserted > user_asserted. The authority class comes from the principal's role, never from the request.",
        authority.min_authority,
    ),
    RuleKind(
        "require_review_if_inferred",
        "review",
        "review",
        ALL_COMMANDS,
        frozenset({"entity", "relationship", "workspace"}),
        NoParams,
        "queue_review",
        _ANY,
        "A fact whose source_type is 'inferred' goes to review. Commands are never inferred, so this passes in P2 (P4 extraction uses it).",
        "Wins over nothing: it only adds a review item for inferred facts.",
        review.require_review_if_inferred,
    ),
    RuleKind(
        "queue_if",
        "review",
        "review",
        ALL_COMMANDS,
        frozenset({"relationship", "workspace"}),
        QueueIfParams,
        "queue_review",
        frozenset({"queue_review"}),
        "Turns a violation of the named implicit rule (disallowed_endpoint or cardinality_violation) from reject into queue_review (ONT-04).",
        "A modifier, not an evaluator: it softens a rejection into a review item and never removes a violation. Other rejections still reject.",
        None,
    ),
]

CATALOG: dict[str, RuleKind] = {k.kind: k for k in _KINDS}
ORDER: dict[str, int] = {k.kind: i for i, k in enumerate(_KINDS)}


def get_kind(kind: str) -> RuleKind | None:
    return CATALOG.get(kind)


def explicit_kinds() -> list[RuleKind]:
    return [k for k in _KINDS if not k.implicit]


def check_params(kind: RuleKind, params: dict[str, Any], path: str) -> tuple[_Params | None, list[Issue]]:
    """Validate a rule's params against the kind's model. Issues are anchored at `<path>.<field>`."""
    try:
        return kind.params_model.model_validate(params), []
    except ValidationError as exc:
        issues: list[Issue] = []
        for err in exc.errors(include_url=False, include_input=False):
            tail = ".".join(str(p) for p in err["loc"])
            issues.append(Issue(code=f"param.{err['type']}", path=f"{path}.{tail}" if tail else path, message=str(err["msg"])))
        return None, issues
