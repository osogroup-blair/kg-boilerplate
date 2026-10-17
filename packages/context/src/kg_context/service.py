"""ContextService.build: the ONLY way a context is made (phase-6 plan §5.3). The public API and the Context Lab both call it, with the same authorisation.

    1 authorise   grants, effective profile (a request can only narrow it), visible subjects, the egress classification of the task. Nothing is retrieved before this.
    2 plan        pure, no model
    3 retrieve    Postgres (facts, conflicts, signals, watermarks) in parallel with the single query embedding, then graph and memory in parallel, each under its own timeout
    4 compose     candidates -> temporal reconciliation -> evidence / relevance gates -> merge -> unknowns -> rank (ctx-rank.v1) -> tiers
    5 pack        a hard budget enforced on the exact rendered bytes; the required tier is never trimmed
    6 status      complete | partial | insufficient_evidence | insufficient_budget (denied was decided in step 1)
    7 trace       metadata only; a failed trace write never fails the build

A source that fails makes the package `partial` and names it; with `allow_partial=false` it is a 503 and no package. Postgres is required: without it there is no answer."""

from __future__ import annotations

import asyncio
import datetime as dt
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any

from kg_common.errors import DependencyUnavailable, NotFound
from kg_common.ids import uuid7, uuid7_str
from kg_common.settings import Settings
from kg_common.tokens import TokenCounter
from kg_db.session import tenant_session
from kg_domain.enums import Role
from kg_domain.principal import Principal, WorkspaceContext
from kg_governance.audit import AuditMetadata, AuditWriter
from kg_governance.authz.policy import Action, is_allowed
from kg_governance.grants import SubjectFilter
from kg_knowledge.services import KnowledgeServices
from kg_memory.models import CLASSIFICATION_ORDER
from kg_memory.reads import Viewer
from kg_memory.scopes import ScopeGrants
from kg_memory.services import MemoryServices
from kg_modelgw.config import ModelConfig
from kg_modelgw.gateway import ModelGateway
from kg_obs import context as obs_context
from kg_obs import metrics
from kg_obs.logging import get_logger
from kg_ontology.spec import ContextProfile, OntologySpec
from sqlalchemy import text

from kg_context import compress
from kg_context.candidates import (
    SubjectInfo,
    conflict_candidate,
    fact_candidate,
    fact_evidence,
    fact_statement,
    hit_from_memory,
    memory_candidate,
    notice_unknown,
    phase,
)
from kg_context.errors import ContextIncomplete, FeatureDisabled, ProfileNotAllowed
from kg_context.gates import apply_gates
from kg_context.merge import dedupe, merge_facts
from kg_context.models import (
    PIPELINE_VERSION,
    RANKER_VERSION,
    SECTION_RANK,
    BudgetsOut,
    Candidate,
    ContextBuildIn,
    ContextOut,
    Evidence,
    FreshnessOut,
    ProfileOut,
    UnavailableSourceOut,
    denied,
)
from kg_context.output import exclusion_counts, fill
from kg_context.pack import PackResult, pack
from kg_context.planner import Plan, make_plan
from kg_context.profiles import EffectiveProfile, builtin_profiles, narrow, resolve
from kg_context.rank import assign_tiers, plain_score, rank
from kg_context.render import HandleBook, assign_handles
from kg_context.retrieve import GraphResult, MemoryResult, PgResult, Retrievers, SourceStatus, guarded
from kg_context.temporal import reconcile
from kg_context.text import terms
from kg_context.trace import TraceReads, TraceStore, item_ref
from kg_context.unknowns import unknowns_for

log = get_logger("kg.context")


@dataclass
class Resolved:
    """Everything step 1 decides: who is asking, with what, about whom."""

    ctx: WorkspaceContext
    viewer: Viewer
    ceiling: str
    profile: EffectiveProfile
    max_tokens: int
    max_items: int
    ontology_version_id: uuid.UUID | None
    spec: OntologySpec | None
    subjects: list[SubjectInfo]
    classification: str  # of the task: what the egress policy judges
    can_see_sensitive: bool
    subjects_filter: SubjectFilter
    disclosure_denied: bool  # P7: the principal's content may not go to external models (no query embedding, no summarised compression)


@dataclass
class Composed:
    candidates: list[Candidate]  # every AUTHORISED candidate (kept or excluded), in rank order for the live ones
    live: list[Candidate]
    book: HandleBook
    facts_seen: int
    memories_seen: int


def _max_class(*values: str | None) -> str:
    return max((v for v in values if v), key=CLASSIFICATION_ORDER.index, default="internal")


def _within(ceiling: str, classification: str) -> bool:
    return CLASSIFICATION_ORDER.index(classification) <= CLASSIFICATION_ORDER.index(ceiling)


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, dt.datetime) else value


