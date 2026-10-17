"""Ontology simulation (ONT-05, AT-17 groundwork; phase-8 plan §5.5, ADR-046): see what a DRAFT ontology would extract from real interactions BEFORE it is published.

A run reads up to 20 sampled interactions, extracts them under the draft's compiled types in an ISOLATED STAGING NAMESPACE of the graph (`ws-<uuid>-sim-<run>`, built only from
the workspace's context and the run id), and reconciles the result into a REPORT: facts by type, types the active version does not have, relationship types the draft would
drop, identity outcomes, and the diff against the active version with its impact counts. Nothing is written to the workspace's real tables or group; the staging namespace is
removed when the run ends, or by a delayed job if the run dies. The report holds counts and type keys, never a name or a text.

Preview first: the request returns the token and cost estimate and the interactions that would be skipped (a `local_only` sample, or one whose consent is withheld, is never
sent). `confirm=true` is what starts the run, and a `budget_usd` below the estimate refuses it."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import uuid
from typing import Any

from kg_common.errors import Conflict, NotFound, ValidationFailed
from kg_common.ids import uuid7
from kg_common.settings import Settings
from kg_common.versions import PIPELINE_VERSION
from kg_db.session import tenant_session
from kg_domain.principal import Principal, WorkspaceContext
from kg_governance.audit import AuditMetadata, AuditSink, SystemAudit
from kg_graph.compile import compiled_for
from kg_graph.service import GraphService
from kg_ingestion.crypto import PayloadCipher
from kg_ingestion.store import read_content
from kg_jobs.client import ClaimedJob
from kg_jobs.errors import Category, JobError
from kg_jobs.outbox import enqueue
from kg_jobs.policy import Decision
from kg_knowledge.ontology_service import OntologyService
from kg_knowledge.specs import SpecCache
from kg_modelgw.config import ModelConfig
from kg_modelgw.egress import egress
from kg_obs import metrics
from kg_obs.logging import get_logger
from kg_ontology import OntologySpec, validate_document
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from kg_extraction.errors import job_error_for
from kg_extraction.handler import EPISODE_SOURCE, episode_body

log = get_logger("kg.extraction.simulation")
STAGE_RUN, STAGE_CLEANUP = "ontology_simulation", "simulation_cleanup"
MAX_SAMPLE = 20
CALLS_PER_EPISODE = 4  # entities, edges, summaries, resolutions: the extraction's own shape
OVERHEAD_PROMPT_TOKENS, ANSWER_TOKENS = 700, 300
SIM_NS = uuid.UUID("0c6a3a86-5a5b-4c43-8b93-3f4c8f6e3d11")


class SampleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n: int = Field(default=5, ge=1, le=MAX_SAMPLE)
    since: dt.datetime | None = None


class SimulateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interaction_ids: list[uuid.UUID] | None = Field(default=None, max_length=MAX_SAMPLE)
    sample: SampleIn | None = None
    budget_usd: float | None = Field(default=None, gt=0.0, le=1000.0)
    confirm: bool = False
    simulation_id: uuid.UUID | None = None


class SimulationOut(BaseModel):
    id: uuid.UUID
    draft_version_id: uuid.UUID
    status: str
    sample_count: int
    runnable_count: int
    skipped: dict[str, int] = {}
    cost_estimate: float | None = None
    cost_actual: float | None = None
    estimate: dict[str, int] = {}
    report: dict[str, Any] = {}
    created_at: dt.datetime
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    status_url: str


class SimulationRefused(Conflict):
    code, default_message = "simulation_refused", "The simulation cannot run"


class SimulationService:
    def __init__(
        self,
        engine: AsyncEngine,
        graph: GraphService,
        specs: SpecCache,
        ontology: OntologyService,
        cipher: PayloadCipher,
        config: ModelConfig,
        settings: Settings,
        *,
        flush_calls: Any = None,
        service: str = "kg-worker",
    ):
        self._engine, self._graph, self._specs, self._ontology, self._cipher = engine, graph, specs, ontology, cipher
        self._config, self._settings, self._flush, self._service = config, settings, flush_calls, service

    # ---- request side: preview, then confirm ----------------------------------------------------------------------------------------------

    async def _draft(self, conn: AsyncConnection, ws: uuid.UUID, draft_id: uuid.UUID) -> OntologySpec:
        row = (
            await conn.execute(
                text("SELECT spec, state FROM ontology_versions WHERE workspace_id = :ws AND id = :id"), {"ws": ws, "id": draft_id}
            )
        ).first()
        if row is None:
            raise NotFound()
        if row.state != "draft":
            raise SimulationRefused("Only a draft can be simulated; a published version is already in use")
        spec, report = validate_document(row.spec)
        if spec is None or report.errors:
            raise ValidationFailed("The draft has validation errors; fix them first", details={"errors": len(report.errors)})
        return spec

    async def _select(self, conn: AsyncConnection, ws: uuid.UUID, body: SimulateIn) -> list[Any]:
        if body.interaction_ids:
            rows = (
                await conn.execute(
                    text(
                        "SELECT id, classification, consent_external, token_estimate, kind FROM interactions WHERE workspace_id = :ws AND id = ANY(:ids) "
                        "AND processing_status <> 'deletion_pending' ORDER BY recorded_at, id"
                    ),
                    {"ws": ws, "ids": list(dict.fromkeys(body.interaction_ids))},
                )
            ).all()
            if len(rows) != len(set(body.interaction_ids)):
                raise NotFound()  # a missing, foreign or frozen interaction is one answer
            return list(rows)
        sample = body.sample or SampleIn()
        return list(
            (
                await conn.execute(
                    text(
                        "SELECT id, classification, consent_external, token_estimate, kind FROM interactions WHERE workspace_id = :ws "
                        "AND processing_status IN ('indexed','partial') AND (CAST(:since AS timestamptz) IS NULL OR recorded_at >= :since) "
                        "ORDER BY recorded_at DESC, id DESC LIMIT :n"
                    ),
                    {"ws": ws, "since": sample.since, "n": sample.n},
                )
            ).all()
        )

    def _estimate(self, runnable: list[Any]) -> tuple[dict[str, int], float | None]:
        prompt = sum(CALLS_PER_EPISODE * (int(r.token_estimate or 0) + OVERHEAD_PROMPT_TOKENS) for r in runnable)
        answer = len(runnable) * CALLS_PER_EPISODE * ANSWER_TOKENS
        alias = self._config.purposes["ontology_simulation"].alias
        return {
            "prompt_tokens": prompt,
            "completion_tokens": answer,
            "calls": len(runnable) * CALLS_PER_EPISODE,
        }, self._config.estimate_cost(alias, prompt, answer)

    async def request(
        self, conn: AsyncConnection, ctx: WorkspaceContext, principal: Principal, draft_id: uuid.UUID, body: SimulateIn, audit: AuditSink
    ) -> SimulationOut:
        ws = ctx.workspace_id
        await self._draft(conn, ws, draft_id)
        if body.simulation_id is not None:  # confirming a preview
            sim = (
                await conn.execute(
                    text(
                        "SELECT id, status, sample_ids FROM ontology_simulations WHERE workspace_id = :ws AND id = :id AND draft_version_id = :d FOR UPDATE"
                    ),
                    {"ws": ws, "id": body.simulation_id, "d": draft_id},
                )
            ).first()
            if sim is None:
                raise NotFound()
            if not body.confirm:
                return await self.get(conn, ws, sim.id)
            if sim.status != "previewed":
                raise SimulationRefused("This simulation was already started")
            return await self._start(conn, ws, sim.id, body.budget_usd, audit)
        rows = await self._select(conn, ws, body)
        runnable = [r for r in rows if self._config.egress_allows(r.classification) and r.consent_external]
        skipped = len(rows) - len(runnable)
        if not rows:
            raise ValidationFailed("There is nothing to sample: no interaction has been processed yet", details={"fields": ["sample"]})
        est, cost = self._estimate(runnable)
        sim_id = uuid7()
        await conn.execute(
            text(
                "INSERT INTO ontology_simulations (id, workspace_id, draft_version_id, status, sample_ids, report, cost_estimate, requested_by) "
                "VALUES (:id, :ws, :d, 'previewed', :ids, CAST(:rep AS jsonb), :cost, :by)"
            ),
            {
                "id": sim_id,
                "ws": ws,
                "d": draft_id,
                "ids": [r.id for r in runnable],
                "rep": json.dumps({"sample_count": len(rows), "skipped_policy": skipped, "estimate": est}),
                "cost": cost,
                "by": principal.principal_id,
            },
        )
        if body.confirm:
            return await self._start(conn, ws, sim_id, body.budget_usd, audit)
        return await self.get(conn, ws, sim_id)

    async def _start(
        self, conn: AsyncConnection, ws: uuid.UUID, sim_id: uuid.UUID, budget: float | None, audit: AuditSink
    ) -> SimulationOut:
        row = (
            await conn.execute(
                text("SELECT cost_estimate, sample_ids FROM ontology_simulations WHERE workspace_id = :ws AND id = :id"),
                {"ws": ws, "id": sim_id},
            )
        ).one()
        if not row.sample_ids:
            raise SimulationRefused("Every sampled interaction would be skipped by policy; nothing would be sent to a model")
        if budget is not None and row.cost_estimate is not None and float(row.cost_estimate) > budget:
            raise SimulationRefused(
                "The estimate is above the budget", details={"estimate_usd": float(row.cost_estimate), "budget_usd": budget}
            )
        group = f"ws-{ws}-sim-{sim_id.hex[:12]}"
        await conn.execute(
            text(
                "UPDATE ontology_simulations SET status = 'running', staging_group = :g, started_at = now() WHERE workspace_id = :ws AND id = :id"
            ),
            {"g": group, "ws": ws, "id": sim_id},
        )
        for stage, delay in ((STAGE_RUN, 0.0), (STAGE_CLEANUP, self._settings.simulation_cleanup_minutes * 60.0)):
            await enqueue(
                conn,
                workspace_id=ws,
                stage=stage,
                subject_type="command",
                subject_id=sim_id,
                dedupe_key=f"simulation:{sim_id}:{stage}",
                payload={"simulation_id": str(sim_id), "generation": 0},
                pipeline_version=PIPELINE_VERSION,
                delay_seconds=delay,
            )
        await audit.record(
            "ontology.simulated",
            resource_type="ontology_simulation",
            resource_id=sim_id,
            metadata=AuditMetadata(simulation_id=sim_id, count=len(row.sample_ids)),
        )
        metrics.simulations_total.labels(outcome="started").inc()
        return await self.get(conn, ws, sim_id)

    async def get(self, conn: AsyncConnection, ws: uuid.UUID, sim_id: uuid.UUID) -> SimulationOut:
        r = (
            await conn.execute(text("SELECT * FROM ontology_simulations WHERE workspace_id = :ws AND id = :id"), {"ws": ws, "id": sim_id})
        ).first()
        if r is None:
            raise NotFound()
        rep = dict(r.report or {})
        return SimulationOut(
            id=r.id,
            draft_version_id=r.draft_version_id,
            status=r.status,
            sample_count=int(rep.get("sample_count", len(r.sample_ids))),
            runnable_count=len(r.sample_ids),
            skipped={"policy": int(rep.get("skipped_policy", 0))},
            cost_estimate=float(r.cost_estimate) if r.cost_estimate is not None else None,
            cost_actual=float(r.cost_actual) if r.cost_actual is not None else None,
            estimate=rep.get("estimate", {}),
            report={k: v for k, v in rep.items() if k not in ("estimate", "sample_count", "skipped_policy")},
            created_at=r.created_at,
            started_at=r.started_at,
            finished_at=r.finished_at,
            status_url=f"/v1/ontologies/simulations/{r.id}",
        )

    # ---- the run (a job) ----------------------------------------------------------------------------------------------------------------

    async def run(self, ctx: WorkspaceContext, sim_id: uuid.UUID, *, attempts: int = 1) -> None:
        ws = ctx.workspace_id
        async with tenant_session(self._engine, ctx) as conn:
            sim = (
                await conn.execute(
                    text("SELECT * FROM ontology_simulations WHERE workspace_id = :ws AND id = :id FOR UPDATE"), {"ws": ws, "id": sim_id}
                )
            ).first()
            if sim is None:
                raise JobError(Category.CONFIG_TERMINAL, "simulation_missing")
            if sim.status in ("completed", "cleaned"):
                return
            draft = (
                await conn.execute(
                    text("SELECT spec FROM ontology_versions WHERE workspace_id = :ws AND id = :id"), {"ws": ws, "id": sim.draft_version_id}
                )
            ).first()
            if draft is None:
                raise JobError(Category.CONFIG_TERMINAL, "draft_missing")
            spec, _ = validate_document(draft.spec)
            if spec is None:
                raise JobError(Category.CONFIG_TERMINAL, "draft_invalid")
            active = await self._specs.active(conn, ws)
            diff = await self._ontology.diff(conn, ctx, sim.draft_version_id, "active")
            names = {
                n.casefold()
                for r in (
                    await conn.execute(
                        text("SELECT display_name, aliases FROM entities WHERE workspace_id = :ws AND status = 'active'"), {"ws": ws}
                    )
                ).all()
                for n in [r.display_name, *list(r.aliases or [])]
            }
            dropped_candidates = {
                r.rel_type: int(r.n)
                for r in (
                    await conn.execute(
                        text(
                            "SELECT rel_type, count(*) AS n FROM facts WHERE workspace_id = :ws AND source_interaction_id = ANY(:ids) AND status NOT IN ('deleted','deletion_pending') GROUP BY 1"
                        ),
                        {"ws": ws, "ids": list(sim.sample_ids)},
                    )
                ).all()
            }
            inputs = []
            for iid in sim.sample_ids:
                head = (
                    await conn.execute(
                        text(
                            "SELECT kind, classification, consent_external, occurred_at, recorded_at, source_system FROM interactions WHERE workspace_id = :ws AND id = :id AND processing_status <> 'deletion_pending'"
                        ),
                        {"ws": ws, "id": iid},
                    )
                ).first()
                sealed = await read_content(conn, ws, iid, "normalized") if head else None
                if head is None or sealed is None:
                    continue
                inputs.append((iid, head, self._cipher.decrypt(sealed, workspace_id=ws, interaction_id=iid, form="normalized").decode()))
        compiled = compiled_for(spec)
        staging = self._graph.staging(ctx, sim_id.hex[:12])
        facts_by_type: dict[str, int] = {}
        nodes_by_type: dict[str, int] = {}
        matched = new = 0
        episodes = 0
        try:
            for iid, head, normalized in inputs:
                if not (self._config.egress_allows(head.classification) and head.consent_external):
                    continue
                euid = str(uuid.uuid5(SIM_NS, f"{sim_id}:{iid}"))
                reference = head.occurred_at or head.recorded_at
                await staging.ensure_episode_node(
                    uuid=euid,
                    name=str(iid),
                    body=episode_body(head.kind, normalized),
                    source=EPISODE_SOURCE[head.kind],
                    source_description="simulation",
                    valid_at=reference,
                    created_at=head.recorded_at,
                )
                try:
                    with egress(
                        classification=head.classification,
                        purpose="ontology_simulation",
                        workspace_id=str(ws),
                        correlation_id=f"sim-{sim_id}-{iid}",
                        service=self._service,
                    ):
                        async with asyncio.timeout(self._settings.graph_ingest_timeout_seconds):
                            result = await staging.add_episode_scoped(
                                uuid=euid,
                                name=str(iid),
                                body=episode_body(head.kind, normalized),
                                source_description="simulation",
                                reference_time=reference,
                                source=EPISODE_SOURCE[head.kind],
                                compiled=compiled,
                            )
                except Exception as exc:
                    if type(exc).__name__ == "CancelledError":
                        raise
                    raise job_error_for(exc, attempt=attempts) from exc
                episodes += 1
                for e in result.edges:
                    facts_by_type[e.name] = facts_by_type.get(e.name, 0) + 1
                for n in result.nodes:
                    label = next(iter(sorted(n.labels)), "Entity")
                    nodes_by_type[label] = nodes_by_type.get(label, 0) + 1
                    if n.name.casefold() in names:
                        matched += 1
                    else:
                        new += 1
            if self._flush is not None:
                await self._flush()
            active_rels = set(active[1].relationship_types) if active else set()
            active_ents = set(active[1].entity_types) if active else set()
            report = {
                "episodes": episodes,
                "facts_by_type": dict(sorted(facts_by_type.items())),
                "nodes_by_type": dict(sorted(nodes_by_type.items())),
                "new_relationship_types": sorted(set(facts_by_type) - active_rels),
                "new_entity_types": sorted(set(nodes_by_type) - active_ents - {"Entity"}),
                "dropped_by_draft": {k: v for k, v in sorted(dropped_candidates.items()) if k not in spec.relationship_types},
                "identity": {"matched_existing": matched, "new": new},
                "diff": {
                    "changes": len(diff.changes),
                    "breaking": diff.breaking_count,
                    "impact": [i.model_dump(mode="json") for i in diff.impact],
                },
            }
            # the scratch graph is gone BEFORE the report says "completed" (a reader of the report can rely on it)
            await staging.clear_group()
            async with tenant_session(self._engine, ctx) as conn:
                actual = (
                    await conn.execute(
                        text("SELECT sum(est_cost_usd) FROM model_calls WHERE workspace_id = :ws AND correlation_id LIKE :c"),
                        {"ws": ws, "c": f"sim-{sim_id}-%"},
                    )
                ).scalar_one()
                prior = (
                    await conn.execute(
                        text("SELECT report FROM ontology_simulations WHERE workspace_id = :ws AND id = :id"), {"ws": ws, "id": sim_id}
                    )
                ).scalar_one()
                merged = {**(prior or {}), **report}
                await conn.execute(
                    text(
                        "UPDATE ontology_simulations SET status = 'completed', report = CAST(:r AS jsonb), cost_actual = :a, finished_at = now() WHERE workspace_id = :ws AND id = :id"
                    ),
                    {"r": json.dumps(merged), "a": actual, "ws": ws, "id": sim_id},
                )
                await SystemAudit(conn, ws).record(
                    "ontology.simulation_completed",
                    resource_type="ontology_simulation",
                    resource_id=sim_id,
                    metadata=AuditMetadata(simulation_id=sim_id, count=episodes),
                )
            metrics.simulations_total.labels(outcome="completed").inc()
        finally:
            await self.cleanup(ctx, sim_id)

    async def cleanup(self, ctx: WorkspaceContext, sim_id: uuid.UUID) -> bool:
        """Removes the staging namespace (idempotent). Called at the end of every run and by the delayed job that backs it up."""
        ws = ctx.workspace_id
        async with tenant_session(self._engine, ctx) as conn:
            row = (
                await conn.execute(
                    text("SELECT status, cleaned_at FROM ontology_simulations WHERE workspace_id = :ws AND id = :id FOR UPDATE"),
                    {"ws": ws, "id": sim_id},
                )
            ).first()
            if row is None or row.cleaned_at is not None:
                return False
        await self._graph.staging(ctx, sim_id.hex[:12]).clear_group()
        async with tenant_session(self._engine, ctx) as conn:
            await conn.execute(
                text(
                    "UPDATE ontology_simulations SET cleaned_at = now(), status = CASE WHEN status = 'running' THEN 'failed' ELSE status END, "
                    "finished_at = COALESCE(finished_at, now()) WHERE workspace_id = :ws AND id = :id"
                ),
                {"ws": ws, "id": sim_id},
            )
        return True


class SimulationJob:
    stage = STAGE_RUN

    def __init__(self, service: SimulationService):
        self._service = service

    async def handle(self, job: ClaimedJob) -> None:
        ctx = WorkspaceContext.for_system_task(job.workspace_id, reason="ontology simulation")
        await self._service.run(ctx, uuid.UUID(str(job.payload["simulation_id"])), attempts=job.attempts)

    async def on_failure(self, job: ClaimedJob, decision: Decision) -> None:
        if decision.outcome in ("dead", "skipped"):
            ctx = WorkspaceContext.for_system_task(job.workspace_id, reason="ontology simulation failure")
            await self._service.cleanup(ctx, uuid.UUID(str(job.payload["simulation_id"])))
            metrics.simulations_total.labels(outcome="failed").inc()


class SimulationCleanupJob:
    stage = STAGE_CLEANUP

    def __init__(self, service: SimulationService):
        self._service = service

    async def handle(self, job: ClaimedJob) -> None:
        ctx = WorkspaceContext.for_system_task(job.workspace_id, reason="simulation cleanup")
        await self._service.cleanup(ctx, uuid.UUID(str(job.payload["simulation_id"])))
