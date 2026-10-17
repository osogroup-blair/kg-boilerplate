"""Memory consolidation (MEM-06, phase-8 plan §5.3, ADR-043): the worker that keeps a memory store from filling with the same thing said again.

    reinforce    deterministic and automatic. Near-identical memories of one scope set are folded into the strongest one: its words are untouched (so no re-embedding), the
                 others become `superseded` with a `consolidated_from` lineage, and every piece of evidence travels to the survivor, so the independent-evidence count rises.
    synthesize   a model condenses a group; the platform validates every sentence; the result is a PROPOSED memory with a review item. Nothing takes effect until a person
                 approves it, and approving supersedes the sources while their evidence is kept by reference.

A distinct claim is never collapsed, a consideration is never escalated into a decision, and nothing crosses a scope set (a private note and a shared memory are never combined).
Both modes are idempotent: the same inputs (ids, revisions, policy) leave one `consolidation_runs` record and a re-run does nothing.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass

from kg_common.ids import uuid7
from kg_common.settings import Settings
from kg_common.versions import PIPELINE_VERSION
from kg_db.session import tenant_session
from kg_domain.principal import WorkspaceContext
from kg_governance.audit import AuditMetadata, CommandType, SystemAudit
from kg_jobs.client import ClaimedJob
from kg_jobs.errors import Category, JobError
from kg_knowledge.specs import SpecCache
from kg_modelgw.config import ModelConfig
from kg_modelgw.gateway import Correlation, ModelGateway
from kg_obs import metrics
from kg_obs.logging import get_logger
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from kg_memory import ledger, store
from kg_memory.consolidate_rules import Mem, Proposal, Statement, buckets, clusters, survivor_of, validate
from kg_memory.consolidate_schedule import _MATCHING, CONSOLIDATABLE, STAGE, key_hash, schedule  # noqa: F401
from kg_memory.errors import job_error_for
from kg_memory.models import AUTHORITY_RANK
from kg_memory.policy import Decision, EffectivePolicy, resolve_policy
from kg_memory.prompts import cons_v1
from kg_memory.store import EvidenceRef, NewMemory
from kg_memory.writers import MemoryWriter

log = get_logger("kg.memory.consolidate")
PURPOSE = "memory_consolidation"


# ---- the model's answer (schema_name = "ConsolidationProposal") ---------------------------------------------------------------------------------------


class ConsStatement(BaseModel):
    model_config = ConfigDict(extra="ignore")
    text: str = Field(max_length=2000)
    source_memory_ids: list[str] = Field(default_factory=list, max_length=50)


class ConsKeepSeparate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    memory_ids: list[str] = Field(default_factory=list, max_length=50)
    reason: str = Field(default="", max_length=500)


class ConsGroup(BaseModel):
    model_config = ConfigDict(extra="ignore")
    source_memory_ids: list[str] = Field(default_factory=list, max_length=50)
    summary: str = Field(default="", max_length=500)
    statements: list[ConsStatement] = Field(default_factory=list, max_length=30)
    keep_separate: list[ConsKeepSeparate] = Field(default_factory=list, max_length=30)


class ConsolidationProposal(BaseModel):
    model_config = ConfigDict(extra="ignore")
    groups: list[ConsGroup] = Field(default_factory=list, max_length=20)


# ---- scheduling (called by the writers) -----------------------------------------------------------------------------------------------------------


# ---- the job -------------------------------------------------------------------------------------------------------------------------------------


@dataclass
class _Loaded:
    mem: Mem
    revision: int
    created_by: uuid.UUID
    importance: float
    summary: str


def _vector(value: str | None) -> tuple[float, ...] | None:
    if not value:
        return None
    return tuple(float(x) for x in value.strip("[]").split(",") if x)


class MemoryConsolidateJob:
    stage = STAGE

    def __init__(
        self,
        engine: AsyncEngine,
        gateway: ModelGateway | None,
        writer: MemoryWriter,
        specs: SpecCache,
        config: ModelConfig,
        settings: Settings,
        *,
        service: str = "kg-worker",
    ):
        self._engine, self._gateway, self._writer, self._specs, self._config, self._settings, self._service = (
            engine,
            gateway,
            writer,
            specs,
            config,
            settings,
            service,
        )

    async def handle(self, job: ClaimedJob) -> None:
        ws = job.workspace_id
        mem_type = str(job.payload.get("type", ""))
        key = str(job.payload.get("key", ""))
        if mem_type not in CONSOLIDATABLE or not key:
            raise JobError(Category.CONFIG_TERMINAL, "not_consolidatable")
        ctx = WorkspaceContext.for_system_task(ws, reason="memory_consolidate stage")
        async with tenant_session(self._engine, ctx) as conn:
            active = await self._specs.active(conn, ws)
            policy = resolve_policy(active[1].memory_policies if active else None, mem_type)
            if policy.consolidation == "disabled":
                metrics.consolidation_runs_total.labels(mode="none", outcome="disabled").inc()
                return
            loaded = (await self._load(conn, ws, mem_type)).get(key, [])
        if len(loaded) < policy.consolidation_min_group:
            metrics.consolidation_runs_total.labels(mode=policy.consolidation, outcome="below_min_group").inc()
            return
        await self._reinforce(
            ctx, ws, loaded, policy
        )  # synthesis is always preceded by the deterministic pass: near duplicates need no model
        if policy.consolidation == "synthesize":
            async with tenant_session(self._engine, ctx) as conn:
                loaded = (await self._load(conn, ws, mem_type)).get(key, [])  # what is still active after the fold
            subjects = list(loaded[0].mem.subjects) if loaded else []
            await self._synthesize(ctx, ws, job, loaded, policy, subjects)

    async def _load(self, conn: AsyncConnection, ws: uuid.UUID, mem_type: str) -> dict[str, list[_Loaded]]:
        """Every active memory of the type, grouped by (scope set, subject set): the key a write scheduled. Nothing crosses a key."""
        rows = (
            await conn.execute(
                text(
                    "SELECT m.id, m.type, m.content, m.summary, m.authority_class, m.recorded_at, m.occurred_at, m.classification, m.assertion, m.importance, m.revision, "
                    "m.created_by, (SELECT e.embedding::text FROM memory_embeddings e WHERE e.workspace_id = m.workspace_id AND e.memory_id = m.id AND e.embedding_version = :v) AS embedding, "
                    "COALESCE((SELECT array_agg(s.scope_kind || ':' || s.scope_ref ORDER BY s.scope_kind, s.scope_ref) FROM memory_scopes s WHERE s.workspace_id = m.workspace_id AND s.memory_id = m.id), ARRAY[]::text[]) AS scopes, "
                    "COALESCE((SELECT array_agg(x.entity_id::text ORDER BY x.entity_id::text) FROM memory_subjects x WHERE x.workspace_id = m.workspace_id AND x.memory_id = m.id AND x.role = 'subject'), ARRAY[]::text[]) AS subjects "
                    "FROM curated_memories m WHERE m.workspace_id = :ws AND m.type = :t AND m.status = 'active' AND m.frozen_at IS NULL ORDER BY m.recorded_at, m.id"
                ),
                {"ws": ws, "t": mem_type, "v": self._settings.memory_embedding_version},
            )
        ).all()
        out: dict[str, list[_Loaded]] = {}
        for r in rows:
            scope_t = tuple(tuple(s.split(":", 1)) for s in r.scopes)
            subject_t = tuple(r.subjects)
            loaded = _Loaded(
                Mem(
                    id=str(r.id),
                    type=r.type,
                    content=r.content,
                    authority=r.authority_class,
                    recorded_at=r.recorded_at,
                    classification=r.classification,
                    assertion=r.assertion,
                    scopes=scope_t,
                    subjects=subject_t,
                    occurred_at=r.occurred_at,
                    embedding=_vector(r.embedding),
                ),
                int(r.revision),
                r.created_by,
                float(r.importance),
                r.summary,
            )
            out.setdefault(key_hash(r.type, list(scope_t), [uuid.UUID(s) for s in subject_t]), []).append(loaded)
        return out

    @staticmethod
    def _input_hash(mode: str, group: list[_Loaded], policy: EffectivePolicy) -> str:
        doc = {
            "m": mode,
            "ids": sorted(f"{x.mem.id}@{x.revision}" for x in group),
            "p": [policy.consolidation, policy.near_duplicate_jaccard, policy.near_duplicate_cosine],
        }
        return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()

    # ---- reinforce -------------------------------------------------------------------------------------------------------------------------

    async def _reinforce(self, ctx: WorkspaceContext, ws: uuid.UUID, loaded: list[_Loaded], policy: EffectivePolicy) -> None:
        by_id = {x.mem.id: x for x in loaded}
        for cluster in clusters(
            [x.mem for x in loaded], jaccard_min=policy.near_duplicate_jaccard, cosine_min=policy.near_duplicate_cosine
        ):
            members = [by_id[m.id] for m in cluster]
            input_hash = self._input_hash("reinforce", members, policy)
            keep = survivor_of(cluster)
            survivor = by_id[keep.id]
            folded = [x for x in members if x.mem.id != keep.id]
            key = key_hash(survivor.mem.type, [(s[0], s[1]) for s in survivor.mem.scopes], [uuid.UUID(s) for s in survivor.mem.subjects])
            async with tenant_session(self._engine, ctx) as conn:
                done = (
                    await conn.execute(
                        text("SELECT 1 FROM consolidation_runs WHERE workspace_id = :ws AND mode = 'reinforce' AND input_hash = :h"),
                        {"ws": ws, "h": input_hash},
                    )
                ).first()
                if done is not None:
                    metrics.consolidation_runs_total.labels(mode="reinforce", outcome="already_done").inc()
                    continue
                if not policy.consolidation_auto_supersede:
                    await self._run(conn, ws, key, "reinforce", input_hash, "auto_supersede_off", None, len(members))
                    metrics.consolidation_runs_total.labels(mode="reinforce", outcome="auto_supersede_off").inc()
                    continue
                locked = {
                    r.id: r
                    for r in (
                        await conn.execute(
                            text(
                                "SELECT id, status, revision FROM curated_memories WHERE workspace_id = :ws AND id = ANY(:ids) ORDER BY id FOR UPDATE"
                            ),
                            {"ws": ws, "ids": [uuid.UUID(x.mem.id) for x in members]},
                        )
                    ).all()
                }
                if any(
                    locked[uuid.UUID(x.mem.id)].status != "active" or locked[uuid.UUID(x.mem.id)].revision != x.revision for x in members
                ):
                    metrics.consolidation_runs_total.labels(mode="reinforce", outcome="changed").inc()
                    continue  # someone changed one of them since it was read: the next job decides again
                audit = SystemAudit(conn, ws)
                sid = uuid.UUID(survivor.mem.id)
                for f in folded:
                    fid = uuid.UUID(f.mem.id)
                    for ev in await store.load_evidence(conn, ws, fid):
                        await store.add_evidence(
                            conn,
                            ws,
                            sid,
                            EvidenceRef(
                                ev.source_kind,
                                interaction_id=ev.interaction_id,
                                episode_uuid=ev.episode_uuid,
                                fact_id=ev.fact_id,
                                command_id=ev.command_id,
                                memory_id=ev.source_memory_id,
                                external_ref=ev.external_ref,
                            ),
                            ev.added_by,
                        )
                    await store.add_evidence(conn, ws, sid, EvidenceRef("memory", memory_id=fid), None)
                    await conn.execute(
                        text(
                            "INSERT INTO memory_lineage (memory_id, related_id, workspace_id, relation) VALUES (:m, :r, :ws, 'consolidated_from') ON CONFLICT DO NOTHING"
                        ),
                        {"m": sid, "r": fid, "ws": ws},
                    )
                    await store.set_status(conn, ws, fid, "superseded", close_validity=True)
                    await audit.record(
                        "memory.superseded_old",
                        resource_type="memory",
                        resource_id=fid,
                        metadata=AuditMetadata(
                            memory_id=fid,
                            target_id=sid,
                            memory_status="superseded",
                            reason_code="consolidated",
                            consolidation_mode="reinforce",
                        ),
                    )
                await store.set_status(conn, ws, sid, "active", confirm=True)  # an independent report of the same thing is a confirmation
                command_id = uuid7()
                await ledger.insert_command(
                    conn,
                    ws,
                    command_id=command_id,
                    command_type=CommandType.MEMORY_CONSOLIDATE,
                    key=f"consolidate:reinforce:{input_hash}",
                    digest=input_hash,
                    actor_principal_id=survivor.created_by,
                    authority=survivor.mem.authority,
                    classification=survivor.mem.classification,
                    ontology_version_id=None,
                    payload={"mode": "reinforce", "survivor": survivor.mem.id, "folded": [f.mem.id for f in folded]},
                    result={
                        "out": {"memory_id": survivor.mem.id, "status": "active", "revision": survivor.revision + 1},
                        "http_status": 200,
                    },
                    http_status=200,
                )
                await audit.record(
                    "consolidation.applied",
                    resource_type="memory",
                    resource_id=sid,
                    metadata=AuditMetadata(
                        memory_id=sid,
                        command_type=CommandType.MEMORY_CONSOLIDATE,
                        count_memories=len(folded),
                        consolidation_mode="reinforce",
                        memory_type=survivor.mem.type,
                    ),
                )
                await self._run(conn, ws, key, "reinforce", input_hash, "reinforced", sid, len(members))
            metrics.consolidation_runs_total.labels(mode="reinforce", outcome="reinforced").inc()

    @staticmethod
    async def _run(
        conn: AsyncConnection, ws: uuid.UUID, key: str, mode: str, input_hash: str, outcome: str, memory_id: uuid.UUID | None, n: int
    ) -> None:
        await conn.execute(
            text(
                "INSERT INTO consolidation_runs (id, workspace_id, key_hash, mode, input_hash, outcome, proposal_memory_id, source_count) "
                "VALUES (:id, :ws, :k, :m, :h, :o, :p, :n) ON CONFLICT (workspace_id, mode, input_hash) DO NOTHING"
            ),
            {"id": uuid7(), "ws": ws, "k": key, "m": mode, "h": input_hash, "o": outcome, "p": memory_id, "n": n},
        )

    # ---- synthesize ------------------------------------------------------------------------------------------------------------------------

    async def _synthesize(
        self, ctx: WorkspaceContext, ws: uuid.UUID, job: ClaimedJob, loaded: list[_Loaded], policy: EffectivePolicy, subjects: list[str]
    ) -> None:
        if self._gateway is None:
            raise JobError(Category.CONFIG_TERMINAL, "no_gateway")
        mem_type = loaded[0].mem.type if loaded else ""
        groups = (
            buckets([x.mem for x in loaded], window_days=policy.episodic_window_days)
            if mem_type == "episodic"
            else [[x.mem for x in loaded]]
        )
        by_id = {x.mem.id: x for x in loaded}
        for bucket in groups:
            if len(bucket) < policy.consolidation_min_group:
                continue
            members = [by_id[m.id] for m in bucket]
            input_hash = self._input_hash("synthesize", members, policy)
            classification = max(
                (m.classification for m in bucket), key=["public", "internal", "confidential", "restricted", "local_only"].index
            )
            key = key_hash(mem_type, [(s[0], s[1]) for s in bucket[0].scopes], [uuid.UUID(s) for s in bucket[0].subjects])
            async with tenant_session(self._engine, ctx) as conn:
                if (
                    await conn.execute(
                        text("SELECT 1 FROM consolidation_runs WHERE workspace_id = :ws AND mode = 'synthesize' AND input_hash = :h"),
                        {"ws": ws, "h": input_hash},
                    )
                ).first():
                    metrics.consolidation_runs_total.labels(mode="synthesize", outcome="already_done").inc()
                    continue
                names = await self._names(conn, ws, subjects)
                if not self._config.egress_allows(classification):
                    await self._run(conn, ws, key, "synthesize", input_hash, "egress_denied", None, len(members))
                    metrics.consolidation_runs_total.labels(mode="synthesize", outcome="egress_denied").inc()
                    continue
            handles = {f"M{i}": x for i, x in enumerate(members, start=1)}
            messages = cons_v1.build_messages(memories=[(h, x.mem.content) for h, x in handles.items()], subject_names=names)
            correlation = str(uuid.uuid5(uuid.NAMESPACE_URL, f"memory-consolidate:{input_hash}"))
            try:
                completion = await self._gateway.complete(
                    PURPOSE,
                    messages,
                    classification=classification,
                    correlation=Correlation(correlation, str(ws), "", self._service),
                    response_schema=ConsolidationProposal,
                )
            except Exception as exc:
                if type(exc).__name__ == "CancelledError":
                    raise
                raise job_error_for(exc, attempt=job.attempts) from exc
            assert isinstance(completion.parsed, ConsolidationProposal)
            await self._apply_proposal(
                ctx, ws, key, input_hash, completion.parsed, handles, bucket, members, classification, completion.alias, correlation
            )

    @staticmethod
    async def _names(conn: AsyncConnection, ws: uuid.UUID, subjects: list[str]) -> list[str]:
        if not subjects:
            return []
        rows = (
            await conn.execute(
                text(
                    "SELECT display_name FROM entities WHERE workspace_id = :ws AND id = ANY(CAST(:ids AS uuid[])) AND status = 'active' ORDER BY display_name"
                ),
                {"ws": ws, "ids": subjects},
            )
        ).all()
        return [r.display_name for r in rows]

    async def _apply_proposal(
        self,
        ctx: WorkspaceContext,
        ws: uuid.UUID,
        key: str,
        input_hash: str,
        proposal: ConsolidationProposal,
        handles: dict[str, _Loaded],
        bucket: list[Mem],
        members: list[_Loaded],
        classification: str,
        alias: str,
        correlation: str,
    ) -> None:
        group_mems = {h: x.mem for h, x in handles.items()}
        written: uuid.UUID | None = None
        outcome = "nothing_to_condense" if not proposal.groups else "dropped"
        drops: list[str] = []
        async with tenant_session(self._engine, ctx) as conn:
            audit = SystemAudit(conn, ws, correlation_id=correlation)
            active = await self._specs.active(
                conn, ws
            )  # the proposal's command carries the version it was made under (a review decision about it needs one)
            version_id = active[0] if active else None
            for g in proposal.groups:
                prop = Proposal(
                    sources=[h.strip().upper() for h in g.source_memory_ids],
                    summary=g.summary,
                    statements=[Statement(s.text, [h.strip().upper() for h in s.source_memory_ids]) for s in g.statements],
                )
                verdict = validate(prop, group_mems)
                if not verdict.ok:
                    drops.append(verdict.reason)
                    metrics.consolidation_drops_total.labels(reason=verdict.reason).inc()
                    continue
                sources = [handles[h] for h in dict.fromkeys(prop.sources)]
                fresh = [await self._still_active(conn, ws, x) for x in sources]
                if not all(fresh):
                    drops.append("changed")
                    metrics.consolidation_drops_total.labels(reason="changed").inc()
                    continue
                written = await self._propose(conn, ws, audit, sources, prop, classification, alias, input_hash, version_id)
                if written is not None:
                    outcome = "proposed"
                    break  # one proposal per run: the next run (the sources are still active) takes what is left
            await self._run(
                conn,
                ws,
                key,
                "synthesize",
                input_hash,
                outcome if outcome != "dropped" else f"dropped_{drops[0] if drops else 'unknown'}",
                written,
                len(members),
            )
        metrics.consolidation_runs_total.labels(
            mode="synthesize", outcome="proposed" if written else "dropped" if outcome.startswith("dropped") else outcome
        ).inc()

    @staticmethod
    async def _still_active(conn: AsyncConnection, ws: uuid.UUID, x: _Loaded) -> bool:
        row = (
            await conn.execute(
                text("SELECT status, revision FROM curated_memories WHERE workspace_id = :ws AND id = :id FOR UPDATE"),
                {"ws": ws, "id": uuid.UUID(x.mem.id)},
            )
        ).first()
        return row is not None and row.status == "active" and row.revision == x.revision

    async def _propose(
        self,
        conn: AsyncConnection,
        ws: uuid.UUID,
        audit: SystemAudit,
        sources: list[_Loaded],
        prop: Proposal,
        classification: str,
        alias: str,
        input_hash: str,
        version_id: uuid.UUID | None,
    ) -> uuid.UUID | None:
        first = sources[0]
        weakest = min((x.mem.authority for x in sources), key=lambda a: AUTHORITY_RANK.get(a, 0))
        content = "\n".join(s.text.strip() for s in prop.statements)
        nm = NewMemory(
            type=first.mem.type,
            origin="consolidated",
            content=content,
            summary=store.derive_summary(prop.summary)[:280],
            status="proposed",
            scopes=list(first.mem.scopes),
            created_by=first.created_by,
            authority=weakest,  # a condensed memory is never stronger than its weakest source
            evidence_status="unverified",
            classification=classification,
            importance=max(x.importance for x in sources),
            pipeline_version=PIPELINE_VERSION,
            subject_ids=[uuid.UUID(s) for s in first.mem.subjects],
            evidence=[EvidenceRef("memory", memory_id=uuid.UUID(x.mem.id)) for x in sources],
            lineage=[(uuid.UUID(x.mem.id), "consolidated_from") for x in sources],
            occurred_at=min((x.mem.occurred_at or x.mem.recorded_at) for x in sources),
            derived_by_alias=alias,
            prompt_version=cons_v1.PROMPT_VERSION,
            purpose="memory consolidation",
        )
        out = await self._writer.persist(
            conn,
            ws,
            audit,
            command_type=CommandType.MEMORY_CONSOLIDATE,
            key=f"consolidate:synthesize:{input_hash}",
            digest=input_hash,
            nm=nm,
            decision=Decision("proposed", "consolidation_proposal", "consolidation_proposal"),
            ontology_version_id=version_id,
            on_duplicate="return",
            audit_action="consolidation.proposed",
            extra_payload={"mode": "synthesize", "sources": [x.mem.id for x in sources]},
        )
        if out.out.deduplicated:
            return None
        return out.out.memory_id
