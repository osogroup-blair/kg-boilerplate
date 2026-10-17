"""Request and response models for the ontology, entity, relationship and command APIs (blueprint 03-runtime/01 shape rules).

Responses never echo request values in error bodies; attribute values appear only in entity and fact reads, where sensitive ones are redacted
unless the caller holds entity.read_sensitive (§5.5)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any, Literal

from kg_ontology import ValidationReport
from kg_ontology.spec import AnyAuthority, Authority, Classification
from kg_ontology.values import parse_instant
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

REDACTED_VALUE: dict[str, bool] = {"redacted": True}
ProjectionStatus = Literal["pending", "projected", "failed", "skipped_by_policy", "not_applicable"]
EmbeddingStatus = Literal["pending", "embedded", "skipped_by_policy"]
CommandStatus = Literal["applied", "rejected", "needs_review"]
CommandTypeName = Literal[
    "entity.create",
    "entity.update",
    "relationship.assert",
    "relationship.end",
    "review.decide",
    "entity.merge",
    "memory.save",
    "memory.propose",
    "memory.extract",
    "memory.supersede",
    "memory.expire",
    "memory.dispute",
    "memory.publish",
    "memory.review",
    "correction.fact_reject",
    "correction.fact_supersede",
    "correction.fact_end",
    "correction.fact_restore",
    "correction.attribute",
    "correction.rename",
    "correction.memory",
    "entity.split",
    "memory.consolidate",
]


def _instant(value: Any) -> Any:
    if value is None or isinstance(value, dt.datetime) and value.tzinfo:
        return value
    parsed = parse_instant(value)
    if parsed is None:
        raise ValueError("not a valid date or datetime")
    return parsed


Instant = Annotated[dt.datetime, BeforeValidator(_instant)]  # a bare date means 00:00 UTC; a naive datetime is taken as UTC


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---- ontology -------------------------------------------------------------------------------------------------------------------------------


class DraftCreateIn(_In):
    format: Literal["yaml", "json"] | None = None
    content: str | dict[str, Any] | None = Field(default=None, max_length=400_000)
    copy_of: uuid.UUID | None = None

    @model_validator(mode="after")
    def _one_source(self) -> DraftCreateIn:
        if (self.content is None) == (self.copy_of is None):
            raise ValueError("send exactly one of content or copy_of")
        if self.content is not None and self.format is None:
            self.format = "json" if isinstance(self.content, dict) else "yaml"
        return self


class DraftReplaceIn(_In):
    format: Literal["yaml", "json"] | None = None
    content: str | dict[str, Any] = Field(max_length=400_000)

    @model_validator(mode="after")
    def _format(self) -> DraftReplaceIn:
        if self.format is None:
            self.format = "json" if isinstance(self.content, dict) else "yaml"
        return self


class PublishIn(_In):
    notes: str = Field(default="", max_length=2000)
    activate: bool = True


class ActivateIn(_In):
    version_id: uuid.UUID
    reason_code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")


class OntologyVersionSummary(BaseModel):
    id: uuid.UUID
    ontology_key: str | None
    version: str | None
    state: Literal["draft", "published", "retired"]
    active: bool
    content_hash: str | None
    revision: int
    error_count: int
    notes: str
    created_at: dt.datetime
    published_at: dt.datetime | None
    retired_at: dt.datetime | None


class OntologyVersionOut(OntologyVersionSummary):
    spec: dict[str, Any]
    source_text: str | None
    base_version_id: uuid.UUID | None
    validation_report: ValidationReport
    activated: bool = False


class ActiveOntologyOut(BaseModel):
    version: OntologyVersionSummary | None
    entity_types: list[str] = []
    relationship_types: list[dict[str, str]] = []


class RuleKindOut(BaseModel):
    kind: str
    family: str
    stage: str
    implicit: bool
    commands: list[str]
    applies_to: list[str]
    default_action: str
    allowed_actions: list[str]
    description: str
    conflict_policy: str
    params_schema: dict[str, Any]


class RuleCatalogOut(BaseModel):
    stages: list[str]
    kinds: list[RuleKindOut]


# ---- entities and relationships -------------------------------------------------------------------------------------------------------------


class ExternalId(_In):
    system: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=255)


class EntityRefIn(_In):
    """An endpoint: by platform id, or by a strong external identifier (never by name, KGR-02)."""

    entity_id: uuid.UUID | None = None
    type: str | None = Field(default=None, max_length=64)
    external_id: ExternalId | None = None

    @model_validator(mode="after")
    def _one_form(self) -> EntityRefIn:
        by_id, by_ext = self.entity_id is not None, self.external_id is not None
        if by_id == by_ext or (by_ext and self.type is None):
            raise ValueError("send entity_id, or type with external_id")
        return self


class _CommandBase(_In):
    source_system: str | None = Field(default=None, max_length=128)
    source_ref: str | None = Field(default=None, max_length=255)
    occurred_at: Instant | None = None
    authority: Authority | None = None  # may only lower the authority the caller's role grants


class _CommandIn(_CommandBase):
    classification: Classification = "internal"


class EntityCreateIn(_CommandIn):
    type: str = Field(max_length=64)
    name: str = Field(max_length=500)
    attributes: dict[str, Any] = Field(default_factory=dict)
    aliases: list[str] = Field(default_factory=list, max_length=50)
    external_ids: list[ExternalId] = Field(default_factory=list, max_length=20)


class EntityUpdateIn(_CommandBase):
    classification: Classification | None = None  # None: unchanged
    name: str | None = Field(default=None, max_length=500)
    attributes: dict[str, Any] = Field(default_factory=dict)
    add_aliases: list[str] = Field(default_factory=list, max_length=50)
    add_external_ids: list[ExternalId] = Field(default_factory=list, max_length=20)


class RelationshipAssertIn(_CommandIn):
    type: str = Field(max_length=64)
    source: EntityRefIn
    target: EntityRefIn
    attributes: dict[str, Any] = Field(default_factory=dict)
    valid_from: Instant | None = None
    valid_to: Instant | None = None
    valid_from_unknown: bool = False
    source_recorded_at: Instant | None = (
        None  # when the source system learned it (KGR-04); the platform's own record time is set by the database
    )


class RelationshipEndIn(_CommandIn):
    valid_to: Instant
    source_recorded_at: Instant | None = None


class EntityOut(BaseModel):
    id: uuid.UUID
    type: str
    name: str
    aliases: list[str]
    attributes: dict[str, Any]
    classification: Classification
    status: Literal["active", "deleted", "candidate", "untyped", "merged"]
    origin: Literal["command", "extracted", "correction"] = "command"
    merged_into_id: uuid.UUID | None = None
    revision: int
    external_ids: list[ExternalId]
    ontology_version_id: uuid.UUID
    recorded_at: dt.datetime
    updated_at: dt.datetime
    projection_status: ProjectionStatus
    embedding_status: EmbeddingStatus
    projected_at: dt.datetime | None


class FactEndpoint(BaseModel):
    id: uuid.UUID
    type: str
    name: str


FactStatus = Literal["active", "superseded", "disputed", "deleted", "proposed", "rejected"]


class EvidenceOut(BaseModel):
    """Why the system holds a fact (KGR-05): a command, or an episode of an interaction. Never content."""

    kind: Literal["command", "episode"]
    command_id: uuid.UUID | None = None
    interaction_id: uuid.UUID | None = None
    episode_uuid: str | None = None
    source_system: str | None = None
    source_ref: str | None = None
    occurred_at: dt.datetime | None = None  # when the source says it happened (interaction) or the command was claimed to
    recorded_at: dt.datetime | None = None  # when the platform received it
    added_at: dt.datetime


class FactOut(BaseModel):
    id: uuid.UUID
    rel_type: str
    source: FactEndpoint
    target: FactEndpoint
    attributes: dict[str, Any]
    fact_text: str | None = None
    valid_from: dt.datetime | None
    valid_from_unknown: bool
    valid_to: dt.datetime | None
    source_recorded_at: dt.datetime | None
    recorded_at: dt.datetime
    superseded_at: dt.datetime | None
    succeeded_by_fact_id: uuid.UUID | None
    status: FactStatus
    phase: Literal["current", "historical", "future"]
    authority_class: AnyAuthority
    classification: Classification
    source_type: Literal["command", "extracted", "inferred", "correction"]
    evidence_status: Literal["verified", "unverified"] = "verified"
    evidence: list[EvidenceOut] = []
    decision_code: str | None = None
    decided_at: dt.datetime | None = None
    source_command_id: uuid.UUID | None
    source_interaction_id: uuid.UUID | None = None
    source_system: str | None
    source_ref: str | None
    ontology_version_id: uuid.UUID
    revision: int
    projection_status: ProjectionStatus


class EntityDetailOut(EntityOut):
    relationships: list[FactOut]
    memories: int | None = None  # curated memories about this entity that THE CALLER may see (P5); never a count of what it may not


# ---- commands -------------------------------------------------------------------------------------------------------------------------------


class CommandOut(BaseModel):
    command_id: uuid.UUID
    command_type: CommandTypeName
    status: CommandStatus
    matched_existing: bool = False
    entity_id: uuid.UUID | None = None
    fact_id: uuid.UUID | None = None
    ended_fact_ids: list[uuid.UUID] = []
    review_item_id: uuid.UUID | None = None
    ontology_version_id: uuid.UUID
    projection_status: ProjectionStatus
    status_url: str


class DecisionStage(BaseModel):
    stage: str
    outcome: str
    violations: list[dict[str, Any]] = []


class CommandSummary(BaseModel):
    id: uuid.UUID
    command_type: CommandTypeName
    status: CommandStatus
    actor_principal_id: uuid.UUID
    authority_class: Authority
    classification: Classification
    source_system: str | None
    source_ref: str | None
    ontology_version_id: uuid.UUID | None  # None only for memory commands of a workspace with no active ontology
    http_status: int
    projection_status: ProjectionStatus
    projection_attempts: int
    last_error_code: str | None
    created_at: dt.datetime
    projected_at: dt.datetime | None
    entity_id: uuid.UUID | None = None
    fact_id: uuid.UUID | None = None


class CommandDetailOut(CommandSummary):
    decision: list[dict[str, Any]]
    result: dict[str, Any]
    status_url: str


# ---- P4: reviews, evidence, graph ----------------------------------------------------------------------------------------------------------

ReviewType = Literal[
    "policy_violation",
    "identity_match",
    "contradiction",
    "graphiti_invalidation",
    "memory_proposal",
    "memory_conflict",
    "source_compromised",
    "deletion_exception",
    "consolidation_proposal",
    "correction_request",
    "authoritative_override",
]
ReviewStatusFilter = Literal["open", "decided"]
Decision = Literal[
    "same_entity",
    "different_entity",
    "needs_more_evidence",
    "keep_existing",
    "mark_disputed",
    "accept_claim",
    "acknowledge",
    "open_correction",
    "approve",
    "reject",
    "approve_with_edit",
    "supersede_existing",
    "restore",
    "confirm_removal",
    "redact_text",
    "delete_item",
    "retain_with_reason",
    "retry_verification",
    "apply_correction",
    "dismiss",
]
RationaleCode = Literal[
    "verified_with_source",
    "source_is_authoritative",
    "reported_by_trusted_party",
    "same_real_world_entity",
    "different_real_world_entity",
    "outdated_information",
    "duplicate_report",
    "incorrect_extraction",
    "insufficient_evidence",
    "policy_exception",
    "no_action_needed",
    "other",
]
ALLOWED_DECISIONS: dict[str, tuple[str, ...]] = {
    "identity_match": ("same_entity", "different_entity", "needs_more_evidence"),
    "contradiction": ("keep_existing", "mark_disputed", "accept_claim"),
    "graphiti_invalidation": ("acknowledge", "open_correction"),
    "policy_violation": ("approve", "reject"),
    "memory_proposal": ("approve", "reject", "approve_with_edit"),
    "memory_conflict": ("keep_existing", "supersede_existing", "mark_disputed"),
    "source_compromised": (
        "restore",
        "confirm_removal",
    ),  # P7: a derivative of a compromised principal: reinstate it, or confirm it is refused
    # P8. A deletion exception: redact the text that names the deleted subject, delete the item, retain it under a legal hold, or verify again.
    "deletion_exception": ("redact_text", "delete_item", "retain_with_reason", "retry_verification"),
    "consolidation_proposal": ("approve", "reject"),  # approving supersedes the sources (their evidence is kept by reference)
    "correction_request": ("apply_correction", "dismiss"),
    "authoritative_override": ("acknowledge",),  # the source system's owner has been told
}


class DecisionIn(_In):
    decision: Decision
    rationale_code: RationaleCode
    rationale_text: str | None = Field(
        default=None, max_length=1000
    )  # stored on the review row only: never copied to audit, logs or metrics
    edited_content: str | None = Field(
        default=None, min_length=1, max_length=10_000
    )  # approve_with_edit on a memory proposal: the reviewer's wording


class ReviewOut(BaseModel):
    id: uuid.UUID
    item_type: ReviewType
    status: ReviewStatusFilter
    rule_kind: str
    reason_code: str
    revision: int
    created_at: dt.datetime
    due_at: dt.datetime | None
    command_id: uuid.UUID | None = None
    subject_entity_id: uuid.UUID | None = None
    candidate_entity_id: uuid.UUID | None = None
    fact_id: uuid.UUID | None = None
    competing_fact_id: uuid.UUID | None = None
    interaction_id: uuid.UUID | None = None
    memory_id: uuid.UUID | None = None
    competing_memory_id: uuid.UUID | None = None
    decision: str | None = None
    rationale_code: str | None = None
    decided_by: uuid.UUID | None = None
    decided_at: dt.datetime | None = None
    submitter_revoked: bool = (
        False  # P7: the principal that submitted the memory behind this item has been revoked (a badge for the reviewer)
    )
    agent_generated: bool = (
        False  # P7: the memory behind this item was produced by an agent (it stays unverified until a reviewer approves it)
    )


class ReviewEntityView(BaseModel):
    """One side of an identity comparison. Sensitive attributes are redacted unless the caller may see them."""

    id: uuid.UUID
    type: str
    name: str
    aliases: list[str]
    attributes: dict[str, Any]
    external_ids: list[ExternalId]
    status: str
    origin: str
    classification: Classification
    mention_count: int
    fact_count: int
    merged_into_id: uuid.UUID | None = None


class IdentityCandidate(BaseModel):
    entity_id: uuid.UUID
    trigram: float | None = None
    cosine: float | None = None
    reasons: list[str] = []


class InteractionRef(BaseModel):
    """An interaction behind a review item. The reviewer reads its content through the audited evidence endpoint, never from this list."""

    interaction_id: uuid.UUID
    episode_uuid: str | None = None
    source_system: str
    source_ref: str
    occurred_at: dt.datetime | None
    recorded_at: dt.datetime


class ReviewMemoryView(BaseModel):
    """One memory a review item is about (P5). Filled by the API from kg_memory with the REVIEWER's own visibility, so it never shows a scope or a memory the
    reviewer could not open directly."""

    role: Literal["candidate", "competing", "source"]  # source: a memory a consolidation proposal would fold in
    id: uuid.UUID
    type: str
    origin: str
    status: str
    summary: str
    content: str
    authority_class: str
    evidence_status: str
    classification: str
    scopes: list[dict[str, str]]
    subject_ids: list[uuid.UUID]
    created_by: uuid.UUID
    recorded_at: dt.datetime


class ReviewDetail(ReviewOut):
    allowed_decisions: list[str]
    rationale_text: str | None = None
    entities: list[ReviewEntityView] = []
    facts: list[FactOut] = []
    candidates: list[IdentityCandidate] = []
    interactions: list[InteractionRef] = []
    violations: list[dict[str, Any]] = []  # a queued command's rule trace: codes and paths, never values
    memories: list[ReviewMemoryView] = []
    # P8: a deletion exception names its request and the codes of what is left to decide (never the text); other types leave these empty
    deletion_request_id: uuid.UUID | None = None
    detail: dict[str, Any] = {}


class DecisionOut(BaseModel):
    review_id: uuid.UUID
    decision: str
    status: ReviewStatusFilter
    command_id: uuid.UUID | None
    revision: int
    projection_status: ProjectionStatus
    entity_ids: list[uuid.UUID] = []
    fact_ids: list[uuid.UUID] = []
    memory_ids: list[uuid.UUID] = []
    applied_command_id: uuid.UUID | None = None  # policy_violation approve: the command that was applied


class MentionOut(BaseModel):
    id: uuid.UUID  # what an identity split names to move a mention
    interaction_id: uuid.UUID
    episode_uuid: str
    resolution: str
    status: str
    created_at: dt.datetime
    source_system: str
    source_ref: str
    occurred_at: dt.datetime | None
    recorded_at: dt.datetime


class AttributeClaimOut(BaseModel):
    id: uuid.UUID
    attribute_key: str
    value: Any
    status: str
    interaction_id: uuid.UUID
    episode_uuid: str
    created_at: dt.datetime


class GraphNodeOut(BaseModel):
    id: uuid.UUID
    type: str
    name: str
    status: str
    origin: str
    hop: int


class GraphEdgeOut(BaseModel):
    id: uuid.UUID
    rel_type: str
    source_id: uuid.UUID
    target_id: uuid.UUID
    status: FactStatus
    evidence_status: Literal["verified", "unverified"]
    authority_class: AnyAuthority
    phase: Literal["current", "historical", "future"]
    valid_from: dt.datetime | None
    valid_to: dt.datetime | None
    fact_text: str | None = None


class GraphOut(BaseModel):
    center_id: uuid.UUID
    nodes: list[GraphNodeOut]
    edges: list[GraphEdgeOut]
    truncated: bool = False  # the node cap cut the neighbourhood: narrow it with hops, types or time


class GraphSearchIn(_In):
    query: str = Field(min_length=1, max_length=500)
    as_of: Instant | None = None
    known_at: Instant | None = None
    center_entity_id: uuid.UUID | None = None
    limit: int = Field(default=10, ge=1, le=25)


class GraphSearchOut(BaseModel):
    facts: list[FactOut]
    unreconciled_hits: int = 0  # graph hits with no Postgres row yet (dropped)


class DerivedSummaryOut(BaseModel):
    """Graphiti's running summary of an entity. DERIVED text: it is never a fact source, never used to answer a question, and always labelled so."""

    entity_id: uuid.UUID
    summary: str | None
    label: Literal["derived_unverified"] = "derived_unverified"
    graph_available: bool = True
