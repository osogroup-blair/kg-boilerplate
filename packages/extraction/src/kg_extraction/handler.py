"""The `graph_ingest` stage (P4 §5.1): run Graphiti on an interaction's normalised content, then reconcile its output into platform truth.

Handler contract (ADR-025): tx 1 marks the stage running; the work happens under the workspace's graph lock; tx 2 writes the reconciliation, the stage result and
advances the pipeline in ONE transaction; the dispatcher then acks. A crash anywhere in between re-runs the job:

* after Graphiti saved but before tx 2 committed, the episode already has entities/edges, so the re-run SKIPS THE MODEL and reconciles from the graph (EX-04,
  GR-09: the cost of an episode is paid at most once per generation);
* after tx 2, the stage is terminal at this generation and the re-run only acks.

Nothing a model says reaches a read path unchecked: the projector restores every touched node and edge from Postgres, inline while the lock is held and, for
a crash in between, in the `graph_projection` job that tx 2 enqueued.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from kg_common.ids import episode_uuid
from kg_common.settings import Settings
from kg_db.session import tenant_session
from kg_domain.principal import WorkspaceContext
from kg_governance.audit import AuditMetadata, PipelineStage, SystemAudit
from kg_graph.compile import compiled_for
from kg_graph.service import GraphService
from kg_ingestion.crypto import PayloadCipher
from kg_ingestion.normalize import TERMINAL_OK, lock_stage, stage_unavailable
from kg_ingestion.pipeline import DEAD, FAILED, SKIPPED_POLICY, SUCCEEDED, Orchestrator
from kg_ingestion.store import read_content
from kg_jobs import faults
from kg_jobs.client import ClaimedJob
from kg_jobs.errors import Category, JobError
from kg_jobs.outbox import enqueue
from kg_jobs.policy import Decision
from kg_knowledge.extracted import EpisodeSource, ExtractedKnowledge
from kg_knowledge.graphlock import GraphBusy, GraphLock
from kg_knowledge.projection import Projector
from kg_knowledge.specs import SpecCache
from kg_modelgw.config import ModelConfig
from kg_modelgw.egress import egress
from kg_obs import metrics
from kg_obs.logging import get_logger
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from kg_extraction.errors import job_error_for
from kg_extraction.reconcile import Inputs, Reconciler, ReconcileSummary

log = get_logger("kg.extraction.graph_ingest")
STAGE = "graph_ingest"
EPISODE_SOURCE = {"message": "message", "conversation": "message", "observation": "text", "document": "text", "json": "json"}
_TIMESTAMP_PREFIX = re.compile(r"^\[[^\]\n]{1,40}\] ", re.MULTILINE)
RESULT_ID_CAP = 200


class GraphIngestResult(BaseModel):
    """The closed shape of `interaction_stages.result` for this stage: ids, counts and codes, never text (EVD-03)."""

    model_config = ConfigDict(extra="forbid")
    episode_uuid: str | None = None
    ontology_version_id: uuid.UUID | None = None
    reused: bool = False
    code: str | None = None
    entities_new: int = 0
    entities_linked: int = 0
    entities_forked: int = 0
    name_variants: int = 0
    facts: dict[str, int] = Field(default_factory=dict)
    evidence_added: int = 0
    reviews: int = 0
    reverts: int = 0
    claims: int = 0
    skipped_edges: int = 0
    chat_calls: int = 0
    embedding_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    entity_ids: list[uuid.UUID] = Field(default_factory=list)
    fact_ids: list[uuid.UUID] = Field(default_factory=list)
    review_ids: list[uuid.UUID] = Field(default_factory=list)
    revert_fact_ids: list[uuid.UUID] = Field(
        default_factory=list
    )  # rows the model changed that the platform put back: the restore scope reads these
    revert_entity_ids: list[uuid.UUID] = Field(default_factory=list)


def episode_body(kind: str, normalized: str) -> str:
    """Graphiti's message episodes expect `speaker: text` lines; the normaliser's `[time] speaker: text` prefix is dropped (the time is the reference time)."""
    return _TIMESTAMP_PREFIX.sub("", normalized) if kind == "conversation" else normalized


class GraphIngestJob:
    stage = STAGE

    def __init__(
        self,
        engine: AsyncEngine,
        cipher: PayloadCipher,
        graph: GraphService,
        projector: Projector,
        lock: GraphLock,
        specs: SpecCache,
        extracted: ExtractedKnowledge,
        config: ModelConfig,
        settings: Settings,
        *,
        flush_calls: Callable[[], Awaitable[None]],
        orchestrator: Orchestrator | None = None,
        service: str = "kg-worker",
    ):
        self._engine, self._cipher, self._graph, self._projector, self._lock = engine, cipher, graph, projector, lock
        self._specs, self._config, self._settings, self._flush, self._service = specs, config, settings, flush_calls, service
        self._orchestrator = orchestrator or Orchestrator()
        self._reconciler = Reconciler(extracted, specs, trgm_min=settings.identity_trgm_min, cos_min=settings.identity_cos_min)

    # ---- the job --------------------------------------------------------------------------------------------------------------------------

    async def handle(self, job: ClaimedJob) -> None:
        started, outcome = time.perf_counter(), "error"
        try:
            outcome = await self._handle(job)
        except GraphBusy as exc:
            outcome = "busy"
            raise JobError(Category.RETRYABLE, "graph_busy") from exc
        finally:
            metrics.graph_ingest_total.labels(outcome=outcome).inc()
            metrics.graph_ingest_duration_seconds.observe(time.perf_counter() - started)

    async def _handle(self, job: ClaimedJob) -> str:
        ws, iid, gen = job.workspace_id, job.subject_id, job.generation
        ctx = WorkspaceContext.for_system_task(ws, reason="graph_ingest stage")
        async with tenant_session(self._engine, ctx) as conn:
            st = await lock_stage(conn, ws, iid, STAGE)
            if st is None:
                raise await stage_unavailable(conn, ws, iid)
            if st.generation > gen:
                return "stale"  # a replay superseded this job
            if st.generation < gen:
                raise JobError(Category.CONFIG_TERMINAL, "generation_mismatch")
            if st.status == DEAD:
                raise JobError(Category.CONFIG_TERMINAL, st.last_error_code or "stage_dead")
            if st.status in TERMINAL_OK:
                return "noop"  # already done at this generation: the ack was lost, the result stands
            head = (
                await conn.execute(
                    text(
                        "SELECT i.kind, i.classification, i.consent_external, i.occurred_at, i.recorded_at, i.source_system, i.source_ref, i.submitted_by, "
                        "i.ontology_version_id, p.kind AS submitter_kind, (p.status = 'revoked' AND p.revoke_reason = 'compromised') AS submitter_compromised "
                        "FROM interactions i JOIN principals p ON p.workspace_id = i.workspace_id AND p.id = i.submitted_by "
                        "WHERE i.workspace_id = :ws AND i.id = :id"
                    ),
                    {"ws": ws, "id": iid},
                )
            ).first()
            if head is None:
                raise JobError(Category.CONFIG_TERMINAL, "interaction_missing")
            pinned = st.ontology_version_id or head.ontology_version_id
            skip = (
                "submitter_compromised"  # P7: a compromised principal's submission is not extracted, even by a job that was already claimed
                if head.submitter_compromised
                else "no_active_ontology"
                if pinned is None
                else "consent_denied"
                if not head.consent_external
                else "egress_denied"
                if not self._config.egress_allows(head.classification)
                else None
            )
            if skip is not None:  # ING-06: nothing leaves, no model is called
                await self._finish(
                    conn, ws, iid, status=SKIPPED_POLICY, code=skip, result=GraphIngestResult(code=skip, ontology_version_id=pinned)
                )
                return "skipped"
            sealed = await read_content(conn, ws, iid, "normalized")
            if sealed is None:
                raise JobError(Category.CONFIG_TERMINAL, "normalized_content_missing")
            try:
                spec = await self._specs.get(conn, ws, pinned)
            except Exception as exc:
                raise JobError(Category.CONFIG_TERMINAL, "ontology_unreadable") from exc
            subjects = (
                await conn.execute(
                    text(
                        "SELECT role, entity_id FROM interaction_subjects WHERE workspace_id = :ws AND interaction_id = :i AND entity_id IS NOT NULL"
                    ),
                    {"ws": ws, "i": iid},
                )
            ).all()
            await conn.execute(
                text(
                    "UPDATE interaction_stages SET status = 'running', attempts = :n, started_at = COALESCE(started_at, now()), finished_at = NULL "
                    "WHERE workspace_id = :ws AND interaction_id = :i AND stage = :s"
                ),
                {"n": job.attempts, "ws": ws, "i": iid, "s": STAGE},
            )

        normalized = self._cipher.decrypt(sealed, workspace_id=ws, interaction_id=iid, form="normalized").decode()
        episode = episode_uuid(iid, gen)
        correlation = f"gi-{iid}-g{gen}"
        src = EpisodeSource(
            interaction_id=iid,
            episode_uuid=episode,
            ontology_version_id=pinned,
            classification=head.classification,
            recorded_at=head.recorded_at,
            source_system=head.source_system,
            source_ref=head.source_ref,
            authority="agent_generated" if head.submitter_kind == "agent" else "conversation_report",
        )
        inputs = Inputs(
            subject_ids=[r.entity_id for r in subjects if r.role == "subject"],
            actor_ids=[r.entity_id for r in subjects if r.role == "actor"],
            submitter_id=head.submitted_by,
        )
        graph = self._graph.scoped(ctx)

        async with self._lock.hold(ws, wait_seconds=self._settings.graph_ingest_lock_wait_seconds):
            compiled = compiled_for(spec)
            reference = head.occurred_at or head.recorded_at
            try:
                await graph.ensure_episode_node(
                    uuid=episode,
                    name=str(iid),
                    body=episode_body(head.kind, normalized),
                    source=EPISODE_SOURCE[head.kind],
                    source_description=head.source_system,
                    valid_at=reference,
                    created_at=head.recorded_at,
                )
                reused = await graph.episode_has_results(episode)
                if reused:
                    result = await graph.read_episode_results(episode)  # a re-run after a crash: the model was paid for already (EX-04)
                else:
                    previous = (
                        await graph.recent_episode_uuids(before=reference, exclude=episode, limit=self._settings.graph_previous_episodes)
                        if self._settings.graph_previous_episodes
                        else []
                    )
                    with egress(
                        classification=head.classification,
                        purpose="graph_extraction",
                        workspace_id=str(ws),
                        correlation_id=correlation,
                        service=self._service,
                    ):
                        async with asyncio.timeout(self._settings.graph_ingest_timeout_seconds):
                            result = await graph.add_episode_scoped(
                                uuid=episode,
                                name=str(iid),
                                body=episode_body(head.kind, normalized),
                                source_description=head.source_system,
                                reference_time=reference,
                                source=EPISODE_SOURCE[head.kind],
                                compiled=compiled,
                                previous_episode_uuids=previous,
                            )
                    faults.current().hit(
                        STAGE, "after_side_effect"
                    )  # FI-09: a crash after Graphiti saved, before the platform committed anything
            except (JobError, GraphBusy):
                raise
            except Exception as exc:
                if type(exc).__name__ in ("CancelledError",):
                    raise
                raise job_error_for(exc, attempt=job.attempts) from exc
            await self._flush()  # the call log is buffered: the cost evidence must include THIS attempt's calls

            async with tenant_session(self._engine, ctx) as conn:
                st = await lock_stage(conn, ws, iid, STAGE)
                if st is None or st.generation != gen or st.status in TERMINAL_OK or st.status == DEAD:
                    return "noop"  # another run finished this generation (lease lost and reclaimed): write nothing twice
                audit = SystemAudit(conn, ws, correlation_id=correlation)
                summary = await self._reconciler.reconcile(conn, ws, src, spec, result, inputs, graph, audit=audit)
                usage = await self._usage(conn, ws, correlation)
                await conn.execute(
                    text(
                        "INSERT INTO graph_episodes (interaction_id, generation, workspace_id, episode_uuid, ontology_version_id, compiled_hash, chat_calls, "
                        "embedding_calls, prompt_tokens, completion_tokens) VALUES (:i, :g, :ws, :ep, :ov, :h, :cc, :ec, :pt, :ct) "
                        "ON CONFLICT (interaction_id, generation) DO UPDATE SET chat_calls = EXCLUDED.chat_calls, embedding_calls = EXCLUDED.embedding_calls, "
                        "prompt_tokens = EXCLUDED.prompt_tokens, completion_tokens = EXCLUDED.completion_tokens"
                    ),
                    {
                        "i": iid,
                        "g": gen,
                        "ws": ws,
                        "ep": episode,
                        "ov": pinned,
                        "h": _compiled_hash(spec),
                        "cc": usage["chat"][0],
                        "ec": usage["embeddings"][0],
                        "pt": usage["chat"][1] + usage["embeddings"][1],
                        "ct": usage["chat"][2] + usage["embeddings"][2],
                    },
                )
                stage_result = self._result(summary, episode, pinned, reused, usage)
                await audit.record(
                    "graph_ingest.completed",
                    resource_type="interaction",
                    resource_id=iid,
                    metadata=AuditMetadata(
                        stage=PipelineStage.GRAPH_INGEST,
                        generation=gen,
                        interaction_id=iid,
                        count_entities=len(summary.new_entities) + len(summary.linked_entities),
                        count_facts=len(summary.fact_ids),
                        count_reverts=len(summary.reverts),
                    ),
                )
                if summary.reverts:
                    await audit.record(
                        "extraction.reverted",
                        resource_type="interaction",
                        resource_id=iid,
                        metadata=AuditMetadata(interaction_id=iid, count_reverts=len(summary.reverts)),
                    )
                await enqueue(
                    conn,
                    workspace_id=ws,
                    stage="graph_projection",
                    subject_type="interaction",
                    subject_id=iid,
                    dedupe_key=f"interaction:{iid}:graph_projection:g{gen}",
                    payload={"interaction_id": str(iid), "generation": gen},
                    pipeline_version=job.pipeline_version,
                    ontology_version_id=pinned,
                    concurrency_key=f"graph:{ws}",
                    correlation_id=job.correlation_id,
                )
                await self._finish(conn, ws, iid, status=SUCCEEDED, code=None, result=stage_result)

            try:  # restore inline while the lock is held; if it fails the enqueued projection job finishes it
                report = await self._projector.restore_interaction(ctx, iid)
                if report.unsettled:
                    log.warning("graph_ingest_restore_unsettled", rows=len(report.unsettled))
            except Exception as exc:
                log.warning("graph_ingest_restore_deferred", error_type=type(exc).__name__)
        log.info(
            "graph_ingest_done",
            episode_uuid=episode,
            ontology_version_id=str(pinned),
            chat_calls=stage_result.chat_calls,
            entity_count=len(summary.new_entities) + len(summary.linked_entities),
            fact_count=len(summary.fact_ids),
            revert_count=len(summary.reverts),
        )
        return "succeeded"

    # ---- helpers --------------------------------------------------------------------------------------------------------------------------

    @staticmethod
    async def _usage(conn: Any, ws: uuid.UUID, correlation: str) -> dict[str, tuple[int, int, int]]:
        rows = (
            await conn.execute(
                text(
                    "SELECT operation, count(*) AS n, COALESCE(sum(prompt_tokens), 0) AS pt, COALESCE(sum(completion_tokens), 0) AS ct FROM model_calls "
                    "WHERE workspace_id = :ws AND correlation_id = :c GROUP BY operation"
                ),
                {"ws": ws, "c": correlation},
            )
        ).all()
        out = {"chat": (0, 0, 0), "embeddings": (0, 0, 0)}
        for r in rows:
            if r.operation in out:
                out[r.operation] = (int(r.n), int(r.pt), int(r.ct))
        return out

    @staticmethod
    def _result(
        summary: ReconcileSummary, episode: str, pinned: uuid.UUID, reused: bool, usage: dict[str, tuple[int, int, int]]
    ) -> GraphIngestResult:
        return GraphIngestResult(
            episode_uuid=episode,
            ontology_version_id=pinned,
            reused=reused,
            entities_new=len(summary.new_entities),
            entities_linked=len(summary.linked_entities),
            entities_forked=len(summary.forked_entities),
            name_variants=summary.name_variants,
            facts=summary.facts_by_status,
            evidence_added=summary.evidence_added,
            reviews=len(set(summary.review_ids)),
            reverts=len(summary.reverts),
            claims=summary.claims,
            skipped_edges=summary.skipped_edges,
            chat_calls=usage["chat"][0],
            embedding_calls=usage["embeddings"][0],
            prompt_tokens=usage["chat"][1] + usage["embeddings"][1],
            completion_tokens=usage["chat"][2] + usage["embeddings"][2],
            entity_ids=[*summary.new_entities][:RESULT_ID_CAP],
            fact_ids=summary.fact_ids[:RESULT_ID_CAP],
            review_ids=list(dict.fromkeys(summary.review_ids))[:RESULT_ID_CAP],
            revert_fact_ids=list(dict.fromkeys(r.fact_id for r in summary.reverts if r.fact_id)),
            revert_entity_ids=list(dict.fromkeys(r.entity_id for r in summary.reverts if r.entity_id)),
        )

    async def _finish(self, conn: Any, ws: uuid.UUID, iid: uuid.UUID, *, status: str, code: str | None, result: GraphIngestResult) -> None:
        await conn.execute(
            text(
                "UPDATE interaction_stages SET status = :st, finished_at = now(), last_error_category = :cat, last_error_code = :code, "
                "result = CAST(:r AS jsonb), started_at = COALESCE(started_at, now()) WHERE workspace_id = :ws AND interaction_id = :i AND stage = :s"
            ),
            {
                "st": status,
                "cat": "policy_terminal" if status == SKIPPED_POLICY else None,
                "code": code,
                "r": result.model_dump_json(exclude_none=True),
                "ws": ws,
                "i": iid,
                "s": STAGE,
            },
        )
        await self._orchestrator.advance(conn, ws, iid)

    # ---- failure bookkeeping (called by the dispatcher before the ack) ------------------------------------------------------------------------

    async def on_failure(self, job: ClaimedJob, decision: Decision) -> None:
        ws, iid = job.workspace_id, job.subject_id
        ctx = WorkspaceContext.for_system_task(ws, reason="graph_ingest stage failure")
        status = {"retry": FAILED, "dead": DEAD, "skipped": SKIPPED_POLICY}.get(decision.outcome)
        if status is None:  # budget_pause: waiting for budget is not a stage failure
            return
        async with tenant_session(self._engine, ctx) as conn:
            st = await lock_stage(conn, ws, iid, STAGE)
            if st is None or st.generation != job.generation or st.status in TERMINAL_OK:
                return
            await conn.execute(
                text(
                    "UPDATE interaction_stages SET status = :status, attempts = :n, last_error_category = :cat, last_error_code = :code, "
                    "finished_at = CASE WHEN :final THEN now() END WHERE workspace_id = :ws AND interaction_id = :i AND stage = :s"
                ),
                {
                    "status": status,
                    "n": job.attempts,
                    "cat": decision.category.value if decision.category else None,
                    "code": decision.code,
                    "final": decision.outcome != "retry",
                    "ws": ws,
                    "i": iid,
                    "s": STAGE,
                },
            )
            if decision.outcome != "retry":
                await self._orchestrator.advance(conn, ws, iid)
        log.info("stage_failed", stage=STAGE, outcome=decision.outcome, error_code=decision.code, attempt=job.attempts)


def _compiled_hash(spec: Any) -> str:
    from kg_ontology import content_hash

    return str(content_hash(spec))
