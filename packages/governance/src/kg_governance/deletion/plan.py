"""What a deletion touches (phase-8 plan §5.4.1 steps 1-2): the closure of a target, and the plan (`deletion_items`) that lists every derivative row once.

The CLOSURE is computed from identities, in Postgres, at request time (it is what the freeze needs):

    subject      the entity and every entity merged into it; the interactions linked to it (subject references, accepted mentions, claims); the facts that touch it;
                 facts and memories that rest ONLY on those interactions; memories whose only subjects are the entity; extracted entities left with nothing.
    interaction  the interaction; what rests only on it.
    memory       the memory; memories derived only from it (lineage).
    principal    the principal's private and unpublished memory; with `include_submissions`, its interactions and what rests only on them.

A fact or memory that has INDEPENDENT evidence survives (its evidence rows that cite the deleted interaction go); one left with none is deleted as unsupported (invariant 2).
Privacy beats recall where the two meet: a memory about someone else that was derived only from an interaction about the subject is deleted with it.

The PLAN then lists every row of every derived table that points at the closure, as `(store, item_kind, item_id, action)`. Item ids are built with the SAME expressions the
database's purge gate uses (`KEYS`), so the gate recognises exactly what the plan listed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

# item_kind -> (store, table, SQL expression for the item id over alias `t`). The expressions mirror public.purge_guard's key columns (migration 0008).
KEYS: dict[str, tuple[str, str, str]] = {
    "interactions": ("postgres", "interactions", "t.id::text"),
    "interaction_contents": ("postgres", "interaction_contents", "t.interaction_id::text || '|' || t.form"),
    "interaction_stages": ("postgres", "interaction_stages", "t.interaction_id::text || '|' || t.stage"),
    "interaction_subjects": ("postgres", "interaction_subjects", "t.interaction_id::text || '|' || t.role || '|' || t.ordinal::text"),
    "graph_episodes": ("postgres", "graph_episodes", "t.interaction_id::text || '|' || t.generation::text"),
    "entity_mentions": ("postgres", "entity_mentions", "t.id::text"),
    "attribute_claims": ("postgres", "attribute_claims", "t.id::text"),
    "entities": ("postgres", "entities", "t.id::text"),
    "entity_external_ids": (
        "postgres",
        "entity_external_ids",
        "t.entity_id::text || '|' || t.type_key || '|' || t.system || '|' || md5(t.value)",
    ),
    "identity_non_matches": ("postgres", "identity_non_matches", "t.entity_a::text || '|' || t.entity_b::text"),
    "entity_lineage": ("postgres", "entity_lineage", "t.entity_id::text || '|' || t.related_id::text || '|' || t.relation"),
    "facts": ("postgres", "facts", "t.id::text"),
    "fact_evidence": ("postgres", "fact_evidence", "t.id::text"),
    "corrections": ("postgres", "corrections", "t.id::text"),
    "attribute_corrections": ("postgres", "attribute_corrections", "t.id::text"),
    "curated_memories": ("postgres", "curated_memories", "t.id::text"),
    "memory_scopes": ("postgres", "memory_scopes", "t.memory_id::text || '|' || t.scope_kind || '|' || md5(t.scope_ref)"),
    "memory_subjects": ("postgres", "memory_subjects", "t.memory_id::text || '|' || t.entity_id::text"),
    "memory_evidence": ("postgres", "memory_evidence", "t.id::text"),
    "memory_lineage": ("postgres", "memory_lineage", "t.memory_id::text || '|' || t.related_id::text || '|' || t.relation"),
    "review_items": ("postgres", "review_items", "t.id::text"),
    "commands": ("postgres", "commands", "t.id::text"),
    "principal_scopes": ("postgres", "principal_scopes", "t.id::text"),
    "memory_embeddings": ("vector", "memory_embeddings", "t.memory_id::text || '|' || t.embedding_version::text"),
    "context_items": ("trace", "context_items", "t.context_id::text || '|' || t.position::text"),
    "context_debug_snapshots": ("trace", "context_debug_snapshots", "t.context_id::text"),
    "context_traces": ("trace", "context_traces", "t.id::text"),
    "outbox_jobs": ("job", "outbox_jobs", "t.id::text"),
    "job_attempts": ("job", "job_attempts", "t.id::text"),
    "idempotency_records": ("idempotency", "idempotency_records", "t.key"),
}
# the tombstone kind of the identity items
TOMBSTONES = {"entities": "entity", "facts": "fact", "interactions": "interaction", "curated_memories": "memory"}


@dataclass
class Closure:
    entities: list[uuid.UUID] = field(default_factory=list)
    facts: list[uuid.UUID] = field(default_factory=list)
    memories: list[uuid.UUID] = field(default_factory=list)
    interactions: list[uuid.UUID] = field(default_factory=list)
    retained_facts: list[uuid.UUID] = field(
        default_factory=list
    )  # cite a deleted interaction but have other evidence: only the citation goes
    retained_memories: list[uuid.UUID] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.entities or self.facts or self.memories or self.interactions)


def _arr(name: str) -> str:
    return f"CAST(:{name} AS uuid[])"


async def _ids(conn: AsyncConnection, sql: str, **params: Any) -> list[uuid.UUID]:
    return [r[0] for r in (await conn.execute(text(sql), params)).all()]


async def compute(
    conn: AsyncConnection, ws: uuid.UUID, kind: str, target: uuid.UUID | None, *, include_submissions: bool = False
) -> Closure:
    E: set[uuid.UUID] = set()
    I: set[uuid.UUID] = set()  # noqa: E741 (the plan's own letters: entities, interactions, facts, memories)
    M: set[uuid.UUID] = set()
    if kind == "subject":
        assert target is not None
        E = set(
            await _ids(
                conn,
                "WITH RECURSIVE d(id) AS (SELECT id FROM entities WHERE workspace_id = :ws AND id = :t UNION "
                "SELECT e.id FROM entities e JOIN d ON e.merged_into_id = d.id WHERE e.workspace_id = :ws) SELECT id FROM d",
                ws=ws,
                t=target,
            )
        )
        I = set(  # noqa: E741
            await _ids(
                conn,
                "SELECT x.interaction_id FROM (SELECT interaction_id FROM interaction_subjects WHERE workspace_id = :ws AND entity_id = ANY(:e) "
                "UNION SELECT interaction_id FROM entity_mentions WHERE workspace_id = :ws AND entity_id = ANY(:e) AND status <> 'rejected' "
                "UNION SELECT interaction_id FROM attribute_claims WHERE workspace_id = :ws AND entity_id = ANY(:e)) x "
                "JOIN interactions i ON i.workspace_id = :ws AND i.id = x.interaction_id AND i.processing_status <> 'deletion_pending'",
                ws=ws,
                e=sorted(E, key=str),
            )
        )
    elif kind == "interaction":
        assert target is not None
        I = {target}  # noqa: E741
    elif kind == "memory":
        assert target is not None
        M = {target}
    elif kind == "principal_data":
        assert target is not None
        M = set(
            await _ids(
                conn,
                "SELECT m.id FROM curated_memories m WHERE m.workspace_id = :ws AND m.created_by = :p AND m.status NOT IN ('deleted','deletion_pending') "
                "AND (m.status = 'proposed' OR NOT EXISTS (SELECT 1 FROM memory_scopes s WHERE s.workspace_id = m.workspace_id AND s.memory_id = m.id "
                "AND s.scope_kind IN ('workspace','subject','project')))",
                ws=ws,
                p=target,
            )
        )
        if include_submissions:
            I = set(  # noqa: E741
                await _ids(
                    conn,
                    "SELECT id FROM interactions WHERE workspace_id = :ws AND submitted_by = :p AND processing_status <> 'deletion_pending'",
                    ws=ws,
                    p=target,
                )
            )
    else:
        raise ValueError(f"no closure for {kind}")

    def lst(s: set[uuid.UUID]) -> list[uuid.UUID]:
        return sorted(s, key=str)

    F: set[uuid.UUID] = set()
    if E:
        F |= set(
            await _ids(
                conn,
                "SELECT id FROM facts WHERE workspace_id = :ws AND (source_entity_id = ANY(:e) OR target_entity_id = ANY(:e)) AND status NOT IN ('deleted','deletion_pending')",
                ws=ws,
                e=lst(E),
            )
        )
    if I:
        F |= set(
            await _ids(
                conn,
                "SELECT f.id FROM facts f WHERE f.workspace_id = :ws AND f.status NOT IN ('deleted','deletion_pending') AND ("
                " (EXISTS (SELECT 1 FROM fact_evidence e WHERE e.workspace_id = f.workspace_id AND e.fact_id = f.id AND e.interaction_id = ANY(:i))"
                "  AND NOT EXISTS (SELECT 1 FROM fact_evidence e WHERE e.workspace_id = f.workspace_id AND e.fact_id = f.id AND (e.interaction_id IS NULL OR e.interaction_id <> ALL(:i))))"
                " OR (f.source_interaction_id = ANY(:i) AND NOT EXISTS (SELECT 1 FROM fact_evidence e WHERE e.workspace_id = f.workspace_id AND e.fact_id = f.id)))",
                ws=ws,
                i=lst(I),
            )
        )
    if E:
        M |= set(
            await _ids(
                conn,
                "SELECT m.id FROM curated_memories m WHERE m.workspace_id = :ws AND m.status NOT IN ('deleted','deletion_pending') "
                "AND EXISTS (SELECT 1 FROM memory_subjects s WHERE s.workspace_id = m.workspace_id AND s.memory_id = m.id) "
                "AND NOT EXISTS (SELECT 1 FROM memory_subjects s WHERE s.workspace_id = m.workspace_id AND s.memory_id = m.id AND s.entity_id <> ALL(:e))",
                ws=ws,
                e=lst(E),
            )
        )
    while True:  # memories that rest only on what is already going: interactions, facts, other memories (a fixpoint: a memory derived from a memory derived from ...)
        found = set(
            await _ids(
                conn,
                "SELECT m.id FROM curated_memories m WHERE m.workspace_id = :ws AND m.status NOT IN ('deleted','deletion_pending') AND m.id <> ALL(:m) "
                "AND EXISTS (SELECT 1 FROM memory_evidence v WHERE v.workspace_id = m.workspace_id AND v.memory_id = m.id AND "
                "  (v.interaction_id = ANY(:i) OR v.fact_id = ANY(:f) OR v.source_memory_id = ANY(:m))) "
                "AND NOT EXISTS (SELECT 1 FROM memory_evidence v WHERE v.workspace_id = m.workspace_id AND v.memory_id = m.id AND NOT ("
                "  (v.interaction_id IS NOT NULL AND v.interaction_id = ANY(:i)) OR (v.fact_id IS NOT NULL AND v.fact_id = ANY(:f)) "
                "  OR (v.source_memory_id IS NOT NULL AND v.source_memory_id = ANY(:m))))",
                ws=ws,
                i=lst(I),
                f=lst(F),
                m=lst(M),
            )
        )
        if not found:
            break
        M |= found
    if I or E:  # extracted entities left with nothing (no fact, no mention elsewhere, no identifier, no memory or interaction naming them)
        E |= set(
            await _ids(
                conn,
                "SELECT e.id FROM entities e WHERE e.workspace_id = :ws AND e.origin = 'extracted' AND e.status IN ('active','candidate','untyped') AND e.id <> ALL(:e) "
                "AND EXISTS (SELECT 1 FROM entity_mentions x WHERE x.workspace_id = e.workspace_id AND x.entity_id = e.id AND x.interaction_id = ANY(:i)) "
                "AND NOT EXISTS (SELECT 1 FROM entity_mentions x WHERE x.workspace_id = e.workspace_id AND x.entity_id = e.id AND x.interaction_id <> ALL(:i)) "
                "AND NOT EXISTS (SELECT 1 FROM facts f WHERE f.workspace_id = e.workspace_id AND (f.source_entity_id = e.id OR f.target_entity_id = e.id) "
                "  AND f.id <> ALL(:f) AND f.status NOT IN ('deleted','deletion_pending')) "
                "AND NOT EXISTS (SELECT 1 FROM entity_external_ids x WHERE x.workspace_id = e.workspace_id AND x.entity_id = e.id) "
                "AND NOT EXISTS (SELECT 1 FROM memory_subjects s WHERE s.workspace_id = e.workspace_id AND s.entity_id = e.id AND s.memory_id <> ALL(:m)) "
                "AND NOT EXISTS (SELECT 1 FROM interaction_subjects s WHERE s.workspace_id = e.workspace_id AND s.entity_id = e.id AND s.interaction_id <> ALL(:i))",
                ws=ws,
                e=lst(E),
                i=lst(I),
                f=lst(F),
                m=lst(M),
            )
        )
    retained_f = (
        await _ids(
            conn,
            "SELECT DISTINCT f.id FROM facts f WHERE f.workspace_id = :ws AND f.status NOT IN ('deleted','deletion_pending') AND f.id <> ALL(:f) "
            "AND (f.source_interaction_id = ANY(:i) OR EXISTS (SELECT 1 FROM fact_evidence e WHERE e.workspace_id = f.workspace_id AND e.fact_id = f.id AND e.interaction_id = ANY(:i)))",
            ws=ws,
            i=lst(I),
            f=lst(F),
        )
        if I
        else []
    )
    retained_m = (
        await _ids(
            conn,
            "SELECT DISTINCT m.id FROM curated_memories m WHERE m.workspace_id = :ws AND m.status NOT IN ('deleted','deletion_pending') AND m.id <> ALL(:m) "
            "AND EXISTS (SELECT 1 FROM memory_evidence v WHERE v.workspace_id = m.workspace_id AND v.memory_id = m.id AND "
            "(v.interaction_id = ANY(:i) OR v.fact_id = ANY(:f) OR v.source_memory_id = ANY(:m)))",
            ws=ws,
            i=lst(I),
            f=lst(F),
            m=lst(M),
        )
        if (I or F or M)
        else []
    )
    return Closure(lst(E), lst(F), lst(M), lst(I), retained_f, retained_m)


async def freeze(conn: AsyncConnection, ws: uuid.UUID, c: Closure) -> None:
    """The freeze (SEC-05): one status change per table, in the caller's transaction. From the commit on, every read already treats these rows as absent."""
    if c.entities:
        await conn.execute(
            text("UPDATE entities SET status = 'deletion_pending', updated_at = now() WHERE workspace_id = :ws AND id = ANY(:ids)"),
            {"ws": ws, "ids": c.entities},
        )
    if c.facts:
        await conn.execute(
            text("UPDATE facts SET status = 'deletion_pending', decided_at = now() WHERE workspace_id = :ws AND id = ANY(:ids)"),
            {"ws": ws, "ids": c.facts},
        )
    if c.memories:
        await conn.execute(
            text(
                "UPDATE curated_memories SET status = 'deletion_pending', revision = revision + 1 WHERE workspace_id = :ws AND id = ANY(:ids)"
            ),
            {"ws": ws, "ids": c.memories},
        )
    if c.interactions:
        await conn.execute(
            text(
                "UPDATE interactions SET processing_status = 'deletion_pending', status_changed_at = now() WHERE workspace_id = :ws AND id = ANY(:ids)"
            ),
            {"ws": ws, "ids": c.interactions},
        )


