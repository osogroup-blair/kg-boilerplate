"""Entity, relationship and command reads. Postgres is authoritative (never the graph), so a read is immediately consistent with its write.

Valid time and record time are independent (ADR-017):
    as_of(T)    valid_from <= T < valid_to          (an unknown valid_from counts as 'always')
    known_at(T) recorded_at <= T and not superseded by T
    current     as_of(now) and known_at(now) and status = 'active'
`now` is the database clock (M12)."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from typing import Any, Literal

from kg_common.errors import NotFound
from kg_common.pagination import clamp_limit, decode_cursor, encode_cursor
from kg_domain.envelope import Page
from kg_domain.principal import WorkspaceContext
from kg_governance.grants import ScopeGrants, SubjectFilter, entity_visible_sql, fact_visible_sql
from kg_obs import metrics
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

from kg_knowledge.models import (
    REDACTED_VALUE,
    CommandDetailOut,
    CommandSummary,
    EntityDetailOut,
    EntityOut,
    EvidenceOut,
    ExternalId,
    FactEndpoint,
    FactOut,
)
from kg_knowledge.specs import SpecCache

_EXTERNAL_IDS = (
    "(SELECT COALESCE(jsonb_agg(jsonb_build_object('system', x.system, 'value', x.value) ORDER BY x.system, x.value), '[]'::jsonb) "
    "FROM entity_external_ids x WHERE x.workspace_id = e.workspace_id AND x.entity_id = e.id) AS external_ids"
)
FACT_SELECT = """
    SELECT f.*, s.type_key AS s_type, s.display_name AS s_name, t.type_key AS t_type, t.display_name AS t_name,
           CASE WHEN f.valid_from > now() THEN 'future' WHEN f.valid_to IS NOT NULL AND f.valid_to <= now() THEN 'historical' ELSE 'current' END AS phase
    FROM facts f JOIN entities s ON s.workspace_id = f.workspace_id AND s.id = f.source_entity_id
                 JOIN entities t ON t.workspace_id = f.workspace_id AND t.id = f.target_entity_id
