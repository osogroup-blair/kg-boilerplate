"""Entity, relationship and command reads. Postgres is authoritative (never the graph), so a read is immediately consistent with its write.

Valid time and record time are independent (ADR-017):
    as_of(T)    valid_from <= T < valid_to          (an unknown valid_from counts as 'always')
    known_at(T) recorded_at <= T and not superseded by T
    current     as_of(now) and known_at(now) and status = 'active'
`now` is the database clock (M12)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from kg_common.errors import NotFound
from kg_common.pagination import clamp_limit, decode_cursor, encode_cursor
from kg_domain.envelope import Page
from kg_domain.principal import WorkspaceContext
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

from kg_knowledge.models import (
    REDACTED_VALUE,
    CommandDetailOut,
    CommandSummary,
    EntityDetailOut,
    EntityOut,
    ExternalId,
    FactEndpoint,
    FactOut,
)
from kg_knowledge.specs import SpecCache

_EXTERNAL_IDS = (
    "(SELECT COALESCE(jsonb_agg(jsonb_build_object('system', x.system, 'value', x.value) ORDER BY x.system, x.value), '[]'::jsonb) "
    "FROM entity_external_ids x WHERE x.workspace_id = e.workspace_id AND x.entity_id = e.id) AS external_ids"
)
_FACT_SELECT = """
    SELECT f.*, s.type_key AS s_type, s.display_name AS s_name, t.type_key AS t_type, t.display_name AS t_name,
           CASE WHEN f.valid_from > now() THEN 'future' WHEN f.valid_to IS NOT NULL AND f.valid_to <= now() THEN 'historical' ELSE 'current' END AS phase
    FROM facts f JOIN entities s ON s.workspace_id = f.workspace_id AND s.id = f.source_entity_id
                 JOIN entities t ON t.workspace_id = f.workspace_id AND t.id = f.target_entity_id
"""


def _redact(attrs: dict[str, Any], sensitive: set[str], allowed: bool) -> dict[str, Any]:
    if allowed:
        return dict(attrs)
    return {k: (dict(REDACTED_VALUE) if k in sensitive else v) for k, v in attrs.items()}


class Reads:
    def __init__(self, specs: SpecCache):
        self._specs = specs

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
        type_key: str | None,
        q: str | None,
        status: str | None,
        limit: int | None,
        cursor: str | None,
    ) -> Page[EntityOut]:
        size, after = clamp_limit(limit), decode_cursor(cursor)
        where, params = ["e.workspace_id = :ws"], {"ws": ctx.workspace_id, "n": size + 1}
        if type_key:
            where.append("e.type_key = :type")
            params["type"] = type_key
        if status:
            where.append("e.status = :status")
            params["status"] = status
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

    async def get_entity(
        self,
        conn: AsyncConnection,
        ctx: WorkspaceContext,
        entity_id: uuid.UUID,
        *,
        can_see_sensitive: bool,
        include_history: bool,
        as_of: dt.datetime | None,
        known_at: dt.datetime | None,
    ) -> EntityDetailOut:
        row = (
            await conn.execute(
                text(f"SELECT e.*, {_EXTERNAL_IDS} FROM entities e WHERE e.workspace_id = :ws AND e.id = :id"),  # noqa: S608
                {"ws": ctx.workspace_id, "id": entity_id},
            )
        ).first()  # noqa: S608
        if row is None:
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
        )
        return EntityDetailOut(**base.model_dump(), relationships=facts)

    # ---- facts ----------------------------------------------------------------------------------------------------------------------------

    async def _fact_out(self, conn: AsyncConnection, ws: uuid.UUID, r: Row[Any], can_see_sensitive: bool) -> FactOut:
        sensitive = await self._sensitive(conn, ws, r.ontology_version_id, r.rel_type, relationship=True)
        return FactOut(
            id=r.id,
            rel_type=r.rel_type,
            source=FactEndpoint(id=r.source_entity_id, type=r.s_type, name=r.s_name),
            target=FactEndpoint(id=r.target_entity_id, type=r.t_type, name=r.t_name),
            attributes=_redact(r.attributes or {}, sensitive, can_see_sensitive),
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
            source_command_id=r.source_command_id,
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
    ) -> list[FactOut]:
        where = ["f.workspace_id = :ws"]
        params: dict[str, Any] = {"ws": ctx.workspace_id, "e": entity_id, "as_of": as_of, "known_at": known_at}
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
        known = "f.recorded_at <= COALESCE(CAST(:known_at AS timestamptz), now()) AND (f.superseded_at IS NULL OR f.superseded_at > COALESCE(CAST(:known_at AS timestamptz), now()))"
        if include_history:
            where.append("f.status <> 'deleted'")
            if known_at is not None:
                where.append(known)
        else:
            where += [
                "f.status = 'active'",
                "(f.valid_from IS NULL OR f.valid_from <= COALESCE(CAST(:as_of AS timestamptz), now()))",
                "(f.valid_to IS NULL OR f.valid_to > COALESCE(CAST(:as_of AS timestamptz), now()))",
                known,
            ]
        sql = f"{_FACT_SELECT} WHERE {' AND '.join(where)} ORDER BY f.valid_from NULLS FIRST, f.recorded_at, f.id"  # noqa: S608 (constant fragments)
        rows = (await conn.execute(text(sql), params)).all()
        return [await self._fact_out(conn, ctx.workspace_id, r, can_see_sensitive) for r in rows]

    async def entity_exists(self, conn: AsyncConnection, ctx: WorkspaceContext, entity_id: uuid.UUID) -> None:
        if (
            await conn.execute(
                text("SELECT 1 FROM entities WHERE workspace_id = :ws AND id = :id"), {"ws": ctx.workspace_id, "id": entity_id}
            )
        ).first() is None:
            raise NotFound()

    async def get_fact(self, conn: AsyncConnection, ctx: WorkspaceContext, fact_id: uuid.UUID, *, can_see_sensitive: bool) -> FactOut:
        row = (
            await conn.execute(text(f"{_FACT_SELECT} WHERE f.workspace_id = :ws AND f.id = :id"), {"ws": ctx.workspace_id, "id": fact_id})
        ).first()  # noqa: S608
        if row is None:
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
        status: str | None,
        command_type: str | None,
        entity_id: uuid.UUID | None,
        limit: int | None,
        cursor: str | None,
    ) -> Page[CommandSummary]:
        size, before = clamp_limit(limit), decode_cursor(cursor)
        where, params = ["workspace_id = :ws"], {"ws": ctx.workspace_id, "n": size + 1}
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

    async def get_command(self, conn: AsyncConnection, ctx: WorkspaceContext, command_id: uuid.UUID) -> CommandDetailOut:
        row = (
            await conn.execute(
                text("SELECT * FROM commands WHERE workspace_id = :ws AND id = :id"), {"ws": ctx.workspace_id, "id": command_id}
            )
        ).first()
        if row is None:
            raise NotFound()
        return CommandDetailOut(
            **self._command_summary(row), decision=row.decision, result=row.result or {}, status_url=f"/v1/commands/{row.id}"
        )
