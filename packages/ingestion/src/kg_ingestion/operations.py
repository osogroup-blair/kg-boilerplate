"""Operator actions on the ledger and the outbox: replay a stage (ING-05), retry or cancel a job (ADM-06). Every action is audited with a reason code
in the SAME transaction as the change. Shared by the API routers and the CLI so both behave identically."""

from __future__ import annotations

import uuid

from kg_common.errors import Conflict, NoActiveOntology, NotFound, ValidationFailed
from kg_domain.principal import WorkspaceContext
from kg_governance.audit import AuditMetadata, AuditWriter, PipelineStage
from kg_jobs import reads as job_reads
from kg_jobs.outbox import enqueue
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from kg_ingestion.models import JobActionOut, ReplayedStage, ReplayOut
from kg_ingestion.normalize import TERMINAL_OK, lock_stage
from kg_ingestion.pipeline import DEAD, PENDING, PIPELINE_V1, REPLAYABLE, RUNNING, SKIPPED_DISABLED, Orchestrator, Pipeline, dedupe_key


class StageRunning(Conflict):
    code, default_message = "stage_running", "The stage is pending or running; wait for it to finish, or cancel its job"


class NothingToReplay(Conflict):
    code, default_message = "nothing_to_replay", "No stage of this event is dead or failed"


class JobNotDead(Conflict):
    code, default_message = "job_not_dead", "Only a dead job can be retried"


class JobNotCancellable(Conflict):
    code, default_message = "job_not_cancellable", "Only a pending or dead job can be cancelled"


class AlreadyReplayed(Conflict):
    code, default_message = "already_replayed", "A newer attempt of this stage already exists"


def _stage_enum(name: str) -> PipelineStage | None:
    try:
        return PipelineStage(name)
    except ValueError:
        return None


