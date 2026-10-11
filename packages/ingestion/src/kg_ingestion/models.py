"""Request and response models for /v1/interactions and /v1/operations/jobs. Responses carry metadata, ids and enums; content leaves only through
the audited /content route."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from kg_ingestion.envelope import Kind

ProcessingStatus = Literal["accepted", "processing", "indexed", "partial", "failed", "deletion_pending"]
StageStatus = Literal["pending", "running", "succeeded", "skipped_by_policy", "skipped_disabled", "invalid", "failed", "dead"]
JobStatus = Literal["pending", "leased", "succeeded", "skipped", "dead", "cancelled"]
ReasonCode = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")


class AcceptedOut(BaseModel):
    event_id: uuid.UUID
    status: ProcessingStatus
    status_url: str
    request_id: str


class DerivativeOut(BaseModel):
    kind: str
    id: uuid.UUID


class StageOut(BaseModel):
    stage: str
    status: StageStatus
    generation: int
    attempts: int
    last_error_category: str | None = None
    last_error_code: str | None = None
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    enabled: bool = True
    enabled_in: str | None = None  # "P4" / "P5" for a stage that is declared but not yet implemented
    result: dict[str, Any] = {}
    derivatives: list[DerivativeOut] = []


class SubjectOut(BaseModel):
    role: Literal["subject", "actor"]
    entity_id: uuid.UUID | None = None
    resolved: bool
    ref_type: str | None = None  # unresolved references show their type and system, never the value
    ref_system: str | None = None


class InteractionSummary(BaseModel):
    id: uuid.UUID
    source_system: str
    source_ref: str
    kind: Kind
    content_type: str | None = None
    occurred_at: dt.datetime | None = None
    occurred_at_unknown: bool
    recorded_at: dt.datetime
    submitted_by: uuid.UUID
    submitted_by_handle: str | None = None
    classification: str
    processing_status: ProcessingStatus
    status_changed_at: dt.datetime
    byte_size: int
    token_estimate: int | None = None


class InteractionDetail(InteractionSummary):
    consent_external: bool
    pipeline_version: str
    ontology_version_id: uuid.UUID | None = None
    stages: list[StageOut]
    subjects: list[SubjectOut]
    status_url: str


class ContentOut(BaseModel):
    form: Literal["raw", "normalized"]
    kind: Kind
    content: Any
    content_type: str | None = None
    metadata: dict[str, str] = {}
    subject_refs: list[dict[str, Any]] = []
    actor_ref: dict[str, Any] | None = None


class ReplayIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage: str | None = Field(default=None, pattern=r"^[a-z][a-z_]{0,31}$")
    reason_code: str = ReasonCode


class ReplayedStage(BaseModel):
    stage: str
    generation: int
    job_id: uuid.UUID | None = None


class ReplayOut(BaseModel):
    event_id: uuid.UUID
    replayed: list[ReplayedStage]
    status: ProcessingStatus


class JobActionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason_code: str = ReasonCode


class AttemptOut(BaseModel):
    attempt_no: int
    worker_id: str
    started_at: dt.datetime
    ended_at: dt.datetime | None = None
    outcome: str | None = None
    error_category: str | None = None
    error_code: str | None = None
    duration_ms: int | None = None


class JobSummary(BaseModel):
    id: uuid.UUID
    stage: str
    subject_type: Literal["interaction", "command"]
    subject_id: uuid.UUID
    status: JobStatus
    attempts: int
    max_attempts: int
    generation: int
    available_at: dt.datetime
    last_error_category: str | None = None
    last_error_code: str | None = None
    pipeline_version: str
    ontology_version_id: uuid.UUID | None = None
    created_at: dt.datetime
    finished_at: dt.datetime | None = None


class JobDetail(JobSummary):
    correlation_id: str | None = None
    attempt_log: list[AttemptOut]


class JobActionOut(BaseModel):
    job_id: uuid.UUID
    status: JobStatus
    new_job_id: uuid.UUID | None = None
    generation: int | None = None


class BacklogCell(BaseModel):
    stage: str
    status: str
    jobs: int
    oldest_age_seconds: float | None = None


class BacklogOut(BaseModel):
    cells: list[BacklogCell]
    dead: int
    oldest_pending_age_seconds: float | None = None
    lease_reclaims_last_hour: int
    lease_reclaims_last_day: int
