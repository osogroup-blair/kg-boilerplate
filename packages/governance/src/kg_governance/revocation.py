"""Revoking an agent or an app (phase-7 plan §5.4, ADR-039): final, immediate, and complete.

One tenant transaction does all of it:

1. the principal becomes `revoked` (who, when, why) and ALL its keys are revoked, so the next request with its key or a JWT minted from it fails (authentication already
   re-checks the database on every request, ADR-020);
2. its WORKING memories expire at once and its AGENT-PRIVATE memories are frozen (no more writes or publishing; erasure is P8);
3. `retired`: proposals it submitted that are still under review stay, and the review item shows a `submitter_revoked` badge (computed at read time from the principal);
4. `compromised` also quarantines everything derived from it, as `disputed` plus a `source_compromised` review item (a reviewer restores with one decision): its unreviewed
   proposals are rejected (`submitter_compromised`), its shared findings and the memories and facts extracted from its submissions are disputed unless an independent source
   also supports them, and the model stages of its pending submissions become `skipped_by_policy`.

Un-revoking does not exist: ids and keys never come back, a replacement agent is a new registration. A second revoke is a 200 with no new effects; escalating `retired` to
`compromised` applies the quarantine (a compromise discovered later is a real case). The ledger command carries counts and the note; nothing here logs a handle or content.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from kg_common.errors import NotFound
from kg_common.ids import uuid7
from kg_obs import metrics
from sqlalchemy import text

from kg_governance.audit import AuditMetadata, CommandType
from kg_governance.principals import REGISTRABLE_KINDS, Registration, RevokeIn, RevokeOut, _digest, principal_out

_NOT_CORROBORATED = (
    "NOT EXISTS (SELECT 1 FROM fact_evidence e2 LEFT JOIN interactions i2 ON i2.workspace_id = e2.workspace_id AND i2.id = e2.interaction_id "
    "LEFT JOIN commands c2 ON c2.workspace_id = e2.workspace_id AND c2.id = e2.command_id "
    "WHERE e2.workspace_id = f.workspace_id AND e2.fact_id = f.id "
    "AND ((e2.source_kind = 'episode' AND i2.submitted_by <> :p) OR (e2.source_kind = 'command' AND c2.actor_principal_id <> :p)))"
)


_DISPUTE_FACTS = (
    "UPDATE facts f SET status = 'disputed', decision_code = 'source_compromised', decided_at = now(), revision = revision + 1, "
    "projection_status = 'pending', projection_attempts = 0, projection_next_at = now(), last_error_code = NULL "
    "WHERE f.workspace_id = :ws AND f.status IN ('active','proposed') AND EXISTS (SELECT 1 FROM fact_evidence e JOIN interactions i "
    "ON i.workspace_id = e.workspace_id AND i.id = e.interaction_id WHERE e.workspace_id = f.workspace_id AND e.fact_id = f.id "
    "AND i.submitted_by = :p)"
)


class Revocation(Registration):
    async def revoke(self, principal_id: uuid.UUID, body: RevokeIn, key: str) -> tuple[RevokeOut, bool]:
        """Returns (answer, replayed)."""
        digest = _digest(CommandType.PRINCIPAL_REVOKE, self._actor.principal_id, [str(principal_id), body.model_dump(mode="json")])
        prior = await self._replay(key, CommandType.PRINCIPAL_REVOKE, digest)
        if prior is not None:
            return RevokeOut.model_validate(prior["out"]), True
        row = await self.row(principal_id, lock=True)
        if row.kind not in {k.value for k in REGISTRABLE_KINDS}:
            raise NotFound()  # users and service principals are not revoked through this API
        out = RevokeOut(principal=principal_out(row))
        escalation = row.status == "revoked" and row.revoke_reason == "retired" and body.reason == "compromised"
        if row.status == "revoked" and not escalation:
            out.already_revoked = True
            return out, False
        ws, p = self.ws, principal_id
        c = self._conn
        if row.status != "revoked":
            await c.execute(
                text(
                    "UPDATE principals SET status = 'revoked', revoked_at = now(), revoked_by = :by, revoke_reason = :reason, updated_at = now(), "
                    "revision = revision + 1 WHERE workspace_id = :ws AND id = :p"
                ),
                {"ws": ws, "p": p, "by": self._actor.principal_id, "reason": body.reason},
            )
            out.keys_revoked = (
                await c.execute(
                    text("UPDATE api_keys SET revoked_at = now() WHERE workspace_id = :ws AND principal_id = :p AND revoked_at IS NULL"),
                    {"ws": ws, "p": p},
                )
            ).rowcount
            # working memory expires now; agent-private memory is frozen (the owner can no longer write or publish it)
            out.working_memories_expired = (
                await c.execute(
                    text(
                        "UPDATE curated_memories SET status = 'expired', revision = revision + 1, expires_at = now(), "
                        "effective_until = GREATEST(effective_from, LEAST(COALESCE(effective_until, now()), now())) "
                        "WHERE workspace_id = :ws AND created_by = :p AND type = 'working' AND status IN ('active','proposed')"
                    ),
                    {"ws": ws, "p": p},
                )
            ).rowcount
            out.private_memories_frozen = (
                await c.execute(
                    text(
                        "UPDATE curated_memories m SET frozen_at = now() WHERE m.workspace_id = :ws AND m.created_by = :p AND m.frozen_at IS NULL "
                        "AND NOT EXISTS (SELECT 1 FROM memory_scopes s WHERE s.workspace_id = m.workspace_id AND s.memory_id = m.id "
                        "AND s.scope_kind IN ('workspace','subject','project'))"
                    ),
                    {"ws": ws, "p": p},
                )
            ).rowcount
        if body.reason == "compromised":
            await self._quarantine(p, out)
        await c.execute(
            text("UPDATE principals SET revoke_reason = :r WHERE workspace_id = :ws AND id = :p"), {"ws": ws, "p": p, "r": body.reason}
        )
        out.principal = principal_out(await self.row(p))
        stored = out.model_dump(mode="json")
        await self._record(
            CommandType.PRINCIPAL_REVOKE,
            key,
            digest,
            {"principal_id": str(p), "reason": body.reason, "note_present": body.note is not None},
            {"out": stored, "note": body.note},
            200,
        )
        await self._audit.record(
            "principal.revoked",
            resource_type="principal",
            resource_id=p,
            metadata=AuditMetadata(
                target_id=p,
                command_type=CommandType.PRINCIPAL_REVOKE,
                revoke_reason=body.reason,
                principal_kind=row.kind,
                count=out.keys_revoked,
                count_facts=out.facts_disputed,
                count_memories=out.shared_memories_disputed + out.proposals_rejected,
            ),
        )
        metrics.revocations_total.labels(reason=body.reason).inc()
        return out, False

    # ---- the memory-poisoning response ------------------------------------------------------------------------------------------------------

    async def _quarantine(self, p: uuid.UUID, out: RevokeOut) -> None:
        c, ws = self._conn, self.ws
        # 1. proposals still under review are refused, and their review items closed
        rejected = (
            await c.execute(
                text(
                    "UPDATE curated_memories SET status = 'rejected', revision = revision + 1 "
                    "WHERE workspace_id = :ws AND created_by = :p AND status = 'proposed' RETURNING id"
                ),
                {"ws": ws, "p": p},
            )
        ).all()
        out.proposals_rejected = len(rejected)
        if rejected:
            await c.execute(
                text(
                    "UPDATE review_items SET status = 'resolved', decision = 'reject', rationale_code = 'submitter_compromised', decided_by = :by, decided_at = now(), "
                    "resolved_by = :by, resolved_at = now(), resolution_code = 'reject', revision = revision + 1 "
                    "WHERE workspace_id = :ws AND status = 'open' AND item_type IN ('memory_proposal','memory_conflict') AND memory_id = ANY(:ids)"
                ),
                {"ws": ws, "by": self._actor.principal_id, "ids": [r.id for r in rejected]},
            )
        # 2. its pending model stages are skipped, and their jobs cancelled
        stages = (
            await c.execute(
                text(
                    "UPDATE interaction_stages s SET status = 'skipped_by_policy', last_error_category = NULL, last_error_code = 'submitter_compromised', finished_at = now() "
                    "FROM interactions i WHERE s.workspace_id = :ws AND i.workspace_id = s.workspace_id AND i.id = s.interaction_id AND i.submitted_by = :p "
                    "AND s.status IN ('pending','failed','dead') RETURNING s.interaction_id"
                ),
                {"ws": ws, "p": p},
            )
        ).all()
        out.stages_skipped = len(stages)
        if stages:
            jobs = (
                await c.execute(
                    text(
                        "SELECT id FROM outbox_jobs WHERE workspace_id = :ws AND subject_type = 'interaction' AND subject_id = ANY(:ids) AND status IN ('pending','dead')"
                    ),
                    {"ws": ws, "ids": sorted({r.interaction_id for r in stages}, key=str)},
                )
            ).all()
            for j in jobs:
                await c.execute(text("SELECT * FROM ops.cancel_job(:id)"), {"id": j.id})
        # 3. what it published, and what was extracted from what it submitted, becomes disputed (unless an independent source also supports it)
        memory_ids = [
            r.id
            for r in (
                await c.execute(
                    text(
                        "UPDATE curated_memories m SET status = 'disputed', revision = revision + 1 "
                        "WHERE m.workspace_id = :ws AND m.status = 'active' AND m.type <> 'working' AND ("
                        "  (m.created_by = :p AND m.origin IN ('proposal','published','explicit') AND EXISTS (SELECT 1 FROM memory_scopes s WHERE s.workspace_id = m.workspace_id "
                        "     AND s.memory_id = m.id AND s.scope_kind IN ('workspace','subject','project'))) "
                        "  OR (m.origin = 'extracted' AND EXISTS (SELECT 1 FROM memory_evidence e JOIN interactions i ON i.workspace_id = e.workspace_id AND i.id = e.interaction_id "
                        "     WHERE e.workspace_id = m.workspace_id AND e.memory_id = m.id AND i.submitted_by = :p) "
                        "   AND NOT EXISTS (SELECT 1 FROM memory_evidence e2 LEFT JOIN interactions i2 ON i2.workspace_id = e2.workspace_id AND i2.id = e2.interaction_id "
                        "     WHERE e2.workspace_id = m.workspace_id AND e2.memory_id = m.id AND ((e2.source_kind = 'interaction' AND i2.submitted_by <> :p) "
                        "OR e2.source_kind IN ('command','external_ref'))))) "
                        "RETURNING m.id"
                    ),
                    {"ws": ws, "p": p},
                )
            ).all()
        ]
        out.shared_memories_disputed = len(memory_ids)
        fact_sql = f"{_DISPUTE_FACTS} AND {_NOT_CORROBORATED} RETURNING f.id"  # noqa: S608 (constant fragments; the principal is a bound parameter)
        fact_ids = [r.id for r in (await c.execute(text(fact_sql), {"ws": ws, "p": p})).all()]
        out.facts_disputed = len(fact_ids)
        opened = 0
        for mid in memory_ids:
            opened += await self._open_review(memory_id=mid, fact_id=None, principal_id=p)
        for fid in fact_ids:
            opened += await self._open_review(memory_id=None, fact_id=fid, principal_id=p)
        out.review_items_opened = opened

    async def _open_review(self, *, memory_id: uuid.UUID | None, fact_id: uuid.UUID | None, principal_id: uuid.UUID) -> int:
        res = await self._conn.execute(
            text(
                "INSERT INTO review_items (id, workspace_id, item_type, rule_kind, reason_code, memory_id, fact_id, detail, due_at) "
                "VALUES (:id, :ws, 'source_compromised', 'principal_revocation', 'source_compromised', :m, :f, CAST(:d AS jsonb), now() + interval '7 days') "
                "ON CONFLICT DO NOTHING"
            ),
            {
                "id": uuid7(),
                "ws": self.ws,
                "m": memory_id,
                "f": fact_id,
                "d": json.dumps({"principal_id": str(principal_id), "revoked_by": str(self._actor.principal_id)}),
            },
        )
        return int(res.rowcount)


def revocation_effects(out: RevokeOut) -> dict[str, Any]:
    """The counts a caller (the CLI, the console's confirmation) shows after a revoke."""
    return out.model_dump(mode="json", exclude={"principal"})
