"""`kg reconcile` (phase-9 plan WP3): one comparison of the ledger with the graph, the memory store and the deletion records, and the SAFE repairs of what it finds.

A scan lists drift BY CLASS with IDS ONLY. A repair is applied only to a class whose repair is safe by construction:

* it re-derives from Postgres, which is authoritative (re-projection, a missing stage, a missing job);
* it removes something that belongs to nothing (an orphan node, edge or episode, a job for a subject that is gone);
* it re-runs a stage that the pipeline would run anyway (`graph_projection`, `memory_embed`);
* a repair that CALLS A MODEL (`graph_ingest` for a missing episode) happens only when the caller said `allow_reingest`, after a cost preview taken from the call log;
* what only a person can settle (a trace item that names a deleted record, a deletion that left residue) becomes a `deletion_exception` review, never an edit.

Every repair is audited (`reconcile.repaired`, class and count) and counted. After a repair the check runs again, so `repaired` is what really went away, not what was attempted.
The nightly scan (`reconcile_scan`) is check-only: it records the run and feeds the `reconcile_drift{class}` gauge that raises the alert."""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from kg_common.ids import episode_uuid, uuid7
from kg_common.versions import PIPELINE_VERSION
from kg_db.session import tenant_session
from kg_domain.principal import WorkspaceContext
from kg_governance.audit import AuditMetadata, SystemAudit
from kg_governance.deletion.plan import sets_of
from kg_governance.deletion.verify import verify
from kg_graph.service import GraphService
from kg_ingestion.reconcile import Reconciler
from kg_jobs.outbox import enqueue
from kg_knowledge.services import KnowledgeServices
from kg_obs import metrics
from kg_obs.logging import get_logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from kg_reconcile.models import SCOPES, ReconcileResult

log = get_logger("kg.reconcile")
MAX_REPAIR = 500  # items repaired per class per run; the next run continues


