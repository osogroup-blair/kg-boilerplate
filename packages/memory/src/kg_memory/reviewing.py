"""Review decisions for memory (phase-5 plan §6.2), registered with `kg_knowledge.ReviewService` through its handler port.

    memory_proposal   approve | reject | approve_with_edit        a proposed memory becomes active, is refused, or is replaced by the reviewer's wording
    memory_conflict   keep_existing | supersede_existing | mark_disputed
    source_compromised  restore | confirm_removal                 a memory or fact derived from a compromised principal (P7): reinstate it, or confirm it stays out

An approved AGENT finding (origin proposal, authority agent_generated) becomes `verified` by the reviewer who approved it: the only way a memory's evidence status ever
changes. An agent finding that policy already activated (the agent held `publish`) carries a review item for exactly this: approving verifies it, rejecting withdraws it.

A decision runs inside the review service's transaction (idempotency, If-Match, audit and the ledger command belong to it). A reviewer decides only memory it can
see itself: anything else is `NotFound`. Superseding an existing memory needs authority at least equal to it (ADR-027): a conversation report never displaces
an administrator's assertion.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from kg_common.errors import Conflict, ValidationFailed
from kg_governance.audit import AuditMetadata, CommandType
from kg_knowledge.commands import CommandContext
from kg_knowledge.extracted import touch
from kg_knowledge.models import DecisionIn
from kg_knowledge.specs import SpecCache
from sqlalchemy import Row, text

from kg_memory import ledger, store
from kg_memory.models import AUTHORITY_RANK
from kg_memory.policy import Decision, authority_for, evidence_status_for
from kg_memory.reads import MemoryReads, Viewer
from kg_memory.scopes import ScopeGrants
from kg_memory.store import EvidenceRef, NewMemory
from kg_memory.writers import InvalidTransition, MemoryWriter, actor_of


class InsufficientAuthority(Conflict):
    code, default_message = "insufficient_authority", "Superseding this memory needs at least its authority"


class MemoryReviewHandler:
    def __init__(self, writer: MemoryWriter, reads: MemoryReads, specs: SpecCache):
        self._writer, self._reads, self._specs = writer, reads, specs

    async def _viewer(self, cc: CommandContext) -> Viewer:
        grants = await ScopeGrants.for_principal(cc.conn, cc.principal)
        return Viewer(cc.principal, grants, curator=True)  # review.decide is held by admins and reviewers, who also hold memory.curate

    async def decide(self, cc: CommandContext, review: Row[Any], body: DecisionIn, *, key: str) -> dict[str, Any]:
        v = await self._viewer(cc)
        if review.item_type == "source_compromised":
            return await self._compromised(cc, v, review, body)
        row = await self._reads.visible_row(cc.conn, v, review.memory_id, lock=True)
        if review.item_type == "memory_proposal":
            return await self._proposal(cc, v, review, row, body, key)
        if review.item_type == "consolidation_proposal":
            return await self._consolidation(cc, review, row, body)
        return await self._conflict(cc, v, review, row, body)

    # ---- memory_proposal --------------------------------------------------------------------------------------------------------------------

    async def _proposal(self, cc: CommandContext, v: Viewer, review: Row[Any], row: Row[Any], body: DecisionIn, key: str) -> dict[str, Any]:
        conn, ws = cc.conn, cc.ctx.workspace_id
        awaiting_verification = row.status == "active" and row.evidence_status == "unverified" and row.origin == "proposal"
        if row.status != "proposed" and not awaiting_verification:
            raise InvalidTransition()
        mid = row.id
        if body.decision == "reject":
            await store.set_status(conn, ws, mid, "rejected")
            await self._audit(cc, mid, row.type, "rejected", body)
            return {"memory_ids": [mid]}
        if body.decision == "approve":
            await store.set_status(conn, ws, mid, "active", confirm=True)
            if row.authority_class == "agent_generated" and row.origin == "proposal":
                await self._verify(cc, mid)
            await self._audit(cc, mid, row.type, "active", body)
            return {"memory_ids": [mid]}
        # approve_with_edit: a NEW active memory with the reviewer's wording, replacing the proposal; the proposal's evidence travels with it
        if not body.edited_content:
            raise ValidationFailed("approve_with_edit needs edited_content", details={"fields": ["edited_content"]})
        _, policy = await self._writer.policy_for(conn, ws, row.type)
        if len(body.edited_content) > policy.max_content_chars:
            raise ValidationFailed("the edit is longer than the memory policy allows", details={"fields": ["edited_content"]})
        scopes = (await store.load_scopes(conn, ws, [mid]))[mid]
        subjects = (await store.load_subjects(conn, ws, [mid]))[mid]
        evidence = [
            EvidenceRef(
                e.source_kind,
                interaction_id=e.interaction_id,
                episode_uuid=e.episode_uuid,
                fact_id=e.fact_id,
                command_id=e.command_id,
                memory_id=e.source_memory_id,
                external_ref=e.external_ref,
            )
            for e in await store.load_evidence(conn, ws, mid)
        ]
        actor = actor_of(cc.principal)
        nm = NewMemory(
            type=row.type,
            origin=row.origin if row.origin != "working" else "explicit",
            content=body.edited_content,
            summary=store.derive_summary(body.edited_content),
            status="active",
            scopes=scopes,
            created_by=cc.principal.principal_id,
            authority=authority_for("explicit", actor),
            evidence_status=evidence_status_for("explicit", "active", actor),
            classification=row.classification,
            importance=float(row.importance),
            pipeline_version=row.pipeline_version,
            subject_ids=subjects["subject"],
            topic_ids=subjects["topic"],
            evidence=[*evidence, EvidenceRef("memory", memory_id=mid)],
            lineage=[(mid, "replaces")],
            occurred_at=row.occurred_at,
            effective_from=row.effective_from,
            purpose=row.purpose,
        )
        await store.set_status(conn, ws, mid, "superseded", close_validity=True)  # the proposal frees its dedupe slot first
        digest = ledger.digest_of(CommandType.MEMORY_REVIEW, cc.principal.principal_id, [str(review.id), body.decision, nm.content])
        written = await self._writer.persist(
            conn,
            ws,
            cc.audit,
            command_type=CommandType.MEMORY_REVIEW,
            key=f"{key}:memory",
            digest=digest,
            nm=nm,
            decision=Decision("active", "approved_with_edit"),
            ontology_version_id=None,
            on_duplicate="raise",
            audit_action="memory.saved",
            extra_payload={"replaces": str(mid), "review_id": str(review.id)},
        )
        await self._audit(cc, mid, row.type, "superseded", body)
        return {"memory_ids": [mid, written.out.memory_id]}

    async def _consolidation(self, cc: CommandContext, review: Row[Any], row: Row[Any], body: DecisionIn) -> dict[str, Any]:
        """A synthesised memory is approved or refused as a whole. Approving makes it active and supersedes the sources that are still active; their evidence was cited by
        reference when the proposal was made, so nothing about where the claim came from is lost. A proposal whose sources have all changed since is stale."""
        conn, ws = cc.conn, cc.ctx.workspace_id
        mid = row.id
        if row.status != "proposed":
            raise InvalidTransition()
        if body.decision == "reject":
            await store.set_status(conn, ws, mid, "rejected")
            await self._audit(cc, mid, row.type, "rejected", body)
            return {"memory_ids": [mid]}
        sources = [
            r.related_id
            for r in (
                await conn.execute(
                    text(
                        "SELECT l.related_id FROM memory_lineage l JOIN curated_memories s ON s.workspace_id = l.workspace_id AND s.id = l.related_id "
                        "WHERE l.workspace_id = :ws AND l.memory_id = :m AND l.relation = 'consolidated_from' AND s.status = 'active' ORDER BY l.related_id FOR UPDATE OF s"
                    ),
                    {"ws": ws, "m": mid},
                )
            ).all()
        ]
        if not sources:
            raise InvalidTransition()
        await store.set_status(conn, ws, mid, "active", confirm=True)
        await self._audit(cc, mid, row.type, "active", body)
        for sid in sources:
            await store.set_status(conn, ws, sid, "superseded", close_validity=True)
            await self._audit(cc, sid, row.type, "superseded", body)
        await cc.audit.record(
            "consolidation.applied",
            resource_type="memory",
            resource_id=mid,
            metadata=AuditMetadata(
                memory_id=mid, review_id=review.id, count_memories=len(sources), consolidation_mode="synthesize", memory_type=row.type
            ),
        )
        return {"memory_ids": [mid, *sources]}

    @staticmethod
    async def _verify(cc: CommandContext, memory_id: uuid.UUID) -> None:
        await cc.conn.execute(
            text(
                "UPDATE curated_memories SET evidence_status = 'verified', verified_by = :by, verified_at = now() "
                "WHERE workspace_id = :ws AND id = :id AND evidence_status = 'unverified'"
            ),
            {"ws": cc.ctx.workspace_id, "id": memory_id, "by": cc.principal.principal_id},
        )

    # ---- source_compromised -----------------------------------------------------------------------------------------------------------------

    async def _compromised(self, cc: CommandContext, v: Viewer, review: Row[Any], body: DecisionIn) -> dict[str, Any]:
        conn, ws = cc.conn, cc.ctx.workspace_id
        restore = body.decision == "restore"
        effects: dict[str, Any] = {}
        if review.memory_id is not None:
            row = await self._reads.visible_row(conn, v, review.memory_id, lock=True)
            if row.status != "disputed":
                raise InvalidTransition()
            await store.set_status(conn, ws, row.id, "active" if restore else "rejected", confirm=restore)
            await self._audit(cc, row.id, row.type, "active" if restore else "rejected", body)
            effects["memory_ids"] = [row.id]
        if review.fact_id is not None:
            fact = (
                await conn.execute(
                    text("SELECT status FROM facts WHERE workspace_id = :ws AND id = :id FOR UPDATE"), {"ws": ws, "id": review.fact_id}
                )
            ).first()
            if fact is None or fact.status != "disputed":
                raise InvalidTransition()
            await touch(
                conn,
                ws,
                "facts",
                review.fact_id,
                status="active" if restore else "rejected",
                decision_code="restored_by_reviewer" if restore else "removal_confirmed",
                decided_at=None if restore else dt.datetime.now(dt.UTC),
            )
            effects["fact_ids"] = [review.fact_id]
        return effects

    # ---- memory_conflict --------------------------------------------------------------------------------------------------------------------

    async def _conflict(self, cc: CommandContext, v: Viewer, review: Row[Any], row: Row[Any], body: DecisionIn) -> dict[str, Any]:
        conn, ws = cc.conn, cc.ctx.workspace_id
        mid, other = row.id, review.competing_memory_id
        if other is None:  # a curator disputed a live memory: the item decides how the dispute ends
            if row.status != "disputed":
                raise InvalidTransition()
            if body.decision == "supersede_existing":
                raise ValidationFailed("there is no competing memory to supersede", details={"fields": ["decision"]})
            if body.decision == "keep_existing":
                await store.set_status(conn, ws, mid, "active", confirm=True)
                await self._audit(cc, mid, row.type, "active", body)
            return {"memory_ids": [mid]}
        existing = await self._reads.visible_row(conn, v, other, lock=True)
        if row.status not in ("proposed", "disputed") or existing.status not in ("active", "disputed"):
            raise InvalidTransition()
        if body.decision == "keep_existing":
            await store.set_status(conn, ws, mid, "rejected")
            if existing.status == "disputed":
                await store.set_status(conn, ws, other, "active", confirm=True)
            await self._audit(cc, mid, row.type, "rejected", body)
            return {"memory_ids": [mid, other]}
        if body.decision == "mark_disputed":
            for m in (mid, other):
                await store.set_status(conn, ws, m, "disputed")
            await self._audit(cc, mid, row.type, "disputed", body)
            return {"memory_ids": [mid, other]}
        # supersede_existing: the reviewer vouches for the new memory, which must not outrank the reviewer
        mine = authority_for("explicit", actor_of(cc.principal))
        if AUTHORITY_RANK[mine] < AUTHORITY_RANK[existing.authority_class]:
            raise InsufficientAuthority()
        await store.set_status(conn, ws, other, "superseded", close_validity=True)
        await store.set_status(conn, ws, mid, "active", confirm=True)
        await conn.execute(
            text(
                "INSERT INTO memory_lineage (memory_id, related_id, workspace_id, relation) VALUES (:m, :r, :ws, 'replaces') ON CONFLICT DO NOTHING"
            ),
            {"m": mid, "r": other, "ws": ws},
        )
        await store.add_evidence(conn, ws, mid, EvidenceRef("memory", memory_id=other), cc.principal.principal_id)
        await self._audit(cc, mid, row.type, "active", body)
        await self._audit(cc, other, existing.type, "superseded", body)
        return {"memory_ids": [mid, other]}

    @staticmethod
    async def _audit(cc: CommandContext, memory_id: uuid.UUID, memory_type: str, status: str, body: DecisionIn) -> None:
        await cc.audit.record(
            "memory.review_decided",
            resource_type="memory",
            resource_id=memory_id,
            metadata=AuditMetadata(
                memory_id=memory_id,
                memory_type=memory_type,
                memory_status=status,
                decision=body.decision,
                rationale_code=body.rationale_code,
            ),
        )
