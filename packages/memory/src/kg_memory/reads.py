"""Scoped reads of curated memory: one record, or a page of them. Every query carries the workspace predicate, the classification ceiling and the scope
visibility predicate; a memory the caller cannot see is `NotFound`, identical to an id that never existed (no existence oracle)."""

from __future__ import annotations

import builtins
import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from kg_common.errors import NotFound
from kg_common.pagination import clamp_limit, decode_cursor, encode_cursor
from kg_domain.envelope import Page
from kg_domain.principal import Principal
from kg_governance.grants import SubjectFilter
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

from kg_memory import store
from kg_memory.models import (
    EmbeddingOut,
    LineageOut,
    MemoryEvidenceOut,
    MemoryOut,
    MemorySummary,
    ScopeOut,
)
from kg_memory.scopes import ScopeGrants, visibility_sql

_CAUTION_HEAD = (
    "SELECT m.id, m.summary, r.id AS review_id FROM curated_memories m "
    "JOIN review_items r ON r.workspace_id = m.workspace_id AND r.memory_id = m.id AND r.item_type = 'source_compromised' AND r.status = 'open' "
    "WHERE m.status = 'disputed'"
)
_CAUTION_TAIL = (
    "AND EXISTS (SELECT 1 FROM memory_subjects ms WHERE ms.workspace_id = m.workspace_id AND ms.memory_id = m.id "
    "AND ms.entity_id = ANY(CAST(:subjects AS uuid[]))) ORDER BY m.recorded_at, m.id LIMIT 20"
)


@dataclass(frozen=True)
class ContextInfo:
    """What the context engine needs about a memory it already found through `MemorySearch`: its evidence pointers and its structured claim (P6)."""

    evidence: tuple[tuple[str, str, str | None], ...]  # (source_kind, id, external_ref) in the order they were added
    related_fact_ids: tuple[uuid.UUID, ...]
    assertion: dict[str, Any] | None
    importance: float
    created_by: uuid.UUID
    origin: str = ""  # explicit | proposal | extracted | working | published | consolidated


@dataclass(frozen=True)
class Viewer:
    """Who is asking, with what: built once per request (grants are loaded once)."""

    principal: Principal
    grants: ScopeGrants
    curator: bool = False  # memory.curate: also sees proposed / disputed / retired memory
    reveal_private: bool = False  # memory.read_private AND the caller asked for it: audited by the route
    subjects: SubjectFilter = (
        SubjectFilter()
    )  # P7: the entities a response may NAME (a memory stays visible by its scopes; a hidden person's id is omitted)

    @property
    def workspace_id(self) -> uuid.UUID:
        return self.principal.workspace_id


def visible_where(
    v: Viewer, *, alias: str = "m", run_id: str | None = None, all_runs: bool = False, prefix: str = "v", status_rule: bool = True
) -> tuple[str, dict[str, Any]]:
    """The predicate EVERY memory read shares: workspace, classification ceiling, status rule, scope visibility. Search passes `status_rule=False` and
    applies its own explicit status list (`as_of` can bring superseded memory back for anyone who may see its scopes)."""
    scope_sql, params = visibility_sql(
        v.grants, run_id=run_id, all_runs=all_runs, reveal_private=v.reveal_private, memory=alias, prefix=prefix
    )
    params[f"{prefix}_ws"] = v.workspace_id
    params[f"{prefix}_classes"] = v.grants.classifications()
    status_sql = ""
    if status_rule:
        params[f"{prefix}_curator"] = v.curator
        params[f"{prefix}_self"] = v.principal.principal_id
        status_sql = f"AND (:{prefix}_curator OR {alias}.status = 'active' OR {alias}.created_by = :{prefix}_self) "
    # a memory frozen by a deletion request (P8, SEC-05) is invisible to everyone, curators included, and search applies the same rule to its own status list
    sql = (
        f"{alias}.workspace_id = :{prefix}_ws AND {alias}.status <> 'deletion_pending' AND {alias}.classification = ANY(CAST(:{prefix}_classes AS text[])) "
        f"{status_sql}AND {scope_sql}"
    )
    return sql, params


def shown_scopes(v: Viewer, scopes: list[tuple[str, str]]) -> list[ScopeOut]:
    """Only the scopes this viewer can use: a memory that also lives in someone else's private scope does not say so."""
    out: list[ScopeOut] = []
    for kind, ref in scopes:
        if kind == "workspace" or v.grants.can_use(kind, ref) or (v.reveal_private and kind in ("agent", "session")):
            out.append(ScopeOut(kind=kind, ref=ref))
    return out