async def cancel_pending_jobs(conn: AsyncConnection, ws: uuid.UUID, c: Closure) -> int:
    """Jobs that have not started for the frozen subjects are cancelled (`ops.cancel_job`); a running one writes nothing more (the stage lock and the projector skip frozen rows)."""
    rows = (
        await conn.execute(
            text(
                "SELECT id FROM outbox_jobs WHERE workspace_id = :ws AND status = 'pending' AND subject_id = ANY(:ids) "
                "AND stage NOT IN ('deletion_plan','deletion_purge','deletion_verify')"
            ),
            {"ws": ws, "ids": [*c.interactions, *c.memories, *c.entities, *c.facts]},
        )
    ).all()
    n = 0
    for r in rows:
        if (await conn.execute(text("SELECT * FROM ops.cancel_job(:j)"), {"j": r.id})).first() is not None:
            n += 1
    return n


async def add_items(
    conn: AsyncConnection,
    ws: uuid.UUID,
    request_id: uuid.UUID,
    kind: str,
    ids: list[uuid.UUID],
    *,
    action: str = "delete",
    reason: str | None = None,
) -> int:
    """Lists identity rows (entities, facts, memories, interactions) by id."""
    if not ids:
        return 0
    store, table, key = KEYS[kind]
    res = await conn.execute(
        text(
            f"INSERT INTO deletion_items (request_id, workspace_id, store, item_kind, item_id, action, reason) "  # noqa: S608 (the table and the key come from KEYS, constants)
            f"SELECT :r, :ws, :store, :kind, {key}, :action, :reason FROM {table} t WHERE t.workspace_id = :ws AND t.id = ANY(:ids) ON CONFLICT DO NOTHING"
        ),
        {"r": request_id, "ws": ws, "store": store, "kind": kind, "action": action, "reason": reason, "ids": ids},
    )
    return int(res.rowcount)


