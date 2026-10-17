"""Curated-memory vocabulary and the request / response models of the memory APIs (blueprint 02-domain/04, phase-5 plan §5.1 and §6.2).

Responses never carry content for a memory the caller cannot see, and error bodies never echo request values."""

from __future__ import annotations

import datetime as dt
import re
import uuid
from typing import Annotated, Any, Literal

from kg_knowledge.models import Instant
from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

MemoryType = Literal["working", "episodic", "semantic", "procedural"]
MemoryStatus = Literal["proposed", "active", "superseded", "disputed", "expired", "rejected", "deleted"]
Origin = Literal["explicit", "proposal", "extracted", "working", "published", "consolidated"]
ScopeKind = Literal["workspace", "subject", "project", "agent", "session"]
Authority = Literal["authoritative_system", "admin_asserted", "user_asserted", "conversation_report", "agent_generated"]
EvidenceStatus = Literal["verified", "unverified"]
Classification = Literal["public", "internal", "confidential", "restricted", "local_only"]
EmbeddingStatus = Literal["pending", "ready", "skipped_by_policy", "failed"]
Relation = Literal["replaces", "consolidated_from", "published_from"]

MEMORY_TYPES: tuple[str, ...] = ("working", "episodic", "semantic", "procedural")
ORIGINS: tuple[str, ...] = ("explicit", "proposal", "extracted", "working", "published", "consolidated")
STATUSES: tuple[str, ...] = ("proposed", "active", "superseded", "disputed", "expired", "rejected", "deleted")
SCOPE_KINDS: tuple[str, ...] = ("workspace", "subject", "project", "agent", "session")
SHARED_KINDS = frozenset({"workspace", "subject", "project"})  # visible beyond one principal; agent and session are private
CLASSIFICATION_ORDER: tuple[str, ...] = ("public", "internal", "confidential", "restricted", "local_only")
AUTHORITY_RANK: dict[str, int] = {
    "agent_generated": 1,
    "conversation_report": 2,
    "user_asserted": 3,
    "admin_asserted": 4,
    "authoritative_system": 5,
}
RANKER_VERSION = "mem-rrf.v1"
PROJECT_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ReasonCode = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ScopeIn(_In):
    """`ref` (alias `id`): an entity id for `subject`, a project key for `project`, `self` (or the caller's own principal id) for `agent`, `self/<run_id>` for
    `session`. `workspace` takes no ref."""

    kind: ScopeKind
    ref: str | None = Field(default=None, max_length=200)

    @model_validator(mode="before")
    @classmethod
    def _id_alias(cls, data: Any) -> Any:
        if isinstance(data, dict) and "id" in data and "ref" not in data:
            data = {**data, "ref": data["id"]}
            data.pop("id")
        return data

    @model_validator(mode="after")
    def _shape(self) -> ScopeIn:
        if self.kind == "workspace":
            if self.ref not in (None, "*"):
                raise ValueError("a workspace scope takes no ref")
            self.ref = "*"
        elif not self.ref:
            raise ValueError("this scope needs a ref")
        elif self.kind == "project" and self.ref != "*" and not PROJECT_KEY.match(self.ref):
            raise ValueError("a project key is letters, digits, '.', '_' or '-'")
        elif self.kind == "subject":
            try:
                uuid.UUID(self.ref)
            except ValueError as exc:
                raise ValueError("a subject scope names an entity id") from exc
        return self


class AssertionIn(_In):
    """An optional structured claim: `predicate` is a short token, `object` a scalar. It lets the platform notice a contradiction between two memories."""

    subject_id: uuid.UUID
    predicate: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    object: str | int | float | bool

    @field_validator("object")
    @classmethod
    def _short(cls, value: Any) -> Any:
        if isinstance(value, str) and len(value) > 200:
            raise ValueError("object is too long")
        return value


class _MemoryBody(_In):
    type: MemoryType
    content: str = Field(min_length=1, max_length=10_000)
    summary: str | None = Field(default=None, min_length=1, max_length=280)
    subject_ids: list[uuid.UUID] = Field(default_factory=list, max_length=10)
    topic_ids: list[uuid.UUID] = Field(default_factory=list, max_length=10)
    occurred_at: Instant | None = None
    effective_from: Instant | None = None
    effective_until: Instant | None = None
    importance: float | None = Field(default=None, ge=0.0, le=1.0)
    assertion: AssertionIn | None = None
    classification: Classification | None = None

    @field_validator("content")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content is blank")
        return value


class MemorySaveIn(_MemoryBody):
    """`POST /v1/memories`: an explicit save by a trusted app or admin (needs attribution and a purpose), or a working / agent-private note by an agent."""

    scopes: list[ScopeIn] = Field(default_factory=list, max_length=8)
    source_interaction_id: uuid.UUID | None = None
    source_ref: str | None = Field(default=None, min_length=1, max_length=256)
    purpose: str | None = Field(default=None, min_length=1, max_length=200)
    run_id: str | None = Field(default=None, pattern=RUN_ID.pattern)
    ttl_hours: float | None = Field(default=None, gt=0.0, le=24 * 30)


class ProposalIn(_MemoryBody):
    """`POST /v1/memories/proposals` (blueprint reference §C). `requested_scope` is advisory and always checked against the caller's grants."""

    requested_scope: ScopeIn | None = None
    scopes: list[ScopeIn] = Field(default_factory=list, max_length=8)
    source_event_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    fact_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    memory_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    purpose: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def _evidence(self) -> ProposalIn:
        if not (self.source_event_ids or self.fact_ids or self.memory_ids):
            raise ValueError("a proposal needs evidence: source_event_ids, fact_ids or memory_ids")
        return self


