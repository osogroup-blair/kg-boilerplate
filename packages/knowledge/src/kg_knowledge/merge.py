"""Identity merge (ADR-028, KGR-01/02): one entity becomes an alias of another, with lineage, as a ledger command.

The canonical id never changes. The merged entity keeps its row (`status = merged`, `merged_into_id`), so its old id still resolves (a read answers 200 with
`merged_into_id`), its name and aliases become aliases of the canonical entity, and its strong identifiers, facts, mentions and attribute claims move across.
Facts that came from a model are assessed again against the canonical entity's facts, because a merge can create a conflict that was invisible before.

The graph follows afterwards: the projector deletes the merged node (moving its MENTIONS edges first) and re-creates each moved fact's edge with the SAME uuid
between the new endpoints. Splitting a wrong merge is a compensating command in P8 (Flow D).
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass, field

from kg_common.errors import Conflict, NotFound, ValidationFailed
from kg_common.ids import uuid7
from kg_governance.audit import AuditMetadata, CommandType
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from kg_knowledge.extracted import ExtractedKnowledge, touch
from kg_knowledge.idempotency import request_hash
from kg_knowledge.specs import SpecCache


class MergeRefused(Conflict):
    code, default_message = "merge_refused", "These entities cannot be merged"


@dataclass
class MergeResult:
    command_id: uuid.UUID
    from_id: uuid.UUID
    into_id: uuid.UUID
    fact_ids: list[uuid.UUID] = field(default_factory=list)  # every fact that now points at the canonical entity and must be re-projected
    demoted: list[uuid.UUID] = field(default_factory=list)  # facts whose status changed because of the merge


async def ledger_command(
    conn: AsyncConnection,
    ws: uuid.UUID,
    *,
    command_type: CommandType,
    key: str,
    actor_principal_id: uuid.UUID,
    ontology_version_id: uuid.UUID | None,
    payload: dict[str, object],
    result: dict[str, object],
    authority: str = "user_asserted",
    source_system: str | None = None,
    source_ref: str | None = None,
    projection: str = "pending",
    command_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """A review decision or a merge is a command like any other: immutable, idempotent, audited. Projection of its rows is driven by the caller, so the
    ledger row starts as `not_applicable` and the outbox job carries the graph work."""
    command_id = command_id or uuid7()
    await conn.execute(
        text(
            "INSERT INTO commands (id, workspace_id, idempotency_key, request_hash, command_type, actor_principal_id, authority_class, classification, "
            "source_system, source_ref, ontology_version_id, payload, status, decision, result, http_status, projection_status) "
            "VALUES (:id, :ws, :key, :h, :ct, :actor, :auth, 'internal', :ss, :sr, :ver, CAST(:payload AS jsonb), 'applied', '[]'::jsonb, CAST(:result AS jsonb), 200, :proj)"
        ),
        {
            "id": command_id,
            "ws": ws,
            "key": key,
            "h": request_hash(command_type.value, payload),
            "ct": command_type.value,
            "actor": actor_principal_id,
            "auth": authority,
            "ss": source_system,
            "sr": source_ref,
            "ver": ontology_version_id,
            "payload": json.dumps(payload, default=str),
            "result": json.dumps(result, default=str),
            "proj": projection,
        },
    )
    return command_id


async def merge_entity(
    conn: AsyncConnection,
    ws: uuid.UUID,
    *,
    specs: SpecCache,
    extracted: ExtractedKnowledge,
    from_id: uuid.UUID,
    into_id: uuid.UUID,
    actor_principal_id: uuid.UUID,
    mention_resolution: str,
    reason: str,
    audit: object,
    authority: str = "user_asserted",
) -> MergeResult:
    """Folds `from_id` into `into_id` inside the caller's transaction. `reason` is a closed token (`subject_ref`, `reviewer`)."""
    if from_id == into_id:
        raise ValidationFailed("an entity cannot be merged into itself")
    lo, hi = sorted((from_id, into_id), key=str)  # lock in a fixed order: two concurrent merges cannot deadlock
    rows = {
        r.id: r
        for r in (
            await conn.execute(
                text(
                    "SELECT id, type_key, display_name, aliases, status, ontology_version_id, origin FROM entities "
                    "WHERE workspace_id = :ws AND id IN (:a, :b) ORDER BY id FOR UPDATE"
                ),
                {"ws": ws, "a": lo, "b": hi},
            )
        ).all()
    }
    if from_id not in rows or into_id not in rows:
        raise NotFound()
    src, dst = rows[from_id], rows[into_id]
    if src.status == "merged":
        raise MergeRefused("The entity was already merged")
    if dst.status == "merged":
        raise MergeRefused("The target was merged into another entity; merge into that one")
    if src.status in ("deleted",) or dst.status in ("deleted",):
        raise MergeRefused("A deleted entity cannot be merged")
    if src.type_key != dst.type_key:
        raise MergeRefused("Entities of different types cannot be merged")

    ids = (
        await conn.execute(
            text("SELECT entity_id, system, value FROM entity_external_ids WHERE workspace_id = :ws AND entity_id IN (:a, :b)"),
            {"ws": ws, "a": from_id, "b": into_id},
        )
    ).all()
    left = {(r.system, r.value) for r in ids if r.entity_id == from_id}
    right = {(r.system, r.value) for r in ids if r.entity_id == into_id}
    if any(sa == sb and va != vb for sa, va in left for sb, vb in right):
        raise MergeRefused("The entities carry different values of the same strong identifier, so they are different things")
    spec = await specs.get(conn, ws, dst.ontology_version_id)
    command_id = await ledger_command(
        conn,
        ws,
        command_type=CommandType.ENTITY_MERGE,
        key=f"merge:{from_id}:{into_id}",
        actor_principal_id=actor_principal_id,
        ontology_version_id=dst.ontology_version_id,
        payload={"from": str(from_id), "into": str(into_id), "reason": reason},
        result={"entity_id": str(into_id), "merged_entity_id": str(from_id)},
        authority=authority,
        source_system="platform",
        source_ref=f"merge:{reason}",
    )
    # names and strong identifiers move to the canonical entity: the old name keeps resolving as an alias, an external id keeps matching
    for alias in (src.display_name, *src.aliases):
        await extracted.add_alias(conn, ws, into_id, alias)
    await conn.execute(
        text("UPDATE entity_external_ids SET entity_id = :into WHERE workspace_id = :ws AND entity_id = :from"),
        {"ws": ws, "into": into_id, "from": from_id},
    )
    await conn.execute(
        text(
            "UPDATE entity_mentions SET entity_id = :into, resolution = :res, status = 'accepted' WHERE workspace_id = :ws AND entity_id = :from"
        ),
        {"ws": ws, "into": into_id, "from": from_id, "res": mention_resolution},
    )
    await conn.execute(
        text("UPDATE attribute_claims SET entity_id = :into WHERE workspace_id = :ws AND entity_id = :from"),
        {"ws": ws, "into": into_id, "from": from_id},
    )
    await conn.execute(
        text(
            "UPDATE review_items SET subject_entity_id = :into WHERE workspace_id = :ws AND subject_entity_id = :from AND status = 'open'"
        ),
        {"ws": ws, "into": into_id, "from": from_id},
    )
    moved = [
        r.id
        for r in (
            await conn.execute(
                text(
                    "SELECT id FROM facts WHERE workspace_id = :ws AND (source_entity_id = :from OR target_entity_id = :from) ORDER BY recorded_at, id"
                ),
                {"ws": ws, "from": from_id},
            )
        ).all()
    ]
    for fid in moved:
        await conn.execute(
            text(
                "UPDATE facts SET source_entity_id = CASE WHEN source_entity_id = :from THEN :into ELSE source_entity_id END, "
                "target_entity_id = CASE WHEN target_entity_id = :from THEN :into ELSE target_entity_id END, revision = revision + 1, "
                "projection_status = 'pending', projection_attempts = 0, projection_next_at = now(), last_error_code = NULL "
                "WHERE workspace_id = :ws AND id = :id"
            ),
            {"ws": ws, "from": from_id, "into": into_id, "id": fid},
        )
    # a fact between the two entities would now relate the canonical entity to itself
    selfref = (
        await conn.execute(
            text(
                "SELECT id FROM facts WHERE workspace_id = :ws AND source_entity_id = target_entity_id AND id = ANY(:ids) AND status <> 'rejected'"
            ),
            {"ws": ws, "ids": moved},
        )
    ).all()
    for r in selfref:
        await touch(conn, ws, "facts", r.id, status="rejected", decision_code="self_reference_after_merge", decided_at=_now())
    await touch(
        conn,
        ws,
        "entities",
        from_id,
        status="merged",
        merged_into_id=into_id,
        merged_at=_now(),
        last_command_id=command_id,
    )
    await touch(conn, ws, "entities", into_id, last_command_id=command_id)
    await _repoint_references(
        conn, ws, from_id, into_id
    )  # after the old entity is `merged` (the database accepts a re-point only for a merged entity)
    result = MergeResult(command_id, from_id, into_id, fact_ids=list(moved))
    # assess the extracted facts that moved against the canonical entity's other facts (a merge can surface a conflict)
    for fid in moved:
        row = (
            await conn.execute(
                text("SELECT status, source_type, decision_code FROM facts WHERE workspace_id = :ws AND id = :id"), {"ws": ws, "id": fid}
            )
        ).one()
        if row.source_type != "extracted" or row.status not in ("active", "proposed"):
            continue
        res, _ = await extracted.reevaluate_fact(conn, ws, spec, fid, hold=True)
        if res.status != row.status:
            result.demoted.append(fid)
        if res.review is not None:
            await extracted.open_review(
                conn,
                ws,
                item_type=res.review,
                rule_kind="conflict_policy" if res.review == "contradiction" else "review",
                reason_code=res.code,
                audit=audit,
                fact_id=fid,
                competing_fact_id=uuid.UUID(res.competing[0]) if res.competing else None,
                detail={"competing": list(res.competing), "after": "merge"},
            )
    if audit is not None:
        await audit.record(  # type: ignore[attr-defined]
            "entity.merged",
            resource_type="entity",
            resource_id=into_id,
            metadata=AuditMetadata(
                command_type=CommandType.ENTITY_MERGE, entity_id=into_id, target_id=from_id, reason_code=reason, count_facts=len(moved)
            ),
        )
    return result


