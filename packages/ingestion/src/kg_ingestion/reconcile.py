"""`kg interactions reconcile`: finds work that the outbox lost track of and (with repair) re-enqueues it.

  missing_stage          an interaction lacks a stage row its pinned pipeline defines
  missing_job            a stage (or a command's projection) is waiting but no live job exists for its current generation
  terminal_job_open_stage  a job reached a terminal state yet its stage is still open (a lost result): repair bumps the generation
  orphan_job             a pending interaction job whose interaction no longer exists: repair cancels it

The outbox makes these rare (the job and the state change commit together); reconcile is the belt for the braces and the proof in the failure tests."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from kg_domain.principal import WorkspaceContext
from kg_jobs.outbox import enqueue
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from kg_ingestion.pipeline import FAILED, PENDING, PIPELINE_V1, RUNNING, Orchestrator, Pipeline, dedupe_key, job_payload
from kg_ingestion.store import insert_stage

MAX_FINDINGS = 500


@dataclass
class Finding:
    kind: str
    subject_type: str
    subject_id: uuid.UUID
    stage: str | None = None
    repaired: bool = False


@dataclass
class ReconcileReport:
    checked_interactions: int = 0
    findings: list[Finding] = field(default_factory=list)
    repaired: int = 0

    @property
    def clean(self) -> bool:
        return all(f.repaired for f in self.findings)


class Reconciler:
    def __init__(self, pipeline: Pipeline = PIPELINE_V1):
        self._pipeline = pipeline
        self._orchestrator = Orchestrator(pipeline)

    async def run(self, conn: AsyncConnection, ctx: WorkspaceContext, *, repair: bool) -> ReconcileReport:
        report = ReconcileReport()
        ws = ctx.workspace_id
        interactions = (
            await conn.execute(
                text(
                    "SELECT id, pipeline_version, ontology_version_id, consent_external FROM interactions "
                    "WHERE workspace_id = :ws AND processing_status IN ('accepted','processing') ORDER BY recorded_at"
                ),
                {"ws": ws},
            )
        ).all()
        report.checked_interactions = len(interactions)
        for i in interactions:
            await self._interaction(conn, ws, i, report, repair)
        await self._commands(conn, ws, report, repair)
        await self._orphans(conn, ws, report, repair)
        report.findings = report.findings[:MAX_FINDINGS]
        return report

    async def _interaction(self, conn: AsyncConnection, ws: uuid.UUID, i, report: ReconcileReport, repair: bool) -> None:  # type: ignore[no-untyped-def]
        stages = {
            s.stage: s
            for s in (
                await conn.execute(
                    text(
                        "SELECT stage, status, generation FROM interaction_stages WHERE workspace_id = :ws AND interaction_id = :i FOR UPDATE"
                    ),
                    {"ws": ws, "i": i.id},
                )
            ).all()
        }
        touched = False
        plan = self._pipeline.initial_statuses(consent_external=i.consent_external)
        for name in self._pipeline.names:
            if name not in stages:
                f = Finding("missing_stage", "interaction", i.id, name)
                report.findings.append(f)
                if repair:
                    await insert_stage(conn, ws, i.id, name, plan[name], i.pipeline_version, i.ontology_version_id)
                    f.repaired, touched = True, True
                    report.repaired += 1
        if touched:
            stages = {
                s.stage: s
                for s in (
                    await conn.execute(
                        text("SELECT stage, status, generation FROM interaction_stages WHERE workspace_id = :ws AND interaction_id = :i"),
                        {"ws": ws, "i": i.id},
                    )
                ).all()
            }
        statuses = {n: s.status for n, s in stages.items()}
        ready = set(self._pipeline.ready(statuses))
        for name, st in stages.items():
            if not (st.status in (RUNNING, FAILED) or (st.status == PENDING and name in ready)):
                continue
            jobs = (
                await conn.execute(
                    text(
                        "SELECT status FROM outbox_jobs WHERE workspace_id = :ws AND subject_type = 'interaction' AND subject_id = :i AND stage = :s "
                        "AND dedupe_key = :k"
                    ),
                    {"ws": ws, "i": i.id, "s": name, "k": dedupe_key(i.id, name, st.generation)},
                )
            ).first()
            if jobs is not None and jobs.status in ("pending", "leased"):
                continue
            kind = "missing_job" if jobs is None else "terminal_job_open_stage"
            f = Finding(kind, "interaction", i.id, name)
            report.findings.append(f)
            if not repair:
                continue
            generation = st.generation
            if jobs is not None:  # the old key is taken by a finished job: a new generation is the only way to run it again
                generation += 1
                await conn.execute(
                    text(
                        "UPDATE interaction_stages SET generation = :g, status = 'pending', attempts = 0, last_error_category = NULL, last_error_code = NULL, "
                        "started_at = NULL, finished_at = NULL WHERE workspace_id = :ws AND interaction_id = :i AND stage = :s"
                    ),
                    {"g": generation, "ws": ws, "i": i.id, "s": name},
                )
            await enqueue(
                conn,
                workspace_id=ws,
                stage=name,
                subject_type="interaction",
                subject_id=i.id,
                dedupe_key=dedupe_key(i.id, name, generation),
                payload=job_payload(i.id, generation),
                pipeline_version=i.pipeline_version,
                ontology_version_id=i.ontology_version_id,
            )
            f.repaired = True
            report.repaired += 1
        if repair and touched:
            await self._orchestrator.advance(conn, ws, i.id)

    async def _commands(self, conn: AsyncConnection, ws: uuid.UUID, report: ReconcileReport, repair: bool) -> None:
        rows = (
            await conn.execute(
                text(
                    "SELECT c.id, c.ontology_version_id, "
                    "(SELECT COALESCE(max((j.payload->>'generation')::int), -1) FROM outbox_jobs j WHERE j.workspace_id = c.workspace_id "
                    " AND j.subject_type = 'command' AND j.subject_id = c.id AND j.stage = 'graph_projection') AS last_generation "
                    "FROM commands c WHERE c.workspace_id = :ws AND c.status = 'applied' AND c.projection_status IN ('pending','failed') "
                    "AND NOT EXISTS (SELECT 1 FROM outbox_jobs j WHERE j.workspace_id = c.workspace_id AND j.subject_type = 'command' "
                    " AND j.subject_id = c.id AND j.stage = 'graph_projection' AND j.status IN ('pending','leased')) ORDER BY c.created_at"
                ),
                {"ws": ws},
            )
        ).all()
        for c in rows:
            f = Finding("missing_job", "command", c.id, "graph_projection")
            report.findings.append(f)
            if not repair:
                continue
            generation = c.last_generation + 1
            await enqueue(
                conn,
                workspace_id=ws,
                stage="graph_projection",
                subject_type="command",
                subject_id=c.id,
                dedupe_key=f"command:{c.id}:graph_projection:g{generation}",
                payload={"command_id": str(c.id), "generation": generation},
                pipeline_version=self._pipeline.version,
                ontology_version_id=c.ontology_version_id,
            )
            f.repaired = True
            report.repaired += 1

    async def _orphans(self, conn: AsyncConnection, ws: uuid.UUID, report: ReconcileReport, repair: bool) -> None:
        rows = (
            await conn.execute(
                text(
                    "SELECT j.id, j.subject_id, j.stage FROM outbox_jobs j WHERE j.workspace_id = :ws AND j.subject_type = 'interaction' "
                    "AND j.status = 'pending' AND NOT EXISTS (SELECT 1 FROM interactions i WHERE i.workspace_id = j.workspace_id AND i.id = j.subject_id)"
                ),
                {"ws": ws},
            )
        ).all()
        for j in rows:
            f = Finding("orphan_job", "interaction", j.subject_id, j.stage)
            report.findings.append(f)
            if repair:
                await conn.execute(text("SELECT * FROM ops.cancel_job(:j)"), {"j": j.id})
                f.repaired = True
                report.repaired += 1