class ContextService:
    def __init__(
        self,
        settings: Settings,
        engine: Any,
        knowledge: KnowledgeServices,
        memory: MemoryServices,
        gateway: ModelGateway | None,
        config: ModelConfig,
        store: TraceStore,
        *,
        service: str,
    ):
        self._s, self._engine, self._knowledge, self._memory, self._gateway, self._config, self._store = (
            settings,
            engine,
            knowledge,
            memory,
            gateway,
            config,
            store,
        )
        self._service = service
        self._retrievers = Retrievers(engine, knowledge, memory)
        self._counter = TokenCounter(margin=settings.context_token_margin)
        self.reads = TraceReads()

    @property
    def counter(self) -> TokenCounter:
        return self._counter

    # ---- 1: authorise ---------------------------------------------------------------------------------------------------------------

    async def _resolve(self, principal: Principal, request: ContextBuildIn) -> Resolved | None:
        """None means `denied`. A bad profile or a widening request is a 422 (it says nothing about data)."""
        ctx = WorkspaceContext.of(principal)
        sensitive = is_allowed(principal, Action.ENTITY_READ_SENSITIVE)
        async with tenant_session(self._engine, ctx, principal.principal_id) as conn:
            grants = await ScopeGrants.for_principal(conn, principal)
            sfilter = await self._knowledge.reads.subject_filter(conn, ctx.workspace_id, grants)
            viewer = Viewer(principal, grants, curator=False, subjects=sfilter)
            active = await self._knowledge.specs.active(conn, ctx.workspace_id)
            version_id, spec = (active[0], active[1]) if active else (None, None)
            reg = (
                await conn.execute(
                    text("SELECT allowed_profiles, model_disclosure FROM principals WHERE workspace_id = :ws AND id = :p"),
                    {"ws": ctx.workspace_id, "p": principal.principal_id},
                )
            ).first()
            allowed = list(reg.allowed_profiles) if reg is not None else []
            if allowed and request.profile not in allowed:
                raise ProfileNotAllowed()  # P7: the principal's registration limits which profiles it may build (a Lab build is judged by the TARGET's registration)
            profile = resolve(request.profile, spec.context_profiles if spec else None, ontology_version_id=version_id)
            profile, max_tokens, max_items = narrow(profile, request)
            entities = await self._knowledge.reads.entities_by_ids(
                conn, ctx, request.subject_ids, can_see_sensitive=sensitive, subjects=sfilter
            )
            seen = [e for e in entities if _within(grants.ceiling, e.classification)]
            if request.subject_ids and not seen:
                return None
            ids = [e.id for e in seen]
            fact_class = await self._knowledge.reads.max_fact_classification(conn, ctx, ids, subjects=sfilter)
        subjects = [SubjectInfo(str(e.id), e.type, e.name, e.attributes, e.classification) for e in seen]
        classification = _max_class(request.sensitivity or "internal", fact_class, *(e.classification for e in seen))
        denied_disclosure = reg is not None and reg.model_disclosure == "denied"
        return Resolved(
            ctx,
            viewer,
            grants.ceiling,
            profile,
            max_tokens,
            max_items,
            version_id,
            spec,
            subjects,
            classification,
            sensitive,
            sfilter,
            denied_disclosure,
        )

    # ---- 3: retrieve ----------------------------------------------------------------------------------------------------------------

    async def _retrieve(
        self, principal: Principal, request: ContextBuildIn, r: Resolved, plan: Plan
    ) -> tuple[PgResult, GraphResult | None, MemoryResult | None, list[SourceStatus], list[UnavailableSourceOut]]:
        timeout = self._s.context_source_timeout_ms
        unavailable: list[UnavailableSourceOut] = []
        statuses: list[SourceStatus] = []
        pg_task = asyncio.create_task(
            guarded(
                "postgres",
                self._retrievers.postgres(
                    principal,
                    r.viewer,
                    plan,
                    r.subjects,
                    can_see_sensitive=r.can_see_sensitive,
                    subjects_filter=r.subjects_filter,
                    signals=r.profile.expected_signals,
                ),
                timeout,
                fatal=True,
            )
        )
        tasks: list[asyncio.Task[Any]] = [pg_task]
        try:
            qvec: list[float] | None = None
            embed_status = SourceStatus("embedding")
            if plan.use_graph or plan.use_memory:
                if r.classification == "local_only":
                    embed_status.ok, embed_status.reason = False, "egress_policy"
                elif r.disclosure_denied:
                    embed_status.ok, embed_status.reason = (
                        False,
                        "model_disclosure_denied",
                    )  # the task is content this principal may not send to an external model
                else:
                    qvec, embed_status = await guarded(
                        "embedding", self._memory.search.embed_query(r.viewer, plan.query, classification=r.classification), timeout
                    )
                    if qvec is None and embed_status.ok:
                        embed_status.ok, embed_status.reason = False, "vector_unavailable"
                statuses.append(embed_status)
            graph_task = mem_task = None
            if plan.use_graph:
                if qvec is not None:
                    graph_task = asyncio.create_task(
                        guarded(
                            "graph",
                            self._retrievers.graph(
                                principal,
                                plan,
                                r.subjects,
                                qvec=qvec,
                                classification=r.classification,
                                can_see_sensitive=r.can_see_sensitive,
                                subjects_filter=r.subjects_filter,
                            ),
                            timeout,
                        )
                    )
                    tasks.append(graph_task)
                else:
                    unavailable.append(
                        UnavailableSourceOut(
                            source="graph",
                            reason=embed_status.reason
                            if embed_status.reason in ("egress_policy", "model_disclosure_denied")
                            else "embedding_unavailable",
                        )
                    )
            if plan.use_memory:
                mem_task = asyncio.create_task(
                    guarded(
                        "memory",
                        self._retrievers.memory(
                            principal, r.viewer, plan, r.subjects, qvec=qvec, project=request.project, run_id=request.run_id
                        ),
                        timeout,
                    )
                )
                tasks.append(mem_task)
                if qvec is None:
                    unavailable.append(UnavailableSourceOut(source="memory_vector", reason=embed_status.reason or "vector_unavailable"))
            pg, pg_status = await pg_task
            statuses.append(pg_status)
            graph_res = mem_res = None
            if graph_task is not None:
                graph_res, gs = await graph_task
                statuses.append(gs)
                if not gs.ok:
                    unavailable.append(UnavailableSourceOut(source="graph", reason=gs.reason or "unavailable"))
            if mem_task is not None:
                mem_res, ms = await mem_task
                statuses.append(ms)
                if not ms.ok:
                    unavailable.append(UnavailableSourceOut(source="memory", reason=ms.reason or "unavailable"))
        except BaseException:
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        assert pg is not None
        return pg, graph_res, mem_res, statuses, unavailable

    # ---- 4: compose -----------------------------------------------------------------------------------------------------------------

    @staticmethod
    def _templates(spec: OntologySpec | None) -> dict[str, str | None]:
        return {name: rel.fact_template for name, rel in spec.relationship_types.items()} if spec else {}

    def _description_terms(self, spec: OntologySpec | None) -> dict[str, set[str]]:
        if spec is None:
            return {}
        return {name: set(terms(f"{name.replace('_', ' ')} {rel.description}")) for name, rel in spec.relationship_types.items()}

    @staticmethod
    def _signal_terms(r: Resolved, c: Candidate) -> set[str]:
        """The words an unknown stands for (its signal's key and label), so `No verified current employer` is material to "where does she work"."""
        if c.kind != "unknown":
            return set()
        key = c.key.split(":")[1] if c.key.count(":") >= 2 else ""
        out = set(terms(key.replace("_", " ")))
        for sig in r.profile.expected_signals:
            if sig.key == key:
                out |= set(terms(sig.label))
        return out

    def _conflicts(self, pg: PgResult, requested: list[str], ref: dt.datetime, plan: Plan, tmpl: dict[str, str | None]) -> list[Candidate]:
        by_source_rel = {
            (str(f.source.id), f.rel_type): f
            for f in pg.facts
            if phase(f, ref, plan.known_at) == "current" and f.evidence_status == "verified"
        }
        out: list[Candidate] = []
        for f in pg.facts:
            ph = phase(f, ref, plan.known_at)
            if ph not in ("proposed", "disputed"):
                continue
            if (plan.known_at is not None and f.recorded_at > plan.known_at) or (f.valid_from is not None and f.valid_from > ref):
                continue  # a claim not yet recorded at known_at, or not yet in force at as_of, is not part of the picture being asked for
            statement = fact_statement(f, tmpl.get(f.rel_type))
            if ph == "proposed":
                current = by_source_rel.get((str(f.source.id), f.rel_type))
                tail = (
                    f"; {fact_statement(current, tmpl.get(current.rel_type))} remains current"
                    if current is not None and current.id != f.id
                    else ""
                )
                text = f"Unverified claim under review: {statement}. Not confirmed{tail}."
            else:
                text = f"Disputed: {statement}. Not settled."
            review = pg.review_links.get(f.id)
            owners = tuple(s for s in requested if s in (str(f.source.id), str(f.target.id)))
            out.append(
                conflict_candidate(
                    f"conflict:{f.id}",
                    text,
                    fact_id=str(f.id),
                    review_id=str(review) if review else None,
                    evidence=fact_evidence(f)[:1],
                    subject_ids=owners,
                )
            )
        for q in pg.cautions:
            out.append(
                conflict_candidate(
                    f"conflict:compromised:{q['memory_id']}",
                    f"Caution: {q['summary']} Its source was later found compromised; not verified.",
                    fact_id=None,
                    review_id=str(q["review_id"]),
                    evidence=[Evidence("memory", str(q["memory_id"]))],
                    subject_ids=tuple(str(s) for s in q["subject_ids"]),
                    memory_id=str(q["memory_id"]),
                )
            )
        for c in pg.memory_conflicts:
            a, b = c["candidate"], c["competing"]
            out.append(
                conflict_candidate(
                    f"conflict:memory:{c['review_id']}",
                    f"Conflicting memories under review: {a['summary']} versus {b['summary']}.",
                    fact_id=None,
                    review_id=str(c["review_id"]),
                    evidence=[Evidence("memory", str(a["id"])), Evidence("memory", str(b["id"]))],
                    subject_ids=tuple(str(s) for s in a["subject_ids"]),
                    memory_id=str(a["id"]),
                )
            )
        return out

    def _compose(
        self,
        request: ContextBuildIn,
        r: Resolved,
        plan: Plan,
        pg: PgResult,
        graph: GraphResult | None,
        mem: MemoryResult | None,
        ref: dt.datetime,
    ) -> Composed:
        requested = [s.id for s in r.subjects]
        desc = self._description_terms(r.spec)
        tmpl = self._templates(r.spec)
        facts: list[Candidate] = []
        for f in pg.facts:
            if not _within(r.ceiling, f.classification) or phase(f, ref, plan.known_at) in ("proposed", "disputed"):
                continue
            facts.append(
                fact_candidate(
                    f,
                    plan=plan,
                    requested=requested,
                    ref=ref,
                    description_terms=desc.get(f.rel_type, set()),
                    source="facts",
                    template=tmpl.get(f.rel_type),
                )
            )
        if graph is not None:
            for i, f in enumerate(graph.facts):
                if not _within(r.ceiling, f.classification) or phase(f, ref, plan.known_at) in ("proposed", "disputed"):
                    continue
                facts.append(
                    fact_candidate(
                        f,
                        plan=plan,
                        requested=requested,
                        ref=ref,
                        description_terms=desc.get(f.rel_type, set()),
                        source="graph",
                        rank_hint=1.0 / (1.0 + i),
                        template=tmpl.get(f.rel_type),
                    )
                )
        facts = merge_facts(facts)
        memories: list[Candidate] = []
        if mem is not None and mem.hits:
            best = max((h.score for h in mem.hits), default=0.0)
            memories = [
                memory_candidate(
                    h,
                    mem.info.get(h.memory_id),
                    plan=plan,
                    requested=requested,
                    ref=ref,
                    best_score=best,
                    standing=h.memory_id in mem.standing,
                )
                for h in mem.hits
            ]
        conflicts = self._conflicts(pg, requested, ref, plan, tmpl) if "uncertainty" in plan.sections else []
        unknown = unknowns_for(r.profile.expected_signals, r.subjects, facts, pg.held) if "uncertainty" in plan.sections else []
        candidates = [*facts, *memories, *conflicts, *unknown]

        reconcile(candidates, history=plan.history)
        apply_gates(candidates, profile=r.profile, plan=plan, signal_terms=lambda c: self._signal_terms(r, c))
        dedupe(candidates)
        live = [c for c in candidates if c.exclusion is None]
        if not [c for c in live if c.kind in ("fact", "memory")]:
            live.append(notice_unknown("No authorized records were found for this task"))
            candidates.append(live[-1])
        ranked = rank(live, r.profile, ablate=self._s.context_ablate or None)
        assign_tiers(ranked, r.profile)
        for c in candidates:
            if c.exclusion is not None:
                plain_score(c, r.profile)
        book = assign_handles(ranked)
        order = {c.key: i for i, c in enumerate(ranked)}
        out = sorted(candidates, key=lambda c: (c.exclusion is not None, order.get(c.key, 10**9), SECTION_RANK.get(c.section, 99), c.key))
        return Composed(out, ranked, book, len(facts), len(memories))

    # ---- 6: status ------------------------------------------------------------------------------------------------------------------

    @staticmethod
    def _has_evidence(selected: list[Candidate]) -> bool:
        return any(c.kind in ("fact", "memory") for c in selected)

    def _freshness(
        self, request: ContextBuildIn, pg: PgResult, plan: Plan
    ) -> tuple[FreshnessOut, dict[str, Any], list[UnavailableSourceOut]]:
        graph, mem = pg.lag.get("graph_ingest", {}), pg.lag.get("memory_propose", {})
        limit = request.freshness_requirement.max_lag_seconds if request.freshness_requirement else None
        stale: list[UnavailableSourceOut] = []

        def over(stage: dict[str, Any]) -> bool:
            return limit is not None and ((stage.get("waiting_age_seconds") or 0.0) > limit or int(stage.get("dead") or 0) > 0)

        if plan.use_graph and over(graph):
            stale.append(UnavailableSourceOut(source="graph", reason="stale"))
        if plan.use_memory and over(mem):
            stale.append(UnavailableSourceOut(source="memory", reason="stale"))
        marks = {name: {k: _iso(v) for k, v in stage.items()} for name, stage in pg.lag.items()}
        fresh = FreshnessOut(
            max_lag_seconds=limit,
            graph_lag_seconds=graph.get("waiting_age_seconds"),
            memory_lag_seconds=mem.get("waiting_age_seconds"),
            stale=bool(stale),
        )
        return fresh, marks, stale

    # ---- build ----------------------------------------------------------------------------------------------------------------------

    async def build(self, principal: Principal, request: ContextBuildIn, *, act_as_by: Principal | None = None) -> ContextOut:
        if not self._s.context_api_enabled:
            raise FeatureDisabled()
        started = time.perf_counter()
        now = dt.datetime.now(dt.UTC)
        r = await self._resolve(principal, request)
        if r is None:
            metrics.context_builds_total.labels(profile="denied", status="denied", compression="none").inc()
            return denied(now)
        t_auth = time.perf_counter() - started
        plan = make_plan(request, r.profile, [s.name for s in r.subjects])
        pg, graph, mem, statuses, unavailable = await self._retrieve(principal, request, r, plan)
        assert pg.db_now is not None
        ref = plan.as_of or pg.db_now
        fresh, marks, stale = self._freshness(request, pg, plan)
        unavailable = [*unavailable, *stale]
        if unavailable and not request.allow_partial:
            raise ContextIncomplete([u.model_dump() for u in unavailable])
        t_sources = time.perf_counter() - started - t_auth
        composed = self._compose(request, r, plan, pg, graph, mem, ref)
        profile = r.profile
        result = pack(
            composed.live,
            counter=self._counter,
            max_tokens=r.max_tokens,
            max_items=r.max_items,
            mode="extractive" if profile.compression_mode == "extractive" else "structured",
            response_mode=request.response_mode,
            item_chars=profile.item_chars,
            section_budget=profile.section_budget,
            as_of=plan.as_of,
        )
        mode_used = "extractive" if profile.compression_mode == "extractive" else "structured"
        fallback: str | None = None
        text = result.text
        used = result.used_tokens
        if profile.compression_mode == "summarized" and r.disclosure_denied:
            fallback = "model_disclosure_denied"  # structured instead: the text would go to an external model
            metrics.context_compression_fallbacks_total.labels(reason=fallback).inc()
        elif profile.compression_mode == "summarized" and result.ok and request.response_mode != "items" and self._gateway is not None:
            summary = await compress.summarize(
                self._gateway,
                self._config,
                structured_text=result.text,
                packed=result.selected,
                mode="structured",
                item_chars=profile.item_chars,
                counter=self._counter,
                max_tokens=r.max_tokens,
                classification=r.classification,
                workspace_id=principal.workspace_id,
                principal_id=principal.principal_id,
                correlation_id=obs_context.correlation_id_var.get() or uuid7_str(),
                service=self._service,
            )
            if summary.text is not None:
                text, used, mode_used = summary.text, self._counter.count(summary.text), "summarized"
            else:
                fallback = summary.reason or "unknown"
                metrics.context_compression_fallbacks_total.labels(reason=fallback).inc()
        elif profile.compression_mode == "summarized" and result.ok:
            fallback = "not_applicable"
        if used > r.max_tokens or self._counter.count(text) > r.max_tokens:  # CTX-01 must never be true; if it ever is, loudly
            metrics.context_budget_violations_total.inc()
            log.error("context_budget_violation", profile=profile.name)
            raise DependencyUnavailable("The context could not be fitted to its budget")

        # ---- status (§5.8)
        evidence = self._has_evidence(result.selected)
        if not result.ok:
            status = "insufficient_budget"
        elif unavailable:
            status = "partial"
        elif not evidence:
            status = "insufficient_evidence"
        else:
            status = "complete"
        context_id = uuid7()
        out = ContextOut(
            context_id=context_id,
            status=status,
            generated_at=now,
            workspace_id=principal.workspace_id,
            agent_id=principal.principal_id,
            profile=ProfileOut(name=profile.name, source=profile.source, ontology_version_id=profile.ontology_version_id),
            compression=mode_used,
            context_text=text,
            budgets=BudgetsOut(
                max_tokens=r.max_tokens,
                used_tokens=used,
                reserved_tokens=result.reserved_tokens,
                required_tokens=result.required_tokens,
                max_items=r.max_items,
                strategy=f"tiered:{RANKER_VERSION}",
                tokenizer_id=self._counter.tokenizer_id,
                truncated=result.truncated,
            ),
            unavailable_sources=unavailable,
            freshness=fresh,
            source_watermarks=marks,
            excluded_source_counts=exclusion_counts(composed.candidates),
            versions=self._versions(mem, mode_used, fallback),
        )
        fill(out, result.selected, composed.book)
        if request.response_mode == "text":
            out.facts = out.memories = out.instructions = out.unknowns = out.conflicts = []
        elif request.response_mode == "items":
            out.context_text = ""
        # ---- 7: trace (a failure here never fails the build)
        latencies = {
            "authorize": round(t_auth, 4),
            "sources": round(t_sources, 4),
            "per_source": {s.source: round(s.seconds, 4) for s in statuses},
            "total": round(time.perf_counter() - started, 4),
        }
        out.retrieval_trace_id = await self._record(
            principal, act_as_by, request, r, plan, composed, result, out, latencies, text, status, mem, graph, pg, statuses, context_id
        )
        if act_as_by is not None:
            await self._audit_lab(act_as_by, principal, out)
        metrics.context_builds_total.labels(profile=profile.name, status=status, compression=mode_used).inc()
        metrics.context_build_duration_seconds.labels(profile=profile.name).observe(time.perf_counter() - started)
        metrics.context_tokens_used.labels(profile=profile.name).observe(used)
        for c in result.selected:
            metrics.context_items_selected.labels(kind=c.kind).inc()
        return out

    def _versions(self, mem: MemoryResult | None, mode: str, fallback: str | None) -> dict[str, str]:
        v = {
            "ranker": RANKER_VERSION,
            "tokenizer": self._counter.tokenizer_id,
            "compression": f"{mode}:{compress.COMPRESSION_VERSION}" if mode == "summarized" else mode,
            "pipeline": PIPELINE_VERSION,
        }
        if mem is not None and mem.ranker_version:
            v["memory_ranker"], v["embedding"] = mem.ranker_version, f"v{mem.embedding_version}"
        if fallback:
            v["compression_fallback"] = fallback
        return v

    async def _record(
        self,
        principal: Principal,
        act_as_by: Principal | None,
        request: ContextBuildIn,
        r: Resolved,
        plan: Plan,
        composed: Composed,
        result: PackResult,
        out: ContextOut,
        latencies: dict[str, Any],
        text: str,
        status: str,
        mem: MemoryResult | None,
        graph: GraphResult | None,
        pg: PgResult,
        statuses: list[SourceStatus],
        context_id: uuid.UUID,
    ) -> uuid.UUID | None:
        by_source: dict[str, int] = {}
        for c in composed.candidates:
            by_source[c.source or "derived"] = by_source.get(c.source or "derived", 0) + 1
        counts = {
            "candidates_by_source": by_source,
            "memory_candidates_considered": mem.considered if mem else 0,
            "graph_unreconciled_hits": graph.unreconciled if graph else 0,
            "exclusions": exclusion_counts(composed.candidates),
            "selected": len(result.selected),
        }
        req = {
            "subject_ids": [s.id for s in r.subjects],
            "project": request.project,
            "as_of": _iso(request.as_of),
            "known_at": _iso(request.known_at),
            "max_tokens": r.max_tokens,
            "max_items": r.max_items,
            "include": sorted(r.profile.include),
            "allow_partial": request.allow_partial,
            "response_mode": request.response_mode,
            "freshness_requirement": request.freshness_requirement.model_dump() if request.freshness_requirement else None,
            "has_run_id": request.run_id is not None,
            "sensitivity": request.sensitivity,
        }
        try:
            async with tenant_session(self._engine, r.ctx, principal.principal_id) as conn:
                await self._store.write(
                    conn,
                    context_id=context_id,
                    workspace_id=principal.workspace_id,
                    principal_id=principal.principal_id,
                    act_as_by=act_as_by.principal_id if act_as_by else None,
                    profile=r.profile.name,
                    profile_source=r.profile.source,
                    ontology_version_id=r.ontology_version_id,
                    status=status,
                    compression=out.compression or "structured",
                    task=request.task,
                    query=plan.query,
                    task_tokens=self._counter.count(request.task),
                    request=req,
                    plan=plan.shape(),
                    versions=out.versions,
                    counts=counts,
                    unavailable=[u.model_dump() for u in out.unavailable_sources],
                    watermarks=out.source_watermarks,
                    latencies=latencies,
                    max_tokens=r.max_tokens,
                    used_tokens=out.budgets.used_tokens if out.budgets else 0,
                    required_tokens=result.required_tokens,
                    candidates=composed.candidates,
                )
                if request.debug_snapshot and is_allowed(act_as_by or principal, Action.CONTEXT_DEBUG):
                    admin = act_as_by or principal
                    expires = await self._store.write_debug(
                        conn,
                        workspace_id=principal.workspace_id,
                        context_id=context_id,
                        created_by=admin.principal_id,
                        payload={"task": request.task, "query": plan.query, "context_text": text},
                        ttl_hours=self._s.context_debug_ttl_hours,
                    )
                    await AuditWriter(
                        conn, admin, request_id=obs_context.request_id_var.get(), correlation_id=obs_context.correlation_id_var.get()
                    ).record(
                        "context.debug_snapshot_created",
                        resource_type="context",
                        resource_id=context_id,
                        metadata=AuditMetadata(context_id=context_id),
                    )
                    _ = expires
        except Exception as exc:  # the answer is already built; losing its explanation must not lose the answer
            metrics.context_trace_write_failures_total.inc()
            log.error("context_trace_write_failed", error_type=type(exc).__name__)
            return None
        return context_id

    async def _audit_lab(self, admin: Principal, target: Principal, out: ContextOut) -> None:
        async with tenant_session(self._engine, WorkspaceContext.of(admin), admin.principal_id) as conn:
            await AuditWriter(
                conn, admin, request_id=obs_context.request_id_var.get(), correlation_id=obs_context.correlation_id_var.get()
            ).record(
                "context.lab_build",
                resource_type="context",
                resource_id=out.context_id,
                metadata=AuditMetadata(
                    context_id=out.context_id, profile=out.profile.name if out.profile else None, target_principal_id=target.principal_id
                ),
            )

    # ---- re-materialisation (GET /v1/context/{id}) ----------------------------------------------------------------------------------

    def can_see_trace(self, principal: Principal, trace: Any) -> bool:
        return trace.principal_id == principal.principal_id or Role.WORKSPACE_ADMIN in principal.roles

    async def rematerialize(self, principal: Principal, context_id: uuid.UUID) -> ContextOut:
        """The package a past build chose, rebuilt from its stored item ids under the caller's CURRENT authorisation (ADR-034). Nothing is stored but ids, so an item that
        is no longer visible or no longer active (a memory superseded, a fact ended, a record deleted, a principal's grant removed) is OMITTED and counted in
        `withdrawn`; it never resurfaces from a copy. The text is regenerated, not stored. A trace the caller may not see is the same 404 as one that never existed."""
        ctx = WorkspaceContext.of(principal)
        sensitive = is_allowed(principal, Action.ENTITY_READ_SENSITIVE)
        async with tenant_session(self._engine, ctx, principal.principal_id) as conn:
            trace = await self.reads.get(conn, ctx.workspace_id, context_id)
            if not self.can_see_trace(principal, trace):
                raise NotFound()
            rows = [i for i in await self.reads.items(conn, ctx.workspace_id, context_id) if i.included]
            grants = await ScopeGrants.for_principal(conn, principal)
            sfilter = await self._knowledge.reads.subject_filter(conn, ctx.workspace_id, grants)
            viewer = Viewer(principal, grants, curator=False, subjects=sfilter)
            active = await self._knowledge.specs.active(conn, ctx.workspace_id)
            spec = active[1] if active else None
            subject_ids = [uuid.UUID(x) for x in trace.request.get("subject_ids", [])]
            entities = await self._knowledge.reads.entities_by_ids(conn, ctx, subject_ids, can_see_sensitive=sensitive, subjects=sfilter)
            owner_revoked = (
                await conn.execute(
                    text("SELECT status FROM principals WHERE workspace_id = :ws AND id = :p"),
                    {"ws": ctx.workspace_id, "p": trace.principal_id},
                )
            ).scalar_one_or_none() == "revoked"
        subjects = [
            SubjectInfo(str(e.id), e.type, e.name, e.attributes, e.classification)
            for e in entities
            if _within(grants.ceiling, e.classification)
        ]
        profile = resolve(trace.profile, spec.context_profiles if spec else None, ontology_version_id=trace.ontology_version_id)
        mode = trace.compression if trace.compression in ("structured", "extractive") else "structured"
        profile = replace(profile, include=frozenset(trace.request.get("include") or profile.include), compression_mode=mode)
        as_of = dt.datetime.fromisoformat(trace.request["as_of"]) if trace.request.get("as_of") else None
        known_at = dt.datetime.fromisoformat(trace.request["known_at"]) if trace.request.get("known_at") else None
        plan = make_plan(
            ContextBuildIn(task="re-materialise", as_of=as_of, known_at=known_at, profile=profile.name), profile, [s.name for s in subjects]
        )
        plan = replace(plan, history=bool(trace.plan.get("history")), sections=frozenset(trace.plan.get("sections") or plan.sections))
        pg = await self._retrievers.postgres(
            principal, viewer, plan, subjects, can_see_sensitive=sensitive, subjects_filter=sfilter, signals=profile.expected_signals
        )
        assert pg.db_now is not None
        ref = as_of or pg.db_now
        requested = [s.id for s in subjects]
        desc = self._description_terms(spec)
        tmpl = self._templates(spec)
        wanted = {(i.item_kind, i.item_id): i for i in rows}
        kept: list[Candidate] = []
        async with tenant_session(self._engine, ctx, principal.principal_id) as conn:
            for (kind, ident), item in wanted.items():
                if ident is None:
                    continue
                if kind == "fact":
                    try:
                        f = await self._knowledge.reads.get_fact(conn, ctx, uuid.UUID(ident), can_see_sensitive=sensitive, subjects=sfilter)
                    except NotFound:
                        continue
                    if not _within(grants.ceiling, f.classification):
                        continue
                    c = fact_candidate(
                        f,
                        plan=plan,
                        requested=requested,
                        ref=ref,
                        description_terms=desc.get(f.rel_type, set()),
                        source="facts",
                        template=tmpl.get(f.rel_type),
                    )
                    if (
                        c.section != item.section
                        or c.temporal in ("proposed", "disputed", "future", "none")
                        or (c.temporal == "historical" and not plan.history)
                    ):
                        continue
                elif kind == "memory":
                    try:
                        m = await self._memory.reads.get(conn, viewer, uuid.UUID(ident))
                    except NotFound:
                        continue
                    live = m.status == "active" and (m.expires_at is None or m.expires_at > pg.db_now)
                    if not live and not (
                        as_of is not None and m.effective_from <= as_of and (m.effective_until is None or m.effective_until > as_of)
                    ):
                        continue
                    hit, info = hit_from_memory(m, float(item.score))
                    c = memory_candidate(hit, info, plan=plan, requested=requested, ref=ref, best_score=max(float(item.score), 1e-9))
                else:
                    continue
                c.relevance = 1.0
                kept.append(c)
        derived = [
            *self._conflicts(pg, requested, ref, plan, tmpl),
            *unknowns_for(profile.expected_signals, subjects, [c for c in kept if c.kind == "fact"], pg.held),
        ]
        by_ref = {(c.kind, item_ref(c)): c for c in derived}
        for kind, ident in wanted:
            derived_one = by_ref.get((kind, ident)) if kind in ("unknown", "conflict") else None
            if derived_one is not None:
                kept.append(derived_one)
        position = {(i.item_kind, i.item_id): i for i in rows}
        for c in kept:
            item = position[(c.kind, item_ref(c))]
            c.tier, c.score, c.features = item.tier, float(item.score), dict(item.features)
        kept.sort(key=lambda c: position[(c.kind, item_ref(c))].position)
        book = assign_handles(kept)
        result = pack(
            kept,
            counter=self._counter,
            max_tokens=int(trace.max_tokens),
            max_items=int(trace.request.get("max_items") or len(kept) or 1),
            mode=mode,
            response_mode=trace.request.get("response_mode", "both"),
            item_chars=profile.item_chars,
            section_budget=profile.section_budget,
            as_of=as_of,
        )
        out = ContextOut(
            context_id=context_id,
            status="complete" if result.ok else "insufficient_budget",
            generated_at=dt.datetime.now(dt.UTC),
            workspace_id=principal.workspace_id,
            agent_id=trace.principal_id,
            profile=ProfileOut(name=profile.name, source=profile.source, ontology_version_id=profile.ontology_version_id),
            compression=mode,
            context_text=result.text,
            budgets=BudgetsOut(
                max_tokens=int(trace.max_tokens),
                used_tokens=result.used_tokens,
                reserved_tokens=result.reserved_tokens,
                required_tokens=result.required_tokens,
                max_items=int(trace.request.get("max_items") or 0),
                strategy=f"tiered:{RANKER_VERSION}",
                tokenizer_id=self._counter.tokenizer_id,
                truncated=result.truncated,
            ),
            retrieval_trace_id=context_id,
            rematerialized=True,
            principal_revoked=owner_revoked,
            withdrawn=max(0, len(rows) - len(result.selected)),
            versions={**self._versions(None, mode, None), "built_with": str(trace.versions.get("ranker", ""))},
        )
        fill(out, result.selected, book)
        if trace.request.get("response_mode") == "items":
            out.context_text = ""
        return out

    # ---- profiles -------------------------------------------------------------------------------------------------------------------

    async def profiles(self, principal: Principal) -> list[dict[str, Any]]:
        ctx = WorkspaceContext.of(principal)
        async with tenant_session(self._engine, ctx, principal.principal_id) as conn:
            active = await self._knowledge.specs.active(conn, ctx.workspace_id)
        onto: dict[str, ContextProfile] = active[1].context_profiles if active else {}
        names = sorted({*builtin_profiles(), *onto})
        return [resolve(n, onto, ontology_version_id=active[0] if active else None).summary() for n in names]
