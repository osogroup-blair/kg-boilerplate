"""The deterministic command pipeline (blueprint evaluation order; Flow B). Four commands go through one ledger:

    entity.create   entity.update   relationship.assert   relationship.end

Every command is validated against the ACTIVE PUBLISHED ontology, decided (apply / reject / queue for review) and stored with its decision trace
in Postgres in one transaction, together with the entity and fact rows it produces and an audit entry. Nothing here touches the graph or a model:
projection runs after the commit (kg_knowledge.projection), and no step can call an LLM (ADR-016).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from kg_common.errors import (
    IdempotencyMismatch,
    NoActiveOntology,
    NotFound,
    PreconditionFailed,
    PreconditionRequired,
    UnknownEntity,
    ValidationFailed,
)
from kg_common.ids import uuid7
from kg_common.versions import PIPELINE_VERSION
from kg_domain.enums import Role
from kg_domain.principal import Principal, WorkspaceContext
from kg_governance.audit import AuditMetadata, AuditWriter, CommandType
from kg_jobs.outbox import enqueue
from kg_obs import metrics
from kg_ontology import OntologySpec
from kg_ontology.conflicts import evidence_status
from kg_ontology.rules.context import EntityRef, FactSnap, RuleCommand, RuleContext, Violation
from kg_ontology.rules.engine import Evaluation, evaluate
from kg_ontology.spec import AUTHORITY_RANK
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

from kg_knowledge.idempotency import lock_key, request_hash
from kg_knowledge.models import (
    CommandOut,
    EntityCreateIn,
    EntityRefIn,
    EntityUpdateIn,
    RelationshipAssertIn,
    RelationshipEndIn,
)
from kg_knowledge.specs import SpecCache

STAGE_ORDER = [
    "authenticate",
    "ontology",
    "schema_validation",
    "source_classification",
    "identity_resolution",
    "temporal_rules",
    "mutation_authorization",
    "review",
    "decision",
]
REDACTED = "[redacted]"


@dataclass
class CommandContext:
    conn: AsyncConnection
    ctx: WorkspaceContext
    principal: Principal
    audit: AuditWriter
    override_queue: bool = False  # a reviewer approved a queued command: the rule that queued it is overridden once (P4 review decision)


@dataclass
class PipelineResult:
    command_id: uuid.UUID
    command_type: str
    status: str  # applied | rejected | needs_review
    out: CommandOut
    violations: list[dict[str, Any]] = field(default_factory=list)  # rule_kind, rule_id, code, path: never values
    replayed: bool = False
    created: bool = True  # False when an entity.create matched an existing entity (HTTP 200)


def derive_authority(principal: Principal, requested: str | None) -> str:
    """The authority class comes from the caller's role, never from the request: a request may only lower it (blueprint source classification)."""
    granted = "user_asserted"
    if Role.WORKSPACE_ADMIN in principal.roles:
        granted = "admin_asserted"
    if Role.APP_WRITER in principal.roles:
        granted = "authoritative_system"
    if requested is not None and AUTHORITY_RANK[requested] > AUTHORITY_RANK[granted]:
        raise ValidationFailed("authority cannot be raised above what the caller's role grants", details={"fields": ["authority"]})
    return requested or granted


def _public_violation(v: Violation | dict[str, Any]) -> dict[str, Any]:
    d = v if isinstance(v, dict) else {"rule_kind": v.rule_kind, "rule_id": v.rule_id, "code": v.code, "path": v.path, "action": v.action}
    return {k: d.get(k) for k in ("rule_kind", "rule_id", "code", "path")}


def _redact_sensitive(payload: dict[str, Any], sensitive: set[str]) -> dict[str, Any]:
    attrs = payload.get("attributes")
    if isinstance(attrs, dict):
        payload["attributes"] = {k: (REDACTED if k in sensitive else v) for k, v in attrs.items()}
    return payload