"""


def visibility_sql(*, include_history: bool, known_at_given: bool) -> str:
    """Which facts a read shows (ADR-017). Binds :as_of, :known_at (both may be NULL = now) and :extras (statuses asked for with include=).

    current   status = active, valid at as_of (an unknown start counts as always), known at known_at
    history   every fact that was ever true or disputed (not proposed or rejected), optionally as known at known_at
    extras    proposed / disputed / rejected facts are added on request, always labelled by their status"""
    known = (
        "f.recorded_at <= COALESCE(CAST(:known_at AS timestamptz), now()) "
        "AND (f.superseded_at IS NULL OR f.superseded_at > COALESCE(CAST(:known_at AS timestamptz), now()))"
    )
    if include_history:
        history = "f.status NOT IN ('deleted','proposed','rejected','deletion_pending')" + (f" AND {known}" if known_at_given else "")
        return f"(({history}) OR f.status = ANY(CAST(:extras AS text[])))"
    current = (
        "f.status = 'active' AND (f.valid_from IS NULL OR f.valid_from <= COALESCE(CAST(:as_of AS timestamptz), now())) "
        f"AND (f.valid_to IS NULL OR f.valid_to > COALESCE(CAST(:as_of AS timestamptz), now())) AND {known}"
    )
    return f"(({current}) OR f.status = ANY(CAST(:extras AS text[])))"


def _redact(attrs: dict[str, Any], sensitive: set[str], allowed: bool) -> dict[str, Any]:
    if allowed:
        return dict(attrs)
    return {k: (dict(REDACTED_VALUE) if k in sensitive else v) for k, v in attrs.items()}


_LEDGER_ONLY = (  # the knowledge commands: memory (P5) and principal-management (P7) commands are read through their own, scope-aware surfaces
    "command_type NOT LIKE 'memory.%' AND command_type NOT LIKE 'principal.%' AND command_type NOT LIKE 'key.%' AND command_type NOT LIKE 'grant.%' "
    "AND command_type NOT LIKE 'settings.%' AND command_type NOT LIKE 'deletion.%' AND command_type <> 'correction.memory'"
)
_MAX_CLASS_HEAD = (
    "SELECT f.classification FROM facts f JOIN entities s ON s.workspace_id = f.workspace_id AND s.id = f.source_entity_id "
    "JOIN entities t ON t.workspace_id = f.workspace_id AND t.id = f.target_entity_id "
    "WHERE f.workspace_id = :ws AND f.status IN ('active','disputed','proposed')"
)
_MAX_CLASS_TAIL = (
    "AND (f.source_entity_id = ANY(:ids) OR f.target_entity_id = ANY(:ids)) "
    "ORDER BY array_position(ARRAY['public','internal','confidential','restricted','local_only'], f.classification) DESC LIMIT 1"
)


class Reads:
    def __init__(self, specs: SpecCache):
        self._specs = specs

    async def subject_filter(self, conn: AsyncConnection, ws: uuid.UUID, grants: ScopeGrants) -> SubjectFilter:
        """The caller's entity/fact filter under the ACTIVE ontology (P7, ADR-037). A workspace with no `subject_scoped` type gets an unenforced filter."""
        return grants.subject_filter(await self._specs.scoped_types(conn, ws))

    @staticmethod
    def hidden(surface: str, n: int = 1) -> None:
        """Counts how often the subject filter hid something (no ids: `acl_hidden_total{surface}`)."""
        if n > 0:
            metrics.acl_hidden_total.labels(surface=surface).inc(n)

    async def _sensitive(
        self, conn: AsyncConnection, ws: uuid.UUID, version_id: uuid.UUID, type_key: str, *, relationship: bool
    ) -> set[str]:
        """Sensitive in the version the row was written under OR in the active one: a later version can only add protection."""
        names: set[str] = set()
        versions = [await self._specs.get(conn, ws, version_id)]
        active = await self._specs.active(conn, ws)
        if active is not None:
            versions.append(active[1])
        for spec in versions:
            declared = (spec.relationship_types if relationship else spec.entity_types).get(type_key)
            if declared is not None:
                names |= {k for k, a in declared.attributes.items() if a.sensitive}
        return names

    # ---- entities -------------------------------------------------------------------------------------------------------------------------

    async def _entity_out(self, conn: AsyncConnection, ws: uuid.UUID, row: Row[Any], can_see_sensitive: bool) -> EntityOut:
        sensitive = await self._sensitive(conn, ws, row.ontology_version_id, row.type_key, relationship=False)
        return EntityOut(
            id=row.id,
            type=row.type_key,
            name=row.display_name,
            aliases=list(row.aliases),
            attributes=_redact(row.attributes or {}, sensitive, can_see_sensitive),
            classification=row.classification,
            status=row.status,
            origin=row.origin,
            merged_into_id=row.merged_into_id,
            revision=row.revision,
            external_ids=[ExternalId.model_validate(x) for x in row.external_ids],
            ontology_version_id=row.ontology_version_id,
            recorded_at=row.recorded_at,
            updated_at=row.updated_at,
            projection_status=row.projection_status,
            embedding_status=row.embedding_status,
            projected_at=row.projected_at,
        )

    async def list_entities(
        self,
        conn: AsyncConnection,
        ctx: WorkspaceContext,
        *,
        can_see_sensitive: bool,
        subjects: SubjectFilter,
        type_key: str | None,
        q: str | None,
        status: str | None,
        limit: int | None,
        cursor: str | None,
    ) -> Page[EntityOut]:
        size, after = clamp_limit(limit), decode_cursor(cursor)
        where, params = ["e.workspace_id = :ws"], {"ws": ctx.workspace_id, "n": size + 1}
        acl, acl_params = entity_visible_sql(subjects, alias="e")
        where.append(acl)
        params.update(acl_params)
        if type_key:
            where.append("e.type_key = :type")
            params["type"] = type_key
        if status:
            where.append("e.status = :status")
            params["status"] = status
        else:
            where.append("e.status <> 'merged'")  # a merged entity is reachable by id (it redirects), but it is not a directory entry
        if q:
            where.append("lower(e.display_name) LIKE '%' || :q || '%' ESCAPE '\\'")
            params["q"] = q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        if after:
            where.append("(e.recorded_at, e.id) > (:c_at, :c_id)")
            params["c_at"], params["c_id"] = after[0], after[1]
        sql = f"SELECT e.*, {_EXTERNAL_IDS} FROM entities e WHERE {' AND '.join(where)} ORDER BY e.recorded_at, e.id LIMIT :n"  # noqa: S608 (constant fragments)
        rows = (await conn.execute(text(sql), params)).all()
        page, more = rows[:size], len(rows) > size
        items = [await self._entity_out(conn, ctx.workspace_id, r, can_see_sensitive) for r in page]
        return Page[EntityOut](items=items, next_cursor=encode_cursor(page[-1].recorded_at, page[-1].id) if more else None)

    async def entities_by_ids(
        self, conn: AsyncConnection, ctx: WorkspaceContext, ids: Sequence[uuid.UUID], *, can_see_sensitive: bool, subjects: SubjectFilter
    ) -> list[EntityOut]:
        """The active entities among `ids`, in the order asked. A merged entity answers as the entity it was merged into; an id that does not exist in this workspace
        (or lives in another) is simply absent, exactly like a deleted one: the caller cannot tell them apart."""
        wanted: list[uuid.UUID] = list(dict.fromkeys(ids))
        found: dict[uuid.UUID, Row[Any]] = {}
        resolve: dict[uuid.UUID, uuid.UUID] = {}
        todo = list(wanted)
        for _ in range(3):  # a merge chain is shallow; bounded all the same
            if not todo:
                break
            acl, acl_params = entity_visible_sql(subjects, alias="e")
            rows = (
                await conn.execute(
                    text(f"SELECT e.*, {_EXTERNAL_IDS} FROM entities e WHERE e.workspace_id = :ws AND e.id = ANY(:ids) AND {acl}"),  # noqa: S608
                    {"ws": ctx.workspace_id, "ids": todo, **acl_params},
                )
            ).all()
            todo = []
            for r in rows:
                if r.status == "merged" and r.merged_into_id is not None:
                    resolve[r.id] = r.merged_into_id
                    todo.append(r.merged_into_id)
                elif r.status == "active":
                    found[r.id] = r
        out: list[EntityOut] = []
        seen: set[uuid.UUID] = set()
        for want in wanted:
            canonical = want
            for _ in range(3):
                canonical = resolve.get(canonical, canonical)
            row = found.get(canonical)
            if row is not None and canonical not in seen:
                seen.add(canonical)
                out.append(await self._entity_out(conn, ctx.workspace_id, row, can_see_sensitive))
        return out

    async def max_fact_classification(
        self, conn: AsyncConnection, ctx: WorkspaceContext, entity_ids: Sequence[uuid.UUID], *, subjects: SubjectFilter
    ) -> str | None:
        """The highest classification among the current facts that touch these entities and that the caller may see (P6: the egress classification of a task about them;
        a fact the caller cannot see must not change how its task is treated)."""
        if not entity_ids:
            return None
        acl, acl_params = fact_visible_sql(subjects, source="s", target="t")
        sql = f"{_MAX_CLASS_HEAD} AND {acl} {_MAX_CLASS_TAIL}"  # noqa: S608 (constant fragments; the filter is bound parameters)
        row = (await conn.execute(text(sql), {"ws": ctx.workspace_id, "ids": list(entity_ids), **acl_params})).first()
        return str(row.classification) if row else None

    async def open_review_links(
        self, conn: AsyncConnection, ctx: WorkspaceContext, fact_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, uuid.UUID]:
        """fact id -> the id of the open contradiction review that concerns it (as the claim or as the fact it competes with). Ids only: the item is read through the
        review API, which has its own authorisation (P6 context conflicts)."""
        if not fact_ids:
            return {}
        rows = (
            await conn.execute(
                text(
                    "SELECT id, fact_id, competing_fact_id FROM review_items WHERE workspace_id = :ws AND item_type = 'contradiction' AND status = 'open' "
                    "AND (fact_id = ANY(:ids) OR competing_fact_id = ANY(:ids)) ORDER BY created_at, id"
                ),
                {"ws": ctx.workspace_id, "ids": list(fact_ids)},
            )
        ).all()
        out: dict[uuid.UUID, uuid.UUID] = {}
        for r in rows:
            for fid in (r.fact_id, r.competing_fact_id):
                if fid is not None:
                    out.setdefault(fid, r.id)
        return out

    async def get_entity(
        self,
        conn: AsyncConnection,
        ctx: WorkspaceContext,
        entity_id: uuid.UUID,
        *,
        can_see_sensitive: bool,
        subjects: SubjectFilter,
        include_history: bool,
        as_of: dt.datetime | None,
        known_at: dt.datetime | None,
        include: Sequence[str] = (),
    ) -> EntityDetailOut:
        acl, acl_params = entity_visible_sql(subjects, alias="e")
        row = (
            await conn.execute(
                text(f"SELECT e.*, {_EXTERNAL_IDS} FROM entities e WHERE e.workspace_id = :ws AND e.id = :id AND {acl}"),  # noqa: S608
                {"ws": ctx.workspace_id, "id": entity_id, **acl_params},
            )
        ).first()  # noqa: S608
        if row is None:
            if subjects.enforced:
                await self._note_hidden_entity(conn, ctx, entity_id, "entity")
            raise NotFound()
        base = await self._entity_out(conn, ctx.workspace_id, row, can_see_sensitive)
        facts = await self.list_facts(
            conn,
            ctx,
            entity_id=entity_id,
            direction="both",
            rel_type=None,
            as_of=as_of,
            known_at=known_at,
            include_history=include_history,
            can_see_sensitive=can_see_sensitive,
            subjects=subjects,
            include=include,
        )
        return EntityDetailOut(**base.model_dump(), relationships=facts)

    # ---- facts ----------------------------------------------------------------------------------------------------------------------------

    async def _evidence(self, conn: AsyncConnection, ws: uuid.UUID, fact_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[EvidenceOut]]:
        """Why each fact is held (KGR-05): the commands and interaction episodes behind it. Metadata only; the content stays in the ledger."""
        if not fact_ids:
            return {}
        rows = (
            await conn.execute(
                text(
                    "SELECT e.fact_id, e.source_kind, e.command_id, e.interaction_id, e.episode_uuid, e.added_at, "
                    "COALESCE(i.source_system, c.source_system) AS source_system, COALESCE(i.source_ref, c.source_ref) AS source_ref, "
                    "COALESCE(i.occurred_at, c.occurred_at) AS occurred_at, COALESCE(i.recorded_at, c.created_at) AS recorded_at "
                    "FROM fact_evidence e LEFT JOIN interactions i ON i.workspace_id = e.workspace_id AND i.id = e.interaction_id "
                    "LEFT JOIN commands c ON c.workspace_id = e.workspace_id AND c.id = e.command_id "
                    "WHERE e.workspace_id = :ws AND e.fact_id = ANY(:ids) ORDER BY e.added_at, e.id"
                ),
                {"ws": ws, "ids": fact_ids},
            )
        ).all()
        out: dict[uuid.UUID, list[EvidenceOut]] = {}
        for r in rows:
            out.setdefault(r.fact_id, []).append(
                EvidenceOut(
                    kind=r.source_kind,
                    command_id=r.command_id,
                    interaction_id=r.interaction_id,
                    episode_uuid=r.episode_uuid,
                    source_system=r.source_system,
                    source_ref=r.source_ref,
                    occurred_at=r.occurred_at,
                    recorded_at=r.recorded_at,
                    added_at=r.added_at,
                )
            )
        return out

    async def _fact_out(
        self, conn: AsyncConnection, ws: uuid.UUID, r: Row[Any], can_see_sensitive: bool, evidence: list[EvidenceOut] | None = None
    ) -> FactOut:
        sensitive = await self._sensitive(conn, ws, r.ontology_version_id, r.rel_type, relationship=True)
        if evidence is None:
            evidence = (await self._evidence(conn, ws, [r.id])).get(r.id, [])
        return FactOut(
            id=r.id,
            rel_type=r.rel_type,
            source=FactEndpoint(id=r.source_entity_id, type=r.s_type, name=r.s_name),
            target=FactEndpoint(id=r.target_entity_id, type=r.t_type, name=r.t_name),
            attributes=_redact(r.attributes or {}, sensitive, can_see_sensitive),
            fact_text=r.fact_text,
            valid_from=r.valid_from,
            valid_from_unknown=r.valid_from_unknown,
            valid_to=r.valid_to,
            source_recorded_at=r.source_recorded_at,
            recorded_at=r.recorded_at,
            superseded_at=r.superseded_at,
            succeeded_by_fact_id=r.succeeded_by_fact_id,
            status=r.status,
            phase=r.phase,
            authority_class=r.authority_class,
            classification=r.classification,
            source_type=r.source_type,
            evidence_status=r.evidence_status,
            evidence=evidence,
            decision_code=r.decision_code,
            decided_at=r.decided_at,
            source_command_id=r.source_command_id,
            source_interaction_id=r.source_interaction_id,
            source_system=r.source_system,
            source_ref=r.source_ref,
            ontology_version_id=r.ontology_version_id,
            revision=r.revision,
            projection_status=r.projection_status,
        )

    async def list_facts(
        self,
        conn: AsyncConnection,
        ctx: WorkspaceContext,
        *,
        entity_id: uuid.UUID,
        direction: Literal["out", "in", "both"],
        rel_type: str | None,
        as_of: dt.datetime | None,
        known_at: dt.datetime | None,
        include_history: bool,
        can_see_sensitive: bool,
        subjects: SubjectFilter,
        include: Sequence[str] = (),
    ) -> list[FactOut]:
        """`include` adds fact statuses that are not "current": proposed, disputed, rejected. They are always labelled by their `status`. A fact is shown only when BOTH its
        endpoints are visible to `subjects` (the predicate is inside the statement)."""
        where = ["f.workspace_id = :ws"]
        extras = sorted({x for x in include if x in ("proposed", "disputed", "rejected")})
        params: dict[str, Any] = {"ws": ctx.workspace_id, "e": entity_id, "as_of": as_of, "known_at": known_at, "extras": extras}
        where.append(
            {
                "out": "f.source_entity_id = :e",
                "in": "f.target_entity_id = :e",
                "both": "(f.source_entity_id = :e OR f.target_entity_id = :e)",
            }[direction]
        )
        if rel_type:
            where.append("f.rel_type = :rel")
            params["rel"] = rel_type
        where.append(visibility_sql(include_history=include_history, known_at_given=known_at is not None))
        acl, acl_params = fact_visible_sql(subjects, source="s", target="t")
        sql = f"{FACT_SELECT} WHERE {' AND '.join(where)} AND {acl} ORDER BY f.valid_from NULLS FIRST, f.recorded_at, f.id"  # noqa: S608 (constant fragments)
        rows = (await conn.execute(text(sql), {**params, **acl_params})).all()
        if subjects.enforced:
            # a count of what the filter hid on this read (no ids): the same statement without the filter, minus what it returned
            count_sql = f"SELECT count(*) AS n FROM ({FACT_SELECT} WHERE {' AND '.join(where)}) x"  # noqa: S608 (constant fragments)
            self.hidden("facts", int((await conn.execute(text(count_sql), params)).scalar_one()) - len(rows))
        evidence = await self._evidence(conn, ctx.workspace_id, [r.id for r in rows])
        return [await self._fact_out(conn, ctx.workspace_id, r, can_see_sensitive, evidence.get(r.id, [])) for r in rows]

    async def entity_exists(self, conn: AsyncConnection, ctx: WorkspaceContext, entity_id: uuid.UUID, *, subjects: SubjectFilter) -> None:
        """404 for an id that does not exist here, lives in another workspace, or that the subject filter hides: one answer for all three."""
        acl, acl_params = entity_visible_sql(subjects, alias="e")
        if (
            await conn.execute(
                text(f"SELECT 1 FROM entities e WHERE e.workspace_id = :ws AND e.id = :id AND {acl}"),  # noqa: S608
                {"ws": ctx.workspace_id, "id": entity_id, **acl_params},
            )
        ).first() is None:
            if subjects.enforced:
                await self._note_hidden_entity(conn, ctx, entity_id, "entity")
            raise NotFound()

    async def _note_hidden_entity(self, conn: AsyncConnection, ctx: WorkspaceContext, entity_id: uuid.UUID, surface: str) -> None:
        """Only on the failure path of an enforced filter: was it the filter that hid it (an entity of this workspace) rather than a plain miss? Counts, never says."""
        row = (
            await conn.execute(
                text("SELECT 1 FROM entities WHERE workspace_id = :ws AND id = :id"), {"ws": ctx.workspace_id, "id": entity_id}
            )
        ).first()
        if row is not None:
            self.hidden(surface)

    async def get_fact(
        self, conn: AsyncConnection, ctx: WorkspaceContext, fact_id: uuid.UUID, *, can_see_sensitive: bool, subjects: SubjectFilter
    ) -> FactOut:
        acl, acl_params = fact_visible_sql(subjects, source="s", target="t")
        row = (
            await conn.execute(
                text(f"{FACT_SELECT} WHERE f.workspace_id = :ws AND f.id = :id AND f.status <> 'deletion_pending' AND {acl}"),  # noqa: S608
                {"ws": ctx.workspace_id, "id": fact_id, **acl_params},
            )
        ).first()
        if row is None:
            if subjects.enforced:
                hidden = (
                    await conn.execute(
                        text("SELECT 1 FROM facts WHERE workspace_id = :ws AND id = :id"), {"ws": ctx.workspace_id, "id": fact_id}
                    )
                ).first()
                if hidden is not None:
                    self.hidden("fact")
            raise NotFound()
        return await self._fact_out(conn, ctx.workspace_id, row, can_see_sensitive)

    # ---- commands -------------------------------------------------------------------------------------------------------------------------

    @staticmethod
    def _command_summary(r: Row[Any]) -> dict[str, Any]:
        result = r.result or {}
        return {
            "id": r.id,
            "command_type": r.command_type,
            "status": r.status,
            "actor_principal_id": r.actor_principal_id,
            "authority_class": r.authority_class,
            "classification": r.classification,
            "source_system": r.source_system,
            "source_ref": r.source_ref,
            "ontology_version_id": r.ontology_version_id,
            "http_status": r.http_status,
            "projection_status": r.projection_status,
            "projection_attempts": r.projection_attempts,
            "last_error_code": r.last_error_code,
            "created_at": r.created_at,
            "projected_at": r.projected_at,
            "entity_id": result.get("entity_id"),
            "fact_id": result.get("fact_id"),
        }

    async def list_commands(
        self,
        conn: AsyncConnection,
        ctx: WorkspaceContext,
        *,
        subjects: SubjectFilter,
        status: str | None,
        command_type: str | None,
        entity_id: uuid.UUID | None,
        limit: int | None,
        cursor: str | None,
    ) -> Page[CommandSummary]:
        size, before = clamp_limit(limit), decode_cursor(cursor)
        # memory commands (P5) and principal-management commands (P7) are not listed: their visibility depends on the memory's scopes / on `principal.manage`
        where, params = ["workspace_id = :ws", _LEDGER_ONLY], {"ws": ctx.workspace_id, "n": size + 1}
        hide_sql, hide_params = self._commands_hidden_sql(subjects)
        where.append(hide_sql)
        params.update(hide_params)
        if status:
            where.append("status = :status")
            params["status"] = status
        if command_type:
            where.append("command_type = :ct")
            params["ct"] = command_type
        if entity_id:
            where.append("(result->>'entity_id' = :ent OR result->'endpoint_entity_ids' @> jsonb_build_array(CAST(:ent AS text)))")
            params["ent"] = str(entity_id)
        if before:
            where.append("(created_at, id) < (:c_at, :c_id)")
            params["c_at"], params["c_id"] = before[0], before[1]
        sql = f"SELECT * FROM commands WHERE {' AND '.join(where)} ORDER BY created_at DESC, id DESC LIMIT :n"  # noqa: S608 (constant fragments)
        rows = (await conn.execute(text(sql), params)).all()
        page, more = rows[:size], len(rows) > size
        return Page[CommandSummary](
            items=[CommandSummary(**self._command_summary(r)) for r in page],
            next_cursor=encode_cursor(page[-1].created_at, page[-1].id) if more else None,
        )

    @staticmethod
    def _commands_hidden_sql(subjects: SubjectFilter) -> tuple[str, dict[str, Any]]:
        """A command is shown only when none of the entities it touches is hidden from the caller (an app writer sees commands about visible subjects only)."""
        frozen = (  # P8: a command about a record whose deletion has been accepted is unreadable from that moment, like the record itself
            "NOT EXISTS (SELECT 1 FROM entities fz WHERE fz.workspace_id = commands.workspace_id AND fz.status = 'deletion_pending' "
            "AND (fz.id::text = commands.result->>'entity_id' OR commands.result->'endpoint_entity_ids' @> to_jsonb(fz.id::text))) "
            "AND NOT EXISTS (SELECT 1 FROM facts fzf WHERE fzf.workspace_id = commands.workspace_id AND fzf.status = 'deletion_pending' "
            "AND fzf.id::text = commands.result->>'fact_id')"
        )
        if not subjects.enforced:
            return frozen, {}
        acl, params = entity_visible_sql(subjects, alias="he")
        return (
            frozen  # noqa: S608
            + " AND NOT EXISTS (SELECT 1 FROM entities he WHERE he.workspace_id = commands.workspace_id AND he.type_key = ANY(CAST(:sfc_scoped AS text[])) "  # noqa: S608
            f"AND NOT {acl} AND (he.id::text = commands.result->>'entity_id' OR commands.result->'endpoint_entity_ids' @> to_jsonb(he.id::text)))",
            {**params, "sfc_scoped": sorted(subjects.scoped_types)},
        )

    async def get_command(
        self, conn: AsyncConnection, ctx: WorkspaceContext, command_id: uuid.UUID, *, subjects: SubjectFilter
    ) -> CommandDetailOut:
        hide_sql, hide_params = self._commands_hidden_sql(subjects)
        sql = f"SELECT * FROM commands WHERE workspace_id = :ws AND id = :id AND {_LEDGER_ONLY} AND {hide_sql}"  # noqa: S608 (constant fragments)
        row = (await conn.execute(text(sql), {"ws": ctx.workspace_id, "id": command_id, **hide_params})).first()
        if row is None:
            raise NotFound()
        return CommandDetailOut(
            **self._command_summary(row), decision=row.decision, result=row.result or {}, status_url=f"/v1/commands/{row.id}"
        )
