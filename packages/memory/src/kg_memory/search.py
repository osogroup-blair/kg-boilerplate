"""Scoped memory search (phase-5 plan §5.6, ADR-029/031): ONE SQL statement that authorises, filters and ranks.

    candidates  workspace + classification ceiling + SCOPE VISIBILITY + status + validity window + type / subject / project filters
    emb         exact cosine of each candidate to the query vector, at the pinned embedding version
    vec         candidates over the cosine floor, ranked
    lex         candidates matching the tsquery, ranked by ts_rank_cd
    hits        lex and vec merged with a FULL JOIN (the planner can only hash or merge it; a LEFT JOIN of each onto `candidates` was planned as a nested loop on a
                row estimate of 6 against 50 000 actual, 2.5 billion comparisons, whenever a query term matched most of the workspace)
    ranked      reciprocal-rank fusion + small deterministic boosts (`mem-rrf.v1`), ties broken by id

The authorisation predicate is part of `candidates`, which everything else reads: there is no unscoped candidate list, so a memory the caller may not see can
appear in no rank, no score, no count and no timing bucket (SEC-02, RET-04). `candidates_considered` counts authorised rows only. If the query cannot be
embedded the search still answers, lexical-only, and says so (`degraded`); it never fails silently and never falls back to an unscoped path.

`mem-rrf.v1`:  score = 1/(60 + rank_lex) + 1/(60 + rank_vec) + 0.002 * importance + 0.001 * ln(1 + evidence) [+ recency_weight * 0.01 * exp(-age_days / 90)].
Importance and evidence are deliberately small: they break near-ties, they never override relevance, and none of it is "truth" (blueprint). Recency is applied
ONLY when the request asks for it: recency is not validity.
"""

from __future__ import annotations

import re
import time
from typing import Any

from kg_common.errors import Forbidden
from kg_common.ids import uuid7_str
from kg_common.settings import Settings
from kg_modelgw.config import ModelConfig
from kg_modelgw.gateway import Correlation, ModelGateway
from kg_obs import metrics
from kg_obs.logging import get_logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from kg_memory.models import RANKER_VERSION, HitComponents, ScopeOut, SearchHit, SearchIn, SearchOut
from kg_memory.reads import Viewer, visible_where
from kg_memory.store import vector_literal

log = get_logger("kg.memory.search")
RRF_K = 60
W_IMPORTANCE, W_EVIDENCE, W_RECENCY = 0.002, 0.001, 0.01
MAX_TOKENS = 32
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
EGRESS_CAP = "restricted"  # local_only is never embedded, so a query is never labelled above restricted (the router would refuse the call)


# Words that carry no topic (English function words; the lexical half is a recall aid for the vector half, which is language-agnostic). Without this, `a` in
# `starting a business` would match half the workspace.
STOPWORDS = frozenset(
    "a an the and or but nor of to in on at by for from with without about as into onto over under is are was were be been being am do does did done have has had "
    "i me my we our you your he she it its they them their this that these those there here what which who whom whose how when where why if then than so not no "
    "can could should would will shall may might must also just very".split()
)


def tsquery_of(query: str) -> str:
    """An OR query over the topic words of the request: `starting a business` finds a memory about a business even if it never says `starting`."""
    words = [t.lower()[:64] for t in _TOKEN.findall(query)]
    tokens = list(dict.fromkeys(w for w in words if w not in STOPWORDS and len(w) > 1)) or list(
        dict.fromkeys(words)
    )  # an all-stopword query still searches
    return " | ".join(tokens[:MAX_TOKENS])


