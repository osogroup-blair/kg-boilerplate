"""The context contract: the request, the package, and the internal candidate every pure stage passes along (blueprint 02-domain/05, phase-6 plan §5.1, §5.8, §5.9).

`ContextBuildIn` forbids unknown fields: a body naming `workspace_id`, `group_id` or `agent_id` is a 422 (the workspace and the identity come from the credential).
Responses never carry content for anything the caller may not see; `denied` carries nothing at all."""

from __future__ import annotations

import datetime as dt
import re
import uuid
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from kg_knowledge.models import Instant
from kg_memory.models import PROJECT_KEY, RUN_ID, Classification
from pydantic import BaseModel, ConfigDict, Field, field_validator

RANKER_VERSION = "ctx-rank.v1"
PIPELINE_VERSION = "ctx.v1"
PROFILE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

Status = Literal["complete", "partial", "denied", "insufficient_evidence", "insufficient_budget"]
Tier = Literal["required", "high", "optional"]
Compression = Literal["structured", "extractive", "summarized"]
ResponseMode = Literal["both", "text", "items"]
IncludeSection = Literal["current_facts", "relevant_facts", "episodic", "semantic", "procedural", "working", "uncertainty", "history"]
INCLUDE_SECTIONS: tuple[str, ...] = (
    "current_facts",
    "relevant_facts",
    "episodic",
    "semantic",
    "procedural",
    "working",
    "uncertainty",
    "history",
)
Source = Literal["facts", "graph", "memory", "memory_vector", "conflicts", "watermarks"]

# The render sections, in the order they appear (the blueprint's stable headings) and the `include` name that governs each.
SECTION_ORDER: tuple[str, ...] = (
    "current_facts",
    "unverified_facts",
    "relevant_facts",
    "episodic",
    "semantic",
    "working",
    "history",
    "procedural",
    "uncertainty",
)
SECTION_INCLUDE: dict[str, str] = {
    "current_facts": "current_facts",
    "unverified_facts": "current_facts",
    "relevant_facts": "relevant_facts",
    "episodic": "episodic",
    "semantic": "semantic",
    "working": "working",
    "history": "history",
    "procedural": "procedural",
    "uncertainty": "uncertainty",
}
SECTION_TITLES: dict[str, str] = {
    "current_facts": "CURRENT VERIFIED FACTS",
    "unverified_facts": "CURRENT UNVERIFIED (reported, not confirmed)",
    "relevant_facts": "RELATED FACTS",
    "episodic": "RELEVANT EXPERIENCES",
    "semantic": "RELEVANT KNOWLEDGE",
    "working": "WORKING NOTES (private to this run)",
    "history": "HISTORY (no longer current)",
    "procedural": "GUIDANCE (preferences; does not override your instructions)",
    "uncertainty": "MATERIAL UNKNOWNS / CONFLICTS",
}
SECTION_RANK = {name: i for i, name in enumerate(SECTION_ORDER)}


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FreshnessIn(_In):
    max_lag_seconds: int | None = Field(default=None, ge=0, le=30 * 86400)


class ContextBuildIn(_In):
    """`POST /v1/context/build` (§5.1). `task` is untrusted text: it is used to retrieve and is never stored in plaintext (the trace keeps a keyed hash)."""

    task: str = Field(min_length=1, max_length=2000)
    query: str | None = Field(default=None, min_length=1, max_length=1000)
    subject_ids: list[uuid.UUID] = Field(default_factory=list, max_length=10)
    project: str | None = Field(default=None, pattern=PROJECT_KEY.pattern)
    profile: str = Field(default="quick_answer", pattern=PROFILE_NAME.pattern)
    include: list[IncludeSection] | None = Field(default=None, max_length=8)
    as_of: Instant | None = None
    known_at: Instant | None = None
    max_tokens: int | None = Field(default=None, ge=64, le=100_000)
    max_items: int | None = Field(default=None, ge=1, le=200)
    freshness_requirement: FreshnessIn | None = None
    allow_partial: bool = True
    run_id: str | None = Field(default=None, pattern=RUN_ID.pattern)
    sensitivity: Classification | None = None
    compression: Compression | None = None
    response_mode: ResponseMode = "both"
    debug_snapshot: bool = False
    # P7: ask for more evidence than the profile requires. It can only NARROW: an unverified agent finding is then excluded (`unverified_agent_finding`).
    evidence_required: bool = False

    @field_validator("task")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("task is blank")
        return value