async def _repoint_references(conn: AsyncConnection, ws: uuid.UUID, from_id: uuid.UUID, into_id: uuid.UUID) -> None:
    """Grants and memory references that name the merged entity follow the canonical one, in the same transaction (P7, T5): a principal granted a candidate that is then
    merged into Sarah keeps seeing Sarah, and a memory scoped to the candidate stays visible to the people granted Sarah. Where the holder already has a row for the
    canonical entity the rows are folded (privileges are unioned), never duplicated."""
    p = {"ws": ws, "from_s": str(from_id), "into_s": str(into_id), "from": from_id, "into": into_id}
    await conn.execute(
        text(
            "UPDATE principal_scopes c SET privileges = ARRAY(SELECT DISTINCT x FROM unnest(c.privileges || m.privileges) x ORDER BY x) "
            "FROM principal_scopes m WHERE c.workspace_id = :ws AND m.workspace_id = :ws AND c.principal_id = m.principal_id "
            "AND c.scope_kind = 'subject' AND m.scope_kind = 'subject' AND c.scope_ref = :into_s AND m.scope_ref = :from_s"
        ),
        p,
    )
    await conn.execute(
        text(
            "UPDATE principal_scopes s SET scope_ref = :into_s WHERE s.workspace_id = :ws AND s.scope_kind = 'subject' AND s.scope_ref = :from_s "
            "AND NOT EXISTS (SELECT 1 FROM principal_scopes x WHERE x.workspace_id = s.workspace_id AND x.principal_id = s.principal_id "
            "AND x.scope_kind = 'subject' AND x.scope_ref = :into_s)"
        ),
        p,
    )
    await conn.execute(
        text("DELETE FROM principal_scopes WHERE workspace_id = :ws AND scope_kind = 'subject' AND scope_ref = :from_s"), p
    )  # what is left already had a canonical row, whose privileges now include these
    await conn.execute(
        text(
            "UPDATE memory_scopes s SET scope_ref = :into_s WHERE s.workspace_id = :ws AND s.scope_kind = 'subject' AND s.scope_ref = :from_s "
            "AND NOT EXISTS (SELECT 1 FROM memory_scopes x WHERE x.workspace_id = s.workspace_id AND x.memory_id = s.memory_id "
            "AND x.scope_kind = 'subject' AND x.scope_ref = :into_s)"
        ),
        p,
    )
    await conn.execute(
        text(
            "UPDATE memory_subjects s SET entity_id = :into WHERE s.workspace_id = :ws AND s.entity_id = :from "
            "AND NOT EXISTS (SELECT 1 FROM memory_subjects x WHERE x.workspace_id = s.workspace_id AND x.memory_id = s.memory_id AND x.entity_id = :into)"
        ),
        p,
    )


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)