class OperationsService:
    def __init__(self, pipeline: Pipeline = PIPELINE_V1):
        self._pipeline = pipeline
        self._orchestrator = Orchestrator(pipeline)

    def _enabled(self, stage: str) -> bool:
        try:
            return self._pipeline.stage(stage).enabled
        except KeyError:
            return False

    # ---- replay (ING-05) --------------------------------------------------------------------------------------------------------------------

    async def replay_interaction(
        self,
        conn: AsyncConnection,
        ctx: WorkspaceContext,
        audit: AuditWriter,
        interaction_id: uuid.UUID,
        *,
        stage: str | None,
        reason_code: str,
        repin: bool = False,
        upgrade_pipeline: bool = False,
    ) -> ReplayOut:
        """`upgrade_pipeline` (P5, audited) repins every stage of an older event that was `skipped_disabled` when it was accepted and is enabled now (e.g. the
        memory stage of an event accepted under p4.v1); it implies `repin`. `repin` (audited) moves the replayed stage to the CURRENT pipeline version and the CURRENT active ontology: how an event accepted before a stage
        existed, or while the workspace had no ontology, gets processed (P4). Without it a replay re-runs under the versions the stage was planned with."""
        ws = ctx.workspace_id
        head = (
            await conn.execute(
                text("SELECT id FROM interactions WHERE workspace_id = :ws AND id = :id FOR UPDATE"), {"ws": ws, "id": interaction_id}
            )
        ).first()
        if head is None:
            raise NotFound()
        stages = {
            r.stage: r
            for r in (
                await conn.execute(
                    text(
                        "SELECT stage, status, generation FROM interaction_stages WHERE workspace_id = :ws AND interaction_id = :id FOR UPDATE"
                    ),
                    {"ws": ws, "id": interaction_id},
                )
            ).all()
        }
        active_version: uuid.UUID | None = None
        repin = repin or upgrade_pipeline
        if repin:
            stage = stage or (None if upgrade_pipeline else "graph_ingest")
            active = (
                await conn.execute(text("SELECT active_version_id FROM workspace_ontology WHERE workspace_id = :ws"), {"ws": ws})
            ).first()
            if active is None or active.active_version_id is None:
                raise NoActiveOntology()
            active_version = active.active_version_id
        if upgrade_pipeline:
            targets = [s for s in stages.values() if s.status == SKIPPED_DISABLED and self._enabled(s.stage) and stage in (None, s.stage)]
            if not targets:
                raise NothingToReplay()
        elif stage is not None:
            if stage not in stages:
                raise ValidationFailed("unknown stage", details={"fields": ["stage"]})
            st = stages[stage]
            if st.status in (PENDING, RUNNING):
                raise StageRunning()
            if st.status == SKIPPED_DISABLED and not (repin and self._pipeline.stage(stage).enabled):
                raise ValidationFailed("the stage is disabled in this pipeline version", details={"fields": ["stage"]})
            targets = [st]
        else:
            targets = [s for s in stages.values() if s.status in REPLAYABLE]
            if not targets:
                raise NothingToReplay()
        replayed: list[tuple[str, int]] = []
        for st in targets:
            generation = st.generation + 1
            await conn.execute(
                text(
                    "UPDATE interaction_stages SET generation = :g, status = 'pending', attempts = 0, last_error_category = NULL, last_error_code = NULL, "
                    "result = '{}'::jsonb, started_at = NULL, finished_at = NULL WHERE workspace_id = :ws AND interaction_id = :i AND stage = :s"
                ),
                {"g": generation, "ws": ws, "i": interaction_id, "s": st.stage},
            )
            if repin and active_version is not None:
                await conn.execute(
                    text(
                        "UPDATE interaction_stages SET pipeline_version = :pv, ontology_version_id = :ov WHERE workspace_id = :ws AND interaction_id = :i AND stage = :s"
                    ),
                    {"pv": self._pipeline.version, "ov": active_version, "ws": ws, "i": interaction_id, "s": st.stage},
                )
                await audit.record(
                    "interaction.repinned",
                    resource_type="interaction",
                    resource_id=interaction_id,
                    metadata=AuditMetadata(
                        stage=_stage_enum(st.stage),
                        generation=generation,
                        reason_code=reason_code,
                        version_id=active_version,
                        interaction_id=interaction_id,
                    ),
                )
            if upgrade_pipeline:
                await audit.record(
                    "interaction.pipeline_upgraded",
                    resource_type="interaction",
                    resource_id=interaction_id,
                    metadata=AuditMetadata(
                        stage=_stage_enum(st.stage), generation=generation, reason_code=reason_code, interaction_id=interaction_id
                    ),
                )
            replayed.append((st.stage, generation))
            await audit.record(
                "interaction.replayed",
                resource_type="interaction",
                resource_id=interaction_id,
                metadata=AuditMetadata(stage=_stage_enum(st.stage), generation=generation, reason_code=reason_code),
            )
        status = await self._orchestrator.advance(
            conn, ws, interaction_id
        )  # enqueues the replayed stages that are ready (dedupe key carries the generation)
        out: list[ReplayedStage] = []
        for name, generation in replayed:
            job = (
                await conn.execute(
                    text("SELECT id FROM outbox_jobs WHERE workspace_id = :ws AND dedupe_key = :k"),
                    {"ws": ws, "k": dedupe_key(interaction_id, name, generation)},
                )
            ).first()
            out.append(ReplayedStage(stage=name, generation=generation, job_id=job.id if job else None))
        return ReplayOut(event_id=interaction_id, replayed=out, status=status)

    # ---- jobs (ADM-06) ----------------------------------------------------------------------------------------------------------------------

    async def retry_job(
        self, conn: AsyncConnection, ctx: WorkspaceContext, audit: AuditWriter, job_id: uuid.UUID, *, reason_code: str
    ) -> JobActionOut:
        """A dead job becomes a NEW job one generation higher; the dead one stays as history."""
        ws = ctx.workspace_id
        job = await job_reads.get_job(conn, ws, job_id)
        if job.status != "dead":
            raise JobNotDead()
        generation = job_reads.generation_of(job.payload) + 1
        if (
            job.subject_type == "interaction" and job.stage == "graph_projection"
        ):  # an interaction's restore job has no stage row: a new generation is a new job
            new_job_id = await enqueue(
                conn,
                workspace_id=ws,
                stage=job.stage,
                subject_type="interaction",
                subject_id=job.subject_id,
                dedupe_key=f"interaction:{job.subject_id}:graph_projection:g{generation}",
                payload={"interaction_id": str(job.subject_id), "generation": generation},
                pipeline_version=job.pipeline_version,
                ontology_version_id=job.ontology_version_id,
                concurrency_key=f"graph:{ws}",
                correlation_id=job.correlation_id,
            )
            if new_job_id is None:
                raise AlreadyReplayed()
        elif job.subject_type == "interaction":
            st = await lock_stage(conn, ws, job.subject_id, job.stage)
            if st is not None and st.generation >= generation:
                raise AlreadyReplayed()
            replay = await self.replay_interaction(conn, ctx, audit, job.subject_id, stage=job.stage, reason_code=reason_code)
            new = replay.replayed[0]
            new_job_id, generation = new.job_id, new.generation
        elif (
            job.subject_type == "memory"
        ):  # P5: a dead embed / expiry job becomes a new job one generation higher (payload carries ids only)
            new_job_id = await enqueue(
                conn,
                workspace_id=ws,
                stage=job.stage,
                subject_type="memory",
                subject_id=job.subject_id,
                dedupe_key=f"memory:{job.subject_id}:{job.stage}:retry{generation}",
                payload={**{k: v for k, v in (job.payload or {}).items() if k != "generation"}, "generation": generation},
                pipeline_version=job.pipeline_version,
                ontology_version_id=job.ontology_version_id,
                correlation_id=job.correlation_id,
            )
            if new_job_id is None:
                raise AlreadyReplayed()
        else:
            new_job_id = await enqueue(
                conn,
                workspace_id=ws,
                stage=job.stage,
                subject_type="command",
                subject_id=job.subject_id,
                dedupe_key=f"command:{job.subject_id}:{job.stage}:g{generation}",
                payload={**{k: v for k, v in (job.payload or {}).items() if k != "generation"}, "generation": generation},
                pipeline_version=job.pipeline_version,
                ontology_version_id=job.ontology_version_id,
                correlation_id=job.correlation_id,
            )
            if new_job_id is None:
                raise AlreadyReplayed()
        await audit.record(
            "job.retried",
            resource_type="outbox_job",
            resource_id=job_id,
            metadata=AuditMetadata(job_id=job_id, stage=_stage_enum(job.stage), generation=generation, reason_code=reason_code),
        )
        return JobActionOut(job_id=job_id, status="dead", new_job_id=new_job_id, generation=generation)

    async def cancel_job(
        self, conn: AsyncConnection, ctx: WorkspaceContext, audit: AuditWriter, job_id: uuid.UUID, *, reason_code: str
    ) -> JobActionOut:
        """Pending or dead -> cancelled. The stage becomes dead (`cancelled_by_operator`) and the interaction is finalised partial/failed."""
        ws = ctx.workspace_id
        job = await job_reads.get_job(conn, ws, job_id)
        if job.status not in ("pending", "dead"):
            raise JobNotCancellable()
        row = (await conn.execute(text("SELECT * FROM ops.cancel_job(:j)"), {"j": job_id})).first()
        if row is None:
            raise JobNotCancellable()  # it was claimed between the read and the cancel
        if row.subject_type == "interaction":
            await self._kill_stage(conn, ws, row.subject_id, row.stage, job_reads.generation_of(row.payload))
        else:
            await conn.execute(
                text(
                    "UPDATE commands SET projection_status = 'failed', last_error_code = 'cancelled_by_operator' "
                    "WHERE workspace_id = :ws AND id = :id AND projection_status = 'pending'"
                ),
                {"ws": ws, "id": row.subject_id},
            )
        await audit.record(
            "job.cancelled",
            resource_type="outbox_job",
            resource_id=job_id,
            metadata=AuditMetadata(
                job_id=job_id, stage=_stage_enum(job.stage), generation=job_reads.generation_of(job.payload), reason_code=reason_code
            ),
        )
        return JobActionOut(job_id=job_id, status="cancelled")

    async def _kill_stage(self, conn: AsyncConnection, ws: uuid.UUID, interaction_id: uuid.UUID, stage: str, generation: int) -> None:
        st = await lock_stage(conn, ws, interaction_id, stage)
        if st is None or st.generation != generation or st.status in TERMINAL_OK:
            return
        if st.status != DEAD:
            await conn.execute(
                text(
                    "UPDATE interaction_stages SET status = 'dead', finished_at = now(), last_error_category = 'config_terminal', "
                    "last_error_code = 'cancelled_by_operator' WHERE workspace_id = :ws AND interaction_id = :i AND stage = :s"
                ),
                {"ws": ws, "i": interaction_id, "s": stage},
            )
        await self._orchestrator.advance(conn, ws, interaction_id)