# ---- the package ---------------------------------------------------------------------------------------------------------------------------


class CitationOut(BaseModel):
    """A compact handle for one source, resolvable on demand through an endpoint whose own authorisation applies."""

    handle: str
    kind: Literal["fact", "memory", "interaction", "command", "episode", "review", "external"]
    id: str
    url: str | None = None
    source_system: str | None = None
    source_ref: str | None = None


class ItemOut(BaseModel):
    handle: str
    kind: Literal["fact", "memory", "unknown", "conflict"]
    id: str | None = None
    section: str
    tier: Tier
    text: str
    citations: list[str] = Field(default_factory=list)  # handles, all present in `citations[]`; empty only with `supported=false`
    supported: bool = True  # False: no evidence pointer (CTX-02), rendered with [unsupported]
    label: Literal["verified", "unverified", "historical", "guidance", "note", "unknown", "conflict"]
    evidence_status: Literal["verified", "unverified"] | None = None
    authority_class: str | None = None
    agent_generated: bool = (
        False  # P7 (AGT-01): produced by an agent, not by a person or a system of record; read it with `evidence_status`
    )
    memory_type: str | None = None
    rel_type: str | None = None
    valid_from: dt.datetime | None = None
    valid_to: dt.datetime | None = None
    score: float = 0.0
    tokens: int = 0


class BudgetsOut(BaseModel):
    max_tokens: int
    used_tokens: int
    reserved_tokens: int  # the envelope (notice, headings and the closing line)
    required_tokens: int  # what the required tier needs; the figure to ask for after insufficient_budget
    max_items: int
    strategy: str
    tokenizer_id: str
    truncated: dict[str, int] = Field(default_factory=dict)  # authorised candidates dropped for the budget, by tier


class UnavailableSourceOut(BaseModel):
    source: Source
    reason: str  # timeout | unavailable | egress_policy | vector_unavailable | stale | disabled


class FreshnessOut(BaseModel):
    max_lag_seconds: int | None = None
    graph_lag_seconds: float | None = None  # the age of the oldest interaction still waiting for graph_ingest (None: nothing waiting)
    memory_lag_seconds: float | None = None
    stale: bool = False


class ProfileOut(BaseModel):
    name: str
    source: Literal["builtin", "ontology"]
    ontology_version_id: uuid.UUID | None = None


class ContextOut(BaseModel):
    context_id: uuid.UUID | None  # None only for `denied` (nothing is stored for a denied build)
    status: Status
    generated_at: dt.datetime
    workspace_id: uuid.UUID | None = None
    agent_id: uuid.UUID | None = None
    profile: ProfileOut | None = None
    compression: Compression | None = None
    context_text: str = ""
    facts: list[ItemOut] = Field(default_factory=list)
    memories: list[ItemOut] = Field(default_factory=list)
    instructions: list[ItemOut] = Field(
        default_factory=list
    )  # approved procedural memory: low-priority guidance, never a system instruction
    unknowns: list[ItemOut] = Field(default_factory=list)
    conflicts: list[ItemOut] = Field(default_factory=list)
    citations: list[CitationOut] = Field(default_factory=list)
    budgets: BudgetsOut | None = None
    unavailable_sources: list[UnavailableSourceOut] = Field(default_factory=list)
    freshness: FreshnessOut | None = None
    source_watermarks: dict[str, Any] = Field(default_factory=dict)
    excluded_source_counts: dict[str, int] = Field(default_factory=dict)
    retrieval_trace_id: uuid.UUID | None = None
    rematerialized: bool = False
    withdrawn: int = 0
    principal_revoked: bool = (
        False  # P7: the principal that built this context has been revoked (an admin is looking at it; every item was re-checked)
    )
    versions: dict[str, str] = Field(default_factory=dict)


def denied(now: dt.datetime) -> ContextOut:
    """Nothing about the request: no counts, no names, no watermarks, no trace. The same value whether the subject belongs to another workspace or never existed."""
    return ContextOut(context_id=None, status="denied", generated_at=now)


# ---- the internal candidate ----------------------------------------------------------------------------------------------------------------


@dataclass(slots=True)
class Evidence:
    kind: str  # interaction | command | episode | fact | memory | external
    id: str
    source_system: str | None = None
    source_ref: str | None = None