def _sensitive_names(spec: OntologySpec, *, entity_type: str | None = None, rel_type: str | None = None) -> set[str]:
    attrs = {}
    if entity_type and entity_type in spec.entity_types:
        attrs = spec.entity_types[entity_type].attributes
    elif rel_type and rel_type in spec.relationship_types:
        attrs = spec.relationship_types[rel_type].attributes
    return {k for k, a in attrs.items() if a.sensitive}


def _fact_snap(r: Row[Any]) -> FactSnap:
    return FactSnap(
        str(r.id), r.rel_type, str(r.source_entity_id), str(r.target_entity_id), r.valid_from, r.valid_to, r.valid_from_unknown, r.status
    )


class CommandPipeline:
    def __init__(self, specs: SpecCache):
        self._specs = specs

    # ---- shared steps ---------------------------------------------------------------------------------------------------------------------

    async def _prelude(self, cc: CommandContext, key: str, digest: str) -> PipelineResult | tuple[uuid.UUID, OntologySpec]:
        """Stage 1-2: serialise on the idempotency key, replay a known command, otherwise load the active published ontology."""
        ws = cc.ctx.workspace_id
        await lock_key(cc.conn, ws, "cmd", key)
        row = (
            await cc.conn.execute(text("SELECT * FROM commands WHERE workspace_id = :ws AND idempotency_key = :k"), {"ws": ws, "k": key})
        ).first()
        if row is not None:
            if row.request_hash != digest:
                raise IdempotencyMismatch()
            return await self._replay(cc, row)
        active = await self._specs.active(cc.conn, ws)
        if active is None:
            raise NoActiveOntology()
        return active

    async def _replay(self, cc: CommandContext, row: Row[Any]) -> PipelineResult:
        result = row.result or {}
        review = (
            await cc.conn.execute(
                text("SELECT id FROM review_items WHERE workspace_id = :ws AND command_id = :c LIMIT 1"),
                {"ws": cc.ctx.workspace_id, "c": row.id},
            )
        ).first()
        violations = [_public_violation(v) for stage in row.decision for v in stage.get("violations", []) if v.get("action") != "warn"]
        return PipelineResult(
            command_id=row.id,
            command_type=row.command_type,
            status=row.status,
            out=self._out(row, result, review.id if review else None),
            violations=violations if row.status == "rejected" else [],
            replayed=True,
            created=not result.get("matched_existing", False),
        )

    @staticmethod
    def _out(row: Row[Any], result: dict[str, Any], review_id: uuid.UUID | None) -> CommandOut:
        return CommandOut(
            command_id=row.id,
            command_type=row.command_type,
            status=row.status,
            matched_existing=bool(result.get("matched_existing", False)),
            entity_id=result.get("entity_id"),
            fact_id=result.get("fact_id"),
            ended_fact_ids=result.get("ended_fact_ids", []),
            review_item_id=review_id,
            ontology_version_id=row.ontology_version_id,
            projection_status=row.projection_status,
            status_url=f"/v1/commands/{row.id}",
        )

    async def _conclude(
        self,
        cc: CommandContext,
        *,
        key: str,
        digest: str,
        command_type: CommandType,
        version_id: uuid.UUID,
        authority: str,
        classification: str,
        source_system: str | None,
        source_ref: str | None,
        occurred_at: Any,
        payload: dict[str, Any],
        ev: Evaluation,
        extra_trace: list[dict[str, Any]],
        command_id: uuid.UUID,
        result: dict[str, Any],
        apply: Callable[[], Awaitable[None]],
    ) -> PipelineResult:
        """Stages 8-9: decide, then persist the command, the rows it produced and the audit entry in the caller's transaction."""
        if (
            cc.override_queue
        ):  # the reviewer's approval overrides the queuing rule (and only that): the violation stays in the trace as a warning
            ev.violations = [
                Violation(v.rule_kind, v.code, v.path, "warn", v.stage, v.rule_id, v.queue_class) if v.action == "queue_review" else v
                for v in ev.violations
            ]
        outcome = ev.outcome
        for v in ev.violations:
            metrics.rule_violations_total.labels(rule_kind=v.rule_kind, action=v.action).inc()
        status, http = {"reject": ("rejected", 422), "queue_review": ("needs_review", 202)}.get(
            outcome, ("applied", 200 if result.get("matched_existing") else 201)
        )
        trace = sorted([*ev.trace, *extra_trace, {"stage": "decision", "outcome": outcome}], key=lambda s: STAGE_ORDER.index(s["stage"]))
        stored_result = result if status == "applied" else {}
        if status == "needs_review" and command_type in (CommandType.ENTITY_UPDATE, CommandType.RELATIONSHIP_END):
            # a reviewer's approval re-applies the command (P4): an update or an end needs to know which row it was aimed at
            stored_result = {"queued_target": {k: result[k] for k in ("entity_id", "fact_id") if k in result}}
        projection = "pending" if status == "applied" and not result.get("matched_existing") else "not_applicable"
        ws = cc.ctx.workspace_id
        await cc.conn.execute(
            text(
                "INSERT INTO commands (id, workspace_id, idempotency_key, request_hash, command_type, actor_principal_id, authority_class, classification, "
                "source_system, source_ref, occurred_at, ontology_version_id, payload, status, decision, result, http_status, projection_status) "
                "VALUES (:id, :ws, :key, :h, :ct, :actor, :auth, :cls, :ss, :sr, :occ, :ver, CAST(:payload AS jsonb), :status, CAST(:decision AS jsonb), "
                "CAST(:result AS jsonb), :http, :proj)"
            ),
            {
                "id": command_id,
                "ws": ws,
                "key": key,
                "h": digest,
                "ct": command_type.value,
                "actor": cc.principal.principal_id,
                "auth": authority,
                "cls": classification,
                "ss": source_system,
                "sr": source_ref,
                "occ": occurred_at,
                "ver": version_id,
                "payload": json.dumps(payload, default=str),
                "status": status,
                "decision": json.dumps(trace, default=str),
                "result": json.dumps(stored_result, default=str),
                "http": http,
                "proj": projection,
            },
        )
        review_id: uuid.UUID | None = None
        queued = next((v for v in ev.violations if v.action == "queue_review"), None)
        if status == "needs_review" and queued is not None:
            review_id = uuid7()
            await cc.conn.execute(
                text(
                    "INSERT INTO review_items (id, workspace_id, item_type, command_id, rule_kind, reason_code) VALUES (:id, :ws, 'policy_violation', :c, :rk, :rc)"
                ),
                {"id": review_id, "ws": ws, "c": command_id, "rk": queued.rule_kind, "rc": queued.code},
            )
        if status == "applied":
            await apply()
            if projection == "pending":
                # the transactional outbox: the projection job commits with the command, so a crash after commit loses nothing (P3 §5.6)
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
                    concurrency_key=f"graph:{ws}",  # graph writers of one workspace are serialised (P4 Q4)
                )
        first = next((v for v in ev.violations if v.action != "warn"), None)
        audit_action = {"applied": "command.applied", "rejected": "command.rejected", "needs_review": "command.queued"}[status]
        await cc.audit.record(
            audit_action,
            outcome="denied" if status == "rejected" else "allowed",
            resource_type="command",
            resource_id=command_id,
            metadata=AuditMetadata(
                command_type=command_type,
                version_id=version_id,
                violation_count=sum(1 for v in ev.violations if v.action != "warn"),
                rule_kind=first.rule_kind if first else None,
            ),
        )
        metrics.commands_total.labels(command_type=command_type.value, outcome=status).inc()
        out = CommandOut(
            command_id=command_id,
            command_type=command_type.value,
            status=status,
            matched_existing=bool(result.get("matched_existing", False)) and status == "applied",
            entity_id=stored_result.get("entity_id"),
            fact_id=stored_result.get("fact_id"),
            ended_fact_ids=stored_result.get("ended_fact_ids", []),
            review_item_id=review_id,
            ontology_version_id=version_id,
            projection_status=projection,
            status_url=f"/v1/commands/{command_id}",
        )
        return PipelineResult(
            command_id=command_id,
            command_type=command_type.value,
            status=status,
            out=out,
            violations=[_public_violation(v) for v in ev.violations if v.action != "warn"] if status == "rejected" else [],
            created=not result.get("matched_existing", False),
        )

    async def _resolve(self, cc: CommandContext, ref: EntityRefIn, field_name: str) -> Row[Any]:
        """An endpoint by platform id or strong external id. Not found (or in another workspace) is the same 422: no existence oracle."""
        ws = cc.ctx.workspace_id
        params: dict[str, Any]
        if ref.entity_id is not None:
            sql = "SELECT id, type_key, display_name, classification FROM entities WHERE workspace_id = :ws AND id = :id AND status = 'active'"
            params = {"ws": ws, "id": ref.entity_id}
        else:
            assert ref.external_id is not None
            sql = (
                "SELECT e.id, e.type_key, e.display_name, e.classification FROM entity_external_ids x "
                "JOIN entities e ON e.workspace_id = x.workspace_id AND e.id = x.entity_id "
                "WHERE x.workspace_id = :ws AND x.type_key = :t AND x.system = :s AND x.value = :v AND e.status = 'active'"
            )
            params = {"ws": ws, "t": ref.type, "s": ref.external_id.system, "v": ref.external_id.value}
        row = (await cc.conn.execute(text(sql), params)).first()
        if row is None:
            raise UnknownEntity(details={"endpoint": field_name})
        return row

    @staticmethod
    def _ref(row: Row[Any]) -> EntityRef:
        return EntityRef(str(row.id), row.type_key, row.display_name, row.classification)

    # ---- entity.create --------------------------------------------------------------------------------------------------------------------

    async def entity_create(self, cc: CommandContext, key: str, body: EntityCreateIn) -> PipelineResult:
        digest = request_hash("entity.create", body.model_dump(mode="json"))
        pre = await self._prelude(cc, key, digest)
        if isinstance(pre, PipelineResult):
            return pre
        version_id, spec = pre
        ws = cc.ctx.workspace_id
        authority = derive_authority(cc.principal, body.authority)
        ext = tuple(sorted({(e.system, e.value) for e in body.external_ids}))
        cmd = RuleCommand(
            kind="entity.create",
            entity_type=body.type,
            name=body.name,
            attributes=body.attributes,
            external_ids=ext,
            classification=body.classification,
            authority=authority,
            source_system=body.source_system,
            source_ref=body.source_ref,
        )
        ev = evaluate(spec, cmd, RuleContext())
        extra: list[dict[str, Any]] = []
        matched: uuid.UUID | None = None
        if ev.outcome != "reject":
            found: set[uuid.UUID] = set()
            for system, value in ext:
                await lock_key(
                    cc.conn, ws, "ext", f"{body.type}:{system}:{value}"
                )  # two parallel creates with the same identifier serialise here
                row = (
                    await cc.conn.execute(
                        text(
                            "SELECT entity_id FROM entity_external_ids WHERE workspace_id = :ws AND type_key = :t AND system = :s AND value = :v"
                        ),
                        {"ws": ws, "t": body.type, "s": system, "v": value},
                    )
                ).first()
                if row is not None:
                    found.add(row.entity_id)
            if (
                len(found) > 1
            ):  # two strong identifiers point at two different entities: a human must resolve it (P4/P8), the platform does not guess
                ev.violations.append(
                    Violation("identity_resolution", "ambiguous_identity", "external_ids", "reject", "identity_resolution")
                )
                extra.append(
                    {
                        "stage": "identity_resolution",
                        "outcome": "reject",
                        "violations": [
                            {
                                "rule_kind": "identity_resolution",
                                "code": "ambiguous_identity",
                                "path": "external_ids",
                                "rule_id": None,
                                "action": "reject",
                            }
                        ],
                    }
                )
            elif found:
                matched = next(iter(found))
                extra.append({"stage": "identity_resolution", "outcome": "matched_existing", "violations": []})
            else:
                extra.append({"stage": "identity_resolution", "outcome": "new", "violations": []})
        entity_id = matched or uuid7()
        command_id = uuid7()
        result: dict[str, Any] = {"entity_id": str(entity_id)}
        if matched:
            result["matched_existing"] = True

        async def apply() -> None:
            if matched:
                return
            aliases = sorted({a.strip() for a in body.aliases if a.strip()})
            await cc.conn.execute(
                text(
                    "INSERT INTO entities (id, workspace_id, type_key, display_name, aliases, attributes, classification, ontology_version_id, created_by_command_id, last_command_id) "
                    "VALUES (:id, :ws, :t, :name, :aliases, CAST(:attrs AS jsonb), :cls, :ver, :c, :c)"
                ),
                {
                    "id": entity_id,
                    "ws": ws,
                    "t": body.type,
                    "name": ev.name,
                    "aliases": aliases,
                    "attrs": json.dumps(ev.attributes),
                    "cls": body.classification,
                    "ver": version_id,
                    "c": command_id,
                },
            )
            for system, value in ext:
                await cc.conn.execute(
                    text("INSERT INTO entity_external_ids (workspace_id, entity_id, type_key, system, value) VALUES (:ws, :e, :t, :s, :v)"),
                    {"ws": ws, "e": entity_id, "t": body.type, "s": system, "v": value},
                )

        payload = _redact_sensitive(body.model_dump(mode="json"), _sensitive_names(spec, entity_type=body.type))
        return await self._conclude(
            cc,
            key=key,
            digest=digest,
            command_type=CommandType.ENTITY_CREATE,
            version_id=version_id,
            authority=authority,
            classification=body.classification,
            source_system=body.source_system,
            source_ref=body.source_ref,
            occurred_at=body.occurred_at,
            payload=payload,
            ev=ev,
            extra_trace=extra,
            command_id=command_id,
            result=result,
            apply=apply,
        )

    # ---- entity.update --------------------------------------------------------------------------------------------------------------------

    async def entity_update(
        self, cc: CommandContext, key: str, entity_id: uuid.UUID, body: EntityUpdateIn, expected_revision: int | None
    ) -> PipelineResult:
        digest = request_hash("entity.update", str(entity_id), body.model_dump(mode="json"), expected_revision)
        pre = await self._prelude(cc, key, digest)
        if isinstance(pre, PipelineResult):
            return pre
        version_id, spec = pre
        ws = cc.ctx.workspace_id
        row = (
            await cc.conn.execute(
                text(
                    "SELECT id, type_key, display_name, aliases, attributes, classification, revision FROM entities WHERE workspace_id = :ws AND id = :id AND status = 'active' FOR UPDATE"
                ),
                {"ws": ws, "id": entity_id},
            )
        ).first()
        if row is None:
            raise NotFound()
        if expected_revision is None:
            raise PreconditionRequired()
        if expected_revision != row.revision:
            raise PreconditionFailed()
        authority = derive_authority(cc.principal, body.authority)
        classification = body.classification or row.classification
        new_ext = tuple(sorted({(e.system, e.value) for e in body.add_external_ids}))
        cmd = RuleCommand(
            kind="entity.update",
            entity_type=row.type_key,
            name=body.name,
            attributes=body.attributes,
            external_ids=new_ext,
            classification=classification,
            authority=authority,
            source_system=body.source_system,
            source_ref=body.source_ref,
            entity=EntityRef(str(row.id), row.type_key, row.display_name, row.classification),
        )
        ev = evaluate(spec, cmd, RuleContext(current_attributes=dict(row.attributes or {})))
        extra: list[dict[str, Any]] = []
        to_insert: list[tuple[str, str]] = []
        if ev.outcome != "reject":
            for system, value in new_ext:
                await lock_key(cc.conn, ws, "ext", f"{row.type_key}:{system}:{value}")
                owner = (
                    await cc.conn.execute(
                        text(
                            "SELECT entity_id FROM entity_external_ids WHERE workspace_id = :ws AND type_key = :t AND system = :s AND value = :v"
                        ),
                        {"ws": ws, "t": row.type_key, "s": system, "v": value},
                    )
                ).first()
                if owner is None:
                    to_insert.append((system, value))
                elif owner.entity_id != entity_id:
                    ev.violations.append(
                        Violation(
                            "identity_resolution", "external_id_in_use", f"add_external_ids[{system}]", "reject", "identity_resolution"
                        )
                    )
            bad = [v for v in ev.violations if v.rule_kind == "identity_resolution"]
            extra.append(
                {
                    "stage": "identity_resolution",
                    "outcome": "reject" if bad else "pass",
                    "violations": [
                        {"rule_kind": v.rule_kind, "code": v.code, "path": v.path, "rule_id": None, "action": v.action} for v in bad
                    ],
                }
            )
        command_id = uuid7()
        result = {"entity_id": str(entity_id)}

        async def apply() -> None:
            attrs = dict(row.attributes or {})
            for k, v in ev.attributes.items():
                if v is None:
                    attrs.pop(k, None)
                else:
                    attrs[k] = v
            aliases = sorted({*row.aliases, *(a.strip() for a in body.add_aliases if a.strip())})
            name = ev.name or row.display_name
            await cc.conn.execute(
                text(
                    "UPDATE entities SET display_name = :name, aliases = :aliases, attributes = CAST(:attrs AS jsonb), classification = :cls, revision = revision + 1, "
                    "ontology_version_id = :ver, last_command_id = :c, updated_at = now(), projection_status = 'pending', projection_attempts = 0, projection_next_at = now(), last_error_code = NULL "
                    "WHERE workspace_id = :ws AND id = :id"
                ),
                {
                    "name": name,
                    "aliases": aliases,
                    "attrs": json.dumps(attrs),
                    "cls": classification,
                    "ver": version_id,
                    "c": command_id,
                    "ws": ws,
                    "id": entity_id,
                },
            )
            for system, value in to_insert:
                await cc.conn.execute(
                    text("INSERT INTO entity_external_ids (workspace_id, entity_id, type_key, system, value) VALUES (:ws, :e, :t, :s, :v)"),
                    {"ws": ws, "e": entity_id, "t": row.type_key, "s": system, "v": value},
                )
            if name != row.display_name:  # fact text embeds endpoint names: refresh the facts that mention this entity
                await cc.conn.execute(
                    text(
                        "UPDATE facts SET projection_status = 'pending', projection_attempts = 0, projection_next_at = now(), last_error_code = NULL "
                        "WHERE workspace_id = :ws AND (source_entity_id = :e OR target_entity_id = :e) AND status = 'active' AND projection_status <> 'skipped_by_policy'"
                    ),
                    {"ws": ws, "e": entity_id},
                )

        payload = _redact_sensitive(body.model_dump(mode="json"), _sensitive_names(spec, entity_type=row.type_key))
        return await self._conclude(
            cc,
            key=key,
            digest=digest,
            command_type=CommandType.ENTITY_UPDATE,
            version_id=version_id,
            authority=authority,
            classification=classification,
            source_system=body.source_system,
            source_ref=body.source_ref,
            occurred_at=body.occurred_at,
            payload=payload,
            ev=ev,
            extra_trace=extra,
            command_id=command_id,
            result=result,
            apply=apply,
        )

    # ---- relationship.assert --------------------------------------------------------------------------------------------------------------

    async def relationship_assert(self, cc: CommandContext, key: str, body: RelationshipAssertIn) -> PipelineResult:
        digest = request_hash("relationship.assert", body.model_dump(mode="json"))
        pre = await self._prelude(cc, key, digest)
        if isinstance(pre, PipelineResult):
            return pre
        version_id, spec = pre
        ws = cc.ctx.workspace_id
        authority = derive_authority(cc.principal, body.authority)
        source_row = await self._resolve(cc, body.source, "source")
        target_row = await self._resolve(cc, body.target, "target")
        # serialise concurrent commands on this source: succession must see a consistent chain (WP6)
        await cc.conn.execute(
            text("SELECT 1 FROM entities WHERE workspace_id = :ws AND id = :id FOR UPDATE"), {"ws": ws, "id": source_row.id}
        )
        facts: tuple[FactSnap, ...] = ()
        if body.type in spec.relationship_types:
            rows = (
                await cc.conn.execute(
                    text(
                        "SELECT id, rel_type, source_entity_id, target_entity_id, valid_from, valid_to, valid_from_unknown, status FROM facts "
                        "WHERE workspace_id = :ws AND source_entity_id = :s AND rel_type = :r AND status = 'active'"
                    ),
                    {"ws": ws, "s": source_row.id, "r": body.type},
                )
            ).all()
            facts = tuple(_fact_snap(r) for r in rows)
        cmd = RuleCommand(
            kind="relationship.assert",
            rel_type=body.type,
            source=self._ref(source_row),
            target=self._ref(target_row),
            attributes=body.attributes,
            valid_from=body.valid_from,
            valid_to=body.valid_to,
            valid_from_unknown=body.valid_from_unknown,
            classification=body.classification,
            authority=authority,
            source_system=body.source_system,
            source_ref=body.source_ref,
        )
        ev = evaluate(spec, cmd, RuleContext(facts=facts))
        command_id, fact_id = uuid7(), uuid7()
        ended = [uuid.UUID(e.fact_id) for e in ev.effects]
        result = {
            "fact_id": str(fact_id),
            "ended_fact_ids": [str(e) for e in ended],
            "endpoint_entity_ids": [str(source_row.id), str(target_row.id)],
        }

        async def apply() -> None:
            await cc.conn.execute(
                text(
                    "INSERT INTO facts (id, workspace_id, rel_type, source_entity_id, target_entity_id, attributes, valid_from, valid_from_unknown, valid_to, source_recorded_at, "
                    "authority_class, classification, source_type, source_command_id, last_command_id, source_system, source_ref, ontology_version_id, evidence_status) "
                    "VALUES (:id, :ws, :rel, :s, :t, CAST(:attrs AS jsonb), :vf, :vfu, :vt, :sra, :auth, :cls, 'command', :c, :c, :ss, :sr, :ver, :evs)"
                ),
                {
                    "id": fact_id,
                    "ws": ws,
                    "rel": body.type,
                    "s": source_row.id,
                    "t": target_row.id,
                    "attrs": json.dumps(ev.attributes),
                    "vf": body.valid_from,
                    "vfu": body.valid_from_unknown,
                    "vt": body.valid_to,
                    "sra": body.source_recorded_at,
                    "auth": authority,
                    "cls": body.classification,
                    "c": command_id,
                    "ss": body.source_system,
                    "sr": body.source_ref,
                    "ver": version_id,
                    "evs": evidence_status(authority),
                },
            )
            await cc.conn.execute(
                text("INSERT INTO fact_evidence (id, fact_id, workspace_id, source_kind, command_id) VALUES (:id, :f, :ws, 'command', :c)"),
                {"id": uuid7(), "f": fact_id, "ws": ws, "c": command_id},
            )
            for effect in ev.effects:
                await cc.conn.execute(
                    text(
                        "UPDATE facts SET valid_to = :vt, succeeded_by_fact_id = :new, revision = revision + 1, last_command_id = :c, projection_status = 'pending', "
                        "projection_attempts = 0, projection_next_at = now(), last_error_code = NULL WHERE workspace_id = :ws AND id = :id AND status = 'active'"
                    ),
                    {"vt": effect.valid_to, "new": fact_id, "c": command_id, "ws": ws, "id": uuid.UUID(effect.fact_id)},
                )

        payload = _redact_sensitive(body.model_dump(mode="json"), _sensitive_names(spec, rel_type=body.type))
        return await self._conclude(
            cc,
            key=key,
            digest=digest,
            command_type=CommandType.RELATIONSHIP_ASSERT,
            version_id=version_id,
            authority=authority,
            classification=body.classification,
            source_system=body.source_system,
            source_ref=body.source_ref,
            occurred_at=body.occurred_at,
            payload=payload,
            ev=ev,
            extra_trace=[],
            command_id=command_id,
            result=result,
            apply=apply,
        )

    # ---- relationship.end -----------------------------------------------------------------------------------------------------------------

    async def relationship_end(self, cc: CommandContext, key: str, fact_id: uuid.UUID, body: RelationshipEndIn) -> PipelineResult:
        digest = request_hash("relationship.end", str(fact_id), body.model_dump(mode="json"))
        pre = await self._prelude(cc, key, digest)
        if isinstance(pre, PipelineResult):
            return pre
        version_id, spec = pre
        ws = cc.ctx.workspace_id
        probe = (
            await cc.conn.execute(
                text("SELECT source_entity_id FROM facts WHERE workspace_id = :ws AND id = :id"), {"ws": ws, "id": fact_id}
            )
        ).first()
        if probe is None:
            raise NotFound()
        # same lock order as relationship.assert: source entity first, then the fact
        await cc.conn.execute(
            text("SELECT 1 FROM entities WHERE workspace_id = :ws AND id = :id FOR UPDATE"), {"ws": ws, "id": probe.source_entity_id}
        )
        fact = (
            await cc.conn.execute(
                text(
                    "SELECT f.id, f.rel_type, f.source_entity_id, f.target_entity_id, f.valid_from, f.valid_to, f.valid_from_unknown, f.status, "
                    "s.type_key AS s_type, s.display_name AS s_name, s.classification AS s_cls, t.type_key AS t_type, t.display_name AS t_name, t.classification AS t_cls "
                    "FROM facts f JOIN entities s ON s.workspace_id = f.workspace_id AND s.id = f.source_entity_id "
                    "JOIN entities t ON t.workspace_id = f.workspace_id AND t.id = f.target_entity_id WHERE f.workspace_id = :ws AND f.id = :id FOR UPDATE OF f"
                ),
                {"ws": ws, "id": fact_id},
            )
        ).one()
        authority = derive_authority(cc.principal, body.authority)
        cmd = RuleCommand(
            kind="relationship.end",
            rel_type=fact.rel_type,
            source=EntityRef(str(fact.source_entity_id), fact.s_type, fact.s_name, fact.s_cls),
            target=EntityRef(str(fact.target_entity_id), fact.t_type, fact.t_name, fact.t_cls),
            valid_to=body.valid_to,
            classification=body.classification,
            authority=authority,
            source_system=body.source_system,
            source_ref=body.source_ref,
            target_fact=_fact_snap(fact),
        )
        ev = evaluate(spec, cmd, RuleContext())
        command_id = uuid7()
        result = {
            "fact_id": str(fact_id),
            "ended_fact_ids": [str(fact_id)],
            "endpoint_entity_ids": [str(fact.source_entity_id), str(fact.target_entity_id)],
        }

        async def apply() -> None:
            await cc.conn.execute(
                text(
                    "UPDATE facts SET valid_to = :vt, revision = revision + 1, last_command_id = :c, projection_status = 'pending', projection_attempts = 0, "
                    "projection_next_at = now(), last_error_code = NULL WHERE workspace_id = :ws AND id = :id"
                ),
                {"vt": body.valid_to, "c": command_id, "ws": ws, "id": fact_id},
            )

        return await self._conclude(
            cc,
            key=key,
            digest=digest,
            command_type=CommandType.RELATIONSHIP_END,
            version_id=version_id,
            authority=authority,
            classification=body.classification,
            source_system=body.source_system,
            source_ref=body.source_ref,
            occurred_at=body.occurred_at,
            payload=body.model_dump(mode="json"),
            ev=ev,
            extra_trace=[],
            command_id=command_id,
            result=result,
            apply=apply,
        )
