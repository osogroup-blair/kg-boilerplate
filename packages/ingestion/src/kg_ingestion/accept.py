"""Idempotent acceptance of one event: the envelope, the ENCRYPTED raw content, the subjects, the stage plan, the normalize job and the audit entry
commit in ONE transaction (the transactional outbox). Only ACCEPTED events consume an Idempotency-Key (ADR-024): a 4xx leaves no row, so the key stays free."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

from kg_common.errors import IdempotencyMismatch, PayloadTooLarge, ValidationFailed
from kg_common.ids import uuid7
from kg_common.tokens import count_tokens
from kg_domain.principal import Principal, WorkspaceContext
from kg_governance.audit import AuditMetadata, AuditWriter, EventKind
from kg_jobs.outbox import enqueue
from kg_obs import metrics
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from kg_ingestion.crypto import ContentHasher, PayloadCipher, canonical_json
from kg_ingestion.envelope import CLOCK_SKEW, InteractionIn
from kg_ingestion.pipeline import PIPELINE_V1, Pipeline, dedupe_key, job_payload
from kg_ingestion.store import SubjectResolver, advisory_lock, insert_content, insert_interaction, insert_stage, insert_subjects


@dataclass(frozen=True)
class AcceptResult:
    event_id: uuid.UUID
    created: bool  # False = a replay of an already accepted event (HTTP 200)
    status: str


class PointerNotSupported(ValidationFailed):
    code, default_message = "pointer_not_supported", "payload_ref is not supported yet; send the content inline"


def _token_estimate(raw_doc: dict[str, object]) -> int:
    content = raw_doc["content"]
    return count_tokens(content if isinstance(content, str) else canonical_json(content).decode())


class AcceptService:
    def __init__(self, cipher: PayloadCipher, hasher: ContentHasher, *, max_content_bytes: int, pipeline: Pipeline = PIPELINE_V1):
        self._cipher, self._hasher, self._max, self._pipeline = cipher, hasher, max_content_bytes, pipeline
        self._resolver = SubjectResolver(hasher)

    async def accept(
        self, conn: AsyncConnection, ctx: WorkspaceContext, principal: Principal, audit: AuditWriter, key: str, body: InteractionIn
    ) -> AcceptResult:
        ws = ctx.workspace_id
        if body.payload_ref is not None:
            raise PointerNotSupported(details={"fields": ["payload_ref"]})
        raw_doc = body.raw_document()
        raw_bytes = canonical_json(raw_doc)
        if len(raw_bytes) > self._max:
            raise PayloadTooLarge(f"content exceeds {self._max} bytes", details={"max_bytes": self._max})
        digest = self._hasher.digest_json(ws, body.request_document())

        await advisory_lock(conn, ws, f"ixn:{body.source_system}", key)
        existing = (
            await conn.execute(
                text(
                    "SELECT id, request_hmac, processing_status FROM interactions "
                    "WHERE workspace_id = :ws AND source_system = :ss AND idempotency_key = :k"
                ),
                {"ws": ws, "ss": body.source_system, "k": key},
            )
        ).first()
        if existing is not None:
            if existing.request_hmac != digest:
                raise IdempotencyMismatch()
            return AcceptResult(existing.id, False, existing.processing_status)

        if body.occurred_at is not None:
            limit = (
                await conn.execute(text("SELECT now() + (:s * interval '1 second') AS t"), {"s": CLOCK_SKEW.total_seconds()})
            ).scalar_one()
            if body.occurred_at > limit:
                raise ValidationFailed("occurred_at is in the future", details={"fields": ["occurred_at"]})

        subjects = await self._resolver.resolve(conn, ws, "subject", list(body.subject_refs))
        actors = await self._resolver.resolve(conn, ws, "actor", [body.actor_ref] if body.actor_ref else [])
        active = (await conn.execute(text("SELECT active_version_id FROM workspace_ontology WHERE workspace_id = :ws"), {"ws": ws})).first()
        ontology_version_id = active.active_version_id if active else None
        tokens = await asyncio.to_thread(_token_estimate, raw_doc)

        interaction_id = uuid7()
        classification = body.effective_classification()
        await insert_interaction(
            conn,
            {
                "id": interaction_id,
                "ws": ws,
                "ss": body.source_system,
                "sr": body.source_ref,
                "key": key,
                "h": digest,
                "kind": body.kind,
                "ct": body.content_type,
                "occ": body.occurred_at,
                "unk": body.occurred_at_unknown,
                "by": principal.principal_id,
                "cls": classification,
                "consent": body.external_processing_allowed,
                "pv": self._pipeline.version,
                "ov": ontology_version_id,
                "bytes": len(raw_bytes),
                "tokens": tokens,
            },
        )
        sealed = self._cipher.encrypt(raw_bytes, workspace_id=ws, interaction_id=interaction_id, form="raw")
        await insert_content(conn, ws, interaction_id, "raw", sealed, self._hasher.digest(ws, raw_bytes), len(raw_bytes))
        await insert_subjects(conn, ws, interaction_id, [*subjects, *actors])
        plan = self._pipeline.initial_statuses(consent_external=body.external_processing_allowed)
        for stage, status in plan.items():
            await insert_stage(conn, ws, interaction_id, stage, status, self._pipeline.version, ontology_version_id)
        for stage in self._pipeline.ready(plan):
            await enqueue(
                conn,
                workspace_id=ws,
                stage=stage,
                subject_type="interaction",
                subject_id=interaction_id,
                dedupe_key=dedupe_key(interaction_id, stage, 0),
                payload=job_payload(interaction_id, 0),
                pipeline_version=self._pipeline.version,
                ontology_version_id=ontology_version_id,
            )
        await audit.record(
            "interaction.accepted",
            resource_type="interaction",
            resource_id=interaction_id,
            metadata=AuditMetadata(event_kind=EventKind(body.kind), classification=classification, count=len(subjects) + len(actors)),
        )
        metrics.interactions_accepted_total.labels(kind=body.kind).inc()
        return AcceptResult(interaction_id, True, "accepted")