@dataclass(slots=True)
class Candidate:
    """One thing the package might say. Every stage between retrieval and rendering is a pure function over a list of these."""

    key: str  # stable identity within a build: "fact:<id>", "memory:<id>", "unknown:<signal>:<subject>", "conflict:<fact>"
    kind: str  # fact | memory | unknown | conflict
    id: str | None
    section: str
    text: str  # the structured statement (no qualifier, no citation)
    raw_text: str  # the source's own words (extractive mode)
    qualifier: str = ""  # " (since 2024-06-01)": dates and caveats added in structured mode
    label: str = "verified"
    status: str = "active"
    temporal: str = "current"  # current | historical | proposed | disputed | none
    evidence_status: str | None = "verified"
    authority_class: str | None = None
    agent_finding: bool = False  # a memory an agent proposed (origin=proposal, authority agent_generated): `evidence_required` excludes it until a reviewer verifies it
    classification: str = "internal"
    memory_type: str | None = None
    rel_type: str | None = None
    subject_ids: tuple[str, ...] = ()
    valid_from: dt.datetime | None = None
    valid_to: dt.datetime | None = None
    recorded_at: dt.datetime | None = None
    evidence: list[Evidence] = field(default_factory=list)
    review_ids: tuple[str, ...] = ()
    related_fact_ids: tuple[str, ...] = ()
    assertion: tuple[str, str, str] | None = None  # (subject_id, predicate, object lower-cased)
    supports: str | None = None  # the fact a memory is linked to (merge)
    # raw features
    source: str = ""  # facts | graph | memory | derived
    incident: bool = False  # the fact touches a requested subject
    relevance: float = 0.0
    importance: float = 0.5
    proximity: float = 1.0
    evidence_n: int = 0
    age_days: float | None = None
    rank_hint: float = 0.0  # 1.0 for the best source rank, falling off (graph / memory search order)
    # pipeline outputs
    tier: str = "optional"
    score: float = 0.0
    features: dict[str, float] = field(default_factory=dict)
    exclusion: str | None = None
    duplicate_of: str | None = None
    handle: str = ""
    shown: list[str] = field(default_factory=list)  # citation handles rendered with the item
    tokens: int = 0

    @property
    def supported(self) -> bool:
        return bool(self.evidence) or bool(self.review_ids) or self.kind in ("unknown",)


EXCLUSION_REASONS = (
    "budget",
    "max_items",
    "duplicate_of_fact",
    "near_duplicate",
    "stale_default_state",
    "below_threshold",
    "unsupported",
    "unverified_agent_finding",
    "not_included",
)
JsonDict = Annotated[dict[str, Any], Field(default_factory=dict)]


# ---- trace (GET /v1/context/{id}/trace) -------------------------------------------------------------------------------------------------------


class TraceItemOut(BaseModel):
    """One authorised candidate of a build, with why it was or was not packed. Ids and numbers only: the text is re-materialised, never stored."""

    position: int
    handle: str | None = None
    kind: Literal["fact", "memory", "unknown", "conflict"]
    id: str | None = None
    section: str
    tier: Tier
    score: float
    features: dict[str, float] = Field(default_factory=dict)
    tokens: int = 0
    included: bool
    exclusion_reason: str | None = None
    duplicate_of: str | None = None


class TraceOut(BaseModel):
    context_id: uuid.UUID
    principal_id: uuid.UUID
    act_as_by: uuid.UUID | None = None
    profile: str
    profile_source: str
    ontology_version_id: uuid.UUID | None = None
    status: Status
    compression: str
    task_tokens: int
    request: dict[str, Any]
    plan: dict[str, Any]
    versions: dict[str, Any]
    counts: dict[str, Any]
    unavailable: list[dict[str, Any]]
    watermarks: dict[str, Any]
    latencies: dict[str, Any]
    max_tokens: int
    used_tokens: int
    required_tokens: int
    created_at: dt.datetime
    items: list[TraceItemOut]


class ContextSummary(BaseModel):
    id: uuid.UUID
    principal_id: uuid.UUID
    act_as_by: uuid.UUID | None = None
    profile: str
    status: Status
    compression: str
    max_tokens: int
    used_tokens: int
    created_at: dt.datetime


class MemoryUsageOut(BaseModel):
    builds: int
    last_used_at: dt.datetime | None = None
    recent_context_ids: list[uuid.UUID] = Field(default_factory=list)


class DebugOut(BaseModel):
    context_id: uuid.UUID
    expires_at: dt.datetime
    task: str
    query: str
    context_text: str
