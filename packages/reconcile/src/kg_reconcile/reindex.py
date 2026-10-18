"""Flow F (phase-9 plan WP8): rebuild a workspace's graph in a SEPARATE namespace, check it, and cut over atomically; keep the old one for rollback.

The life of a run:  preview -> (confirm) building -> catching_up -> ready -> activated   (or cancelled / failed / rolled_back)

* **building.** A job (`graph_reindex`, its own concurrency key, so the live namespace keeps working) writes every projectable entity and fact of Postgres into the NEW namespace
  under parked uuids (kg_graph.reindex explains why), reusing the vectors the live graph already holds for unchanged names and facts, copies the episodes and their MENTIONS (or
  rebuilds an episode from its decrypted content when the live graph lost it), and stamps the Postgres revision each element was rendered from.
* **catching up.** Postgres moves on while the build runs. Every pass compares the revisions with what was built and writes the difference, until a pass changes nothing.
* **ready.** The build is verified against Postgres (the same element sets, sampled property equality) and compared with the live namespace; the report is stored on the run.
* **activated (the cutover).** Under the workspace's graph lock (no projection and no extraction writes meanwhile): a last catch-up, ONE Neo4j transaction that parks the live
  group and unparks the built one, then ONE Postgres transaction that flips the pointer and writes the ledger command and the audit entry. `NOTIFY` tells every process; the
  process that did it updates its own registry at once. A state-based repair then makes the new live graph equal Postgres again (anything projected in the instant of the exchange).
  If the Postgres half fails the exchange is undone. The old namespace is kept (`retired`) for REINDEX_RETENTION_DAYS, then cleared.
* **rolled back.** The same exchange the other way, within the retention window. Postgres is authoritative, so the repair after a rollback brings the older graph up to date.

A deletion clears every INACTIVE namespace of the workspace (the old one and any build in progress), so no copy of a deleted subject outlives the purge."""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import uuid
from dataclasses import dataclass
from typing import Any

from kg_common.errors import Conflict, NotFound, PlatformError
from kg_common.ids import uuid7
from kg_common.settings import Settings
from kg_common.versions import PIPELINE_VERSION
from kg_db.session import tenant_session
from kg_domain.principal import Principal, WorkspaceContext
from kg_extraction.handler import EPISODE_SOURCE, episode_body
from kg_governance.audit import AuditMetadata, AuditSink, SystemAudit
from kg_graph.group import reindex_namespace
from kg_graph.service import GraphService
from kg_ingestion.crypto import PayloadCipher
from kg_ingestion.operations import NothingToReplay, OperationsService, StageRunning
from kg_ingestion.store import read_content
from kg_jobs import faults
from kg_jobs.client import ClaimedJob
from kg_jobs.errors import Category, JobError
from kg_jobs.outbox import enqueue
from kg_jobs.policy import Decision
from kg_knowledge.graphlock import GraphBusy
from kg_knowledge.projection import render_edge, render_node
from kg_knowledge.services import KnowledgeServices
from kg_modelgw.config import ModelConfig
from kg_modelgw.egress import egress
from kg_modelgw.transport import stricter
from kg_obs import metrics
from kg_obs.logging import get_logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

log = get_logger("kg.reindex")
STAGE = "graph_reindex"
BATCH = 100
MAX_PASSES = 8
SAMPLE_VERIFY = 150
OPEN = ("preview", "building", "catching_up", "ready")


class ReindexConflict(Conflict):
    code = "reindex_conflict"


class ReindexNotReady(Conflict):
    code, default_message = "reindex_not_ready", "The rebuilt namespace is not ready to be activated"


class ReindexVerificationFailed(PlatformError):
    code, http_status, default_message = "reindex_verification_failed", 409, "The rebuilt namespace does not match Postgres"


