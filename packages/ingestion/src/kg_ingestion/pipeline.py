"""The pipeline definition (blueprint Flow A) and the orchestrator that advances an interaction through it.

    normalize ──► graph_ingest (P4, disabled) ──┐
              └─► memory_propose (P5, disabled) ─┴─► finalize (inline, never a job)

`Orchestrator.advance` runs INSIDE the handler's tenant transaction, right after a stage result is written: it enqueues the stages that became ready
(dedupe keys make that idempotent) and finalises the interaction status once every stage is terminal."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from kg_jobs.outbox import enqueue
from kg_obs import metrics
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from kg_ingestion import PIPELINE_VERSION

# stage states (interaction_stages.status)
PENDING, RUNNING, SUCCEEDED, SKIPPED_POLICY, SKIPPED_DISABLED, INVALID, FAILED, DEAD = (
    "pending",
    "running",
    "succeeded",
    "skipped_by_policy",
    "skipped_disabled",
    "invalid",
    "failed",
    "dead",
)
DONE_OK = frozenset({SUCCEEDED, SKIPPED_POLICY, SKIPPED_DISABLED})  # a derivative that is "complete" for status purposes
OPEN = frozenset({PENDING, RUNNING, FAILED})  # FAILED = failed attempt, a retry is scheduled
BAD = frozenset({DEAD, INVALID})
REPLAYABLE = frozenset({DEAD, FAILED})  # what a replay without an explicit stage picks up; an explicit stage may also be `invalid`


@dataclass(frozen=True)
class StageDef:
    name: str
    deps: tuple[str, ...] = ()
    enabled: bool = True
    model_stage: bool = False  # sends content to a model: subject to consent (ING-06) and egress policy
    enabled_in: str | None = None  # shown in the console for a disabled stage ("enabled in P4")


@dataclass(frozen=True)
class Pipeline:
    version: str
    stages: tuple[StageDef, ...]

    def stage(self, name: str) -> StageDef:
        for s in self.stages:
            if s.name == name:
                return s
        raise KeyError(name)

    @property
    def names(self) -> list[str]:
        return [s.name for s in self.stages]

    def initial_statuses(self, *, consent_external: bool) -> dict[str, str]:
        """The stage plan recorded at acceptance. A disabled stage is listed as skipped_disabled (so a source-only event is valid, EVD-04);
        an enabled model stage of an event whose consent denies external processing is skipped_by_policy (ING-06)."""
        plan: dict[str, str] = {}
        for s in self.stages:
            if not s.enabled:
                plan[s.name] = SKIPPED_DISABLED
            elif s.model_stage and not consent_external:
                plan[s.name] = SKIPPED_POLICY
            else:
                plan[s.name] = PENDING
        return plan

    def ready(self, statuses: Mapping[str, str]) -> list[str]:
        """Pending stages whose dependencies are all complete."""
        return [s.name for s in self.stages if statuses.get(s.name) == PENDING and all(statuses.get(d) in DONE_OK for d in s.deps)]

    def roots(self) -> list[str]:
        return [s.name for s in self.stages if not s.deps]

    def finalize(self, statuses: Mapping[str, str]) -> str:
        """The interaction's processing status from its stage states (phase-3 plan §5.3). `accepted` is only ever set at acceptance."""
        if any(statuses.get(r) in BAD for r in self.roots()):
            return "failed"
        if any(v in OPEN for v in statuses.values()):
            return "processing"
        if all(v in DONE_OK for v in statuses.values()):
            return "indexed"
        return "partial"  # every stage is terminal, a derivative stage is dead or invalid, the rest succeeded: visible, never silent


PIPELINE_V1 = Pipeline(
    PIPELINE_VERSION,
    (
        StageDef("normalize"),
        StageDef("graph_ingest", deps=("normalize",), enabled=False, model_stage=True, enabled_in="P4"),
        StageDef("memory_propose", deps=("normalize",), enabled=False, model_stage=True, enabled_in="P5"),
    ),
)


def dedupe_key(interaction_id: uuid.UUID, stage: str, generation: int) -> str:
    return f"interaction:{interaction_id}:{stage}:g{generation}"


def job_payload(interaction_id: uuid.UUID, generation: int) -> dict[str, str | int]:
    return {"interaction_id": str(interaction_id), "generation": generation}


class Orchestrator:
    def __init__(self, pipeline: Pipeline = PIPELINE_V1):
        self.pipeline = pipeline

    async def advance(self, conn: AsyncConnection, workspace_id: uuid.UUID, interaction_id: uuid.UUID) -> str:
        """Enqueues the stages that are ready and finalises the status. Returns the interaction's processing status afterwards."""
        head = (
            await conn.execute(
                text(
                    "SELECT pipeline_version, ontology_version_id, processing_status FROM interactions "
                    "WHERE workspace_id = :ws AND id = :id FOR UPDATE"
                ),
                {"ws": workspace_id, "id": interaction_id},
            )
        ).first()
        if head is None:
            return "unknown"
        rows = (
            await conn.execute(
                text("SELECT stage, status, generation FROM interaction_stages WHERE workspace_id = :ws AND interaction_id = :id"),
                {"ws": workspace_id, "id": interaction_id},
            )
        ).all()
        statuses = {r.stage: r.status for r in rows}
        generations = {r.stage: r.generation for r in rows}
        for stage in self.pipeline.ready(statuses):
            await enqueue(
                conn,
                workspace_id=workspace_id,
                stage=stage,
                subject_type="interaction",
                subject_id=interaction_id,
                dedupe_key=dedupe_key(interaction_id, stage, generations[stage]),
                payload=job_payload(interaction_id, generations[stage]),
                pipeline_version=head.pipeline_version,
                ontology_version_id=head.ontology_version_id,
            )
        new = self.pipeline.finalize(statuses)
        if new != head.processing_status:
            await conn.execute(
                text("UPDATE interactions SET processing_status = :s, status_changed_at = now() WHERE workspace_id = :ws AND id = :id"),
                {"s": new, "ws": workspace_id, "id": interaction_id},
            )
            metrics.interactions_status_total.labels(status=new).inc()
        return new
