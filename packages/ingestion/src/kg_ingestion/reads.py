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
from kg_governance.grants import SubjectFilter, entity_visible_sql
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


_BY_SUBJECT = (
    "EXISTS (SELECT 1 FROM interaction_subjects s JOIN entities se ON se.workspace_id = s.workspace_id AND se.id = s.entity_id "
    "WHERE s.workspace_id = i.workspace_id AND s.interaction_id = i.id AND s.role = 'subject' AND s.entity_id = :ent"
)
_SUBJECT_FROM = (
    "FROM interaction_subjects s LEFT JOIN entities se ON se.workspace_id = s.workspace_id AND se.id = s.entity_id "
    "WHERE s.workspace_id = :ws AND s.interaction_id = :id ORDER BY s.role DESC, s.ordinal"
)


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
        subjects: SubjectFilter,
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
        where, params = (
            ["i.workspace_id = :ws", "i.processing_status <> 'deletion_pending'"],
            {"ws": ctx.workspace_id, "n": size + 1},
        )  # a frozen one is absent (SEC-05)
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
            acl, acl_params = entity_visible_sql(subjects, alias="se")
            by_subject = (  # noqa: S608 (constant fragments; the filter is bound parameters)
                f"{_BY_SUBJECT} AND {acl})"
            )
            where.append(by_subject)
            params["ent"] = subject_entity_id
            params.update(acl_params)  # a subject the caller cannot see filters like one that has no interactions
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
        self,
        conn: AsyncConnection,
        ctx: WorkspaceContext,
        interaction_id: uuid.UUID,
        *,
        subjects: SubjectFilter,
        only_submitted_by: uuid.UUID | None = None,
    ) -> InteractionDetail:
        own_sql, own_params = _own(only_submitted_by)
        row = (
            await conn.execute(
                text(
                    f"SELECT {_SUMMARY}, i.consent_external, i.pipeline_version, i.ontology_version_id {_FROM} WHERE i.workspace_id = :ws AND i.id = :id AND i.processing_status <> 'deletion_pending' {own_sql}"
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
        acl, acl_params = entity_visible_sql(subjects, alias="se")
        # the reference stays, as `hidden`, without its id (P7)
        subject_sql = f"SELECT s.role, s.ordinal, s.entity_id, s.ref_type, s.ref_system, s.resolved, (s.entity_id IS NOT NULL AND se.id IS NOT NULL AND NOT {acl}) AS hidden {_SUBJECT_FROM}"  # noqa: S608
        subject_rows = (await conn.execute(text(subject_sql), {"ws": ctx.workspace_id, "id": interaction_id, **acl_params})).all()
        forms = {
            r.form
            for r in (
                await conn.execute(
                    text("SELECT form FROM interaction_contents WHERE workspace_id = :ws AND interaction_id = :id"),
                    {"ws": ctx.workspace_id, "id": interaction_id},
                )
            ).all()
        }
        normalized_done = any(s.stage == "normalize" and s.status == "succeeded" for s in stages)
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
                    entity_id=None if s.hidden else s.entity_id,
                    resolved=s.resolved,
                    ref_type=None if s.hidden else s.ref_type,
                    ref_system=None if s.hidden else s.ref_system,
                    hidden=bool(s.hidden),
                )
                for s in subject_rows
            ],
            status_url=f"/v1/interactions/{interaction_id}",
            raw_content_expired="raw" not in forms,
            normalized_content_expired=normalized_done and "normalized" not in forms,
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
        episode = (s.result or {}).get("episode_uuid") if s.stage == "graph_ingest" else None
        if episode:  # the episode the extraction created: the provenance path from an event to what it produced (EVD-03)
            derivatives = [DerivativeOut(kind="graph_episode", id=uuid.UUID(str(episode)))]
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
                text("SELECT kind FROM interactions WHERE workspace_id = :ws AND id = :id AND processing_status <> 'deletion_pending'"),
                {"ws": ctx.workspace_id, "id": interaction_id},
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