class SupersedeIn(_In):
    content: str = Field(min_length=1, max_length=10_000)
    summary: str | None = Field(default=None, min_length=1, max_length=280)
    reason_code: ReasonCode
    importance: float | None = Field(default=None, ge=0.0, le=1.0)
    effective_from: Instant | None = None
    effective_until: Instant | None = None


class ReasonIn(_In):
    reason_code: ReasonCode


class MemoryPublishIn(_In):
    scopes: list[ScopeIn] = Field(min_length=1, max_length=8)
    reason_code: ReasonCode


class SearchIn(_In):
    query: str = Field(min_length=1, max_length=1000)
    types: list[MemoryType] | None = None
    subject_ids: list[uuid.UUID] | None = Field(default=None, max_length=10)
    project: str | None = Field(default=None, pattern=PROJECT_KEY.pattern)
    run_id: str | None = Field(default=None, pattern=RUN_ID.pattern)
    all_runs: bool = False
    as_of: Instant | None = None
    include: list[Literal["proposed", "disputed"]] = Field(default_factory=list, max_length=2)
    recency_weight: float | None = Field(default=None, ge=0.0, le=1.0)
    limit: int = Field(default=10, ge=1, le=50)


# ---- responses ------------------------------------------------------------------------------------------------------------------------------


class ScopeOut(BaseModel):
    kind: ScopeKind
    ref: str


class MemoryEvidenceOut(BaseModel):
    source_kind: str
    interaction_id: uuid.UUID | None = None
    episode_uuid: str | None = None
    fact_id: uuid.UUID | None = None
    command_id: uuid.UUID | None = None
    memory_id: uuid.UUID | None = None
    external_ref: str | None = None
    added_at: dt.datetime


class LineageOut(BaseModel):
    relation: Relation
    memory_id: uuid.UUID  # the OTHER memory (the visible one)
    direction: Literal["replaces", "replaced_by", "published_from", "published_as", "consolidated_from", "consolidated_into"]


class EmbeddingOut(BaseModel):
    status: EmbeddingStatus
    model_id: str | None = None
    version: int | None = None
    dim: int | None = None
    embedded_at: dt.datetime | None = None


class MemorySummary(BaseModel):
    id: uuid.UUID
    type: MemoryType
    origin: Origin
    status: MemoryStatus
    summary: str
    authority_class: Authority
    evidence_status: EvidenceStatus
    classification: Classification
    importance: float
    created_by: uuid.UUID
    recorded_at: dt.datetime
    effective_from: dt.datetime
    effective_until: dt.datetime | None
    expires_at: dt.datetime | None
    embedding_status: EmbeddingStatus
    revision: int
    scopes: list[ScopeOut]
    subject_ids: list[uuid.UUID]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def agent_generated(self) -> bool:
        """AGT-01: produced by an agent. Read it with `evidence_status` (`unverified` until a reviewer approves it)."""
        return self.authority_class == "agent_generated"


class MemoryOut(MemorySummary):
    content: str
    assertion: dict[str, Any] | None = None
    topic_ids: list[uuid.UUID] = []
    confidence: float | None = None
    occurred_at: dt.datetime | None = None
    last_confirmed_at: dt.datetime | None = None
    run_id: str | None = None
    purpose: str | None = None
    derived_by_alias: str | None = None
    pipeline_version: str
    prompt_version: str | None = None
    command_id: uuid.UUID
    evidence: list[MemoryEvidenceOut] = []
    lineage: list[LineageOut] = []
    embedding: EmbeddingOut


class WriteOut(BaseModel):
    memory_id: uuid.UUID
    status: MemoryStatus
    deduplicated: bool = False
    review_id: uuid.UUID | None = None
    revision: int
    command_id: uuid.UUID | None = None
    expires_at: dt.datetime | None = None
    embedding_status: EmbeddingStatus | None = None


class HitComponents(BaseModel):
    lex_rank: int | None
    vec_rank: int | None
    cosine: float | None
    importance: float
    evidence_count: int


class SearchHit(BaseModel):
    memory_id: uuid.UUID
    type: MemoryType
    status: MemoryStatus
    summary: str
    content: str
    evidence_status: EvidenceStatus
    authority_class: Authority
    classification: Classification
    scopes: list[ScopeOut]
    subject_ids: list[uuid.UUID]
    effective_from: dt.datetime
    effective_until: dt.datetime | None
    score: float
    matched_by: Literal["lexical", "vector", "both"]
    components: HitComponents

    @computed_field  # type: ignore[prop-decorator]
    @property
    def agent_generated(self) -> bool:
        """AGT-01: produced by an agent. Read it with `evidence_status` (`unverified` until a reviewer approves it)."""
        return self.authority_class == "agent_generated"


class SearchOut(BaseModel):
    hits: list[SearchHit]
    candidates_considered: int  # authorised candidates ONLY: never a count of what the caller could not see
    embedding_coverage: float
    embedding_version: int
    ranker_version: str
    degraded: list[str] = []


class GrantOut(BaseModel):
    principal_id: uuid.UUID
    scope_kind: str
    scope_ref: str
    created_at: dt.datetime
