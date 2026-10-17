"""Verification (phase-8 plan §5.4.1 step 4, SEC-05 strict): after a purge, prove that nothing is left.

* ID SCANS: no row of any table that could point at a deleted record still does. Ledger command rows, tombstones and audit entries are allowed to hold the ids
  (an id carries no content); everything else is not.
* RESIDUAL-TEXT SCANS: the manifest's names, aliases and external ids appear in no Postgres text column (memory content and summary, fact text, entity names and
  aliases, review text, command payloads, correction text, idempotency responses, trace requests) and, through the graph port, in no node name or summary, edge fact or
  episode content of this workspace's group.

A scan result is a LOCATION and a COUNT, never the text. Any residual makes the request `completed_with_exceptions` and raises an exception item for a person.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kg_domain.principal import WorkspaceContext
from kg_obs import metrics
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from kg_governance.deletion.plan import Sets, _like
from kg_governance.deletion.ports import GraphPurgePort

# (label, SQL) pairs. :e :f :m :i are uuid arrays; :et :ft :mt :it their text forms.
ID_SCANS: list[tuple[str, str]] = [
    ("entities.id", "SELECT count(*) FROM entities WHERE workspace_id = :ws AND id = ANY(:e)"),
    (
        "facts.id",
        "SELECT count(*) FROM facts WHERE workspace_id = :ws AND (id = ANY(:f) OR source_entity_id = ANY(:e) OR target_entity_id = ANY(:e) OR source_interaction_id = ANY(:i))",
    ),
    ("fact_evidence", "SELECT count(*) FROM fact_evidence WHERE workspace_id = :ws AND (fact_id = ANY(:f) OR interaction_id = ANY(:i))"),
    (
        "entity_mentions",
        "SELECT count(*) FROM entity_mentions WHERE workspace_id = :ws AND (entity_id = ANY(:e) OR interaction_id = ANY(:i))",
    ),
    (
        "attribute_claims",
        "SELECT count(*) FROM attribute_claims WHERE workspace_id = :ws AND (entity_id = ANY(:e) OR interaction_id = ANY(:i))",
    ),
    ("entity_external_ids", "SELECT count(*) FROM entity_external_ids WHERE workspace_id = :ws AND entity_id = ANY(:e)"),
    (
        "identity_non_matches",
        "SELECT count(*) FROM identity_non_matches WHERE workspace_id = :ws AND (entity_a = ANY(:e) OR entity_b = ANY(:e))",
    ),
    ("entity_lineage", "SELECT count(*) FROM entity_lineage WHERE workspace_id = :ws AND (entity_id = ANY(:e) OR related_id = ANY(:e))"),
    ("interactions", "SELECT count(*) FROM interactions WHERE workspace_id = :ws AND id = ANY(:i)"),
    ("interaction_contents", "SELECT count(*) FROM interaction_contents WHERE workspace_id = :ws AND interaction_id = ANY(:i)"),
    ("interaction_stages", "SELECT count(*) FROM interaction_stages WHERE workspace_id = :ws AND interaction_id = ANY(:i)"),
    (
        "interaction_subjects",
        "SELECT count(*) FROM interaction_subjects WHERE workspace_id = :ws AND (interaction_id = ANY(:i) OR entity_id = ANY(:e))",
    ),
    ("graph_episodes", "SELECT count(*) FROM graph_episodes WHERE workspace_id = :ws AND interaction_id = ANY(:i)"),
    ("curated_memories.id", "SELECT count(*) FROM curated_memories WHERE workspace_id = :ws AND id = ANY(:m)"),
    (
        "memory_scopes",
        "SELECT count(*) FROM memory_scopes WHERE workspace_id = :ws AND (memory_id = ANY(:m) OR (scope_kind = 'subject' AND scope_ref = ANY(:et)))",
    ),
    ("memory_subjects", "SELECT count(*) FROM memory_subjects WHERE workspace_id = :ws AND (memory_id = ANY(:m) OR entity_id = ANY(:e))"),
    (
        "memory_evidence",
        "SELECT count(*) FROM memory_evidence WHERE workspace_id = :ws AND (memory_id = ANY(:m) OR source_memory_id = ANY(:m) OR fact_id = ANY(:f) OR interaction_id = ANY(:i))",
    ),
    ("memory_lineage", "SELECT count(*) FROM memory_lineage WHERE workspace_id = :ws AND (memory_id = ANY(:m) OR related_id = ANY(:m))"),
    ("memory_embeddings", "SELECT count(*) FROM memory_embeddings WHERE workspace_id = :ws AND memory_id = ANY(:m)"),
    (
        "review_items",
        "SELECT count(*) FROM review_items WHERE workspace_id = :ws AND item_type <> 'deletion_exception' AND (subject_entity_id = ANY(:e) OR candidate_entity_id = ANY(:e) "
        "OR fact_id = ANY(:f) OR competing_fact_id = ANY(:f) OR memory_id = ANY(:m) OR competing_memory_id = ANY(:m) OR interaction_id = ANY(:i))",
    ),
    (
        "context_items",
        "SELECT count(*) FROM context_items WHERE workspace_id = :ws AND ((item_kind = 'fact' AND item_id = ANY(:ft)) OR (item_kind = 'memory' AND item_id = ANY(:mt)))",
    ),
    (
        "outbox_jobs",
        "SELECT count(*) FROM outbox_jobs WHERE workspace_id = :ws AND (subject_id = ANY(:i) OR subject_id = ANY(:m) OR subject_id = ANY(:e))",
    ),
    (
        "principal_scopes",
        "SELECT count(*) FROM principal_scopes WHERE workspace_id = :ws AND scope_kind = 'subject' AND scope_ref = ANY(:et)",
    ),
    (
        "corrections",
        "SELECT count(*) FROM corrections WHERE workspace_id = :ws AND (target_id = ANY(:e) OR target_id = ANY(:f) OR target_id = ANY(:m))",
    ),
]
# (label, table.column expression, WHERE column expression). `{x}` is replaced by a bound LIKE pattern.
TEXT_SCANS: list[tuple[str, str]] = [
    (
        "curated_memories.content",
        "SELECT id::text FROM curated_memories WHERE workspace_id = :ws AND status <> 'deletion_pending' AND (content ILIKE ANY(CAST(:likes AS text[])) OR summary ILIKE ANY(CAST(:likes AS text[])))",
    ),
    (
        "facts.fact_text",
        "SELECT id::text FROM facts WHERE workspace_id = :ws AND status <> 'deletion_pending' AND (fact_text ILIKE ANY(CAST(:likes AS text[])) OR attributes::text ILIKE ANY(CAST(:likes AS text[])))",
    ),
    (
        "entities.name",
        "SELECT id::text FROM entities WHERE workspace_id = :ws AND status <> 'deletion_pending' AND (display_name ILIKE ANY(CAST(:likes AS text[])) OR aliases::text ILIKE ANY(CAST(:likes AS text[])) OR attributes::text ILIKE ANY(CAST(:likes AS text[])))",
    ),
    (
        "review_items.text",
        "SELECT id::text FROM review_items WHERE workspace_id = :ws AND item_type <> 'deletion_exception' AND (rationale_text ILIKE ANY(CAST(:likes AS text[])) OR detail::text ILIKE ANY(CAST(:likes AS text[])))",
    ),
    (
        "commands.payload",
        "SELECT id::text FROM commands WHERE workspace_id = :ws AND command_type <> 'deletion.request' AND payload::text ILIKE ANY(CAST(:likes AS text[]))",
    ),
    (
        "corrections.reason_text",
        "SELECT id::text FROM corrections WHERE workspace_id = :ws AND reason_text ILIKE ANY(CAST(:likes AS text[]))",
    ),
    (
        "idempotency_records.response",
        "SELECT key FROM idempotency_records WHERE workspace_id = :ws AND response::text ILIKE ANY(CAST(:likes AS text[]))",
    ),
    (
        "context_traces.request",
        "SELECT id::text FROM context_traces WHERE workspace_id = :ws AND request::text ILIKE ANY(CAST(:likes AS text[]))",
    ),
]


@dataclass
class Residual:
    store: str
    location: str
    count: int
    row_ids: list[str] = field(default_factory=list)  # ids of the rows that hold a term (never the text); capped


@dataclass
class VerifyResult:
    id_residuals: list[Residual] = field(default_factory=list)
    text_residuals: list[Residual] = field(default_factory=list)
    graph_residuals: list[Residual] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.id_residuals or self.text_residuals or self.graph_residuals)

    def as_dict(self) -> dict[str, object]:
        return {
            "id_residuals": sum(r.count for r in self.id_residuals),
            "text_residuals": sum(r.count for r in self.text_residuals),
            "graph_residuals": sum(r.count for r in self.graph_residuals),
            "locations": [f"{r.store}:{r.location}" for r in (*self.id_residuals, *self.text_residuals, *self.graph_residuals)],
        }


def usable_terms(terms: list[str]) -> list[str]:
    """A term shorter than three characters would match half the alphabet; it is not scanned (the ids and identifiers are)."""
    return sorted({t.strip() for t in terms if len(t.strip()) >= 3})


async def verify(
    conn: AsyncConnection, ctx: WorkspaceContext, s: Sets, terms: list[str], graph: GraphPurgePort | None, port_ids: list[str] | None = None
) -> VerifyResult:
    ws = ctx.workspace_id
    out = VerifyResult()
    p = {
        "ws": ws,
        "e": s.e,
        "f": s.f,
        "m": s.m,
        "i": s.i,
        "et": [str(x) for x in s.e],
        "ft": [str(x) for x in s.f],
        "mt": [str(x) for x in s.m],
    }
    for label, sql in ID_SCANS:
        n = int((await conn.execute(text(sql), p)).scalar_one())
        if n:
            out.id_residuals.append(Residual("postgres", label, n))
            metrics.deletion_residuals_total.labels(store="postgres").inc(n)
    use = usable_terms(terms)
    if use:
        likes = [_like(t) for t in use]
        for label, sql in TEXT_SCANS:
            rows = (await conn.execute(text(sql), {"ws": ws, "likes": likes})).all()
            if rows:
                out.text_residuals.append(Residual("postgres", label, len(rows), [str(r[0]) for r in rows][:50]))
                metrics.deletion_residuals_total.labels(store="postgres").inc(len(rows))
    if graph is not None:
        for g in await graph.scan(ctx, terms=use, ids=[*(str(x) for x in s.e), *(str(x) for x in s.f), *(str(x) for x in s.i)]):
            out.graph_residuals.append(Residual("graph", g.location, g.count))
            metrics.deletion_residuals_total.labels(store="graph").inc(g.count)
    return out