class Reconcile:
    def __init__(self, engine: AsyncEngine, knowledge: KnowledgeServices, graph: GraphService | None, *, embedding_version: int = 1):
        self._engine, self._knowledge, self._graph, self._embed_version = engine, knowledge, graph, embedding_version
        self._interactions = Reconciler()

    # ---- entry ---------------------------------------------------------------------------------------------------------------------------------
    async def run(
        self,
        ctx: WorkspaceContext,
        *,
        scope: str = "all",
        repair: bool = False,
        allow_reingest: bool = False,
        trigger: str = "manual",
        started_by: uuid.UUID | None = None,
        run_id: uuid.UUID | None = None,
    ) -> ReconcileResult:
        if scope not in SCOPES:
            raise ValueError(f"unknown scope {scope!r}")
        result = ReconcileResult(run_id or uuid7(), ctx.workspace_id, scope, "repair" if repair else "check", trigger)
        await self._open(ctx, result, started_by, existing=run_id is not None)
        try:
            await self._scan_and_repair(ctx, result, repair=repair, allow_reingest=allow_reingest)
        except Exception as exc:
            result.status = "failed"
            result.errors.append(type(exc).__name__)
            log.warning("reconcile_failed", error_type=type(exc).__name__)
        await self._close(ctx, result)
        return result

    async def _open(self, ctx: WorkspaceContext, r: ReconcileResult, started_by: uuid.UUID | None, *, existing: bool) -> None:
        async with tenant_session(self._engine, ctx) as conn:
            if not existing:
                await conn.execute(
                    text(
                        "INSERT INTO reconcile_runs (id, workspace_id, scope, mode, trigger, status, started_by) VALUES (:id, :ws, :scope, :mode, :trig, 'running', :by)"
                    ),
                    {"id": r.run_id, "ws": ctx.workspace_id, "scope": r.scope, "mode": r.mode, "trig": r.trigger, "by": started_by},
                )
            row = (
                await conn.execute(
                    text("SELECT started_at FROM reconcile_runs WHERE workspace_id = :ws AND id = :id"),
                    {"ws": ctx.workspace_id, "id": r.run_id},
                )
            ).one()
            r.started_at = row.started_at

    async def _close(self, ctx: WorkspaceContext, r: ReconcileResult) -> None:
        async with tenant_session(self._engine, ctx) as conn:
            await conn.execute(
                text(
                    "UPDATE reconcile_runs SET status = :st, drift = CAST(:d AS jsonb), repaired = CAST(:rep AS jsonb), error_code = :err, finished_at = now() "
                    "WHERE workspace_id = :ws AND id = :id"
                ),
                {
                    "st": r.status,
                    "d": json.dumps(r.drift),
                    "rep": json.dumps(r.repaired),
                    "err": r.errors[0] if r.errors else None,
                    "ws": ctx.workspace_id,
                    "id": r.run_id,
                },
            )
            audit = SystemAudit(conn, ctx.workspace_id)
            for cls, n in sorted(r.repaired.items()):
                metrics.reconcile_repairs_total.labels(drift_class=cls).inc(n)
                await audit.record(
                    "reconcile.repaired",
                    resource_type="reconcile_run",
                    resource_id=r.run_id,
                    metadata=AuditMetadata(reconcile_class=cls, count_repaired=n),
                )

    # ---- the scan ------------------------------------------------------------------------------------------------------------------------------
    async def _scan_and_repair(self, ctx: WorkspaceContext, r: ReconcileResult, *, repair: bool, allow_reingest: bool) -> None:
        wanted = ("graph", "ledger", "memory", "deletions") if r.scope == "all" else (r.scope,)
        checks: dict[str, Callable[[WorkspaceContext, ReconcileResult], Awaitable[None]]] = {
            "graph": self._check_graph,
            "ledger": self._check_ledger,
            "memory": self._check_memory,
            "deletions": self._check_deletions,
        }
        for scope in wanted:
            try:
                await checks[scope](ctx, r)
            except Exception as exc:
                r.errors.append(f"{scope}:{type(exc).__name__}")
                log.warning("reconcile_check_failed", scope=scope, error_type=type(exc).__name__)
        if not repair:
            return
        before = dict(r.drift)
        await self._repair(ctx, r, allow_reingest=allow_reingest)
        # what really went away: scan again and subtract
        again = ReconcileResult(r.run_id, r.workspace_id, r.scope, r.mode, r.trigger)
        for scope in wanted:
            try:
                await checks[scope](ctx, again)
            except Exception as exc:
                r.errors.append(f"{scope}:{type(exc).__name__}")
        for cls, n in before.items():
            gone = n - again.drift.get(cls, 0)
            if gone > 0:
                r.repaired[cls] = gone
        r.drift, r.samples = again.drift, {**r.samples, **again.samples}
        for cls in list(r.samples):
            if cls not in again.drift:
                r.samples.pop(cls)
        # a class that has more than a run's worth of work reports what is left

    # ---- graph ---------------------------------------------------------------------------------------------------------------------------------
    async def _check_graph(self, ctx: WorkspaceContext, r: ReconcileResult) -> None:
        if self._graph is None:
            r.errors.append("graph:unavailable")
            return
        report = await self._knowledge.projector.reconcile(ctx)
        grouped: dict[str, list[str]] = {}
        for d in report.drift:
            kind = "entity" if d.kind == "node" else "fact"
            problem = "orphan" if d.problem == "unexpected" else d.problem
            grouped.setdefault(f"graph.{kind}.{problem}", []).append(d.id)
        for name, ids in grouped.items():
            r.add(name, ids)
        graph = self._graph.scoped(ctx)
        async with tenant_session(self._engine, ctx) as conn:
            rows = (
                await conn.execute(
                    text("SELECT interaction_id, episode_uuid FROM graph_episodes WHERE workspace_id = :ws"), {"ws": ctx.workspace_id}
                )
            ).all()
            gens = (
                await conn.execute(
                    text(
                        "SELECT i.id AS interaction_id, COALESCE(max(s.generation), 0) AS g, bool_or(s.status IN ('pending','running')) AS busy "
                        "FROM interactions i LEFT JOIN interaction_stages s ON s.workspace_id = i.workspace_id AND s.interaction_id = i.id AND s.stage = 'graph_ingest' "
                        "WHERE i.workspace_id = :ws GROUP BY i.id"
                    ),
                    {"ws": ctx.workspace_id},
                )
            ).all()
            mentions = (
                await conn.execute(
                    text("SELECT episode_uuid, entity_id FROM entity_mentions WHERE workspace_id = :ws"), {"ws": ctx.workspace_id}
                )
            ).all()
        present = {x["u"] for x in await graph.run_cypher("MATCH (e:Episodic {group_id: $group_id}) RETURN e.uuid AS u")}
        recorded = {x.episode_uuid: x.interaction_id for x in rows}
        r.add("graph.episode.missing", sorted({str(x.interaction_id) for x in rows if x.episode_uuid not in present}))
        deterministic: dict[str, tuple[uuid.UUID, bool]] = {}
        for g in gens:
            for gen in range(int(g.g) + 2):
                deterministic[episode_uuid(g.interaction_id, gen)] = (g.interaction_id, bool(g.busy))
        orphans, unrecorded = [], set()
        for ep in sorted(present - set(recorded)):
            owner = deterministic.get(ep)
            if owner is None:
                orphans.append(ep)
            elif not owner[1]:  # an extraction that is running right now has not recorded its episode yet: not drift
                unrecorded.add(str(owner[0]))
        r.add("graph.episode.orphan", orphans)
        r.add("graph.episode.unrecorded", sorted(unrecorded))
        graph_mentions = {
            (x["e"], x["n"])
            for x in await graph.run_cypher(
                "MATCH (e:Episodic {group_id: $group_id})-[:MENTIONS]->(n:Entity) RETURN e.uuid AS e, n.uuid AS n"
            )
        }
        missing = sorted(
            {
                str(recorded[m.episode_uuid])
                for m in mentions
                if m.episode_uuid in recorded and (m.episode_uuid, str(m.entity_id)) not in graph_mentions
            }
        )
        r.add("graph.mention.missing", missing)

    # ---- ledger --------------------------------------------------------------------------------------------------------------------------------
    async def _check_ledger(self, ctx: WorkspaceContext, r: ReconcileResult) -> None:
        async with tenant_session(self._engine, ctx) as conn:
            report = await self._interactions.run(conn, ctx, repair=False)
            grouped: dict[str, list[str]] = {}
            for f in report.findings:
                grouped.setdefault(f"ledger.{f.kind}", []).append(str(f.subject_id))
            for name, ids in grouped.items():
                r.add(name, sorted(set(ids)))
            r.add("ledger.job_missing_subject", [str(x.id) for x in await self._jobs_without_subject(conn, ctx.workspace_id)])
            tomb = (
                await conn.execute(
                    text(
                        "SELECT DISTINCT i.context_id FROM context_items i JOIN deletion_tombstones t ON t.workspace_id = i.workspace_id "
                        "AND t.id::text = i.item_id AND t.kind = i.item_kind WHERE i.workspace_id = :ws AND i.item_kind IN ('fact','memory') LIMIT 500"
                    ),
                    {"ws": ctx.workspace_id},
                )
            ).all()
            r.add("ledger.context_item_tombstoned", [str(x.context_id) for x in tomb])

    @staticmethod
    async def _jobs_without_subject(conn: Any, ws: uuid.UUID) -> list[Any]:
        return list(
            (
                await conn.execute(
                    text(
                        "SELECT j.id FROM outbox_jobs j WHERE j.workspace_id = :ws AND j.status = 'pending' AND ("
                        " (j.subject_type = 'command' AND NOT EXISTS (SELECT 1 FROM commands c WHERE c.workspace_id = j.workspace_id AND c.id = j.subject_id) "
                        "   AND NOT EXISTS (SELECT 1 FROM deletion_requests d WHERE d.workspace_id = j.workspace_id AND d.id = j.subject_id))"
                        " OR (j.subject_type = 'memory' AND NOT EXISTS (SELECT 1 FROM curated_memories m WHERE m.workspace_id = j.workspace_id AND m.id = j.subject_id)))"
                    ),
                    {"ws": ws},
                )
            ).all()
        )

    # ---- memory --------------------------------------------------------------------------------------------------------------------------------
    async def _check_memory(self, ctx: WorkspaceContext, r: ReconcileResult) -> None:
        async with tenant_session(self._engine, ctx) as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT m.id FROM curated_memories m WHERE m.workspace_id = :ws AND m.status = 'active' AND m.embedding_status NOT IN ('skipped_by_policy') "
                        "AND NOT EXISTS (SELECT 1 FROM memory_embeddings e WHERE e.workspace_id = m.workspace_id AND e.memory_id = m.id AND e.embedding_version = :v) "
                        "AND NOT EXISTS (SELECT 1 FROM outbox_jobs j WHERE j.workspace_id = m.workspace_id AND j.subject_type = 'memory' AND j.subject_id = m.id "
                        " AND j.stage = 'memory_embed' AND j.status IN ('pending','leased')) ORDER BY m.recorded_at LIMIT 1000"
                    ),
                    {"ws": ctx.workspace_id, "v": self._embed_version},
                )
            ).all()
            r.add("memory.embedding_missing", [str(x.id) for x in rows])

    # ---- deletions -----------------------------------------------------------------------------------------------------------------------------
    async def _check_deletions(self, ctx: WorkspaceContext, r: ReconcileResult) -> None:
        async with tenant_session(self._engine, ctx) as conn:
            done = (
                await conn.execute(
                    text(
                        "SELECT id FROM deletion_requests WHERE workspace_id = :ws AND status IN ('completed','completed_with_exceptions') "
                        "AND kind IN ('subject','interaction','memory','principal_data') ORDER BY completed_at DESC LIMIT 200"
                    ),
                    {"ws": ctx.workspace_id},
                )
            ).all()
            dirty = []
            for req in done:
                sets = await sets_of(conn, req.id)
                if not (sets.e or sets.f or sets.m or sets.i):
                    continue
                result = await verify(conn, ctx, sets, [], None)
                if result.id_residuals:
                    dirty.append(str(req.id))
            r.add("deletions.residual", dirty)

    # ---- repairs -------------------------------------------------------------------------------------------------------------------------------
    async def _repair(self, ctx: WorkspaceContext, r: ReconcileResult, *, allow_reingest: bool) -> None:
        ws = ctx.workspace_id
        graph_classes = [c for c in r.drift if c.startswith(("graph.entity.", "graph.fact."))]
        if graph_classes:
            await self._knowledge.projector.reconcile(ctx, repair=True)
        if self._graph is not None:
            await self._repair_graph_episodes(ctx, r, allow_reingest=allow_reingest)
        if any(c.startswith("ledger.") and c not in ("ledger.job_missing_subject", "ledger.context_item_tombstoned") for c in r.drift):
            async with tenant_session(self._engine, ctx) as conn:
                await self._interactions.run(conn, ctx, repair=True)
        if r.drift.get("ledger.job_missing_subject"):
            async with tenant_session(self._engine, ctx) as conn:
                for job in (await self._jobs_without_subject(conn, ws))[:MAX_REPAIR]:
                    await conn.execute(text("SELECT * FROM ops.cancel_job(:j)"), {"j": job.id})
        if r.drift.get("memory.embedding_missing"):
            async with tenant_session(self._engine, ctx) as conn:
                for mid in [uuid.UUID(x) for x in r.samples.get("memory.embedding_missing", [])][:MAX_REPAIR]:
                    await self._requeue_embed(conn, ws, mid)
        for cls in ("ledger.context_item_tombstoned", "deletions.residual"):
            if r.drift.get(cls):
                await self._open_exceptions(ctx, r, cls)

    async def _repair_graph_episodes(self, ctx: WorkspaceContext, r: ReconcileResult, *, allow_reingest: bool) -> None:
        assert self._graph is not None
        ws = ctx.workspace_id
        graph = self._graph.scoped(ctx)
        for ep in r.samples.get("graph.episode.orphan", []) if r.drift.get("graph.episode.orphan") else []:
            await graph.remove_episode_scoped(ep)
        # graph_projection restores mentions (no model call)
        for iid in r.samples.get("graph.mention.missing", []):
            async with tenant_session(self._engine, ctx) as conn:
                await self._interactions.requeue_stage(conn, ws, uuid.UUID(iid), "graph_projection")
        reingest = sorted({*r.samples.get("graph.episode.missing", []), *r.samples.get("graph.episode.unrecorded", [])})
        reingest_n = r.drift.get("graph.episode.missing", 0) + r.drift.get("graph.episode.unrecorded", 0)
        if reingest_n:
            r.reingest_preview = await self._reingest_preview(ctx, reingest_n)
            if not allow_reingest:
                for cls in ("graph.episode.missing", "graph.episode.unrecorded"):
                    if r.drift.get(cls):
                        r.needs_confirmation[cls] = r.drift[cls]
                return
            for iid in reingest:
                async with tenant_session(self._engine, ctx) as conn:
                    await self._interactions.requeue_stage(conn, ws, uuid.UUID(iid), "graph_ingest")

    async def _reingest_preview(self, ctx: WorkspaceContext, n: int) -> dict[str, float | int]:
        """What re-running `graph_ingest` for `n` interactions is expected to cost: the mean of the last extraction calls in the call log (an estimate, never a promise)."""
        async with tenant_session(self._engine, ctx) as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT COALESCE(avg(est_cost_usd), 0)::float8 AS cost, count(*) AS calls FROM (SELECT est_cost_usd FROM model_calls "
                        "WHERE workspace_id = :ws AND purpose = 'graph_extraction' AND outcome = 'success' ORDER BY created_at DESC LIMIT 200) x"
                    ),
                    {"ws": ctx.workspace_id},
                )
            ).one()
        return {
            "interactions": n,
            "per_interaction_usd": round(float(row.cost), 6),
            "estimated_usd": round(float(row.cost) * n, 4),
            "based_on_calls": int(row.calls),
        }

    async def _requeue_embed(self, conn: Any, ws: uuid.UUID, mid: uuid.UUID) -> None:
        gen = int(
            (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM outbox_jobs WHERE workspace_id = :ws AND subject_type = 'memory' AND subject_id = :m AND stage = 'memory_embed'"
                    ),
                    {"ws": ws, "m": mid},
                )
            ).scalar_one()
        )
        await enqueue(
            conn,
            workspace_id=ws,
            stage="memory_embed",
            subject_type="memory",
            subject_id=mid,
            dedupe_key=f"memory:{mid}:embed:v{self._embed_version}:g{gen}",
            payload={"memory_id": str(mid), "generation": gen},
            pipeline_version=PIPELINE_VERSION,
            ontology_version_id=None,
        )

    async def _open_exceptions(self, ctx: WorkspaceContext, r: ReconcileResult, cls: str) -> None:
        """Only a person can settle these: one open review per request (or trace) and class, so a repeated run does not pile them up."""
        ids = r.samples.get(cls, [])
        async with tenant_session(self._engine, ctx) as conn:
            for ident in ids:
                if cls == "deletions.residual":
                    req = uuid.UUID(ident)
                    location, reason = "reconcile:id_residual", "verification_failed"
                else:
                    owner = (
                        await conn.execute(
                            text(
                                "SELECT t.request_id FROM deletion_tombstones t JOIN context_items i ON i.workspace_id = t.workspace_id AND t.id::text = i.item_id "
                                "WHERE i.workspace_id = :ws AND i.context_id = :c LIMIT 1"
                            ),
                            {"ws": ctx.workspace_id, "c": uuid.UUID(ident)},
                        )
                    ).first()
                    if owner is None:
                        continue
                    req, location, reason = owner.request_id, "reconcile:context_item", "verification_failed"
                dup = (
                    await conn.execute(
                        text(
                            "SELECT 1 FROM review_items WHERE workspace_id = :ws AND item_type = 'deletion_exception' AND deletion_request_id = :r AND status = 'open' "
                            "AND detail ->> 'location' = :loc"
                        ),
                        {"ws": ctx.workspace_id, "r": req, "loc": location},
                    )
                ).first()
                if dup is not None:
                    continue
                await conn.execute(
                    text(
                        "INSERT INTO review_items (id, workspace_id, item_type, rule_kind, reason_code, deletion_request_id, detail, due_at) "
                        "VALUES (:id, :ws, 'deletion_exception', 'deletion', :rc, :r, CAST(:d AS jsonb), now() + interval '30 days')"
                    ),
                    {
                        "id": uuid7(),
                        "ws": ctx.workspace_id,
                        "rc": reason,
                        "r": req,
                        "d": json.dumps({"reason": reason, "request_id": str(req), "location": location, "memory_id": None}),
                    },
                )
                metrics.reviews_created_total.labels(item_type="deletion_exception").inc()