@dataclass
class Sets:
    """The closure as the plan sees it, read back from the identity items (so a plan can be extended after the fact, e.g. by a review decision)."""

    e: list[uuid.UUID]
    f: list[uuid.UUID]
    m: list[uuid.UUID]
    i: list[uuid.UUID]


async def sets_of(conn: AsyncConnection, request_id: uuid.UUID) -> Sets:
    rows = (
        await conn.execute(
            text(
                "SELECT item_kind, item_id FROM deletion_items WHERE request_id = :r AND store = 'postgres' AND item_kind = ANY(:k) AND action = 'delete'"
            ),
            {"r": request_id, "k": ["entities", "facts", "curated_memories", "interactions"]},
        )
    ).all()
    by: dict[str, list[uuid.UUID]] = {"entities": [], "facts": [], "curated_memories": [], "interactions": []}
    for r in rows:
        by[r.item_kind].append(uuid.UUID(r.item_id))
    return Sets(by["entities"], by["facts"], by["curated_memories"], by["interactions"])


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


async def plan_dependents(conn: AsyncConnection, ws: uuid.UUID, request_id: uuid.UUID, s: Sets, *, terms: list[str]) -> dict[str, int]:
    """Step 2 of the lifecycle, Postgres half: every derived row that points at the closure, listed once (idempotent: a re-run adds nothing). Returns counts per item kind."""
    p: dict[str, Any] = {
        "r": request_id,
        "ws": ws,
        "e": s.e,
        "f": s.f,
        "m": s.m,
        "i": s.i,
        "et": [str(x) for x in s.e],
        "ft": [str(x) for x in s.f],
        "mt": [str(x) for x in s.m],
        "likes": [_like(t) for t in terms],
        "idlikes": [_like(str(x)) for x in [*s.e, *s.f, *s.m, *s.i]],
        "etlikes": [_like(str(x)) for x in s.e],
    }
    jobs_pred = "o.workspace_id = :ws AND (o.subject_id = ANY(:i) OR o.subject_id = ANY(:m) OR o.subject_id = ANY(:e))"
    spec: list[tuple[str, str, str, bool]] = [
        # (item_kind, predicate over alias t, action, only when `terms` / ids are given)
        ("interaction_contents", "t.interaction_id = ANY(:i)", "delete", False),
        ("interaction_stages", "t.interaction_id = ANY(:i)", "delete", False),
        ("interaction_subjects", "t.interaction_id = ANY(:i) OR t.entity_id = ANY(:e)", "delete", False),
        ("graph_episodes", "t.interaction_id = ANY(:i)", "delete", False),
        ("entity_mentions", "t.interaction_id = ANY(:i) OR t.entity_id = ANY(:e)", "delete", False),
        ("attribute_claims", "t.interaction_id = ANY(:i) OR t.entity_id = ANY(:e)", "delete", False),
        ("fact_evidence", "t.fact_id = ANY(:f) OR t.interaction_id = ANY(:i)", "delete", False),
        ("entity_external_ids", "t.entity_id = ANY(:e)", "delete", False),
        ("identity_non_matches", "t.entity_a = ANY(:e) OR t.entity_b = ANY(:e)", "delete", False),
        ("entity_lineage", "t.entity_id = ANY(:e) OR t.related_id = ANY(:e)", "delete", False),
        ("attribute_corrections", "t.entity_id = ANY(:e)", "delete", False),
        (
            "corrections",
            "(t.target_kind = 'entity' AND (t.target_id = ANY(:e) OR t.new_target_id = ANY(:e))) OR (t.target_kind = 'fact' AND (t.target_id = ANY(:f) OR t.new_target_id = ANY(:f))) "
            "OR (t.target_kind = 'memory' AND (t.target_id = ANY(:m) OR t.new_target_id = ANY(:m)))",
            "delete",
            False,
        ),
        ("memory_scopes", "t.memory_id = ANY(:m)", "delete", False),
        ("memory_subjects", "t.memory_id = ANY(:m) OR t.entity_id = ANY(:e)", "delete", False),
        (
            "memory_evidence",
            "t.memory_id = ANY(:m) OR t.source_memory_id = ANY(:m) OR t.fact_id = ANY(:f) OR t.interaction_id = ANY(:i)",
            "delete",
            False,
        ),
        ("memory_lineage", "t.memory_id = ANY(:m) OR t.related_id = ANY(:m)", "delete", False),
        ("memory_embeddings", "t.memory_id = ANY(:m)", "delete", False),
        (
            "review_items",
            "t.item_type <> 'deletion_exception' AND (t.subject_entity_id = ANY(:e) OR t.candidate_entity_id = ANY(:e) OR t.fact_id = ANY(:f) OR t.competing_fact_id = ANY(:f) "
            "OR t.memory_id = ANY(:m) OR t.competing_memory_id = ANY(:m) OR t.interaction_id = ANY(:i))",
            "delete",
            False,
        ),
        ("outbox_jobs", "t.subject_id = ANY(:i) OR t.subject_id = ANY(:m) OR t.subject_id = ANY(:e)", "delete", False),
        ("job_attempts", f"t.job_id IN (SELECT o.id FROM outbox_jobs o WHERE {jobs_pred})", "delete", False),  # noqa: S608
        (
            "context_items",
            "(t.item_kind = 'fact' AND t.item_id = ANY(:ft)) OR (t.item_kind = 'memory' AND t.item_id = ANY(:mt))",
            "delete",
            False,
        ),
        (
            "context_debug_snapshots",
            "t.context_id IN (SELECT i2.context_id FROM context_items i2 WHERE i2.workspace_id = :ws AND ((i2.item_kind = 'fact' AND i2.item_id = ANY(:ft)) OR (i2.item_kind = 'memory' AND i2.item_id = ANY(:mt))))",
            "delete",
            False,
        ),
        ("principal_scopes", "t.scope_kind = 'subject' AND t.scope_ref = ANY(:et)", "delete", False),
        ("context_traces", "t.request::text LIKE ANY(CAST(:etlikes AS text[]))", "redact", False),
        (
            "commands",
            "t.payload::text LIKE ANY(CAST(:likes AS text[])) OR t.payload::text LIKE ANY(CAST(:idlikes AS text[]))",
            "redact",
            True,
        ),
        (
            "idempotency_records",
            "t.response::text LIKE ANY(CAST(:likes AS text[])) OR t.response::text LIKE ANY(CAST(:idlikes AS text[]))",
            "delete",
            True,
        ),
        (
            "review_items",
            "t.item_type <> 'deletion_exception' AND (t.rationale_text LIKE ANY(CAST(:likes AS text[])) OR t.detail::text LIKE ANY(CAST(:likes AS text[])))",
            "redact",
            True,
        ),
        ("corrections", "t.reason_text LIKE ANY(CAST(:likes AS text[]))", "redact", True),
    ]
    counts: dict[str, int] = {}
    for kind, pred, action, needs_terms in spec:
        if needs_terms and not (terms or s.e or s.f or s.m or s.i):
            continue
        p_local = p
        if kind == "context_traces" and not s.e:
            continue
        store, table, key = KEYS[kind]
        if action == "redact" and kind == "corrections":
            continue  # a correction row naming the subject in free text is deleted with the target; a stray mention elsewhere is left to the residual scan
        res = await conn.execute(
            text(
                f"INSERT INTO deletion_items (request_id, workspace_id, store, item_kind, item_id, action) "  # noqa: S608 (KEYS and the predicates above are constants)
                f"SELECT :r, :ws, '{store}', '{kind}', {key}, '{action}' FROM {table} t WHERE t.workspace_id = :ws AND ({pred}) ON CONFLICT DO NOTHING"
            ),
            p_local,
        )
        counts[kind] = counts.get(kind, 0) + int(res.rowcount)
    return counts