def _sha(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


@dataclass
class Eligible:
    entities: list[Any]
    facts: list[Any]
    episodes: dict[uuid.UUID, tuple[str, ...]]


class ReindexService:
    def __init__(
        self,
        engine: AsyncEngine,
        graph: GraphService,
        knowledge: KnowledgeServices,
        cipher: PayloadCipher,
        config: ModelConfig,
        settings: Settings,
        *,
        service: str,
    ):
        self._engine, self._graph, self._k, self._cipher, self._config, self._settings, self._service = (
            engine,
            graph,
            knowledge,
            cipher,
            config,
            settings,
            service,
        )

    # ---- reads ------------------------------------------------------------------------------------------------------------------------------
    async def _eligible(self, conn: AsyncConnection, ws: uuid.UUID) -> Eligible:
        ents = (
            await conn.execute(
                text(
                    "SELECT * FROM entities WHERE workspace_id = :ws AND status IN ('active','candidate','untyped') AND projection_status = 'projected' ORDER BY recorded_at, id"
                ),
                {"ws": ws},
            )
        ).all()
        ids = {e.id for e in ents}
        facts = [
            f
            for f in (
                await conn.execute(
                    text(
                        "SELECT f.*, s.display_name AS s_name, t.display_name AS t_name, s.classification AS s_cls, t.classification AS t_cls "
                        "FROM facts f JOIN entities s ON s.workspace_id = f.workspace_id AND s.id = f.source_entity_id "
                        "JOIN entities t ON t.workspace_id = f.workspace_id AND t.id = f.target_entity_id "
                        "WHERE f.workspace_id = :ws AND f.status NOT IN ('deleted','deletion_pending') AND f.projection_status = 'projected' ORDER BY f.recorded_at, f.id"
                    ),
                    {"ws": ws},
                )
            ).all()
            if f.source_entity_id in ids and f.target_entity_id in ids
        ]
        episodes: dict[uuid.UUID, list[str]] = {}
        for ev in (
            await conn.execute(
                text(
                    "SELECT fact_id, episode_uuid FROM fact_evidence WHERE workspace_id = :ws AND source_kind = 'episode' ORDER BY added_at, id"
                ),
                {"ws": ws},
            )
        ).all():
            episodes.setdefault(ev.fact_id, []).append(ev.episode_uuid)
        return Eligible(list(ents), facts, {k: tuple(v) for k, v in episodes.items()})

    async def _run_row(self, conn: AsyncConnection, ws: uuid.UUID, run_id: uuid.UUID) -> Any:
        row = (
            await conn.execute(text("SELECT * FROM reindex_runs WHERE workspace_id = :ws AND id = :id"), {"ws": ws, "id": run_id})
        ).first()
        if row is None:
            raise NotFound()
        return row

    async def _active_namespace(self, conn: AsyncConnection, ws: uuid.UUID) -> str:
        row = (
            await conn.execute(
                text("SELECT namespace FROM workspace_graph_namespaces WHERE workspace_id = :ws AND status = 'active'"), {"ws": ws}
            )
        ).first()
        return str(row.namespace) if row else f"ws-{ws}"

    async def list_runs(self, conn: AsyncConnection, ws: uuid.UUID, limit: int = 20) -> list[dict[str, Any]]:
        rows = (
            await conn.execute(
                text("SELECT * FROM reindex_runs WHERE workspace_id = :ws ORDER BY created_at DESC LIMIT :n"), {"ws": ws, "n": limit}
            )
        ).all()
        return [self._out(r) for r in rows]

    async def get(self, conn: AsyncConnection, ws: uuid.UUID, run_id: uuid.UUID) -> dict[str, Any]:
        return self._out(await self._run_row(conn, ws, run_id))

    async def namespaces(self, conn: AsyncConnection, ws: uuid.UUID) -> list[dict[str, Any]]:
        rows = (
            await conn.execute(
                text(
                    "SELECT namespace, status, reindex_run_id, created_at, activated_at, retired_at, cleared_at FROM workspace_graph_namespaces WHERE workspace_id = :ws "
                    "ORDER BY created_at DESC"
                ),
                {"ws": ws},
            )
        ).all()
        return [dict(r._mapping) for r in rows]

    @staticmethod
    def _out(r: Any) -> dict[str, Any]:
        return {
            "id": r.id,
            "namespace": r.namespace,
            "previous_namespace": r.previous_namespace,
            "mode": r.mode,
            "status": r.status,
            "ontology_version_id": r.ontology_version_id,
            "estimate": r.estimate,
            "progress": r.progress,
            "report": r.report,
            "error_code": r.error_code,
            "created_at": r.created_at,
            "updated_at": r.updated_at,
            "activated_at": r.activated_at,
            "rolled_back_at": r.rolled_back_at,
        }

    # ---- preview and start --------------------------------------------------------------------------------------------------------------------
    async def preview(self, conn: AsyncConnection, ctx: WorkspaceContext, *, mode: str) -> dict[str, Any]:
        ws = ctx.workspace_id
        n_ent = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM entities WHERE workspace_id = :ws AND status IN ('active','candidate','untyped') AND projection_status = 'projected'"
                ),
                {"ws": ws},
            )
        ).scalar_one()
        n_fact = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM facts WHERE workspace_id = :ws AND status NOT IN ('deleted','deletion_pending') AND projection_status = 'projected'"
                ),
                {"ws": ws},
            )
        ).scalar_one()
        n_int = (
            await conn.execute(
                text("SELECT count(*) FROM interactions WHERE workspace_id = :ws AND processing_status <> 'deletion_pending'"), {"ws": ws}
            )
        ).scalar_one()
        embed = (
            await conn.execute(
                text(
                    "SELECT COALESCE(avg(est_cost_usd), 0)::float8 AS c FROM (SELECT est_cost_usd FROM model_calls WHERE workspace_id = :ws AND operation = 'embeddings' "
                    "AND outcome = 'ok' ORDER BY created_at DESC LIMIT 200) x"
                ),
                {"ws": ws},
            )
        ).scalar_one()
        extract = (
            await conn.execute(
                text(
                    "SELECT COALESCE(avg(est_cost_usd), 0)::float8 AS c FROM (SELECT est_cost_usd FROM model_calls WHERE workspace_id = :ws AND purpose = 'graph_extraction' "
                    "AND outcome = 'ok' ORDER BY created_at DESC LIMIT 200) x"
                ),
                {"ws": ws},
            )
        ).scalar_one()
        active = await self._active_namespace(conn, ws)
        number = await self._next_number(conn, ws)
        embeddings = int(n_ent) + int(n_fact)  # an upper bound: unchanged names and facts reuse the live graph's vector
        out: dict[str, Any] = {
            "namespace": reindex_namespace(ctx, number),
            "previous_namespace": active,
            "mode": mode,
            "entities": int(n_ent),
            "facts": int(n_fact),
            "interactions": int(n_int),
            "embedding_calls_at_most": embeddings,
            "estimated_cost_usd": round(embeddings * float(embed), 4),
            "per_embedding_usd": round(float(embed), 8),
        }
        if mode == "reextract":
            out["reextraction_interactions"] = int(n_int)
            out["reextraction_cost_usd"] = round(int(n_int) * float(extract), 4)
            out["estimated_cost_usd"] = round(out["estimated_cost_usd"] + out["reextraction_cost_usd"], 4)
        return out

    async def _next_number(self, conn: AsyncConnection, ws: uuid.UUID) -> int:
        rows = (await conn.execute(text("SELECT namespace FROM workspace_graph_namespaces WHERE workspace_id = :ws"), {"ws": ws})).all()
        used = [int(m.group(1)) for r in rows if (m := re.search(r"-r(\d+)$", r.namespace))]
        return (max(used) if used else 0) + 1

    async def start(
        self,
        conn: AsyncConnection,
        ctx: WorkspaceContext,
        principal: Principal,
        *,
        mode: str,
        ontology_version_id: uuid.UUID | None,
        audit: AuditSink,
        key: str,
    ) -> dict[str, Any]:
        ws = ctx.workspace_id
        digest = _sha("graph.reindex_start", str(principal.principal_id), mode, str(ontology_version_id))
        await conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"reindex:{ws}"})
        prior = (
            await conn.execute(
                text("SELECT request_hash, result FROM commands WHERE workspace_id = :ws AND idempotency_key = :k"), {"ws": ws, "k": key}
            )
        ).first()
        if prior is not None:
            if prior.request_hash != digest:
                raise ReindexConflict("this Idempotency-Key was used for a different request")
            return await self.get(conn, ws, uuid.UUID(prior.result["run_id"]))
        open_run = (
            await conn.execute(
                text("SELECT id FROM reindex_runs WHERE workspace_id = :ws AND status = ANY(:s)"), {"ws": ws, "s": list(OPEN)}
            )
        ).first()
        if open_run is not None:
            raise ReindexConflict("a reindex of this workspace is already in progress", details={"run_id": str(open_run.id)})
        if mode == "reextract":
            active = (
                await conn.execute(text("SELECT active_version_id FROM workspace_ontology WHERE workspace_id = :ws"), {"ws": ws})
            ).first()
            if ontology_version_id is None or active is None or active.active_version_id != ontology_version_id:
                raise ReindexConflict(
                    "re-extraction runs under the ACTIVE ontology: activate the version first", details={"field": "ontology_version_id"}
                )
        estimate = await self.preview(conn, ctx, mode=mode)
        run_id, namespace, previous = uuid7(), estimate["namespace"], estimate["previous_namespace"]
        await conn.execute(
            text(
                "INSERT INTO reindex_runs (id, workspace_id, ontology_version_id, namespace, previous_namespace, mode, status, estimate, started_by) "
                "VALUES (:id, :ws, :ov, :ns, :prev, :mode, 'building', CAST(:est AS jsonb), :by)"
            ),
            {
                "id": run_id,
                "ws": ws,
                "ov": ontology_version_id,
                "ns": namespace,
                "prev": previous,
                "mode": mode,
                "est": json.dumps(estimate),
                "by": principal.principal_id,
            },
        )
        await conn.execute(  # a workspace created after migration 0009 has no row for its own group yet: it is the active one
            text(
                "INSERT INTO workspace_graph_namespaces (workspace_id, namespace, status) VALUES (:ws, :ns, 'active') ON CONFLICT DO NOTHING"
            ),
            {"ws": ws, "ns": previous},
        )
        await conn.execute(
            text(
                "INSERT INTO workspace_graph_namespaces (workspace_id, namespace, status, reindex_run_id) VALUES (:ws, :ns, 'building', :run)"
            ),
            {"ws": ws, "ns": namespace, "run": run_id},
        )
        await self._ledger(conn, ws, principal, "graph.reindex_start", key, digest, run_id, {"namespace": namespace, "mode": mode})
        await enqueue(
            conn,
            workspace_id=ws,
            stage=STAGE,
            subject_type="command",
            subject_id=run_id,
            dedupe_key=f"reindex:{run_id}:build:g0",
            payload={"run_id": str(run_id), "generation": 0},
            pipeline_version=PIPELINE_VERSION,
            concurrency_key=f"reindex:{ws}:{namespace}",
        )
        await audit.record(
            "reindex.started",
            resource_type="reindex_run",
            resource_id=run_id,
            metadata=AuditMetadata(reindex_id=run_id, namespace=namespace, previous_namespace=previous),
        )
        metrics.reindex_runs.labels(status="building").inc()
        return await self.get(conn, ws, run_id)

    async def _ledger(
        self,
        conn: AsyncConnection,
        ws: uuid.UUID,
        principal: Principal,
        command_type: str,
        key: str,
        digest: str,
        run_id: uuid.UUID,
        payload: dict[str, Any],
    ) -> None:
        await conn.execute(
            text(
                "INSERT INTO commands (id, workspace_id, idempotency_key, request_hash, command_type, actor_principal_id, authority_class, classification, source_system, source_ref, "
                "ontology_version_id, payload, status, decision, result, http_status, projection_status) VALUES (:id, :ws, :k, :h, :type, :actor, 'admin_asserted', 'internal', "
                "'reindex', :ref, NULL, CAST(:payload AS jsonb), 'applied', '[]'::jsonb, CAST(:result AS jsonb), 200, 'not_applicable')"
            ),
            {
                "id": uuid7(),
                "ws": ws,
                "k": key,
                "h": digest,
                "type": command_type,
                "actor": principal.principal_id,
                "ref": str(run_id),
                "payload": json.dumps(payload),
                "result": json.dumps({"run_id": str(run_id)}),
            },
        )

    # ---- progress ---------------------------------------------------------------------------------------------------------------------------
    async def _progress(self, ctx: WorkspaceContext, run_id: uuid.UUID, status: str | None = None, **progress: Any) -> None:
        async with tenant_session(self._engine, ctx) as conn:
            await conn.execute(
                text(
                    "UPDATE reindex_runs SET progress = progress || CAST(:p AS jsonb), status = COALESCE(:st, status), updated_at = now() WHERE workspace_id = :ws AND id = :id"
                ),
                {"p": json.dumps(progress, default=str), "st": status, "ws": ctx.workspace_id, "id": run_id},
            )

    async def _fail(self, ctx: WorkspaceContext, run_id: uuid.UUID, code: str) -> None:
        async with tenant_session(self._engine, ctx) as conn:
            await conn.execute(
                text(
                    "UPDATE reindex_runs SET status = 'failed', error_code = :c, updated_at = now() WHERE workspace_id = :ws AND id = :id AND status = ANY(:s)"
                ),
                {"c": code, "ws": ctx.workspace_id, "id": run_id, "s": list(OPEN)},
            )
        metrics.reindex_runs.labels(status="failed").inc()

    # ---- build ------------------------------------------------------------------------------------------------------------------------------
    async def build(self, ctx: WorkspaceContext, run_id: uuid.UUID) -> None:
        ws = ctx.workspace_id
        async with tenant_session(self._engine, ctx) as conn:
            run = await self._run_row(conn, ws, run_id)
        if run.status not in ("building", "catching_up", "ready"):
            return  # cancelled, failed, activated: nothing to build
        ns, active = run.namespace, run.previous_namespace
        await self._progress(ctx, run_id, "building", phase="writing", passes=0)
        passes = 0
        changed = 1
        while changed and passes < MAX_PASSES:
            changed = await self._sync(ctx, run_id, ns, active, first=passes == 0)
            passes += 1
            await self._progress(ctx, run_id, "catching_up" if passes > 1 else "building", passes=passes, last_pass_changes=changed)
        if changed:
            raise JobError(Category.RETRYABLE, "reindex_not_converging")
        rg = self._graph.reindexer(ctx)
        await self._episodes(ctx, run_id, ns, active)
        await rg.stamp_originals(ns)
        report = await self.verify(ctx, ns, active)
        await self._progress(ctx, run_id, phase="verified")
        async with tenant_session(self._engine, ctx) as conn:
            await conn.execute(
                text(
                    "UPDATE reindex_runs SET report = CAST(:r AS jsonb), status = :st, updated_at = now() WHERE workspace_id = :ws AND id = :id"
                ),
                {"r": json.dumps(report, default=str), "st": "ready" if report["ok"] else "failed", "ws": ws, "id": run_id},
            )
            if not report["ok"]:
                await conn.execute(
                    text("UPDATE reindex_runs SET error_code = 'verification_failed' WHERE workspace_id = :ws AND id = :id"),
                    {"ws": ws, "id": run_id},
                )
        metrics.reindex_runs.labels(status="ready" if report["ok"] else "failed").inc()
        log.info("reindex_built", ok=report["ok"], passes=passes)

    async def _sync(self, ctx: WorkspaceContext, run_id: uuid.UUID, ns: str, active: str, *, first: bool) -> int:
        """One pass: bring the built namespace to the revisions Postgres has NOW. Returns how many elements it wrote or removed."""
        ws = ctx.workspace_id
        rg = self._graph.reindexer(ctx)
        tag = f"r{ns.rsplit('-r', 1)[1]}:"
        async with tenant_session(self._engine, ctx) as conn:
            el = await self._eligible(conn, ws)
            specs = {}
            for vid in {r.ontology_version_id for r in (*el.entities, *el.facts)}:
                specs[vid] = await self._k.specs.get(conn, ws, vid)
        have_nodes, have_edges = await rg.inventory(ns)
        want_nodes = {str(e.id): e for e in el.entities}
        want_edges = {str(f.id): f for f in el.facts}
        stale_edges = [u for u in have_edges if u not in want_edges]
        stale_nodes = [u for u in have_nodes if u not in want_nodes]
        todo_nodes = [e for u, e in want_nodes.items() if have_nodes.get(u) != e.revision]
        todo_edges = [f for u, f in want_edges.items() if have_edges.get(u) != f.revision]
        removed = await rg.delete_tagged(ns, nodes=stale_nodes, edges=stale_edges) if (stale_nodes or stale_edges) else 0
        building = self._graph.building(ctx, ns)
        live = self._graph.scoped(ctx) if active == self._graph.namespaces.active(ctx) else self._graph.namespace_of(ctx, active)
        done_nodes = done_edges = 0
        for i in range(0, len(todo_nodes), BATCH):
            chunk = todo_nodes[i : i + BATCH]
            vectors = await live.read_node_vectors([str(e.id) for e in chunk])
            revs: dict[str, int] = {}
            for e in chunk:
                want = render_node(e, specs[e.ontology_version_id])
                old = vectors.get(want.uuid)
                with egress(
                    classification=e.classification,
                    purpose="graph_projection",
                    workspace_id=str(ws),
                    correlation_id=f"reindex-{run_id}",
                    service=self._service,
                ):
                    await building.upsert_entity_node(
                        uuid=tag + want.uuid,
                        name=want.name,
                        type_key=want.type_key,
                        attributes=want.attributes,
                        created_at=want.created_at,
                        embedding=old[1] if old is not None and old[0] == want.name else None,
                    )
                revs[want.uuid] = e.revision
            await rg.stamp_originals(ns)
            await rg.set_revisions(ns, nodes=revs)
            done_nodes += len(chunk)
            await self._progress(
                ctx, run_id, entities_done=len(have_nodes) + done_nodes if first else done_nodes, entities_total=len(want_nodes)
            )
            faults.current().hit(
                STAGE, "after_side_effect"
            )  # FI-19: a crash between batches; the next attempt writes only what is still missing
        for i in range(0, len(todo_edges), BATCH):
            chunk = todo_edges[i : i + BATCH]
            vectors = await live.read_edge_vectors([str(f.id) for f in chunk])
            revs = {}
            for f in chunk:
                edge = render_edge(f, f.s_name, f.t_name, specs[f.ontology_version_id], el.episodes.get(f.id, ()))
                old = vectors.get(edge.uuid)
                with egress(
                    classification=stricter(stricter(f.classification, f.s_cls), f.t_cls),
                    purpose="graph_projection",
                    workspace_id=str(ws),
                    correlation_id=f"reindex-{run_id}",
                    service=self._service,
                ):
                    await building.upsert_fact_edge(
                        uuid=tag + edge.uuid,
                        source_uuid=tag + edge.source_uuid,
                        target_uuid=tag + edge.target_uuid,
                        name=edge.name,
                        fact=edge.fact,
                        valid_at=edge.valid_at,
                        invalid_at=edge.invalid_at,
                        created_at=edge.created_at,
                        expired_at=edge.expired_at,
                        attributes=edge.attributes,
                        episodes=[tag + e for e in edge.episodes],
                        embedding=old[1] if old is not None and old[0] == edge.fact else None,
                    )
                revs[edge.uuid] = f.revision
            await rg.stamp_originals(ns)
            await rg.set_revisions(ns, edges=revs)
            done_edges += len(chunk)
            await self._progress(ctx, run_id, facts_done=done_edges, facts_total=len(want_edges))
        return removed + len(todo_nodes) + len(todo_edges)

    async def _episodes(self, ctx: WorkspaceContext, run_id: uuid.UUID, ns: str, active: str) -> None:
        """Episodes and mentions: copied from the live namespace when it has them, rebuilt from the decrypted content (the normalised text) when it does not."""
        ws = ctx.workspace_id
        rg = self._graph.reindexer(ctx)
        tag = f"r{ns.rsplit('-r', 1)[1]}:"
        copied, mentions = await rg.copy_episodes(active, ns)
        await rg.stamp_originals(ns)
        async with tenant_session(self._engine, ctx) as conn:
            recorded = (
                await conn.execute(
                    text(
                        "SELECT g.interaction_id, g.episode_uuid, i.kind, i.source_system, i.recorded_at, i.occurred_at FROM graph_episodes g "
                        "JOIN interactions i ON i.workspace_id = g.workspace_id AND i.id = g.interaction_id WHERE g.workspace_id = :ws AND i.processing_status <> 'deletion_pending'"
                    ),
                    {"ws": ws},
                )
            ).all()
        have = await rg.episode_uuids(ns)
        rebuilt = 0
        building = self._graph.building(ctx, ns)
        for r in recorded:
            if r.episode_uuid in have:
                continue
            body = ""
            if self._settings.graph_store_episode_content:
                async with tenant_session(self._engine, ctx) as conn:
                    sealed = await read_content(conn, ws, r.interaction_id, "normalized")
                if sealed is not None:
                    body = episode_body(
                        r.kind, self._cipher.decrypt(sealed, workspace_id=ws, interaction_id=r.interaction_id, form="normalized").decode()
                    )
            await building.ensure_episode_node(
                uuid=tag + r.episode_uuid,
                name=str(r.interaction_id),
                body=body,
                source=EPISODE_SOURCE[r.kind],
                source_description=r.source_system,
                valid_at=r.occurred_at or r.recorded_at,
                created_at=r.recorded_at,
            )
            rebuilt += 1
        if rebuilt:
            async with tenant_session(self._engine, ctx) as conn:
                pairs = [
                    (m.episode_uuid, str(m.entity_id))
                    for m in (
                        await conn.execute(
                            text("SELECT episode_uuid, entity_id FROM entity_mentions WHERE workspace_id = :ws AND status <> 'rejected'"),
                            {"ws": ws},
                        )
                    ).all()
                ]
            await rg.stamp_originals(ns)
            await rg.ensure_mentions(ns, pairs)
            await rg.stamp_originals(ns)
        await self._progress(ctx, run_id, episodes_copied=copied, mentions_copied=mentions, episodes_rebuilt=rebuilt)

    # ---- verify and compare -----------------------------------------------------------------------------------------------------------------
    async def verify(self, ctx: WorkspaceContext, ns: str, active: str) -> dict[str, Any]:
        """The built namespace against Postgres (element sets and a sample of properties) and against the live namespace (counts)."""
        ws = ctx.workspace_id
        rg = self._graph.reindexer(ctx)
        tag = f"r{ns.rsplit('-r', 1)[1]}:"
        async with tenant_session(self._engine, ctx) as conn:
            el = await self._eligible(conn, ws)
            specs = {vid: await self._k.specs.get(conn, ws, vid) for vid in {r.ontology_version_id for r in (*el.entities, *el.facts)}}
        have_nodes, have_edges = await rg.inventory(ns)
        want_nodes = {str(e.id): e for e in el.entities}
        want_edges = {str(f.id): f for f in el.facts}
        missing_n, extra_n = sorted(set(want_nodes) - set(have_nodes)), sorted(set(have_nodes) - set(want_nodes))
        missing_e, extra_e = sorted(set(want_edges) - set(have_edges)), sorted(set(have_edges) - set(want_edges))
        stale = [u for u, e in want_nodes.items() if have_nodes.get(u) not in (e.revision,)] + [
            u for u, f in want_edges.items() if have_edges.get(u) not in (f.revision,)
        ]
        mismatched: list[str] = []
        building = self._graph.building(ctx, ns)
        sample = random.Random(7).sample(sorted(want_nodes), min(SAMPLE_VERIFY, len(want_nodes)))  # noqa: S311 (a reproducible sample, not a secret)
        for u in sample:
            e = want_nodes[u]
            want = render_node(e, specs[e.ontology_version_id])
            got = await building.read_node(tag + u)
            if got is None or got.name != want.name or got.labels != frozenset({want.type_key}) or got.attributes != want.attributes:
                mismatched.append(u)
        for u in random.Random(8).sample(sorted(want_edges), min(SAMPLE_VERIFY, len(want_edges))):  # noqa: S311
            f = want_edges[u]
            edge = render_edge(f, f.s_name, f.t_name, specs[f.ontology_version_id], el.episodes.get(f.id, ()))
            got_e = await building.read_edge(tag + u)
            if got_e is None or (got_e.name, got_e.fact, got_e.valid_at, got_e.invalid_at, got_e.attributes) != (
                edge.name,
                edge.fact,
                edge.valid_at,
                edge.invalid_at,
                edge.attributes,
            ):
                mismatched.append(u)
        built = (await rg.counts(ns)).as_dict()
        live = (await rg.counts(active)).as_dict()
        ok = not (missing_n or extra_n or missing_e or extra_e or stale or mismatched)
        return {
            "ok": ok,
            "built": built,
            "active": live,
            "postgres": {"entities": len(want_nodes), "facts": len(want_edges)},
            "missing_nodes": missing_n[:20],
            "extra_nodes": extra_n[:20],
            "missing_edges": missing_e[:20],
            "extra_edges": extra_e[:20],
            "stale": stale[:20],
            "mismatched_sample": mismatched[:20],
            "sampled": len(sample),
            "only_in_active": {"nodes": max(0, live["nodes"] - built["nodes"]), "edges": max(0, live["edges"] - built["edges"])},
        }

    # ---- cutover and rollback ---------------------------------------------------------------------------------------------------------------
    async def activate(
        self, ctx: WorkspaceContext, run_id: uuid.UUID, principal: Principal, *, key: str, audit: AuditSink | None = None
    ) -> dict[str, Any]:
        ws = ctx.workspace_id
        async with tenant_session(self._engine, ctx) as conn:
            run = await self._run_row(conn, ws, run_id)
            prior = (
                await conn.execute(
                    text("SELECT request_hash FROM commands WHERE workspace_id = :ws AND idempotency_key = :k"), {"ws": ws, "k": key}
                )
            ).first()
            if run.status == "activated" and prior is not None:
                return self._out(run)
        if run.status != "ready":
            raise ReindexNotReady(details={"status": run.status})
        ns, old = run.namespace, run.previous_namespace
        try:
            async with self._k.lock.hold(ws, wait_seconds=60.0):
                # a last catch-up and a last verification, with every other writer of this graph held off
                for _ in range(3):
                    if not await self._sync(ctx, run_id, ns, old, first=False):
                        break
                else:
                    raise ReindexVerificationFailed(details={"reason": "still_changing"})
                await self._graph.reindexer(ctx).stamp_originals(ns)
                report = await self.verify(ctx, ns, old)
                if not report["ok"]:
                    raise ReindexVerificationFailed(details={"reason": "mismatch"})
                await self._exchange_and_flip(
                    ctx,
                    run_id,
                    principal,
                    retire=old,
                    activate=ns,
                    key=key,
                    command="graph.namespace_activate",
                    status="activated",
                    report=report,
                )
        except GraphBusy as exc:
            raise ReindexConflict("the graph is busy; try again in a moment", details={"retryable": True}) from exc
        metrics.reindex_runs.labels(status="activated").inc()
        await self._heal(ctx)
        if run.mode == "reextract":
            await self._enqueue_reextract(ctx, run_id, generation=1)
        async with tenant_session(self._engine, ctx) as conn:
            if audit is not None:
                await audit.record(
                    "reindex.cutover",
                    resource_type="reindex_run",
                    resource_id=run_id,
                    metadata=AuditMetadata(reindex_id=run_id, namespace=ns, previous_namespace=old),
                )
            return await self.get(conn, ws, run_id)

    async def rollback(
        self, ctx: WorkspaceContext, namespace: str, principal: Principal, *, key: str, audit: AuditSink | None = None
    ) -> dict[str, Any]:
        """Re-activates a RETIRED namespace (within the retention window) by the same exchange the other way."""
        ws = ctx.workspace_id
        async with tenant_session(self._engine, ctx) as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT status, cleared_at, reindex_run_id FROM workspace_graph_namespaces WHERE workspace_id = :ws AND namespace = :ns"
                    ),
                    {"ws": ws, "ns": namespace},
                )
            ).first()
            if row is None:
                raise NotFound()
            if row.status != "retired" or row.cleared_at is not None:
                raise ReindexConflict(
                    "only a retired namespace that has not been cleared can be re-activated", details={"status": row.status}
                )
            current = await self._active_namespace(conn, ws)
            # the run that RETIRED it (the one that made `current` active) is the one to mark rolled back
            retiring = (
                await conn.execute(
                    text("SELECT id FROM reindex_runs WHERE workspace_id = :ws AND namespace = :cur AND status = 'activated'"),
                    {"ws": ws, "cur": current},
                )
            ).first()
        run_id = retiring.id if retiring is not None else row.reindex_run_id
        try:
            async with self._k.lock.hold(ws, wait_seconds=60.0):
                await self._exchange_and_flip(
                    ctx,
                    run_id,
                    principal,
                    retire=current,
                    activate=namespace,
                    key=key,
                    command="graph.namespace_rollback",
                    status="rolled_back",
                    report=None,
                )
        except GraphBusy as exc:
            raise ReindexConflict("the graph is busy; try again in a moment", details={"retryable": True}) from exc
        metrics.reindex_runs.labels(status="rolled_back").inc()
        await self._heal(ctx)
        if audit is not None:
            await audit.record(
                "reindex.rolled_back",
                resource_type="reindex_run",
                resource_id=run_id,
                metadata=AuditMetadata(reindex_id=run_id, namespace=namespace, previous_namespace=current),
            )
        async with tenant_session(self._engine, ctx) as conn:
            return await self.get(conn, ws, run_id)

    async def _exchange_and_flip(
        self,
        ctx: WorkspaceContext,
        run_id: uuid.UUID,
        principal: Principal,
        *,
        retire: str,
        activate: str,
        key: str,
        command: str,
        status: str,
        report: dict[str, Any] | None,
    ) -> None:
        ws = ctx.workspace_id
        rg = self._graph.reindexer(ctx)
        await rg.exchange(retire=retire, activate=activate)  # ONE Neo4j transaction
        try:
            async with tenant_session(self._engine, ctx) as conn:
                await conn.execute(
                    text(
                        "INSERT INTO workspace_graph_namespaces (workspace_id, namespace, status, retired_at) VALUES (:ws, :ns, 'retired', now()) "
                        "ON CONFLICT (workspace_id, namespace) DO UPDATE SET status = 'retired', retired_at = now()"
                    ),
                    {"ws": ws, "ns": retire},
                )
                await conn.execute(
                    text(
                        "UPDATE workspace_graph_namespaces SET status = 'active', activated_at = now(), retired_at = NULL WHERE workspace_id = :ws AND namespace = :ns"
                    ),
                    {"ws": ws, "ns": activate},
                )
                if status == "rolled_back":
                    await conn.execute(
                        text(
                            "UPDATE reindex_runs SET status = 'rolled_back', rolled_back_at = now(), updated_at = now() WHERE workspace_id = :ws AND id = :id"
                        ),
                        {"ws": ws, "id": run_id},
                    )
                else:
                    await conn.execute(
                        text(
                            "UPDATE reindex_runs SET status = 'activated', activated_at = now(), updated_at = now(), report = COALESCE(CAST(:r AS jsonb), report) WHERE workspace_id = :ws AND id = :id"
                        ),
                        {"r": json.dumps(report, default=str) if report is not None else None, "ws": ws, "id": run_id},
                    )
                await self._ledger(
                    conn,
                    ws,
                    principal,
                    command,
                    key,
                    _sha(command, str(run_id), activate),
                    run_id,
                    {"namespace": activate, "previous_namespace": retire},
                )
        except BaseException:
            await rg.exchange(retire=activate, activate=retire)  # the pointer did not move: neither does the graph
            raise
        self._graph.namespaces.set(ws, activate)  # this process need not wait for its own NOTIFY

    async def _heal(self, ctx: WorkspaceContext) -> None:
        """The exchange takes an instant; anything projected into the old group in it is made right by the state-based repair (Postgres is authoritative)."""
        try:
            await self._k.projector.reconcile(ctx, repair=True)
        except Exception as exc:
            log.warning("reindex_heal_failed", error_type=type(exc).__name__)

    # ---- re-extraction after the cutover (mode=reextract) ----------------------------------------------------------------------------------------
    async def _enqueue_reextract(self, ctx: WorkspaceContext, run_id: uuid.UUID, *, generation: int, delay_seconds: float = 0.0) -> None:
        async with tenant_session(self._engine, ctx) as conn:
            await enqueue(
                conn,
                workspace_id=ctx.workspace_id,
                stage=STAGE,
                subject_type="command",
                subject_id=run_id,
                dedupe_key=f"reindex:{run_id}:reextract:g{generation}",
                payload={"run_id": str(run_id), "phase": "reextract", "generation": generation},
                pipeline_version=PIPELINE_VERSION,
                concurrency_key=f"reindex-extract:{ctx.workspace_id}",
                delay_seconds=delay_seconds,
            )

    async def reextract_batch(
        self, ctx: WorkspaceContext, run_id: uuid.UUID, generation: int, *, batch: int = 25, max_outstanding: int = 100
    ) -> int:
        """Once the rebuilt namespace is live, every interaction is extracted again under the ACTIVE ontology (the existing replay with `repin`), in batches, never more than
        `max_outstanding` extraction jobs ahead of the workers. The model calls are the cost the preview named; the results go through the ordinary review flow. Returns how
        many interactions this batch replayed (0: done)."""
        ws = ctx.workspace_id
        ops = OperationsService()
        async with tenant_session(self._engine, ctx) as conn:
            run = await self._run_row(conn, ws, run_id)
            outstanding = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM outbox_jobs WHERE workspace_id = :ws AND stage = 'graph_ingest' AND status IN ('pending','leased')"
                    ),
                    {"ws": ws},
                )
            ).scalar_one()
        if run.status != "activated":
            return 0
        if outstanding >= max_outstanding:
            await self._enqueue_reextract(ctx, run_id, generation=generation + 1, delay_seconds=30.0)
            return -1
        cursor = (run.progress or {}).get("reextract_after") or "00000000-0000-0000-0000-000000000000"
        async with tenant_session(self._engine, ctx) as conn:
            ids = [
                r.id
                for r in (
                    await conn.execute(
                        text(
                            "SELECT i.id FROM interactions i JOIN interaction_stages s ON s.workspace_id = i.workspace_id AND s.interaction_id = i.id AND s.stage = 'graph_ingest' "
                            "WHERE i.workspace_id = :ws AND i.processing_status <> 'deletion_pending' AND s.status NOT IN ('pending','running','skipped_disabled') "
                            "AND i.id > CAST(:c AS uuid) ORDER BY i.id LIMIT :n"
                        ),
                        {"ws": ws, "c": cursor, "n": batch},
                    )
                ).all()
            ]
        replayed = 0
        for iid in ids:
            async with tenant_session(self._engine, ctx) as conn:
                try:
                    await ops.replay_interaction(
                        conn, ctx, SystemAudit(conn, ws), iid, stage="graph_ingest", reason_code="reindex", repin=True
                    )
                    replayed += 1
                except (StageRunning, NothingToReplay):
                    continue
        if ids:
            await self._progress(
                ctx, run_id, reextract_after=str(ids[-1]), reextract_done=int((run.progress or {}).get("reextract_done", 0)) + replayed
            )
            await self._enqueue_reextract(ctx, run_id, generation=generation + 1)
        else:
            await self._progress(ctx, run_id, reextract_finished=True)
        return replayed

    # ---- cancel and clean-up -----------------------------------------------------------------------------------------------------------------
    async def cancel(self, ctx: WorkspaceContext, run_id: uuid.UUID, audit: AuditSink | None = None) -> dict[str, Any]:
        ws = ctx.workspace_id
        async with tenant_session(self._engine, ctx) as conn:
            run = await self._run_row(conn, ws, run_id)
            if run.status not in OPEN and run.status != "failed":
                raise ReindexConflict("only a run that has not been activated can be cancelled", details={"status": run.status})
            await conn.execute(
                text("UPDATE reindex_runs SET status = 'cancelled', updated_at = now() WHERE workspace_id = :ws AND id = :id"),
                {"ws": ws, "id": run_id},
            )
            await conn.execute(
                text(
                    "UPDATE workspace_graph_namespaces SET status = 'retired', retired_at = now() WHERE workspace_id = :ws AND namespace = :ns AND status = 'building'"
                ),
                {"ws": ws, "ns": run.namespace},
            )
            if audit is not None:
                await audit.record(
                    "reindex.cancelled",
                    resource_type="reindex_run",
                    resource_id=run_id,
                    metadata=AuditMetadata(reindex_id=run_id, namespace=run.namespace),
                )
        await self.clear_namespace(ctx, run.namespace)
        async with tenant_session(self._engine, ctx) as conn:
            return await self.get(conn, ws, run_id)

    async def clear_namespace(self, ctx: WorkspaceContext, namespace: str) -> int:
        n = await self._graph.reindexer(ctx).clear(namespace)
        async with tenant_session(self._engine, ctx) as conn:
            await conn.execute(
                text(
                    "UPDATE workspace_graph_namespaces SET cleared_at = now() WHERE workspace_id = :ws AND namespace = :ns AND status <> 'active'"
                ),
                {"ws": ctx.workspace_id, "ns": namespace},
            )
        return n

    async def clear_expired(self) -> int:
        """Retired namespaces past REINDEX_RETENTION_DAYS (called by the worker). Returns how many were cleared."""
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(text("SELECT * FROM ops.retired_namespaces_due(:d)"), {"d": self._settings.reindex_retention_days})
            ).all()
            await conn.rollback()
        done = 0
        for r in rows:
            ctx = WorkspaceContext.for_system_task(r.ws_id, reason="reindex retention")
            await self.clear_namespace(ctx, r.namespace)
            done += 1
        return done


class ReindexJob:
    stage = STAGE

    def __init__(self, service: ReindexService):
        self._svc = service

    async def handle(self, job: ClaimedJob) -> None:
        ctx = WorkspaceContext.for_system_task(job.workspace_id, reason="graph reindex job")
        run_id = uuid.UUID(str(job.payload["run_id"]))
        try:
            if job.payload.get("phase") == "reextract":
                await self._svc.reextract_batch(ctx, run_id, int(job.payload.get("generation") or 1))
                return
            await self._svc.build(ctx, run_id)
        except (JobError, asyncio.CancelledError):
            raise
        except NotFound:
            return
        except Exception as exc:
            log.warning("reindex_step_failed", error_type=type(exc).__name__)
            raise JobError(Category.RETRYABLE, "reindex_step_failed") from exc

    async def on_failure(self, job: ClaimedJob, decision: Decision) -> None:
        if decision.outcome == "dead":
            await self._svc._fail(
                WorkspaceContext.for_system_task(job.workspace_id, reason="reindex failure"),
                uuid.UUID(str(job.payload["run_id"])),
                decision.code or "attempts_exhausted",
            )


_ = (SystemAudit,)