SQL = """
WITH candidates AS (
    SELECT m.id, m.workspace_id, m.tsv, m.importance, m.recorded_at FROM curated_memories m
    WHERE {visible}
      AND m.status = ANY(CAST(:statuses AS text[]))
      AND m.effective_from <= COALESCE(CAST(:as_of AS timestamptz), now())
      AND (m.effective_until IS NULL OR m.effective_until > COALESCE(CAST(:as_of AS timestamptz), now()))
      AND (m.expires_at IS NULL OR m.expires_at > COALESCE(CAST(:as_of AS timestamptz), now()))
      {filters}
),
emb AS (
    SELECT c.id, c.workspace_id, c.importance, c.recorded_at, 1 - (e.embedding <=> CAST(:qvec AS vector)) AS cosine
    FROM candidates c JOIN memory_embeddings e ON e.workspace_id = c.workspace_id AND e.memory_id = c.id AND e.embedding_version = :ver
),
vec AS (
    SELECT id, workspace_id, importance, recorded_at, cosine, row_number() OVER (ORDER BY cosine DESC, id) AS r FROM emb WHERE cosine >= :min_cos
),
lex AS (
    SELECT c.id, c.workspace_id, c.importance, c.recorded_at, row_number() OVER (ORDER BY ts_rank_cd(c.tsv, to_tsquery('simple', :tsq)) DESC, c.id) AS r
    FROM candidates c WHERE :tsq <> '' AND c.tsv @@ to_tsquery('simple', :tsq)
),
stats AS (
    SELECT (SELECT count(*) FROM candidates) AS considered, (SELECT count(*) FROM emb) AS embedded
),
hits AS (
    SELECT COALESCE(lex.id, vec.id) AS id, COALESCE(lex.workspace_id, vec.workspace_id) AS workspace_id, COALESCE(lex.importance, vec.importance) AS importance,
           COALESCE(lex.recorded_at, vec.recorded_at) AS recorded_at, lex.r AS lex_r, vec.r AS vec_r, vec.cosine
    FROM lex FULL JOIN vec ON vec.id = lex.id
),
ranked AS (
    SELECT h.id, cm.type, cm.status, cm.summary, cm.content, cm.evidence_status, cm.authority_class, cm.classification, cm.effective_from, cm.effective_until,
           h.importance, h.lex_r AS lex_rank, h.vec_r AS vec_rank, h.cosine, ev.n AS evidence_count,
           (COALESCE(1.0 / ({k} + h.lex_r), 0) + COALESCE(1.0 / ({k} + h.vec_r), 0) + {w_imp} * h.importance::float8 + {w_ev} * ln(1 + ev.n)
            + :recency * {w_rec} * exp(-EXTRACT(EPOCH FROM (now() - h.recorded_at)) / 86400.0 / 90.0))::float8 AS score,
           (SELECT COALESCE(jsonb_agg(jsonb_build_array(s.scope_kind, s.scope_ref) ORDER BY s.scope_kind, s.scope_ref), '[]'::jsonb)
              FROM memory_scopes s WHERE s.workspace_id = h.workspace_id AND s.memory_id = h.id) AS scopes,
           (SELECT COALESCE(jsonb_agg(ms.entity_id ORDER BY ms.entity_id), '[]'::jsonb)
              FROM memory_subjects ms WHERE ms.workspace_id = h.workspace_id AND ms.memory_id = h.id AND ms.role = 'subject') AS subjects
    FROM hits h
    JOIN curated_memories cm ON cm.workspace_id = h.workspace_id AND cm.id = h.id
    CROSS JOIN LATERAL (SELECT count(*) AS n FROM memory_evidence e WHERE e.workspace_id = h.workspace_id AND e.memory_id = h.id) ev
    ORDER BY score DESC, h.id
    LIMIT :k_limit
)
SELECT st.considered, st.embedded, r.*
FROM stats st LEFT JOIN ranked r ON true
ORDER BY r.score DESC NULLS LAST, r.id
"""


