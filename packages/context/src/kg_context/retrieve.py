"""The sources (phase-6 plan §5.3 step 3). Each is one coroutine with its OWN tenant transaction and its OWN timeout, run in parallel by the service.

    postgres   a subject's facts at `as_of` / `known_at` (Postgres is authoritative: critical current state never waits for Neo4j), open contradictions, the
               memory-assertion half of the expected signals, the pipeline watermarks
    graph      Graphiti's hybrid search through `GraphSearch`, every hit joined back to Postgres (a hit Postgres does not know is dropped and counted)
    memory     `MemorySearch` per requested type: the visibility predicate is INSIDE the ranking statement, so nothing unauthorised ever has a rank, a score or a count

A source that fails or is slow becomes an `unavailable` entry with a reason code; the others still answer. Nothing here logs or returns content."""

from __future__ import annotations

import asyncio
import datetime as dt
import time
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any

from kg_common.errors import DependencyUnavailable, NotFound
from kg_db.session import tenant_session
from kg_domain.principal import Principal, WorkspaceContext
from kg_governance.grants import SubjectFilter
from kg_jobs.reads import stage_lag
from kg_knowledge.models import FactOut, GraphSearchIn
from kg_knowledge.services import KnowledgeServices
from kg_memory.models import SearchHit, SearchIn
from kg_memory.reads import ContextInfo, Viewer
from kg_memory.services import MemoryServices
from kg_obs import metrics
from kg_obs.logging import get_logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from kg_context.candidates import SubjectInfo, hit_from_memory
from kg_context.planner import Plan
from kg_context.unknowns import predicates_needed

log = get_logger("kg.context.retrieve")
STAGES = ("graph_ingest", "memory_propose")


@dataclass
class SourceStatus:
    source: str
    ok: bool = True
    reason: str | None = None
    seconds: float = 0.0
    candidates: int = 0  # AUTHORISED candidates only


@dataclass
class PgResult:
    facts: list[FactOut] = field(default_factory=list)
    review_links: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)
    held: set[tuple[uuid.UUID, str]] = field(default_factory=set)
    memory_conflicts: list[dict[str, Any]] = field(default_factory=list)
    cautions: list[dict[str, Any]] = field(
        default_factory=list
    )  # P7: memories quarantined because their source was compromised, as the viewer may see them
    lag: dict[str, dict[str, Any]] = field(default_factory=dict)
    db_now: dt.datetime | None = None


@dataclass
class GraphResult:
    facts: list[FactOut] = field(default_factory=list)
    unreconciled: int = 0


@dataclass
class MemoryResult:
    hits: list[SearchHit] = field(default_factory=list)
    info: dict[uuid.UUID, ContextInfo] = field(default_factory=dict)
    considered: int = 0
    degraded: list[str] = field(default_factory=list)
    embedding_version: int = 0
    ranker_version: str = ""
    standing: set[uuid.UUID] = field(default_factory=set)  # members of the working set that the TASK did not find (see Retrievers.memory)


async def guarded[T](source: str, coro: Awaitable[T], timeout_ms: int, *, fatal: bool = False) -> tuple[T | None, SourceStatus]:
    """Runs one source under its timeout. Returns (result, status); a failure becomes an `unavailable` status unless `fatal` (Postgres), which re-raises."""
    started = time.perf_counter()
    status = SourceStatus(source)
    result: T | None = None
    try:
        result = await asyncio.wait_for(coro, timeout=timeout_ms / 1000.0)
    except TimeoutError:
        if fatal:  # Postgres is required: without it there is no honest answer
            raise DependencyUnavailable("The facts store did not answer in time") from None
        status.ok, status.reason = False, "timeout"
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        if fatal:
            log.error("context_required_source_failed", source=source, error_type=type(exc).__name__)
            raise DependencyUnavailable("The facts store is unavailable") from exc
        status.ok, status.reason = False, "unavailable"
        log.warning("context_source_failed", source=source, error_type=type(exc).__name__)
    status.seconds = time.perf_counter() - started
    metrics.context_source_duration_seconds.labels(source=source).observe(status.seconds)
    if not status.ok:
        metrics.context_source_failures_total.labels(source=source, reason=status.reason or "unavailable").inc()
    return result, status


