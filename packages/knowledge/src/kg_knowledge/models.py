"""Request and response models for the ontology, entity, relationship and command APIs (blueprint 03-runtime/01 shape rules).

Responses never echo request values in error bodies; attribute values appear only in entity and fact reads, where sensitive ones are redacted
unless the caller holds entity.read_sensitive (§5.5)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any, Literal

from kg_ontology import ValidationReport
from kg_ontology.spec import Authority, Classification
from kg_ontology.values import parse_instant
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

REDACTED_VALUE: dict[str, bool] = {"redacted": True}
ProjectionStatus = Literal["pending", "projected", "failed", "skipped_by_policy", "not_applicable"]
EmbeddingStatus = Literal["pending", "embedded", "skipped_by_policy"]
CommandStatus = Literal["applied", "rejected", "needs_review"]
CommandTypeName = Literal["entity.create", "entity.update", "relationship.assert", "relationship.end"]


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
    status: Literal["active", "deleted"]
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


class FactOut(BaseModel):
    id: uuid.UUID
    rel_type: str
    source: FactEndpoint
    target: FactEndpoint
    attributes: dict[str, Any]
    valid_from: dt.datetime | None
    valid_from_unknown: bool
    valid_to: dt.datetime | None
    source_recorded_at: dt.datetime | None
    recorded_at: dt.datetime
    superseded_at: dt.datetime | None
    succeeded_by_fact_id: uuid.UUID | None
    status: Literal["active", "superseded", "disputed", "deleted"]
    phase: Literal["current", "historical", "future"]
    authority_class: Authority
    classification: Classification
    source_type: Literal["command", "extracted", "inferred"]
    source_command_id: uuid.UUID
    source_system: str | None
    source_ref: str | None
    ontology_version_id: uuid.UUID
    revision: int
    projection_status: ProjectionStatus


class EntityDetailOut(EntityOut):
    relationships: list[FactOut]


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
    ontology_version_id: uuid.UUID
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
