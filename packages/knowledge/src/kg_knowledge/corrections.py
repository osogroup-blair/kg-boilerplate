"""Corrections (phase-8 plan §5.1, §5.2; blueprint Flow D, KGR-01, ADR-018): audited ledger commands that change what the platform holds to be true WITHOUT rewriting
history.

    fact.reject      the fact was never true                      the edge expires; dependent memories are disputed; predecessors it ended become current again
    fact.supersede   the fact was wrong, here is the right one    a new fact (authority admin_asserted, source_type correction) replaces it
    fact.end         it stopped being true at ...                 valid_to is set (succession, ADR-023)
    fact.restore     a rejection or dispute was itself wrong      back to active; the dependents' reviews are reopened
    entity.correct_attribute / entity.rename / entity.split       an attribute value, a name (the old one stays an alias; the id never changes, KGR-01), an identity

A correction is NOT governed by the authority ranking: a reviewer or admin may supersede an `authoritative_system` fact. That is a deliberate, audited override, and it
raises an `authoritative_override` item so the source system can be fixed. Original evidence is never rewritten: a correction appends; only deletion (P8 Flow E) removes.

Postgres stays authoritative. Each command changes rows and bumps their revisions in ONE transaction, then the graph follows (the projector restores the rows it names).
Dependents in the memory package are reached through a port (kg_knowledge cannot import kg_memory): `MemoryDependents`.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Protocol

from kg_common.errors import (
    Conflict,
    NoActiveOntology,
    NotFound,
    PreconditionFailed,
    PreconditionRequired,
    RuleViolation,
    UnknownEntity,
    ValidationFailed,
)
from kg_common.ids import uuid7
from kg_common.versions import PIPELINE_VERSION
from kg_domain.enums import Role
from kg_domain.principal import Principal, WorkspaceContext
from kg_governance.audit import AuditMetadata, CommandType
from kg_governance.grants import SubjectFilter, entity_visible_sql, fact_visible_sql
from kg_jobs.outbox import enqueue
from kg_obs import metrics
from kg_ontology.rules.context import EntityRef, RuleCommand, RuleContext
from kg_ontology.rules.engine import evaluate
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

from kg_knowledge.commands import CommandContext
from kg_knowledge.correction_models import (
    CorrectionOut,
    CorrectionRecord,
    DependentMemory,
    EntityCorrectionIn,
    FactCorrectionIn,
    PreviewOut,
)
from kg_knowledge.extracted import ExtractedKnowledge, touch
from kg_knowledge.idempotency import lock_key, request_hash
from kg_knowledge.merge import ledger_command
from kg_knowledge.models import EntityRefIn
from kg_knowledge.specs import SpecCache

_NOW = dt.datetime.now
REVERSIBLE_FACT = ("active", "proposed", "disputed", "superseded")
OPEN_FACT_REVIEWS = ("contradiction", "graphiti_invalidation", "policy_violation")


class InvalidCorrection(Conflict):
    code, default_message = "invalid_correction", "This correction does not apply to the target in its current state"


class MemoryDependents(Protocol):
    """What the memory package offers the correction service: the memories that rest on a fact, and the two propagation steps."""

    async def visible(self, conn: AsyncConnection, principal: Principal, fact_ids: list[uuid.UUID]) -> list[DependentMemory]:
        """Memories with one of these facts as evidence that THE CALLER may see (counts and ids shown to a reviewer never exceed their own view)."""
        ...

    async def flag(self, cc: CommandContext, fact_ids: list[uuid.UUID], *, correction_id: uuid.UUID) -> list[uuid.UUID]:
        """Shared memories resting on these facts become `disputed` with a `memory_conflict` item (`source_fact_rejected`). Returns the memory ids."""
        ...

    async def unflag(self, cc: CommandContext, fact_ids: list[uuid.UUID], *, correction_id: uuid.UUID) -> list[uuid.UUID]:
        """The reverse, after a restore: disputed-because-of-this memories are active again and their items are closed."""
        ...

    async def about_entity(self, conn: AsyncConnection, principal: Principal, entity_id: uuid.UUID) -> list[DependentMemory]: ...


@dataclass
class Applied:
    out: CorrectionOut
    replayed: bool = False


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _authority(principal: Principal) -> str:
    return "admin_asserted" if Role.WORKSPACE_ADMIN in principal.roles else "user_asserted"


class Corrections:
    def __init__(self, specs: SpecCache, extracted: ExtractedKnowledge, *, hmac_secret: bytes, trace_retention_days: int = 30):
        self._specs, self._extracted = specs, extracted
        self._secret, self._trace_days = hmac_secret, trace_retention_days
        self._memory: MemoryDependents | None = None

    def register_memory(self, port: MemoryDependents) -> None:
        self._memory = port

    # ---- plumbing -----------------------------------------------------------------------------------------------------------------------

    def _value_hmac(self, ws: uuid.UUID, entity_id: uuid.UUID, attribute: str, value: Any) -> str:
        key = hmac.new(self._secret, ws.bytes, hashlib.sha256).digest()
        return hmac.new(
            key, f"{entity_id}|{attribute}|{json.dumps(value, sort_keys=True, default=str)}".encode(), hashlib.sha256
        ).hexdigest()

    @staticmethod
    def _payload(target_kind: str, target_id: uuid.UUID, body: Any, revision: int | None) -> dict[str, Any]:
        d = body.model_dump(mode="json")
        reason_text = d.pop("reason_text", None)  # the text lives on the correction row; the ledger keeps its hash
        d["reason_text_sha256"] = _sha(reason_text) if reason_text else None
        if (
            "value" in d and d["value"] is not None
        ):  # an attribute value may be sensitive: the ledger keeps a hash (the value itself is on the entity)
            d["value"] = {"sha256": _sha(d["value"])}
        return {"target_kind": target_kind, "target_id": str(target_id), "expected_revision": revision, "correction": d}

    async def _replayed(self, cc: CommandContext, key: str, command_type: CommandType, digest: str) -> Applied | None:
        ws = cc.ctx.workspace_id
        await lock_key(cc.conn, ws, "cmd", key)
        prior = (
            await cc.conn.execute(text("SELECT * FROM commands WHERE workspace_id = :ws AND idempotency_key = :k"), {"ws": ws, "k": key})
        ).first()
        if prior is None:
            return None
        if prior.request_hash != digest or prior.command_type != command_type.value:
            from kg_common.errors import IdempotencyMismatch

            raise IdempotencyMismatch()
        r = prior.result or {}
        reviews = [
            x.id
            for x in (
                await cc.conn.execute(
                    text(
                        "SELECT id FROM review_items WHERE workspace_id = :ws AND detail ->> 'correction_id' = :c ORDER BY created_at, id"
                    ),
                    {"ws": ws, "c": r["correction_id"]},
                )
            ).all()
        ]
        out = CorrectionOut(
            correction_id=r["correction_id"],
            command_id=prior.id,
            kind=r["kind"],
            target_kind=r["target_kind"],
            target_id=r["target_id"],
            new_target_id=r.get("new_target_id"),
            revision=r["revision"],
            projection_status=prior.projection_status,
            dependent_memory_ids=r.get("dependent_memory_ids", []),
            review_item_ids=reviews,
            reopened_fact_ids=r.get("reopened_fact_ids", []),
            authoritative_override=bool(r.get("authoritative_override", False)),
            grant_holders=r.get("grant_holders", []),
            moved=r.get("moved", {}),
            status_url=f"/v1/commands/{prior.id}",
        )
        return Applied(out, replayed=True)

    async def _record(
        self,
        cc: CommandContext,
        *,
        key: str,
        command_type: CommandType,
        command_id: uuid.UUID,
        correction_id: uuid.UUID,
        kind: str,
        target_kind: str,
        target_id: uuid.UUID,
        new_target_id: uuid.UUID | None,
        body: Any,
        payload: dict[str, Any],
        version_id: uuid.UUID | None,
        result: dict[str, Any],
    ) -> None:
        """The ledger row first (rows the command creates reference it), then the correction row."""
        ws = cc.ctx.workspace_id
        await ledger_command(
            cc.conn,
            ws,
            command_type=command_type,
            key=key,
            actor_principal_id=cc.principal.principal_id,
            ontology_version_id=version_id,
            payload=payload,
            result={"correction_id": str(correction_id), "kind": kind, "target_kind": target_kind, "target_id": str(target_id), **result},
            authority=_authority(cc.principal),
            source_system="correction",
            source_ref=f"{target_kind}:{target_id}",
            command_id=command_id,
        )
        await cc.conn.execute(
            text(
                "INSERT INTO corrections (id, workspace_id, command_id, target_kind, target_id, kind, reason_code, reason_text, new_target_id, created_by) "
                "VALUES (:id, :ws, :c, :tk, :t, :k, :rc, :rt, :nt, :by)"
            ),
            {
                "id": correction_id,
                "ws": ws,
                "c": command_id,
                "tk": target_kind,
                "t": target_id,
                "k": kind,
                "rc": body.reason_code,
                "rt": body.reason_text,
                "nt": new_target_id,
                "by": cc.principal.principal_id,
            },
        )

    async def _conclude(
        self,
        cc: CommandContext,
        *,
        kind: str,
        command_type: CommandType,
        command_id: uuid.UUID,
        reason_code: str,
        target_kind: str,
        target_id: uuid.UUID,
        version_id: uuid.UUID | None,
        entity_id: uuid.UUID | None = None,
        fact_id: uuid.UUID | None = None,
        memory_count: int = 0,
        override: bool = False,
    ) -> None:
        ws = cc.ctx.workspace_id
        await cc.audit.record(
            "correction.applied",
            resource_type=target_kind,
            resource_id=target_id,
            metadata=AuditMetadata(
                correction_kind=kind,
                command_type=command_type,
                reason_code=reason_code,
                target_id=target_id,
                entity_id=entity_id,
                fact_id=fact_id,
                count_memories=memory_count or None,
            ),
        )
        if override:
            await cc.audit.record(
                "correction.authoritative_override",
                resource_type="fact",
                resource_id=fact_id,
                metadata=AuditMetadata(
                    correction_kind=kind, fact_id=fact_id, override_authority="authoritative_system", reason_code=reason_code
                ),
            )
        await enqueue(
            cc.conn,
            workspace_id=ws,
            stage="graph_projection",
            subject_type="command",
            subject_id=command_id,
            dedupe_key=f"command:{command_id}:graph_projection:g0",
            payload={"command_id": str(command_id), "generation": 0},
            pipeline_version=PIPELINE_VERSION,
            ontology_version_id=version_id,
            concurrency_key=f"graph:{ws}",
        )
        metrics.corrections_total.labels(kind=kind).inc()

    async def _override_item(
        self, cc: CommandContext, fact_id: uuid.UUID, correction_id: uuid.UUID, reason_code: str, new_fact_id: uuid.UUID | None
    ) -> list[uuid.UUID]:
        """A notice for the source system's owner: an authoritative fact was displaced by a person."""
        item = uuid7()
        row = (
            await cc.conn.execute(
                text(
                    "INSERT INTO review_items (id, workspace_id, item_type, rule_kind, reason_code, fact_id, detail, due_at) "
                    "VALUES (:id, :ws, 'authoritative_override', 'correction', :rc, :f, CAST(:d AS jsonb), now() + interval '30 days') ON CONFLICT DO NOTHING RETURNING id"
                ),
                {
                    "id": item,
                    "ws": cc.ctx.workspace_id,
                    "rc": reason_code,
                    "f": fact_id,
                    "d": json.dumps({"correction_id": str(correction_id), "new_fact_id": str(new_fact_id) if new_fact_id else None}),
                },
            )
        ).first()
        if row is None:
            return []
        await cc.audit.record(
            "review.created",
            resource_type="review_item",
            resource_id=item,
            metadata=AuditMetadata(review_id=item, review_type="authoritative_override", reason_code=reason_code, fact_id=fact_id),
        )
        metrics.reviews_created_total.labels(item_type="authoritative_override").inc()
        return [item]

    # ---- loading ------------------------------------------------------------------------------------------------------------------------

    async def _fact(self, conn: AsyncConnection, ws: uuid.UUID, fact_id: uuid.UUID, subjects: SubjectFilter, *, lock: bool) -> Row[Any]:
        """The fact with its endpoints, visible to the caller (a hidden or frozen one is the same 404 as a missing one). Locks the source entity first, like every other
        fact writer, so a correction and a command on the same source cannot deadlock."""
        acl, params = fact_visible_sql(subjects, source="s", target="t")
        sql = (
            "SELECT f.*, s.type_key AS s_type, s.display_name AS s_name, s.classification AS s_cls, "  # noqa: S608
            "t.type_key AS t_type, t.display_name AS t_name, t.classification AS t_cls FROM facts f "
            "JOIN entities s ON s.workspace_id = f.workspace_id AND s.id = f.source_entity_id "
            "JOIN entities t ON t.workspace_id = f.workspace_id AND t.id = f.target_entity_id "
            f"WHERE f.workspace_id = :ws AND f.id = :id AND f.status NOT IN ('deleted','deletion_pending') AND {acl}"  # noqa: S608 (constant fragments)
        )
        probe = (await conn.execute(text(sql), {"ws": ws, "id": fact_id, **params})).first()
        if probe is None:
            raise NotFound()
        if not lock:
            return probe
        await conn.execute(
            text("SELECT 1 FROM entities WHERE workspace_id = :ws AND id = :id FOR UPDATE"), {"ws": ws, "id": probe.source_entity_id}
        )
        await conn.execute(text("SELECT 1 FROM facts WHERE workspace_id = :ws AND id = :id FOR UPDATE"), {"ws": ws, "id": fact_id})
        row = (await conn.execute(text(sql), {"ws": ws, "id": fact_id, **params})).first()
        if row is None:
            raise NotFound()
        return row

    async def _entity(self, conn: AsyncConnection, ws: uuid.UUID, entity_id: uuid.UUID, subjects: SubjectFilter, *, lock: bool) -> Row[Any]:
        acl, params = entity_visible_sql(subjects, alias="e")
        sql = f"SELECT e.* FROM entities e WHERE e.workspace_id = :ws AND e.id = :id AND e.status NOT IN ('deleted','deletion_pending') AND {acl}{' FOR UPDATE OF e' if lock else ''}"  # noqa: S608
        row = (await conn.execute(text(sql), {"ws": ws, "id": entity_id, **params})).first()
        if row is None:
            raise NotFound()
        return row

    async def _resolve(self, conn: AsyncConnection, ws: uuid.UUID, ref: EntityRefIn, subjects: SubjectFilter, field: str) -> Row[Any]:
        acl, params = entity_visible_sql(subjects, alias="e")
        if ref.entity_id is not None:
            sql = f"SELECT e.id, e.type_key, e.display_name, e.classification FROM entities e WHERE e.workspace_id = :ws AND e.id = :id AND e.status = 'active' AND {acl}"  # noqa: S608
            p: dict[str, Any] = {"ws": ws, "id": ref.entity_id, **params}
        else:
            assert ref.external_id is not None
            sql = (  # noqa: S608
                "SELECT e.id, e.type_key, e.display_name, e.classification FROM entity_external_ids x JOIN entities e ON e.workspace_id = x.workspace_id AND e.id = x.entity_id "  # noqa: S608
                f"WHERE x.workspace_id = :ws AND x.type_key = :t AND x.system = :s AND x.value = :v AND e.status = 'active' AND {acl}"  # noqa: S608
            )
            p = {"ws": ws, "t": ref.type, "s": ref.external_id.system, "v": ref.external_id.value, **params}
        row = (await conn.execute(text(sql), p)).first()
        if row is None:
            raise UnknownEntity(details={"endpoint": field})
        return row

    @staticmethod
    def _revision(current: int, if_match: int | None) -> None:
        if if_match is None:
            raise PreconditionRequired()
        if if_match != current:
            raise PreconditionFailed()

    # ---- facts ------------------------------------------------------------------------------------------------------------------------------

    async def fact(
        self, cc: CommandContext, key: str, fact_id: uuid.UUID, body: FactCorrectionIn, if_match: int | None, subjects: SubjectFilter
    ) -> Applied:
        command_type = {
            "reject": CommandType.CORRECTION_FACT_REJECT,
            "supersede": CommandType.CORRECTION_FACT_SUPERSEDE,
            "end": CommandType.CORRECTION_FACT_END,
            "restore": CommandType.CORRECTION_FACT_RESTORE,
        }[body.kind]
        payload = self._payload("fact", fact_id, body, if_match)
        digest = request_hash(command_type.value, payload)
        if (replay := await self._replayed(cc, key, command_type, digest)) is not None:
            return replay
        conn, ws = cc.conn, cc.ctx.workspace_id
        fact = await self._fact(conn, ws, fact_id, subjects, lock=True)
        self._revision(fact.revision, if_match)
        active = await self._specs.active(conn, ws)
        version_id = active[0] if active else fact.ontology_version_id
        command_id, correction_id = uuid7(), uuid7()
        authoritative = fact.authority_class == "authoritative_system" and body.kind in ("reject", "supersede", "end")
        new_fact_id: uuid.UUID | None = None
        reopened: list[uuid.UUID] = []
        flagged: list[uuid.UUID] = []
        if body.kind == "reject":
            if fact.status not in REVERSIBLE_FACT:
                raise InvalidCorrection()
            reopened = await self._reopenable(conn, ws, fact) if body.reopen_predecessors else []
        elif body.kind == "supersede":
            if fact.status not in ("active", "proposed", "disputed"):
                raise InvalidCorrection()
            if active is None:
                raise NoActiveOntology()
            new_fact_id = uuid7()
        elif body.kind == "end":
            if fact.status != "active":
                raise InvalidCorrection()
            if fact.valid_from is not None and body.valid_to is not None and body.valid_to < fact.valid_from:
                raise ValidationFailed("valid_to is before the fact began", details={"fields": ["valid_to"]})
        else:  # restore
            if fact.status not in ("rejected", "disputed"):
                raise InvalidCorrection()
        dependents: list[uuid.UUID] = []
        if body.kind in ("reject", "supersede"):
            dependents = await self._dependent_ids(conn, ws, [fact_id])
        replacement: dict[str, Any] | None = None
        if body.kind == "supersede":
            assert active is not None and new_fact_id is not None
            replacement = await self._replacement(conn, ws, active[1], fact, body, subjects)
        result: dict[str, Any] = {
            "entity_ids": [str(fact.source_entity_id), str(fact.target_entity_id)]
            + ([str(replacement["source"].id), str(replacement["target"].id)] if replacement else []),
            "fact_ids": [str(fact_id)] + ([str(new_fact_id)] if new_fact_id else []) + [str(x) for x in reopened],
            "new_target_id": str(new_fact_id) if new_fact_id else None,
            "revision": fact.revision + 1,
            "dependent_memory_ids": [str(x) for x in dependents],
            "reopened_fact_ids": [str(x) for x in reopened],
            "authoritative_override": authoritative,
        }
        await self._record(
            cc,
            key=key,
            command_type=command_type,
            command_id=command_id,
            correction_id=correction_id,
            kind=f"fact.{body.kind}",
            target_kind="fact",
            target_id=fact_id,
            new_target_id=new_fact_id,
            body=body,
            payload=payload,
            version_id=version_id,
            result=result,
        )
        now = _NOW(dt.UTC)
        if body.kind == "reject":
            await touch(
                conn,
                ws,
                "facts",
                fact_id,
                status="rejected",
                decision_code="rejected_by_correction",
                decided_at=now,
                last_command_id=command_id,
            )
            for pid in reopened:
                await touch(conn, ws, "facts", pid, valid_to=None, succeeded_by_fact_id=None, last_command_id=command_id)
        elif body.kind == "supersede":
            assert replacement is not None and new_fact_id is not None and active is not None
            await self._insert_replacement(cc, new_fact_id, command_id, active[0], fact, replacement)
            await touch(
                conn,
                ws,
                "facts",
                fact_id,
                status="superseded",
                superseded_at=now,
                succeeded_by_fact_id=new_fact_id,
                decision_code="superseded_by_correction",
                last_command_id=command_id,
            )
        elif body.kind == "end":
            await touch(conn, ws, "facts", fact_id, valid_to=body.valid_to, last_command_id=command_id)
        else:
            await touch(
                conn,
                ws,
                "facts",
                fact_id,
                status="active",
                decision_code="restored_by_correction",
                decided_at=None,
                last_command_id=command_id,
            )
        if body.kind in ("reject", "supersede"):
            await self._close_fact_reviews(cc, fact_id)
        if self._memory is not None:
            if body.kind in ("reject", "supersede"):
                flagged = await self._memory.flag(cc, [fact_id], correction_id=correction_id)
            elif body.kind == "restore":
                flagged = await self._memory.unflag(cc, [fact_id], correction_id=correction_id)
        items = await self._override_item(cc, fact_id, correction_id, body.reason_code, new_fact_id) if authoritative else []
        await self._conclude(
            cc,
            kind=f"fact.{body.kind}",
            command_type=command_type,
            command_id=command_id,
            reason_code=body.reason_code,
            target_kind="fact",
            target_id=fact_id,
            version_id=version_id,
            fact_id=fact_id,
            memory_count=len(flagged),
            override=authoritative,
        )
        out = CorrectionOut(
            correction_id=correction_id,
            command_id=command_id,
            kind=f"fact.{body.kind}",
            target_kind="fact",
            target_id=fact_id,
            new_target_id=new_fact_id,
            revision=fact.revision + 1,
            dependent_memory_ids=flagged or dependents,
            review_item_ids=items,
            reopened_fact_ids=reopened,
            authoritative_override=authoritative,
            status_url=f"/v1/commands/{command_id}",
        )
        return Applied(out)

    async def _reopenable(self, conn: AsyncConnection, ws: uuid.UUID, fact: Row[Any]) -> list[uuid.UUID]:
        """Facts this one ended by succession (ADR-023): the predecessor's valid_to was set to this fact's valid_from. Rejecting the successor makes them current again."""
        if fact.valid_from is None:
            return []
        rows = (
            await conn.execute(
                text(
                    "SELECT id FROM facts WHERE workspace_id = :ws AND succeeded_by_fact_id = :f AND status = 'active' AND valid_to IS NOT NULL AND valid_to = :vf "
                    "ORDER BY recorded_at, id FOR UPDATE"
                ),
                {"ws": ws, "f": fact.id, "vf": fact.valid_from},
            )
        ).all()
        return [r.id for r in rows]

    async def _dependent_ids(self, conn: AsyncConnection, ws: uuid.UUID, fact_ids: list[uuid.UUID]) -> list[uuid.UUID]:
        rows = (
            await conn.execute(
                text(
                    "SELECT DISTINCT m.id FROM memory_evidence e JOIN curated_memories m ON m.workspace_id = e.workspace_id AND m.id = e.memory_id "
                    "WHERE e.workspace_id = :ws AND e.fact_id = ANY(:ids) AND m.status IN ('active','proposed','disputed') "
                    "AND EXISTS (SELECT 1 FROM memory_scopes s WHERE s.workspace_id = m.workspace_id AND s.memory_id = m.id AND s.scope_kind IN ('workspace','subject','project')) "
                    "ORDER BY m.id"
                ),
                {"ws": ws, "ids": fact_ids},
            )
        ).all()
        return [r.id for r in rows]

    async def _close_fact_reviews(self, cc: CommandContext, fact_id: uuid.UUID) -> None:
        await cc.conn.execute(
            text(
                "UPDATE review_items SET status = 'resolved', resolved_by = :by, resolved_at = now(), resolution_code = 'corrected', revision = revision + 1 "
                "WHERE workspace_id = :ws AND status = 'open' AND item_type = ANY(:types) AND (fact_id = :f OR competing_fact_id = :f)"
            ),
            {"ws": cc.ctx.workspace_id, "by": cc.principal.principal_id, "types": list(OPEN_FACT_REVIEWS), "f": fact_id},
        )

    async def _replacement(
        self, conn: AsyncConnection, ws: uuid.UUID, spec: Any, old: Row[Any], body: FactCorrectionIn, subjects: SubjectFilter
    ) -> dict[str, Any]:
        """The replacement fact, validated against the ACTIVE ontology (types, endpoints, attributes, temporal order). The authority and succession rules are not applied:
        a correction is a deliberate override (the rule context holds no other facts, and the evaluation runs at the highest authority)."""
        source = await self._resolve(conn, ws, body.source, subjects, "source") if body.source else None
        target = await self._resolve(conn, ws, body.target, subjects, "target") if body.target else None
        src = source or SimpleNamespace(id=old.source_entity_id, type_key=old.s_type, display_name=old.s_name, classification=old.s_cls)
        tgt = target or SimpleNamespace(id=old.target_entity_id, type_key=old.t_type, display_name=old.t_name, classification=old.t_cls)
        attributes = dict(old.attributes or {}) if body.attributes is None else body.attributes
        valid_from = body.valid_from if body.valid_from is not None else old.valid_from
        valid_to = body.valid_to if body.valid_to is not None else old.valid_to
        cmd = RuleCommand(
            kind="relationship.assert",
            rel_type=old.rel_type,
            source=EntityRef(str(src.id), src.type_key, src.display_name, src.classification),
            target=EntityRef(str(tgt.id), tgt.type_key, tgt.display_name, tgt.classification),
            attributes=attributes,
            valid_from=valid_from,
            valid_to=valid_to,
            valid_from_unknown=bool(old.valid_from_unknown) and valid_from is None,
            classification=old.classification,
            authority="authoritative_system",
            source_system="correction",
            source_ref=f"fact:{old.id}",  # a correction cites what it corrects (require_source)
        )
        ev = evaluate(spec, cmd, RuleContext())
        if ev.outcome == "reject":
            raise RuleViolation(
                details={
                    "violations": [
                        {"rule_kind": v.rule_kind, "rule_id": v.rule_id, "code": v.code, "path": v.path}
                        for v in ev.violations
                        if v.action == "reject"
                    ]
                }
            )
        return {
            "source": src,
            "target": tgt,
            "attributes": ev.attributes,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "valid_from_unknown": cmd.valid_from_unknown,
        }

    async def _insert_replacement(
        self, cc: CommandContext, new_id: uuid.UUID, command_id: uuid.UUID, version_id: uuid.UUID, old: Row[Any], r: dict[str, Any]
    ) -> None:
        ws = cc.ctx.workspace_id
        await cc.conn.execute(
            text(
                "INSERT INTO facts (id, workspace_id, rel_type, source_entity_id, target_entity_id, attributes, valid_from, valid_from_unknown, valid_to, source_recorded_at, "
                "authority_class, classification, source_type, source_command_id, last_command_id, source_system, source_ref, ontology_version_id, evidence_status) "
                "VALUES (:id, :ws, :rel, :s, :t, CAST(:attrs AS jsonb), :vf, :vfu, :vt, NULL, 'admin_asserted', :cls, 'correction', :c, :c, 'correction', :ref, :ver, 'verified')"
            ),
            {
                "id": new_id,
                "ws": ws,
                "rel": old.rel_type,
                "s": r["source"].id,
                "t": r["target"].id,
                "attrs": json.dumps(r["attributes"], default=str),
                "vf": r["valid_from"],
                "vfu": r["valid_from_unknown"],
                "vt": r["valid_to"],
                "cls": old.classification,
                "c": command_id,
                "ref": f"fact:{old.id}",
                "ver": version_id,
            },
        )
        await cc.conn.execute(
            text("INSERT INTO fact_evidence (id, fact_id, workspace_id, source_kind, command_id) VALUES (:id, :f, :ws, 'command', :c)"),
            {"id": uuid7(), "f": new_id, "ws": ws, "c": command_id},
        )

    # ---- entities ---------------------------------------------------------------------------------------------------------------------

    async def entity(
        self, cc: CommandContext, key: str, entity_id: uuid.UUID, body: EntityCorrectionIn, if_match: int | None, subjects: SubjectFilter
    ) -> Applied:
        if body.kind == "split":
            return await self._split(cc, key, entity_id, body, if_match, subjects)
        command_type = CommandType.CORRECTION_ATTRIBUTE if body.kind == "correct_attribute" else CommandType.CORRECTION_RENAME
        payload = self._payload("entity", entity_id, body, if_match)
        digest = request_hash(command_type.value, payload)
        if (replay := await self._replayed(cc, key, command_type, digest)) is not None:
            return replay
        conn, ws = cc.conn, cc.ctx.workspace_id
        e = await self._entity(conn, ws, entity_id, subjects, lock=True)
        if e.status not in ("active", "candidate", "untyped"):
            raise InvalidCorrection()
        self._revision(e.revision, if_match)
        spec = await self._specs.get(conn, ws, e.ontology_version_id)
        active = await self._specs.active(conn, ws)
        if active is not None:
            spec = active[1] if e.type_key in active[1].entity_types else spec
        version_id = active[0] if active else e.ontology_version_id
        command_id, correction_id = uuid7(), uuid7()
        attrs = dict(e.attributes or {})
        name, aliases = e.display_name, list(e.aliases)
        old_value: Any = None
        if body.kind == "correct_attribute":
            assert body.attribute is not None
            old_value = attrs.get(body.attribute)
            cmd = RuleCommand(
                kind="entity.update",
                entity_type=e.type_key,
                attributes={body.attribute: body.value},
                classification=e.classification,
                authority="authoritative_system",  # a correction is a deliberate override of the authority gates, not of the schema
                source_system="correction",
                source_ref=f"entity:{entity_id}",
                entity=EntityRef(str(e.id), e.type_key, e.display_name, e.classification),
            )
            ev = evaluate(spec, cmd, RuleContext(current_attributes=attrs))
            if ev.outcome == "reject":
                raise RuleViolation(
                    details={
                        "violations": [
                            {"rule_kind": v.rule_kind, "rule_id": v.rule_id, "code": v.code, "path": v.path}
                            for v in ev.violations
                            if v.action == "reject"
                        ]
                    }
                )
            for k, v in ev.attributes.items():
                if v is None:
                    attrs.pop(k, None)
                else:
                    attrs[k] = v
        else:
            assert body.name is not None
            if body.name == e.display_name:
                raise InvalidCorrection()
            if e.display_name.lower() not in {a.lower() for a in aliases} and e.display_name.lower() != body.name.lower():
                aliases.append(e.display_name)  # the old name keeps resolving (KGR-01)
            name = body.name
        touched_facts: list[uuid.UUID] = []
        if body.kind == "rename":  # fact text embeds the endpoint names: these edges are restored too
            touched_facts = [
                r.id
                for r in (
                    await conn.execute(
                        text(
                            "SELECT id FROM facts WHERE workspace_id = :ws AND (source_entity_id = :e OR target_entity_id = :e) AND status NOT IN ('deleted','deletion_pending')"
                        ),
                        {"ws": ws, "e": entity_id},
                    )
                ).all()
            ]
        result = {"entity_ids": [str(entity_id)], "fact_ids": [str(f) for f in touched_facts], "revision": e.revision + 1}
        await self._record(
            cc,
            key=key,
            command_type=command_type,
            command_id=command_id,
            correction_id=correction_id,
            kind=f"entity.{body.kind}",
            target_kind="entity",
            target_id=entity_id,
            new_target_id=None,
            body=body,
            payload=payload,
            version_id=version_id,
            result=result,
        )
        await conn.execute(
            text(
                "UPDATE entities SET display_name = :name, aliases = :aliases, attributes = CAST(:attrs AS jsonb), revision = revision + 1, ontology_version_id = :ver, "
                "last_command_id = :c, updated_at = now(), projection_status = 'pending', projection_attempts = 0, projection_next_at = now(), last_error_code = NULL "
                "WHERE workspace_id = :ws AND id = :id"
            ),
            {
                "name": name,
                "aliases": sorted(set(aliases)),
                "attrs": json.dumps(attrs, default=str),
                "ver": e.ontology_version_id,
                "c": command_id,
                "ws": ws,
                "id": entity_id,
            },
        )
        if body.kind == "correct_attribute":
            assert body.attribute is not None
            await conn.execute(
                text(
                    "INSERT INTO attribute_corrections (id, workspace_id, correction_id, entity_id, attribute_key, entity_revision, old_value_hmac, new_value_hmac) "
                    "VALUES (:id, :ws, :cid, :e, :a, :rev, :o, :n)"
                ),
                {
                    "id": uuid7(),
                    "ws": ws,
                    "cid": correction_id,
                    "e": entity_id,
                    "a": body.attribute,
                    "rev": e.revision + 1,
                    "o": self._value_hmac(ws, entity_id, body.attribute, old_value) if old_value is not None else None,
                    "n": self._value_hmac(ws, entity_id, body.attribute, body.value) if body.value is not None else None,
                },
            )
        else:  # fact text embeds the endpoint names: refresh the facts that mention this entity (as entity.update does)
            await conn.execute(
                text(
                    "UPDATE facts SET projection_status = 'pending', projection_attempts = 0, projection_next_at = now(), last_error_code = NULL "
                    "WHERE workspace_id = :ws AND (source_entity_id = :e OR target_entity_id = :e) AND status = 'active' AND projection_status <> 'skipped_by_policy'"
                ),
                {"ws": ws, "e": entity_id},
            )
        holders = await self._grant_holders(conn, ws, entity_id) if body.kind == "rename" else []
        await self._conclude(
            cc,
            kind=f"entity.{body.kind}",
            command_type=command_type,
            command_id=command_id,
            reason_code=body.reason_code,
            target_kind="entity",
            target_id=entity_id,
            version_id=version_id,
            entity_id=entity_id,
        )
        return Applied(
            CorrectionOut(
                correction_id=correction_id,
                command_id=command_id,
                kind=f"entity.{body.kind}",
                target_kind="entity",
                target_id=entity_id,
                revision=e.revision + 1,
                grant_holders=holders,
                status_url=f"/v1/commands/{command_id}",
            )
        )

    async def _grant_holders(self, conn: AsyncConnection, ws: uuid.UUID, entity_id: uuid.UUID) -> list[uuid.UUID]:
        rows = (
            await conn.execute(
                text(
                    "SELECT DISTINCT principal_id FROM principal_scopes WHERE workspace_id = :ws AND scope_kind = 'subject' AND scope_ref = :e ORDER BY principal_id"
                ),
                {"ws": ws, "e": str(entity_id)},
            )
        ).all()
        return [r.principal_id for r in rows]

    # ---- identity split (ADR-028 compensation) ----------------------------------------------------------------------------------------

    async def _split(
        self, cc: CommandContext, key: str, entity_id: uuid.UUID, body: EntityCorrectionIn, if_match: int | None, subjects: SubjectFilter
    ) -> Applied:
        command_type = CommandType.ENTITY_SPLIT
        payload = self._payload("entity", entity_id, body, if_match)
        digest = request_hash(command_type.value, payload)
        if (replay := await self._replayed(cc, key, command_type, digest)) is not None:
            return replay
        assert body.new_entity is not None and body.move is not None
        conn, ws = cc.conn, cc.ctx.workspace_id
        orig = await self._entity(conn, ws, entity_id, subjects, lock=True)
        if orig.status != "active":
            raise InvalidCorrection()
        self._revision(orig.revision, if_match)
        active = await self._specs.active(conn, ws)
        if active is None:
            raise NoActiveOntology()
        version_id, spec = active
        new_type = body.new_entity.type or orig.type_key
        if new_type not in spec.entity_types:
            raise ValidationFailed("the split-off entity's type is not in the active ontology", details={"fields": ["new_entity.type"]})
        move = body.move
        mention_ids = list(dict.fromkeys(move.mention_ids))
        fact_ids = list(dict.fromkeys(move.fact_ids))
        memory_ids = list(dict.fromkeys(move.memory_ids))
        mentions = (
            await conn.execute(
                text(
                    "SELECT id, interaction_id, episode_uuid, node_uuid FROM entity_mentions WHERE workspace_id = :ws AND entity_id = :e AND id = ANY(:ids) ORDER BY id FOR UPDATE"
                ),
                {"ws": ws, "e": entity_id, "ids": mention_ids},
            )
        ).all()
        facts = (
            await conn.execute(
                text(
                    "SELECT id, source_entity_id, target_entity_id FROM facts WHERE workspace_id = :ws AND id = ANY(:ids) AND (source_entity_id = :e OR target_entity_id = :e) "
                    "AND status NOT IN ('deleted','deletion_pending') ORDER BY id FOR UPDATE"
                ),
                {"ws": ws, "e": entity_id, "ids": fact_ids},
            )
        ).all()
        memories = (
            await conn.execute(
                text("SELECT memory_id FROM memory_subjects WHERE workspace_id = :ws AND entity_id = :e AND memory_id = ANY(:ids)"),
                {"ws": ws, "e": entity_id, "ids": memory_ids},
            )
        ).all()
        ext = (
            await conn.execute(
                text("SELECT type_key, system, value FROM entity_external_ids WHERE workspace_id = :ws AND entity_id = :e"),
                {"ws": ws, "e": entity_id},
            )
        ).all()
        wanted_ext = {(x.system, x.value) for x in move.external_ids}
        moved_ext = [x for x in ext if (x.system, x.value) in wanted_ext]
        if (
            len(mentions) != len(mention_ids)
            or len(facts) != len(fact_ids)
            or len(memories) != len(memory_ids)
            or len(moved_ext) != len(wanted_ext)
        ):
            raise ValidationFailed("everything moved to the new entity must belong to the original entity", details={"fields": ["move"]})
        if not (mention_ids or fact_ids or memory_ids or wanted_ext):
            raise ValidationFailed("a split moves something", details={"fields": ["move"]})
        new_id, command_id, correction_id = uuid7(), uuid7(), uuid7()
        holders = await self._grant_holders(conn, ws, entity_id)
        episodes = [{"episode_uuid": m.episode_uuid, "interaction_id": str(m.interaction_id)} for m in mentions]
        interactions = sorted({m.interaction_id for m in mentions}, key=str)
        result: dict[str, Any] = {
            "entity_ids": [str(entity_id), str(new_id)],
            "fact_ids": [str(f.id) for f in facts],
            "new_target_id": str(new_id),
            "revision": orig.revision + 1,
            "grant_holders": [str(h) for h in holders],
            "moved": {"mentions": len(mentions), "facts": len(facts), "memories": len(memories), "external_ids": len(moved_ext)},
            "split": {
                "original": str(entity_id),
                "new": str(new_id),
                "mentions": episodes,
                "interaction_ids": [str(i) for i in interactions],
            },
        }
        await self._record(
            cc,
            key=key,
            command_type=command_type,
            command_id=command_id,
            correction_id=correction_id,
            kind="entity.split",
            target_kind="entity",
            target_id=entity_id,
            new_target_id=new_id,
            body=body,
            payload=payload,
            version_id=version_id,
            result=result,
        )
        # 1. the split-off entity
        taken = {a.strip().lower() for a in body.new_entity.aliases if a.strip()}
        keep_aliases = [a for a in orig.aliases if a.lower() not in taken]  # a name that belonged to the other person leaves this one
        await conn.execute(
            text(
                "INSERT INTO entities (id, workspace_id, type_key, display_name, aliases, attributes, classification, origin, ontology_version_id, created_by_command_id, last_command_id) "
                "VALUES (:id, :ws, :t, :name, :aliases, '{}'::jsonb, :cls, 'correction', :ver, :c, :c)"
            ),
            {
                "id": new_id,
                "ws": ws,
                "t": new_type,
                "name": body.new_entity.display_name.strip(),
                "aliases": sorted(
                    {
                        a.strip()
                        for a in body.new_entity.aliases
                        if a.strip() and a.strip().lower() != body.new_entity.display_name.strip().lower()
                    }
                ),
                "cls": orig.classification,
                "ver": version_id,
                "c": command_id,
            },
        )
        # 2. lineage and the pair that must not be suggested again (the memory re-point triggers read the lineage)
        await conn.execute(
            text(
                "INSERT INTO entity_lineage (workspace_id, entity_id, related_id, relation, command_id) VALUES (:ws, :n, :o, 'split_from', :c)"
            ),
            {"ws": ws, "n": new_id, "o": entity_id, "c": command_id},
        )
        a, b = sorted((entity_id, new_id), key=str)
        await conn.execute(
            text(
                "INSERT INTO identity_non_matches (entity_a, entity_b, workspace_id, decided_by, reason_code) VALUES (:a, :b, :ws, :by, :rc) ON CONFLICT DO NOTHING"
            ),
            {"a": a, "b": b, "ws": ws, "by": cc.principal.principal_id, "rc": "wrong_identity"},
        )
        # 3. mentions (and the attribute claims of the same interactions), facts, memories, strong identifiers
        if mentions:
            await conn.execute(
                # the mention now names the split-off node; the projector moves the episode's MENTIONS edge from the original's node (restore_command)
                text(
                    "UPDATE entity_mentions SET entity_id = :n, node_uuid = CAST(:n AS text), resolution = 'reviewer', status = 'accepted' "
                    "WHERE workspace_id = :ws AND id = ANY(:ids)"
                ),
                {"n": new_id, "ws": ws, "ids": [m.id for m in mentions]},
            )
            await conn.execute(
                text(
                    "UPDATE attribute_claims SET entity_id = :n WHERE workspace_id = :ws AND entity_id = :o AND interaction_id = ANY(:ints)"
                ),
                {"n": new_id, "o": entity_id, "ws": ws, "ints": interactions},
            )
        for f in facts:
            await touch(
                conn,
                ws,
                "facts",
                f.id,
                source_entity_id=new_id if f.source_entity_id == entity_id else f.source_entity_id,
                target_entity_id=new_id if f.target_entity_id == entity_id else f.target_entity_id,
                last_command_id=command_id,
            )
        if memory_ids:
            await conn.execute(
                text(
                    "UPDATE memory_subjects s SET entity_id = :n WHERE s.workspace_id = :ws AND s.entity_id = :o AND s.memory_id = ANY(:ids) "
                    "AND NOT EXISTS (SELECT 1 FROM memory_subjects x WHERE x.workspace_id = s.workspace_id AND x.memory_id = s.memory_id AND x.entity_id = :n)"
                ),
                {"n": new_id, "o": entity_id, "ws": ws, "ids": memory_ids},
            )
            await conn.execute(
                text(
                    "UPDATE memory_scopes s SET scope_ref = :ns WHERE s.workspace_id = :ws AND s.scope_kind = 'subject' AND s.scope_ref = :os AND s.memory_id = ANY(:ids) "
                    "AND NOT EXISTS (SELECT 1 FROM memory_scopes x WHERE x.workspace_id = s.workspace_id AND x.memory_id = s.memory_id AND x.scope_kind = 'subject' AND x.scope_ref = :ns)"
                ),
                {"ns": str(new_id), "os": str(entity_id), "ws": ws, "ids": memory_ids},
            )
        for x in moved_ext:
            await conn.execute(
                text(
                    "UPDATE entity_external_ids SET entity_id = :n WHERE workspace_id = :ws AND type_key = :t AND system = :s AND value = :v AND entity_id = :o"
                ),
                {"n": new_id, "o": entity_id, "ws": ws, "t": x.type_key, "s": x.system, "v": x.value},
            )
        await touch(conn, ws, "entities", entity_id, aliases=sorted(keep_aliases), last_command_id=command_id)
        if body.reextract and interactions:
            await self._reextract(cc, interactions, version_id)
        await self._conclude(
            cc,
            kind="entity.split",
            command_type=command_type,
            command_id=command_id,
            reason_code=body.reason_code,
            target_kind="entity",
            target_id=entity_id,
            version_id=version_id,
            entity_id=entity_id,
        )
        await cc.audit.record(
            "entity.split",
            resource_type="entity",
            resource_id=entity_id,
            metadata=AuditMetadata(
                command_type=command_type,
                entity_id=entity_id,
                target_id=new_id,
                reason_code=body.reason_code,
                count_facts=len(facts),
                count_memories=len(memories),
                count=len(mentions),
            ),
        )
        return Applied(
            CorrectionOut(
                correction_id=correction_id,
                command_id=command_id,
                kind="entity.split",
                target_kind="entity",
                target_id=entity_id,
                new_target_id=new_id,
                revision=orig.revision + 1,
                grant_holders=holders,
                moved=result["moved"],
                status_url=f"/v1/commands/{command_id}",
            )
        )

    async def _reextract(self, cc: CommandContext, interaction_ids: list[uuid.UUID], version_id: uuid.UUID) -> None:
        """Replays the extraction of the interactions whose mentions moved, under a NEW generation (the stage row records it), so the model sees the text again with the
        two identities kept apart. The stages are made pending; the orchestrator's outbox job does the work."""
        from kg_common.versions import PIPELINE_VERSION as _PV

        ws = cc.ctx.workspace_id
        for iid in interaction_ids:
            st = (
                await cc.conn.execute(
                    text(
                        "SELECT generation, status FROM interaction_stages WHERE workspace_id = :ws AND interaction_id = :i AND stage = 'graph_ingest' FOR UPDATE"
                    ),
                    {"ws": ws, "i": iid},
                )
            ).first()
            if st is None or st.status in ("pending", "running"):
                continue
            gen = st.generation + 1
            await cc.conn.execute(
                text(
                    "UPDATE interaction_stages SET generation = :g, status = 'pending', attempts = 0, last_error_category = NULL, last_error_code = NULL, result = '{}'::jsonb, "
                    "started_at = NULL, finished_at = NULL WHERE workspace_id = :ws AND interaction_id = :i AND stage = 'graph_ingest'"
                ),
                {"g": gen, "ws": ws, "i": iid},
            )
            await enqueue(
                cc.conn,
                workspace_id=ws,
                stage="graph_ingest",
                subject_type="interaction",
                subject_id=iid,
                dedupe_key=f"interaction:{iid}:graph_ingest:g{gen}",
                payload={"interaction_id": str(iid), "generation": gen},
                pipeline_version=_PV,
                ontology_version_id=version_id,
                concurrency_key=f"graph:{ws}",
            )

    # ---- previews (read-only) -----------------------------------------------------------------------------------------------------

    async def preview_fact(
        self,
        conn: AsyncConnection,
        ctx: WorkspaceContext,
        principal: Principal,
        fact_id: uuid.UUID,
        kind: str,
        subjects: SubjectFilter,
        *,
        include_contexts: bool,
    ) -> PreviewOut:
        ws = ctx.workspace_id
        fact = await self._fact(conn, ws, fact_id, subjects, lock=False)
        deps = await self._memory.visible(conn, principal, [fact_id]) if self._memory is not None else []
        acl, params = fact_visible_sql(subjects, source="s", target="t")
        siblings = (
            await conn.execute(
                text(
                    "SELECT DISTINCT f.id FROM fact_evidence a JOIN fact_evidence b ON b.workspace_id = a.workspace_id AND b.episode_uuid = a.episode_uuid AND b.fact_id <> a.fact_id "  # noqa: S608
                    "JOIN facts f ON f.workspace_id = b.workspace_id AND f.id = b.fact_id "
                    "JOIN entities s ON s.workspace_id = f.workspace_id AND s.id = f.source_entity_id JOIN entities t ON t.workspace_id = f.workspace_id AND t.id = f.target_entity_id "
                    f"WHERE a.workspace_id = :ws AND a.fact_id = :f AND a.source_kind = 'episode' AND f.status NOT IN ('deleted','deletion_pending') AND {acl} ORDER BY f.id"  # noqa: S608
                ),
                {"ws": ws, "f": fact_id, **params},
            )
        ).all()
        reopened = await self._reopenable(conn, ws, fact) if kind == "reject" else []
        reviews = (
            await conn.execute(
                text(
                    "SELECT id FROM review_items WHERE workspace_id = :ws AND status = 'open' AND (fact_id = :f OR competing_fact_id = :f) ORDER BY created_at, id"
                ),
                {"ws": ws, "f": fact_id},
            )
        ).all()
        contexts = await self._context_count(conn, ws, "fact", fact_id) if include_contexts else None
        counts: dict[str, int] = {}
        for d in deps:
            counts[d.status] = counts.get(d.status, 0) + 1
        ops = {
            "reject": ["expire_edge", *(["restore_predecessor_edges"] if reopened else [])],
            "supersede": ["expire_edge", "create_edge"],
            "end": ["set_edge_invalid_at"],
            "restore": ["unexpire_edge"],
        }[kind.removeprefix("fact.")]
        return PreviewOut(
            kind=f"fact.{kind.removeprefix('fact.')}",
            target_kind="fact",
            target_id=fact_id,
            authoritative=fact.authority_class == "authoritative_system",
            dependent_memories=deps,
            dependent_memory_counts=counts,
            sibling_fact_ids=[r.id for r in siblings],
            predecessor_fact_ids=reopened,
            open_review_ids=[r.id for r in reviews],
            recent_context_count=contexts,
            graph_operations=ops,
        )

    async def preview_entity(
        self,
        conn: AsyncConnection,
        ctx: WorkspaceContext,
        principal: Principal,
        entity_id: uuid.UUID,
        kind: str,
        subjects: SubjectFilter,
        *,
        include_contexts: bool,
    ) -> PreviewOut:
        ws = ctx.workspace_id
        await self._entity(conn, ws, entity_id, subjects, lock=False)
        deps = await self._memory.about_entity(conn, principal, entity_id) if self._memory is not None else []
        mentions = (
            await conn.execute(
                text("SELECT count(*) AS n FROM entity_mentions WHERE workspace_id = :ws AND entity_id = :e"), {"ws": ws, "e": entity_id}
            )
        ).scalar_one()
        reviews = (
            await conn.execute(
                text(
                    "SELECT id FROM review_items WHERE workspace_id = :ws AND status = 'open' AND (subject_entity_id = :e OR candidate_entity_id = :e) ORDER BY created_at, id"
                ),
                {"ws": ws, "e": entity_id},
            )
        ).all()
        contexts = await self._context_count(conn, ws, "entity", entity_id) if include_contexts else None
        counts: dict[str, int] = {}
        for d in deps:
            counts[d.status] = counts.get(d.status, 0) + 1
        ops = {
            "correct_attribute": ["upsert_node"],
            "rename": ["upsert_node", "refresh_edge_text"],
            "split": ["create_node", "move_mentions", "recreate_edges_with_new_endpoint"],
        }[kind.removeprefix("entity.")]
        return PreviewOut(
            kind=f"entity.{kind.removeprefix('entity.')}",
            target_kind="entity",
            target_id=entity_id,
            dependent_memories=deps,
            dependent_memory_counts=counts,
            open_review_ids=[r.id for r in reviews],
            recent_context_count=contexts,
            grant_holders=await self._grant_holders(conn, ws, entity_id) if kind.removeprefix("entity.") in ("split", "rename") else [],
            mention_count=int(mentions),
            graph_operations=ops,
        )

    async def _context_count(self, conn: AsyncConnection, ws: uuid.UUID, kind: str, item_id: uuid.UUID) -> int:
        """Builds in the trace retention window that INCLUDED this record (an admin-only number). Context items name facts and memories; an entity counts its facts."""
        if kind == "fact":
            sql = (
                "SELECT count(DISTINCT i.context_id) FROM context_items i JOIN context_traces t ON t.workspace_id = i.workspace_id AND t.id = i.context_id "
                "WHERE i.workspace_id = :ws AND i.item_kind = 'fact' AND i.item_id = :id AND i.included AND t.created_at > now() - make_interval(days => :d)"
            )
            params: dict[str, Any] = {"ws": ws, "id": str(item_id), "d": self._trace_days}
        else:
            sql = (
                "SELECT count(DISTINCT i.context_id) FROM context_items i JOIN context_traces t ON t.workspace_id = i.workspace_id AND t.id = i.context_id "
                "JOIN facts f ON f.workspace_id = i.workspace_id AND f.id::text = i.item_id AND (f.source_entity_id = :e OR f.target_entity_id = :e) "
                "WHERE i.workspace_id = :ws AND i.item_kind = 'fact' AND i.included AND t.created_at > now() - make_interval(days => :d)"
            )
            params = {"ws": ws, "e": item_id, "d": self._trace_days}
        return int((await conn.execute(text(sql), params)).scalar_one())

    # ---- history ----------------------------------------------------------------------------------------------------------------------

    async def history(
        self, conn: AsyncConnection, ctx: WorkspaceContext, *, target_id: uuid.UUID | None, include_text: bool, limit: int = 50
    ) -> list[CorrectionRecord]:
        rows = (
            await conn.execute(
                text(
                    "SELECT * FROM corrections WHERE workspace_id = :ws AND (CAST(:t AS uuid) IS NULL OR target_id = :t OR new_target_id = :t) "
                    "ORDER BY created_at DESC, id DESC LIMIT :n"
                ),
                {"ws": ctx.workspace_id, "t": target_id, "n": max(1, min(limit, 200))},
            )
        ).all()
        return [
            CorrectionRecord(
                id=r.id,
                command_id=r.command_id,
                target_kind=r.target_kind,
                target_id=r.target_id,
                kind=r.kind,
                reason_code=r.reason_code,
                reason_text=r.reason_text if include_text else None,
                new_target_id=r.new_target_id,
                created_by=r.created_by,
                created_at=r.created_at,
            )
            for r in rows
        ]