class Retrievers:
    def __init__(self, engine: AsyncEngine, knowledge: KnowledgeServices, memory: MemoryServices):
        self._engine, self._knowledge, self._memory = engine, knowledge, memory

    # ---- Postgres -------------------------------------------------------------------------------------------------------------------

    async def postgres(
        self,
        principal: Principal,
        viewer: Viewer,
        plan: Plan,
        subjects: list[SubjectInfo],
        *,
        can_see_sensitive: bool,
        subjects_filter: SubjectFilter,
        signals: tuple[Any, ...],
    ) -> PgResult:
        ctx = WorkspaceContext.of(principal)
        out = PgResult()
        reads = self._knowledge.reads
        async with tenant_session(self._engine, ctx, principal.principal_id) as conn:
            out.db_now = (await conn.execute(text("SELECT now() AS n"))).one().n
            seen: dict[uuid.UUID, FactOut] = {}
            for s in subjects:
                for f in await reads.list_facts(
                    conn,
                    ctx,
                    entity_id=uuid.UUID(s.id),
                    direction="both",
                    rel_type=None,
                    as_of=plan.as_of,
                    known_at=plan.known_at,
                    include_history=plan.history,
                    can_see_sensitive=can_see_sensitive,
                    subjects=subjects_filter,
                    include=("proposed", "disputed"),
                ):
                    seen.setdefault(f.id, f)
            out.facts = list(seen.values())
            out.review_links = await reads.open_review_links(conn, ctx, [f.id for f in out.facts if f.status in ("proposed", "disputed")])
            ids = [uuid.UUID(s.id) for s in subjects]
            preds = predicates_needed(signals)
            if preds:
                out.held = await self._memory.reads.assertion_keys(conn, viewer, ids, preds, as_of=plan.as_of)
            if "uncertainty" in plan.sections:
                out.memory_conflicts = await self._memory.reads.open_conflicts(conn, viewer, ids)
                out.cautions = await self._memory.reads.compromised_cautions(conn, viewer, ids)
            out.lag = await stage_lag(conn, ctx.workspace_id, STAGES)
        return out

    # ---- graph ----------------------------------------------------------------------------------------------------------------------

    async def graph(
        self,
        principal: Principal,
        plan: Plan,
        subjects: list[SubjectInfo],
        *,
        qvec: list[float],
        classification: str,
        can_see_sensitive: bool,
        subjects_filter: SubjectFilter,
    ) -> GraphResult:
        ctx = WorkspaceContext.of(principal)
        body = GraphSearchIn(
            query=plan.query[:500],
            as_of=plan.as_of,
            known_at=plan.known_at,
            center_entity_id=uuid.UUID(subjects[0].id) if subjects else None,
            limit=plan.profile.graph_limit,
        )
        async with tenant_session(self._engine, ctx, principal.principal_id) as conn:
            try:
                found = await self._knowledge.search.run(
                    conn,
                    ctx,
                    body,
                    can_see_sensitive=can_see_sensitive,
                    subjects=subjects_filter,
                    classification=classification,
                    query_vector=qvec,
                )
            except NotFound:
                # the subject has not reached the graph yet (its projection is pending): search the workspace's graph without a centre rather than call the graph down.
                # Its own facts still come from Postgres, which is authoritative.
                found = await self._knowledge.search.run(
                    conn,
                    ctx,
                    body.model_copy(update={"center_entity_id": None}),
                    can_see_sensitive=can_see_sensitive,
                    subjects=subjects_filter,
                    classification=classification,
                    query_vector=qvec,
                )
        return GraphResult(found.facts, found.unreconciled_hits)

    # ---- memory ---------------------------------------------------------------------------------------------------------------------

    async def memory(
        self,
        principal: Principal,
        viewer: Viewer,
        plan: Plan,
        subjects: list[SubjectInfo],
        *,
        qvec: list[float] | None,
        project: str | None,
        run_id: str | None,
    ) -> MemoryResult:
        ctx = WorkspaceContext.of(principal)
        out = MemoryResult()
        ids = [uuid.UUID(s.id) for s in subjects]
        limit = plan.profile.limit_per_type
        best: dict[uuid.UUID, SearchHit] = {}
        async with tenant_session(self._engine, ctx, principal.principal_id) as conn:
            for mtype in plan.memory_types:
                if mtype == "working" and not run_id:
                    continue  # working memory is a run's own; without a run there is nothing to ask for
                filters: list[dict[str, Any]] = []
                if mtype == "working" or not ids:
                    filters.append({})
                else:
                    filters.append({"subject_ids": ids})
                if project and mtype != "working":
                    filters.append({"project": project})
                for extra in filters:
                    body = SearchIn(
                        query=plan.memory_query[:1000],
                        types=[mtype],
                        run_id=run_id if mtype == "working" else None,
                        as_of=plan.as_of,
                        limit=limit,
                        **extra,
                    )
                    found = await self._memory.search.run(conn, viewer, body, embed=False, query_vector=qvec)
                    out.considered += found.candidates_considered
                    out.degraded = sorted(set(out.degraded) | set(found.degraded))
                    out.embedding_version, out.ranker_version = found.embedding_version, found.ranker_version
                    for h in found.hits:
                        if h.memory_id not in best or h.score > best[h.memory_id].score:
                            best[h.memory_id] = h
            out.info = {}
            if plan.as_of is None:
                await self._standing(conn, viewer, plan, ids, project, best, out)
            out.hits = sorted(best.values(), key=lambda h: (-h.score, str(h.memory_id)))
            out.info = {
                **await self._memory.reads.context_info(conn, viewer, [h.memory_id for h in out.hits if h.memory_id not in out.info]),
                **out.info,
            }
        out.hits = [h for h in out.hits if h.memory_id in out.info]  # a hit the viewer cannot re-read is dropped (it cannot be cited)
        return out

    async def _standing(
        self,
        conn: Any,
        viewer: Viewer,
        plan: Plan,
        subject_ids: list[uuid.UUID],
        project: str | None,
        best: dict[uuid.UUID, SearchHit],
        out: MemoryResult,
    ) -> None:
        """The working set a task does not have to find (blueprint packing priority 3 and 4): a subject's approved GUIDANCE, and, when the caller names a project, that
        project's shared memory. They come from the authorised list (the same visibility predicate as every memory read), are marked `standing`, and then compete
        like any other candidate. Nothing here widens what the caller may see."""
        now = (await conn.execute(text("SELECT now() AS n"))).one().n
        types = [t for t in plan.memory_types if t != "working"]
        plans: list[tuple[str, dict[str, Any]]] = []
        if "procedural" in types:
            plans += [("procedural", {"subject_id": sid}) for sid in subject_ids]
            if project:
                plans.append(("procedural", {"project": project}))
        if project:
            plans += [(t, {"project": project}) for t in types if t != "procedural"]
        limit = plan.profile.limit_per_type
        for mtype, flt in plans:
            page = await self._memory.reads.list(
                conn,
                viewer,
                type_=mtype,
                status="active",
                subject_id=flt.get("subject_id"),
                project=flt.get("project"),
                origin=None,
                classification=None,
                created_by=None,
                updated_since=None,
                limit=limit,
                cursor=None,
            )
            for m in page.items:
                if (
                    (m.expires_at is not None and m.expires_at <= now)
                    or (m.effective_until is not None and m.effective_until <= now)
                    or m.effective_from > now
                ):
                    continue
                out.standing.add(m.id)
                if m.id in best:
                    continue
                full = await self._memory.reads.get(conn, viewer, m.id)
                hit, info = hit_from_memory(full, 0.0)
                best[m.id] = hit
                out.info[m.id] = info