class MemorySearch:
    def __init__(self, gateway: ModelGateway | None, config: ModelConfig, settings: Settings, *, service: str):
        """`gateway=None` (an offline CLI) makes every search lexical-only and says so."""
        self._gateway, self._config, self._settings, self._service = gateway, config, settings, service

    @property
    def embedding_version(self) -> int:
        return self._settings.memory_embedding_version

    async def embed_query(self, v: Viewer, query: str, *, classification: str | None = None) -> list[float] | None:
        """The query vector, or None when it cannot be had (no gateway, an outage, a policy refusal): the search then runs lexical-only and says so. The context
        engine (P6) embeds ONCE with the classification of the task and hands the vector to both the graph and the memory search (`run(..., query_vector=)`)."""
        classification = classification or (v.grants.ceiling if v.grants.ceiling != "local_only" else EGRESS_CAP)
        if self._gateway is None:
            return None
        try:
            result = await self._gateway.embed(
                "embedding",
                [query],
                kind="query",
                classification=classification,
                correlation=Correlation(uuid7_str(), str(v.workspace_id), str(v.principal.principal_id), self._service),
            )
        except Exception as exc:  # the vector half is optional: any failure degrades, visibly, to lexical-only (never to an unscoped path)
            if type(exc).__name__ == "CancelledError":
                raise
            log.warning("memory_query_embedding_unavailable", error_type=type(exc).__name__)
            return None
        return result.vectors[0]

    def _statuses(self, v: Viewer, body: SearchIn) -> list[str]:
        if body.include and not v.curator:
            raise Forbidden("Including proposed or disputed memory needs memory.curate", details={"action": "memory.curate"})
        statuses = ["active", *body.include]
        if body.as_of is not None:  # history: what was true then, including what has since been superseded or has expired
            statuses += ["superseded", "expired"]
        return statuses

    def build(self, v: Viewer, body: SearchIn, qvec: list[float] | None) -> tuple[str, dict[str, Any]]:
        visible, params = visible_where(v, run_id=body.run_id, all_runs=body.all_runs, status_rule=False)
        filters: list[str] = []
        if body.types:
            filters.append("AND m.type = ANY(CAST(:types AS text[]))")
            params["types"] = list(body.types)
        if body.subject_ids:
            filters.append(
                "AND EXISTS (SELECT 1 FROM memory_subjects fs WHERE fs.workspace_id = m.workspace_id AND fs.memory_id = m.id "
                "AND fs.entity_id = ANY(CAST(:subject_ids AS uuid[])))"
            )
            params["subject_ids"] = [str(s) for s in body.subject_ids]
        if body.project:
            filters.append(
                "AND EXISTS (SELECT 1 FROM memory_scopes fp WHERE fp.workspace_id = m.workspace_id AND fp.memory_id = m.id "
                "AND fp.scope_kind = 'project' AND fp.scope_ref = :project)"
            )
            params["project"] = body.project
        sql = SQL.format(
            visible=visible,
            filters=" ".join(filters),
            k=RRF_K,
            w_imp=W_IMPORTANCE,
            w_ev=W_EVIDENCE,
            w_rec=W_RECENCY,
        )
        params.update(
            {
                "statuses": self._statuses(v, body),
                "as_of": body.as_of,
                "qvec": vector_literal(qvec) if qvec is not None else None,
                "ver": self.embedding_version,
                "min_cos": self._settings.memory_vec_min_cosine,
                "tsq": tsquery_of(body.query),
                "recency": body.recency_weight or 0.0,
                "k_limit": body.limit,
            }
        )
        return sql, params

    async def run(
        self, conn: AsyncConnection, v: Viewer, body: SearchIn, *, embed: bool = True, query_vector: list[float] | None = None
    ) -> SearchOut:
        """`embed=False` uses `query_vector` as given (None = lexical-only): the caller has already decided how, and whether, the query leaves."""
        started = time.perf_counter()
        qvec = await self.embed_query(v, body.query) if embed else query_vector
        sql, params = self.build(v, body, qvec)
        rows = (await conn.execute(text(sql), params)).all()  # noqa: S608 (the only interpolated parts are constants and the visibility predicate)
        degraded = ["vector_unavailable"] if qvec is None else []
        considered = int(rows[0].considered) if rows else 0
        embedded = int(rows[0].embedded) if rows else 0
        hits: list[SearchHit] = []
        for r in rows:
            if r.id is None:
                continue
            scopes = [ScopeOut(kind=k, ref=ref) for k, ref in r.scopes if _shown(v, k, ref, body)]
            hits.append(
                SearchHit(
                    memory_id=r.id,
                    type=r.type,
                    status=r.status,
                    summary=r.summary,
                    content=r.content,
                    evidence_status=r.evidence_status,
                    authority_class=r.authority_class,
                    classification=r.classification,
                    scopes=scopes,
                    subject_ids=[x for x in r.subjects],
                    effective_from=r.effective_from,
                    effective_until=r.effective_until,
                    score=round(float(r.score), 9),
                    matched_by="both" if r.lex_rank and r.vec_rank else "vector" if r.vec_rank else "lexical",
                    components=HitComponents(
                        lex_rank=int(r.lex_rank) if r.lex_rank else None,
                        vec_rank=int(r.vec_rank) if r.vec_rank else None,
                        cosine=round(float(r.cosine), 6) if r.cosine is not None else None,
                        importance=float(r.importance),
                        evidence_count=int(r.evidence_count),
                    ),
                )
            )
        metrics.memory_search_total.labels(degraded="true" if degraded else "false").inc()
        metrics.memory_search_candidates.observe(considered)
        metrics.memory_search_duration_seconds.observe(time.perf_counter() - started)
        return SearchOut(
            hits=hits,
            candidates_considered=considered,
            embedding_coverage=round(embedded / considered, 4) if considered else 1.0,
            embedding_version=self.embedding_version,
            ranker_version=RANKER_VERSION,
            degraded=degraded,
        )


def _shown(v: Viewer, kind: str, ref: str, body: SearchIn) -> bool:
    if kind == "workspace" or v.grants.can_use(kind, ref):
        return True
    return v.reveal_private and kind in ("agent", "session")
