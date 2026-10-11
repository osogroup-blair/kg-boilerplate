"""Reads of the ledger. Metadata only; the decrypted content leaves through `content()` alone, which the router audits (EVD-06)."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

from kg_common.errors import NotFound
from kg_common.pagination import clamp_limit, decode_cursor, encode_cursor
from kg_domain.envelope import Page
from kg_domain.principal import WorkspaceContext
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

from kg_ingestion.crypto import PayloadCipher
from kg_ingestion.models import ContentOut, DerivativeOut, InteractionDetail, InteractionSummary, StageOut, SubjectOut
from kg_ingestion.normalize import normalized_content_id
from kg_ingestion.pipeline import PIPELINE_V1, Pipeline
from kg_ingestion.store import read_content

_SUMMARY = (
    "i.id, i.source_system, i.source_ref, i.kind, i.content_type, i.occurred_at, i.occurred_at_unknown, i.recorded_at, i.submitted_by, "
    "p.handle AS submitted_by_handle, i.classification, i.processing_status, i.status_changed_at, i.byte_size, i.token_estimate"
)
_FROM = "FROM interactions i LEFT JOIN principals p ON p.workspace_id = i.workspace_id AND p.id = i.submitted_by"


def _own(principal_id: uuid.UUID | None) -> tuple[str, dict[str, Any]]:
    return ("AND i.submitted_by = :me", {"me": principal_id}) if principal_id is not None else ("", {})


class InteractionReads:
    def __init__(self, cipher: PayloadCipher, pipeline: Pipeline = PIPELINE_V1):
        self._cipher, self._pipeline = cipher, pipeline

    async def list(
        self,
        conn: AsyncConnection,
        ctx: WorkspaceContext,
        *,
        only_submitted_by: uuid.UUID | None,
        status: str | None,
        kind: str | None,
        source_system: str | None,
        subject_entity_id: uuid.UUID | None,
        since: dt.datetime | None,
        limit: int | None,
        cursor: str | None,
    ) -> Page[InteractionSummary]:
        """`only_submitted_by` is the ownership predicate for agents (P7 replaces it with scopes): applied in SQL, not after the fact."""
        size, before = clamp_limit(limit), decode_cursor(cursor)
        where, params = ["i.workspace_id = :ws"], {"ws": ctx.workspace_id, "n": size + 1}
        for column, key, value in (
            ("i.processing_status", "status", status),
            ("i.kind", "kind", kind),
            ("i.source_system", "ss", source_system),
        ):
            if value:
                where.append(f"{column} = :{key}")
                params[key] = value
        if only_submitted_by is not None:
            where.append("i.submitted_by = :me")
            params["me"] = only_submitted_by
        if subject_entity_id is not None:
            where.append(
                "EXISTS (SELECT 1 FROM interaction_subjects s WHERE s.workspace_id = i.workspace_id AND s.interaction_id = i.id "
                "AND s.role = 'subject' AND s.entity_id = :ent)"
            )
            params["ent"] = subject_entity_id
        if since is not None:
            where.append("i.recorded_at >= :since")
            params["since"] = since
        if before:
            where.append("(i.recorded_at, i.id) < (:c_at, :c_id)")
            params["c_at"], params["c_id"] = before[0], before[1]
        sql = f"SELECT {_SUMMARY} {_FROM} WHERE {' AND '.join(where)} ORDER BY i.recorded_at DESC, i.id DESC LIMIT :n"  # noqa: S608 (constant fragments)
        rows = (await conn.execute(text(sql), params)).all()
        page, more = rows[:size], len(rows) > size
        return Page[InteractionSummary](
            items=[InteractionSummary(**dict(r._mapping)) for r in page],
            next_cursor=encode_cursor(page[-1].recorded_at, page[-1].id) if more else None,
        )

    async def get(
        self, conn: AsyncConnection, ctx: WorkspaceContext, interaction_id: uuid.UUID, *, only_submitted_by: uuid.UUID | None = None
    ) -> InteractionDetail:
        own_sql, own_params = _own(only_submitted_by)
        row = (
            await conn.execute(
                text(
                    f"SELECT {_SUMMARY}, i.consent_external, i.pipeline_version, i.ontology_version_id {_FROM} WHERE i.workspace_id = :ws AND i.id = :id {own_sql}"
                ),  # noqa: S608
                {"ws": ctx.workspace_id, "id": interaction_id, **own_params},
            )
        ).first()
        if row is None:
            raise NotFound()
        stages = (
            await conn.execute(
                text("SELECT * FROM interaction_stages WHERE workspace_id = :ws AND interaction_id = :id"),
                {"ws": ctx.workspace_id, "id": interaction_id},
            )
        ).all()
        subjects = (
            await conn.execute(
                text(
                    "SELECT role, ordinal, entity_id, ref_type, ref_system, resolved FROM interaction_subjects "
                    "WHERE workspace_id = :ws AND interaction_id = :id ORDER BY role DESC, ordinal"
                ),
                {"ws": ctx.workspace_id, "id": interaction_id},
            )
        ).all()
        by_name = {s.stage: s for s in stages}
        ordered = [by_name[n] for n in self._pipeline.names if n in by_name] + [
            s for n, s in by_name.items() if n not in self._pipeline.names
        ]
        return InteractionDetail(
            **dict(row._mapping),
            stages=[self._stage_out(s, interaction_id) for s in ordered],
            subjects=[
                SubjectOut(
                    role=s.role,
                    entity_id=s.entity_id,
                    resolved=s.resolved,
                    ref_type=s.ref_type,
                    ref_system=s.ref_system,
                )
                for s in subjects
            ],
            status_url=f"/v1/interactions/{interaction_id}",
        )

    def _stage_out(self, s: Row[Any], interaction_id: uuid.UUID) -> StageOut:
        try:
            definition = self._pipeline.stage(s.stage)
            enabled, enabled_in = definition.enabled, definition.enabled_in
        except KeyError:
            enabled, enabled_in = True, None
        derivatives = (
            [DerivativeOut(kind="normalized_content", id=normalized_content_id(interaction_id))]
            if s.stage == "normalize" and s.status == "succeeded"
            else []
        )
        return StageOut(
            stage=s.stage,
            status=s.status,
            generation=s.generation,
            attempts=s.attempts,
            last_error_category=s.last_error_category,
            last_error_code=s.last_error_code,
            started_at=s.started_at,
            finished_at=s.finished_at,
            enabled=enabled,
            enabled_in=enabled_in,
            result=s.result or {},
            derivatives=derivatives,
        )

    async def content(self, conn: AsyncConnection, ctx: WorkspaceContext, interaction_id: uuid.UUID, form: str) -> ContentOut:
        """Decrypts one form. The CALLER must audit this read in the same transaction (interaction.content_read)."""
        head = (
            await conn.execute(
                text("SELECT kind FROM interactions WHERE workspace_id = :ws AND id = :id"), {"ws": ctx.workspace_id, "id": interaction_id}
            )
        ).first()
        sealed = await read_content(conn, ctx.workspace_id, interaction_id, form) if head else None
        if head is None or sealed is None:
            raise NotFound()
        plaintext = self._cipher.decrypt(sealed, workspace_id=ctx.workspace_id, interaction_id=interaction_id, form=form)
        if form == "normalized":
            return ContentOut(form="normalized", kind=head.kind, content=plaintext.decode())
        doc = json.loads(plaintext)
        return ContentOut(
            form="raw",
            kind=head.kind,
            content=doc["content"],
            content_type=doc.get("content_type"),
            metadata=doc.get("metadata") or {},
            subject_refs=doc.get("subject_refs") or [],
            actor_ref=doc.get("actor_ref"),
        )