def _summary(r: Row[Any], v: Viewer, scopes: list[tuple[str, str]], subjects: list[uuid.UUID]) -> dict[str, Any]:
    return {
        "id": r.id,
        "type": r.type,
        "origin": r.origin,
        "status": r.status,
        "summary": r.summary,
        "authority_class": r.authority_class,
        "evidence_status": r.evidence_status,
        "classification": r.classification,
        "importance": float(r.importance),
        "created_by": r.created_by,
        "recorded_at": r.recorded_at,
        "effective_from": r.effective_from,
        "effective_until": r.effective_until,
        "expires_at": r.expires_at,
        "embedding_status": r.embedding_status,
        "revision": r.revision,
        "scopes": shown_scopes(v, scopes),
        "subject_ids": subjects,
    }


class MemoryReads:
    def __init__(self, embedding_version: int):
        self._embedding_version = embedding_version

    async def visible_row(
        self, conn: AsyncConnection, v: Viewer, memory_id: uuid.UUID, *, lock: bool = False, run_id: str | None = None
    ) -> Row[Any]:
        where, params = visible_where(
            v, run_id=run_id, all_runs=True
        )  # naming a record is not a search: a principal reaches ITS OWN session memory by id
        row = (
            await conn.execute(
                text(f"SELECT m.* FROM curated_memories m WHERE m.id = :mid AND {where}{' FOR UPDATE OF m' if lock else ''}"),  # noqa: S608 (constant fragments)
                {**params, "mid": memory_id},
            )
        ).first()
        if row is None:
            raise NotFound()
        return row

    async def get(self, conn: AsyncConnection, v: Viewer, memory_id: uuid.UUID) -> MemoryOut:
        r = await self.visible_row(conn, v, memory_id)
        ws = v.workspace_id
        scopes = (await store.load_scopes(conn, ws, [r.id]))[r.id]
        subjects = (await store.load_subjects(conn, ws, [r.id], subjects=v.subjects))[r.id]
        evidence = [
            MemoryEvidenceOut(
                source_kind=e.source_kind,
                interaction_id=e.interaction_id,
                episode_uuid=e.episode_uuid,
                fact_id=e.fact_id,
                command_id=e.command_id,
                memory_id=e.source_memory_id,
                external_ref=e.external_ref,
                added_at=e.added_at,
            )
            for e in await store.load_evidence(conn, ws, r.id)
        ]
        emb = (
            await conn.execute(
                text(
                    "SELECT embedding_model_id, embedding_version, embedding_dim, embedded_at FROM memory_embeddings "
                    "WHERE workspace_id = :ws AND memory_id = :m AND embedding_version = :v"
                ),
                {"ws": ws, "m": r.id, "v": self._embedding_version},
            )
        ).first()
        return MemoryOut(
            **_summary(r, v, scopes, subjects["subject"]),
            content=r.content,
            assertion=r.assertion,
            topic_ids=subjects["topic"],
            confidence=float(r.confidence) if r.confidence is not None else None,
            occurred_at=r.occurred_at,
            last_confirmed_at=r.last_confirmed_at,
            run_id=r.run_id,
            purpose=r.purpose,
            derived_by_alias=r.derived_by_alias,
            pipeline_version=r.pipeline_version,
            prompt_version=r.prompt_version,
            command_id=r.command_id,
            evidence=evidence,
            lineage=await self._lineage(conn, v, r.id),
            embedding=EmbeddingOut(
                status=r.embedding_status,
                model_id=emb.embedding_model_id if emb else None,
                version=emb.embedding_version if emb else None,
                dim=emb.embedding_dim if emb else None,
                embedded_at=emb.embedded_at if emb else None,
            ),
        )

    async def _lineage(self, conn: AsyncConnection, v: Viewer, memory_id: uuid.UUID) -> list[LineageOut]:
        ws = v.workspace_id
        rows = (
            await conn.execute(
                text(
                    "SELECT memory_id, related_id, relation FROM memory_lineage WHERE workspace_id = :ws AND (memory_id = :m OR related_id = :m) "
                    "ORDER BY created_at, memory_id, related_id"
                ),
                {"ws": ws, "m": memory_id},
            )
        ).all()
        others = {(x.related_id if x.memory_id == memory_id else x.memory_id) for x in rows}
        if not others:
            return []
        where, params = visible_where(v, prefix="lv", all_runs=True)
        seen = {
            x.id
            for x in (
                await conn.execute(
                    text(f"SELECT m.id FROM curated_memories m WHERE m.id = ANY(:ids) AND {where}"),  # noqa: S608 (constant fragments)
                    {**params, "ids": sorted(others, key=str)},
                )
            ).all()
        }
        names = {
            ("replaces", True): "replaces",
            ("replaces", False): "replaced_by",
            ("published_from", True): "published_from",
            ("published_from", False): "published_as",
            ("consolidated_from", True): "consolidated_from",
            ("consolidated_from", False): "consolidated_into",
        }
        out: list[LineageOut] = []
        for x in rows:
            mine = x.memory_id == memory_id
            other = x.related_id if mine else x.memory_id
            if other in seen:
                out.append(LineageOut(relation=x.relation, memory_id=other, direction=names[(x.relation, mine)]))
        return out

    async def list(
        self,
        conn: AsyncConnection,
        v: Viewer,
        *,
        type_: str | None,
        status: str | None,
        subject_id: uuid.UUID | None,
        project: str | None,
        origin: str | None,
        classification: str | None,
        created_by: uuid.UUID | None,
        updated_since: dt.datetime | None,
        limit: int | None,
        cursor: str | None,
    ) -> Page[MemorySummary]:
        size, before = clamp_limit(limit), decode_cursor(cursor)
        where, params = visible_where(
            v, run_id=None, all_runs=True
        )  # the owner's own session memory is listed too (never another principal's)
        params["n"] = size + 1
        clauses = [where]
        if type_:
            clauses.append("m.type = :type")
            params["type"] = type_
        if status:
            clauses.append("m.status = :status")
            params["status"] = status
        elif not v.curator:
            clauses.append("m.status = 'active'")  # a non-curator's default list is the live memory, even for its own proposals
        if origin:
            clauses.append("m.origin = :origin")
            params["origin"] = origin
        if classification:
            clauses.append("m.classification = :cls")
            params["cls"] = classification
        if created_by:
            clauses.append("m.created_by = :creator")
            params["creator"] = created_by
        if updated_since:
            clauses.append("m.recorded_at >= :since")
            params["since"] = updated_since
        if subject_id:
            clauses.append(
                "EXISTS (SELECT 1 FROM memory_subjects ms WHERE ms.workspace_id = m.workspace_id AND ms.memory_id = m.id AND ms.entity_id = :subject)"
            )
            params["subject"] = subject_id
        if project:
            clauses.append(
                "EXISTS (SELECT 1 FROM memory_scopes ps WHERE ps.workspace_id = m.workspace_id AND ps.memory_id = m.id "
                "AND ps.scope_kind = 'project' AND ps.scope_ref = :project)"
            )
            params["project"] = project
        if before:
            clauses.append("(m.recorded_at, m.id) < (:c_at, :c_id)")
            params["c_at"], params["c_id"] = before[0], before[1]
        sql = f"SELECT m.* FROM curated_memories m WHERE {' AND '.join(clauses)} ORDER BY m.recorded_at DESC, m.id DESC LIMIT :n"  # noqa: S608
        rows = (await conn.execute(text(sql), params)).all()
        page, more = rows[:size], len(rows) > size
        ids = [r.id for r in page]
        scopes = await store.load_scopes(conn, v.workspace_id, ids)
        subjects = await store.load_subjects(conn, v.workspace_id, ids, subjects=v.subjects)
        items = [MemorySummary(**_summary(r, v, scopes[r.id], subjects[r.id]["subject"])) for r in page]
        return Page[MemorySummary](items=items, next_cursor=encode_cursor(page[-1].recorded_at, page[-1].id) if more else None)

    async def context_info(self, conn: AsyncConnection, v: Viewer, ids: builtins.list[uuid.UUID]) -> dict[uuid.UUID, ContextInfo]:
        """Evidence pointers and assertion of the memories among `ids` that THIS viewer may see (the same predicate as every memory read): an invisible id is simply
        absent, so a caller cannot learn anything about it from this call."""
        if not ids:
            return {}
        where, params = visible_where(v, prefix="ci", all_runs=True)
        sql = f"SELECT m.id, m.assertion, m.importance, m.created_by, m.origin FROM curated_memories m WHERE m.id = ANY(:ids) AND {where}"  # noqa: S608 (constant fragments)
        visible = {r.id: r for r in (await conn.execute(text(sql), {**params, "ids": ids})).all()}
        if not visible:
            return {}
        evidence = await store.load_evidence_many(conn, v.workspace_id, list(visible))
        out: dict[uuid.UUID, ContextInfo] = {}
        for mid, r in visible.items():
            rows = evidence.get(mid, [])
            pointers: builtins.list[tuple[str, str, str | None]] = []
            facts: builtins.list[uuid.UUID] = []
            for e in rows:
                if e.source_kind == "fact" and e.fact_id is not None:
                    facts.append(e.fact_id)
                    pointers.append(("fact", str(e.fact_id), None))
                elif e.source_kind == "interaction" and e.interaction_id is not None:
                    pointers.append(("interaction", str(e.interaction_id), None))
                elif e.source_kind == "command" and e.command_id is not None:
                    pointers.append(("command", str(e.command_id), None))
                elif e.source_kind == "memory" and e.source_memory_id is not None:
                    pointers.append(("memory", str(e.source_memory_id), None))
                elif e.source_kind == "episode" and e.episode_uuid is not None:
                    pointers.append(("episode", str(e.episode_uuid), None))
                elif e.external_ref is not None:
                    pointers.append(("external", e.external_ref, e.external_ref))
            out[mid] = ContextInfo(
                evidence=tuple(pointers),
                related_fact_ids=tuple(dict.fromkeys(facts)),
                assertion=r.assertion,
                importance=float(r.importance),
                created_by=r.created_by,
                origin=r.origin,
            )
        return out

    async def assertion_keys(
        self,
        conn: AsyncConnection,
        v: Viewer,
        subject_ids: builtins.list[uuid.UUID],
        predicates: builtins.list[str],
        *,
        as_of: dt.datetime | None,
    ) -> set[tuple[uuid.UUID, str]]:
        """(subject, predicate) pairs for which an ACTIVE memory this viewer may see makes a structured claim: the memory half of an expected-signal check (P6).
        Authorised in SQL like every read; nothing about a memory the viewer cannot see can influence the answer."""
        if not subject_ids or not predicates:
            return set()
        where, params = visible_where(v, prefix="as", all_runs=False)
        rows = (
            await conn.execute(
                text(
                    f"SELECT DISTINCT ms.entity_id, m.assertion->>'predicate' AS predicate FROM curated_memories m "  # noqa: S608 (constant fragments)
                    "JOIN memory_subjects ms ON ms.workspace_id = m.workspace_id AND ms.memory_id = m.id AND ms.role = 'subject' "
                    f"WHERE m.status = 'active' AND m.assertion IS NOT NULL AND m.assertion->>'predicate' = ANY(CAST(:preds AS text[])) "
                    "AND ms.entity_id = ANY(CAST(:subjects AS uuid[])) "
                    "AND m.effective_from <= COALESCE(CAST(:as_of AS timestamptz), now()) "
                    "AND (m.effective_until IS NULL OR m.effective_until > COALESCE(CAST(:as_of AS timestamptz), now())) "
                    f"AND (m.expires_at IS NULL OR m.expires_at > COALESCE(CAST(:as_of AS timestamptz), now())) AND {where}"
                ),
                {**params, "preds": predicates, "subjects": [str(s) for s in subject_ids], "as_of": as_of},
            )
        ).all()
        return {(r.entity_id, r.predicate) for r in rows}

    async def open_conflicts(
        self, conn: AsyncConnection, v: Viewer, subject_ids: builtins.list[uuid.UUID]
    ) -> builtins.list[dict[str, Any]]:
        """Open memory conflicts about these subjects whose BOTH memories this viewer may see (P6): `{review_id, candidate, competing}` as plain dicts. A conflict that
        involves a memory the viewer cannot see is absent, not redacted."""
        if not subject_ids:
            return []
        rows = (
            await conn.execute(
                text(
                    "SELECT id, memory_id, competing_memory_id FROM review_items WHERE workspace_id = :ws AND item_type = 'memory_conflict' AND status = 'open' "
                    "AND memory_id IS NOT NULL AND competing_memory_id IS NOT NULL ORDER BY created_at, id LIMIT 50"
                ),
                {"ws": v.workspace_id},
            )
        ).all()
        wanted = {str(s) for s in subject_ids}
        out: builtins.list[dict[str, Any]] = []
        for r in rows:
            views = {x["role"]: x for x in await self.review_views(conn, v, r.memory_id, r.competing_memory_id)}
            if len(views) != 2:
                continue
            if not wanted & {str(s) for x in views.values() for s in x["subject_ids"]}:
                continue
            out.append({"review_id": r.id, "candidate": views["candidate"], "competing": views["competing"]})
        return out

    async def compromised_cautions(
        self, conn: AsyncConnection, v: Viewer, subject_ids: builtins.list[uuid.UUID]
    ) -> builtins.list[dict[str, Any]]:
        """Memories about these subjects that were quarantined because their source was compromised (`disputed` plus an open `source_compromised` review) and whose
        scopes THIS viewer may use (P7). A context shows them only as a caution, never as knowledge. Authorised in SQL like every memory read."""
        if not subject_ids:
            return []
        where, params = visible_where(v, prefix="cc", all_runs=False, status_rule=False)
        sql = f"{_CAUTION_HEAD} AND {where} {_CAUTION_TAIL}"  # noqa: S608 (constant fragments; values are bound)
        rows = (await conn.execute(text(sql), {**params, "subjects": [str(s) for s in subject_ids]})).all()
        subjects = await store.load_subjects(conn, v.workspace_id, [r.id for r in rows], subjects=v.subjects)
        return [{"memory_id": r.id, "summary": r.summary, "review_id": r.review_id, "subject_ids": subjects[r.id]["subject"]} for r in rows]

    async def count_visible(self, conn: AsyncConnection, v: Viewer, entity_id: uuid.UUID) -> int:
        """Memories about an entity that THIS viewer may see (the `memories` count on an entity read): never a count of what it may not."""
        where, params = visible_where(v)
        row = (
            await conn.execute(
                text(
                    f"SELECT count(*) AS n FROM curated_memories m WHERE m.status = 'active' AND {where} AND EXISTS (SELECT 1 FROM memory_subjects ms "  # noqa: S608
                    "WHERE ms.workspace_id = m.workspace_id AND ms.memory_id = m.id AND ms.entity_id = :entity)"
                ),
                {**params, "entity": entity_id},
            )
        ).one()
        return int(row.n)

    async def review_views(
        self,
        conn: AsyncConnection,
        v: Viewer,
        candidate: uuid.UUID | None,
        competing: uuid.UUID | None,
        *,
        sources_of: uuid.UUID | None = None,
    ) -> builtins.list[dict[str, Any]]:  # `list` is this class's method
        """The memories a review item is about, as THIS viewer may see them (an invisible one is simply absent). Plain dicts: the review model lives in kg_knowledge."""
        out: builtins.list[dict[str, Any]] = []
        pairs: builtins.list[tuple[str, uuid.UUID | None]] = [("competing", competing), ("candidate", candidate)]
        if sources_of is not None:  # a consolidation proposal: the memories it would fold in, as this viewer may see them
            folded = (
                await conn.execute(
                    text(
                        "SELECT related_id FROM memory_lineage WHERE workspace_id = :ws AND memory_id = :m AND relation = 'consolidated_from' ORDER BY related_id"
                    ),
                    {"ws": v.workspace_id, "m": sources_of},
                )
            ).all()
            pairs += [("source", r.related_id) for r in folded]
        for role, mid in pairs:
            if mid is None:
                continue
            try:
                r = await self.visible_row(conn, v, mid)
            except NotFound:
                continue
            scopes = (await store.load_scopes(conn, v.workspace_id, [mid]))[mid]
            subjects = (await store.load_subjects(conn, v.workspace_id, [mid], subjects=v.subjects))[mid]["subject"]
            out.append(
                {
                    "role": role,
                    "id": r.id,
                    "type": r.type,
                    "origin": r.origin,
                    "status": r.status,
                    "summary": r.summary,
                    "content": r.content,
                    "authority_class": r.authority_class,
                    "evidence_status": r.evidence_status,
                    "classification": r.classification,
                    "scopes": [{"kind": s.kind, "ref": s.ref} for s in shown_scopes(v, scopes)],
                    "subject_ids": subjects,
                    "created_by": r.created_by,
                    "recorded_at": r.recorded_at,
                }
            )
        return out
