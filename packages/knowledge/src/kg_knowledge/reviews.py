"""The review queue (P4, ADR-027/028): identity matches, contradictions, Graphiti invalidations and queued policy violations.

A review decision is a LEDGER COMMAND (`review.decide`), not a direct graph mutation (Q13): it goes through the same idempotency, audit and projection path as
every other command, and the projector makes the graph follow the rows it changed. A decision never displaces a recorded, verified fact: that is a
correction (P8), and the endpoint says so (`409 requires_correction`).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from kg_common.errors import Conflict, IdempotencyMismatch, NotFound, PreconditionFailed, PreconditionRequired, ValidationFailed
from kg_common.ids import uuid7
from kg_common.pagination import clamp_limit, decode_cursor, encode_cursor
from kg_common.versions import PIPELINE_VERSION
from kg_domain.envelope import Page
from kg_domain.principal import WorkspaceContext
from kg_governance.audit import AuditMetadata, CommandType
from kg_jobs.outbox import enqueue
from kg_obs import metrics
from kg_ontology.conflicts import Invalidation
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

from kg_knowledge.commands import CommandContext, CommandPipeline, PipelineResult
from kg_knowledge.extracted import ExtractedKnowledge, touch
from kg_knowledge.idempotency import lock_key, request_hash
from kg_knowledge.merge import ledger_command, merge_entity
from kg_knowledge.models import (
    ALLOWED_DECISIONS,
    DecisionIn,
    DecisionOut,
    EntityCreateIn,
    EntityUpdateIn,
    ExternalId,
    IdentityCandidate,
    InteractionRef,
    RelationshipAssertIn,
    RelationshipEndIn,
    ReviewDetail,
    ReviewEntityView,
    ReviewOut,
)
from kg_knowledge.reads import Reads
from kg_knowledge.specs import SpecCache


class RequiresCorrection(Conflict):
    code, default_message = "requires_correction", "Displacing a recorded fact is a correction, not a review decision"


class NotAvailable(Conflict):
    code, default_message = "not_available", "This decision is not available yet"


class AlreadyDecided(Conflict):
    code, default_message = "already_decided", "This review item has already been decided"


class CannotReapply(Conflict):
    code, default_message = "cannot_reapply", "The queued command cannot be applied again (sensitive values are not kept in the ledger)"


_NOW = dt.datetime.now


def _status(db_status: str) -> str:
    return "open" if db_status == "open" else "decided"


def _out(r: Row[Any]) -> ReviewOut:
    return ReviewOut(
        id=r.id,
        item_type=r.item_type,
        status=_status(r.status),
        rule_kind=r.rule_kind,
        reason_code=r.reason_code,
        revision=r.revision,
        created_at=r.created_at,
        due_at=r.due_at,
        command_id=r.command_id,
        subject_entity_id=r.subject_entity_id,
        candidate_entity_id=r.candidate_entity_id,
        fact_id=r.fact_id,
        competing_fact_id=r.competing_fact_id,
        interaction_id=r.interaction_id,
        decision=r.decision,
        rationale_code=r.rationale_code,
        decided_by=r.decided_by,
        decided_at=r.decided_at,
    )


class ReviewService:
    def __init__(self, specs: SpecCache, extracted: ExtractedKnowledge, pipeline: CommandPipeline, reads: Reads):
        self._specs, self._extracted, self._pipeline, self._reads = specs, extracted, pipeline, reads

    # ---- reads ----------------------------------------------------------------------------------------------------------------------------

    async def list_items(
        self,
        conn: AsyncConnection,
        ctx: WorkspaceContext,
        *,
        item_type: str | None,
        status: str | None,
        limit: int | None,
        cursor: str | None,
    ) -> Page[ReviewOut]:
        size, before = clamp_limit(limit), decode_cursor(cursor)
        where, params = ["workspace_id = :ws"], {"ws": ctx.workspace_id, "n": size + 1}
        if item_type:
            where.append("item_type = :t")
            params["t"] = item_type
        if status == "open":
            where.append("status = 'open'")
        elif status == "decided":
            where.append("status <> 'open'")
        if before:
            where.append("(created_at, id) < (:c_at, :c_id)")
            params["c_at"], params["c_id"] = before[0], before[1]
        sql = f"SELECT * FROM review_items WHERE {' AND '.join(where)} ORDER BY created_at DESC, id DESC LIMIT :n"  # noqa: S608 (constant fragments)
        rows = (await conn.execute(text(sql), params)).all()
        page, more = rows[:size], len(rows) > size
        return Page[ReviewOut](items=[_out(r) for r in page], next_cursor=encode_cursor(page[-1].created_at, page[-1].id) if more else None)

    async def _row(self, conn: AsyncConnection, ws: uuid.UUID, review_id: uuid.UUID, *, lock: bool = False) -> Row[Any]:
        row = (
            await conn.execute(
                text(f"SELECT * FROM review_items WHERE workspace_id = :ws AND id = :id{' FOR UPDATE' if lock else ''}"),  # noqa: S608
                {"ws": ws, "id": review_id},
            )
        ).first()
        if row is None:
            raise NotFound()
        return row

    async def _entity_view(
        self, conn: AsyncConnection, ws: uuid.UUID, entity_id: uuid.UUID, can_see_sensitive: bool
    ) -> ReviewEntityView | None:
        e = (
            await conn.execute(
                text(
                    "SELECT e.*, (SELECT count(*) FROM entity_mentions m WHERE m.workspace_id = e.workspace_id AND m.entity_id = e.id) AS mentions, "
                    "(SELECT count(*) FROM facts f WHERE f.workspace_id = e.workspace_id AND (f.source_entity_id = e.id OR f.target_entity_id = e.id)) AS fact_count, "
                    "(SELECT COALESCE(jsonb_agg(jsonb_build_object('system', x.system, 'value', x.value) ORDER BY x.system, x.value), '[]'::jsonb) "
                    "FROM entity_external_ids x WHERE x.workspace_id = e.workspace_id AND x.entity_id = e.id) AS external_ids "
                    "FROM entities e WHERE e.workspace_id = :ws AND e.id = :id"
                ),
                {"ws": ws, "id": entity_id},
            )
        ).first()
        if e is None:
            return None
        sensitive = await self._reads._sensitive(conn, ws, e.ontology_version_id, e.type_key, relationship=False)
        attrs = (
            dict(e.attributes or {})
            if can_see_sensitive
            else {k: ({"redacted": True} if k in sensitive else v) for k, v in (e.attributes or {}).items()}
        )
        return ReviewEntityView(
            id=e.id,
            type=e.type_key,
            name=e.display_name,
            aliases=list(e.aliases),
            attributes=attrs,
            external_ids=[ExternalId.model_validate(x) for x in e.external_ids],
            status=e.status,
            origin=e.origin,
            classification=e.classification,
            mention_count=int(e.mentions),
            fact_count=int(e.fact_count),
            merged_into_id=e.merged_into_id,
        )

    async def interactions_for(self, conn: AsyncConnection, ws: uuid.UUID, review_id: uuid.UUID) -> list[InteractionRef]:
        """The interactions an item rests on: the one that raised it, the evidence of its facts, and what mentions its candidate entity."""
        r = await self._row(conn, ws, review_id)
        rows = (
            await conn.execute(
                text(
                    "SELECT i.id, i.source_system, i.source_ref, i.occurred_at, i.recorded_at, refs.episode_uuid FROM interactions i "
                    "JOIN (SELECT :i AS interaction_id, NULL::text AS episode_uuid WHERE CAST(:i AS uuid) IS NOT NULL "
                    "UNION SELECT interaction_id, episode_uuid FROM fact_evidence WHERE workspace_id = :ws AND fact_id = ANY(:facts) AND interaction_id IS NOT NULL "
                    "UNION SELECT interaction_id, episode_uuid FROM entity_mentions WHERE workspace_id = :ws AND entity_id = :cand) refs "
                    "ON refs.interaction_id = i.id WHERE i.workspace_id = :ws ORDER BY i.recorded_at, i.id"
                ),
                {
                    "ws": ws,
                    "i": r.interaction_id,
                    "facts": [f for f in (r.fact_id, r.competing_fact_id) if f is not None],
                    "cand": r.candidate_entity_id,
                },
            )
        ).all()
        seen: dict[uuid.UUID, InteractionRef] = {}
        for x in rows:
            if x.id not in seen or (seen[x.id].episode_uuid is None and x.episode_uuid):
                seen[x.id] = InteractionRef(
                    interaction_id=x.id,
                    episode_uuid=x.episode_uuid,
                    source_system=x.source_system,
                    source_ref=x.source_ref,
                    occurred_at=x.occurred_at,
                    recorded_at=x.recorded_at,
                )
        return list(seen.values())

    async def get(self, conn: AsyncConnection, ctx: WorkspaceContext, review_id: uuid.UUID, *, can_see_sensitive: bool) -> ReviewDetail:
        ws = ctx.workspace_id
        r = await self._row(conn, ws, review_id)
        entities: list[ReviewEntityView] = []
        for eid in (r.subject_entity_id, r.candidate_entity_id):
            if eid is not None and (view := await self._entity_view(conn, ws, eid, can_see_sensitive)) is not None:
                entities.append(view)
        facts = []
        for fid in (r.competing_fact_id, r.fact_id):  # the existing fact first, then the claim: side by side, in that order
            if fid is not None:
                facts.append(await self._reads.get_fact(conn, ctx, fid, can_see_sensitive=can_see_sensitive))
        violations: list[dict[str, Any]] = []
        if r.command_id is not None:
            cmd = (
                await conn.execute(
                    text("SELECT decision FROM commands WHERE workspace_id = :ws AND id = :id"), {"ws": ws, "id": r.command_id}
                )
            ).first()
            for stage in (cmd.decision if cmd else []) or []:
                violations += [
                    {k: v.get(k) for k in ("rule_kind", "rule_id", "code", "path", "action")}
                    for v in stage.get("violations", [])
                    if v.get("action") != "warn"
                ]
        detail = r.detail or {}
        candidates = [IdentityCandidate.model_validate(c) for c in detail.get("candidates", [])]
        return ReviewDetail(
            **_out(r).model_dump(),
            allowed_decisions=list(ALLOWED_DECISIONS[r.item_type]) if r.status == "open" else [],
            rationale_text=r.rationale_text,
            entities=entities,
            facts=facts,
            candidates=candidates,
            interactions=await self.interactions_for(conn, ws, review_id),
            violations=violations,
        )

    # ---- decide ---------------------------------------------------------------------------------------------------------------------------

    async def decide(
        self, cc: CommandContext, review_id: uuid.UUID, body: DecisionIn, *, key: str, if_match: int | None
    ) -> tuple[DecisionOut, bool]:
        """Returns (result, replayed). Everything below runs in the caller's tenant transaction: a refusal raises and nothing is written."""
        conn, ws = cc.conn, cc.ctx.workspace_id
        payload: dict[str, object] = {"review_id": str(review_id), "decision": body.decision, "rationale_code": body.rationale_code}
        digest = request_hash("review.decide", payload)
        await lock_key(conn, ws, "cmd", key)
        prior = (
            await conn.execute(text("SELECT * FROM commands WHERE workspace_id = :ws AND idempotency_key = :k"), {"ws": ws, "k": key})
        ).first()
        if prior is not None:
            if prior.request_hash != digest or prior.command_type != CommandType.REVIEW_DECIDE.value:
                raise IdempotencyMismatch()
            replay = DecisionOut.model_validate(prior.result["out"])
            replay.projection_status = prior.projection_status  # the stored answer, with the CURRENT state of its projection
            return replay, True
        review = await self._row(conn, ws, review_id, lock=True)
        if if_match is None:
            raise PreconditionRequired()
        if if_match != review.revision:
            raise PreconditionFailed()
        if review.status != "open":
            raise AlreadyDecided()
        if body.decision not in ALLOWED_DECISIONS[review.item_type]:
            raise ValidationFailed("this decision does not apply to this kind of review item", details={"fields": ["decision"]})

        handler = {
            "identity_match": self._identity,
            "contradiction": self._contradiction,
            "graphiti_invalidation": self._invalidation,
            "policy_violation": self._policy,
        }[review.item_type]
        effects = await handler(cc, review, body)
        entity_ids = sorted({*effects.get("entity_ids", [])}, key=str)
        fact_ids = sorted({*effects.get("fact_ids", [])}, key=str)

        stays_open = body.decision == "needs_more_evidence"
        ontology_version = await self._ontology_version(conn, ws, review)
        command_id = uuid7()
        out = DecisionOut(
            review_id=review_id,
            decision=body.decision,
            status="open" if stays_open else "decided",
            command_id=command_id,
            revision=review.revision + 1,
            projection_status="pending",
            entity_ids=entity_ids,
            fact_ids=fact_ids,
            applied_command_id=effects.get("applied_command_id"),
        )
        await ledger_command(
            conn,
            ws,
            command_type=CommandType.REVIEW_DECIDE,
            key=key,
            actor_principal_id=cc.principal.principal_id,
            ontology_version_id=ontology_version,
            payload=payload,
            result={"entity_ids": [str(e) for e in entity_ids], "fact_ids": [str(f) for f in fact_ids], "out": out.model_dump(mode="json")},
            authority="admin_asserted" if "workspace_admin" in {r.value for r in cc.principal.roles} else "user_asserted",
            source_system="review",
            source_ref=str(review_id),
            command_id=command_id,
        )
        if stays_open:
            await conn.execute(
                text(
                    "UPDATE review_items SET revision = revision + 1, due_at = now() + interval '7 days' WHERE workspace_id = :ws AND id = :id"
                ),
                {"ws": ws, "id": review_id},
            )
        else:
            await conn.execute(
                text(
                    "UPDATE review_items SET status = 'resolved', decision = :d, rationale_code = :rc, rationale_text = :rt, decided_by = :by, decided_at = now(), "
                    "resolved_by = :by, resolved_at = now(), resolution_code = :d, revision = revision + 1 WHERE workspace_id = :ws AND id = :id"
                ),
                {
                    "d": body.decision,
                    "rc": body.rationale_code,
                    "rt": body.rationale_text,
                    "by": cc.principal.principal_id,
                    "ws": ws,
                    "id": review_id,
                },
            )
        await cc.audit.record(
            "review.decided",
            resource_type="review_item",
            resource_id=review_id,
            metadata=AuditMetadata(
                review_id=review_id,
                review_type=review.item_type,
                decision=body.decision,
                rationale_code=body.rationale_code,
                count_entities=len(entity_ids),
                count_facts=len(fact_ids),
            ),
        )
        metrics.review_decisions_total.labels(item_type=review.item_type, decision=body.decision).inc()
        await enqueue(
            conn,
            workspace_id=ws,
            stage="graph_projection",
            subject_type="command",
            subject_id=command_id,
            dedupe_key=f"command:{command_id}:graph_projection:g0",
            payload={"command_id": str(command_id), "generation": 0},
            pipeline_version=PIPELINE_VERSION,
            ontology_version_id=ontology_version,
            concurrency_key=f"graph:{ws}",
        )
        return out, False

    async def _ontology_version(self, conn: AsyncConnection, ws: uuid.UUID, review: Row[Any]) -> uuid.UUID:
        for table, col in (
            ("facts", review.fact_id),
            ("entities", review.candidate_entity_id),
            ("entities", review.subject_entity_id),
            ("commands", review.command_id),
        ):
            if col is not None:
                query = text(f"SELECT ontology_version_id FROM {table} WHERE workspace_id = :ws AND id = :id")  # noqa: S608 (the table is a constant above)
                row = (await conn.execute(query, {"ws": ws, "id": col})).first()
                if row is not None:
                    return uuid.UUID(str(row.ontology_version_id))
        raise NotFound()

    async def _spec(self, cc: CommandContext, version_id: uuid.UUID):  # type: ignore[no-untyped-def]
        return await self._specs.get(cc.conn, cc.ctx.workspace_id, version_id)

    # ---- identity_match -------------------------------------------------------------------------------------------------------------------

    async def _identity(self, cc: CommandContext, review: Row[Any], body: DecisionIn) -> dict[str, Any]:
        conn, ws = cc.conn, cc.ctx.workspace_id
        cand, subj = review.candidate_entity_id, review.subject_entity_id
        if body.decision == "needs_more_evidence":
            return {"entity_ids": [cand, subj]}
        if body.decision == "same_entity":
            merged = await merge_entity(
                conn,
                ws,
                specs=self._specs,
                extracted=self._extracted,
                from_id=cand,
                into_id=subj,
                actor_principal_id=cc.principal.principal_id,
                mention_resolution="reviewer",
                reason="reviewer",
                audit=cc.audit,
            )
            return {"entity_ids": [cand, subj], "fact_ids": merged.fact_ids}
        # different_entity: the candidate is its own entity, the pair is remembered, and the facts that waited for this identity are assessed again
        a, b = sorted((cand, subj), key=str)
        await conn.execute(
            text(
                "INSERT INTO identity_non_matches (entity_a, entity_b, workspace_id, decided_by, reason_code) VALUES (:a, :b, :ws, :by, :rc) ON CONFLICT DO NOTHING"
            ),
            {"a": a, "b": b, "ws": ws, "by": cc.principal.principal_id, "rc": body.rationale_code},
        )
        await touch(conn, ws, "entities", cand, status="active")
        waiting = [
            r.id
            for r in (
                await conn.execute(
                    text(
                        "SELECT id FROM facts WHERE workspace_id = :ws AND status = 'proposed' AND decision_code = 'identity_pending' "
                        "AND (source_entity_id = :c OR target_entity_id = :c)"
                    ),
                    {"ws": ws, "c": cand},
                )
            ).all()
        ]
        for fid in waiting:
            version = (
                await conn.execute(
                    text("SELECT ontology_version_id FROM facts WHERE workspace_id = :ws AND id = :id"), {"ws": ws, "id": fid}
                )
            ).scalar_one()
            res, _ = await self._extracted.reevaluate_fact(conn, ws, await self._spec(cc, version), fid)
            if res.review is not None:
                await self._extracted.open_review(
                    conn,
                    ws,
                    item_type=res.review,
                    rule_kind="conflict_policy",
                    reason_code=res.code,
                    audit=cc.audit,
                    fact_id=fid,
                    competing_fact_id=uuid.UUID(res.competing[0]) if res.competing else None,
                    detail={"competing": list(res.competing), "after": "identity_decision"},
                )
        return {"entity_ids": [cand, subj], "fact_ids": waiting}

    # ---- contradiction --------------------------------------------------------------------------------------------------------------------

    async def _contradiction(self, cc: CommandContext, review: Row[Any], body: DecisionIn) -> dict[str, Any]:
        conn, ws = cc.conn, cc.ctx.workspace_id
        claim = review.fact_id
        competing = [uuid.UUID(x) for x in (review.detail or {}).get("competing", [])] or (
            [review.competing_fact_id] if review.competing_fact_id else []
        )
        now = _NOW(dt.UTC)
        if body.decision == "keep_existing":
            await touch(conn, ws, "facts", claim, status="rejected", decision_code="keep_existing", decided_at=now)
            for other in competing:  # a dispute ends with the existing side standing
                row = (
                    await conn.execute(text("SELECT status FROM facts WHERE workspace_id = :ws AND id = :id"), {"ws": ws, "id": other})
                ).first()
                if row is not None and row.status == "disputed":
                    await touch(conn, ws, "facts", other, status="active", decision_code="dispute_resolved", decided_at=None)
        elif body.decision == "mark_disputed":
            for fid in (claim, *competing):
                await touch(conn, ws, "facts", fid, status="disputed", decision_code="mark_disputed", decided_at=now)
        else:  # accept_claim: the reviewer vouches, so the claim runs through the rules again as admin_asserted. It may END a predecessor (a dated succession); it never displaces one.
            version = (
                await conn.execute(
                    text("SELECT ontology_version_id FROM facts WHERE workspace_id = :ws AND id = :id"), {"ws": ws, "id": claim}
                )
            ).scalar_one()
            res, _ = await self._extracted.reevaluate_fact(
                conn,
                ws,
                await self._spec(cc, version),
                claim,
                authority="admin_asserted",
                invalidated=[Invalidation(str(c)) for c in competing],
                hold=False,
            )
            if res.status != "active":
                raise RequiresCorrection(details={"code": res.code})  # the whole decision rolls back with this exception
        return {"fact_ids": [claim, *competing], "entity_ids": []}

    # ---- graphiti_invalidation ------------------------------------------------------------------------------------------------------------

    async def _invalidation(self, cc: CommandContext, review: Row[Any], body: DecisionIn) -> dict[str, Any]:
        if body.decision == "open_correction":
            raise NotAvailable("Corrections of verified facts arrive in a later phase")
        return {"fact_ids": [review.fact_id]}

    # ---- policy_violation -----------------------------------------------------------------------------------------------------------------

    async def _policy(self, cc: CommandContext, review: Row[Any], body: DecisionIn) -> dict[str, Any]:
        conn, ws = cc.conn, cc.ctx.workspace_id
        if review.fact_id is not None:  # an extracted fact a rule wanted a human to see
            if body.decision == "approve":
                await touch(conn, ws, "facts", review.fact_id, status="active", decision_code="approved_by_reviewer", decided_at=None)
            else:
                await touch(
                    conn, ws, "facts", review.fact_id, status="rejected", decision_code="rejected_by_reviewer", decided_at=_NOW(dt.UTC)
                )
            return {"fact_ids": [review.fact_id]}
        if body.decision == "reject":
            return {}
        return await self._reapply(cc, review)

    async def _reapply(self, cc: CommandContext, review: Row[Any]) -> dict[str, Any]:
        """Approves a queued COMMAND: it is applied once, now, with the rule that queued it overridden. The original stays in the ledger as `needs_review`; the
        applied command is a new one, made by the reviewer, so the ledger shows who let it through."""
        conn, ws = cc.conn, cc.ctx.workspace_id
        cmd = (
            await conn.execute(
                text("SELECT * FROM commands WHERE workspace_id = :ws AND id = :id FOR UPDATE"), {"ws": ws, "id": review.command_id}
            )
        ).one()
        payload = cmd.payload or {}
        if cmd.status != "needs_review" or "[redacted]" in str(payload):
            raise CannotReapply()
        key = f"review:{review.id}"
        c2 = CommandContext(conn, cc.ctx, cc.principal, cc.audit, override_queue=True)
        queued = (cmd.result or {}).get("queued_target", {})
        result: PipelineResult
        if cmd.command_type == "entity.create":
            result = await self._pipeline.entity_create(c2, key, EntityCreateIn.model_validate(payload))
        elif cmd.command_type == "relationship.assert":
            result = await self._pipeline.relationship_assert(c2, key, RelationshipAssertIn.model_validate(payload))
        elif cmd.command_type == "entity.update" and queued.get("entity_id"):
            rev = (
                await conn.execute(
                    text("SELECT revision FROM entities WHERE workspace_id = :ws AND id = :id"),
                    {"ws": ws, "id": uuid.UUID(queued["entity_id"])},
                )
            ).scalar_one()
            result = await self._pipeline.entity_update(
                c2, key, uuid.UUID(queued["entity_id"]), EntityUpdateIn.model_validate(payload), rev
            )
        elif cmd.command_type == "relationship.end" and queued.get("fact_id"):
            result = await self._pipeline.relationship_end(c2, key, uuid.UUID(queued["fact_id"]), RelationshipEndIn.model_validate(payload))
        else:
            raise CannotReapply()
        if result.status != "applied":
            raise Conflict(
                "The command is still not acceptable", details={"command_id": str(result.command_id), "violations": result.violations}
            )
        out: dict[str, Any] = {"applied_command_id": result.command_id, "entity_ids": [], "fact_ids": []}
        if result.out.entity_id:
            out["entity_ids"].append(result.out.entity_id)
        if result.out.fact_id:
            out["fact_ids"].append(result.out.fact_id)
        return out
