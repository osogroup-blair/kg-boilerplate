"""Append-only audit writer. Metadata is a closed model: IDs, counts, hashes and enums only; free text cannot be stored."""

from __future__ import annotations

import enum
import uuid
from typing import Any, Literal, Protocol

from kg_common.ids import uuid7
from kg_domain.principal import Principal
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


class AuditReason(enum.StrEnum):
    NOT_IN_WORKSPACE = "not_in_workspace"
    MISSING_ROLE = "missing_role"


class CommandType(enum.StrEnum):
    ENTITY_CREATE = "entity.create"
    ENTITY_UPDATE = "entity.update"
    RELATIONSHIP_ASSERT = "relationship.assert"
    RELATIONSHIP_END = "relationship.end"
    REVIEW_DECIDE = "review.decide"
    ENTITY_MERGE = "entity.merge"
    MEMORY_SAVE = "memory.save"
    MEMORY_PROPOSE = "memory.propose"
    MEMORY_EXTRACT = "memory.extract"
    MEMORY_SUPERSEDE = "memory.supersede"
    MEMORY_EXPIRE = "memory.expire"
    MEMORY_DISPUTE = "memory.dispute"
    MEMORY_PUBLISH = "memory.publish"
    MEMORY_REVIEW = "memory.review"
    PRINCIPAL_REGISTER = "principal.register"
    PRINCIPAL_UPDATE = "principal.update"
    PRINCIPAL_REVOKE = "principal.revoke"
    KEY_CREATE = "key.create"
    KEY_REVOKE = "key.revoke"
    GRANT_ADD = "grant.add"
    GRANT_REMOVE = "grant.remove"
    CORRECTION_FACT_REJECT = "correction.fact_reject"
    CORRECTION_FACT_SUPERSEDE = "correction.fact_supersede"
    CORRECTION_FACT_END = "correction.fact_end"
    CORRECTION_FACT_RESTORE = "correction.fact_restore"
    CORRECTION_ATTRIBUTE = "correction.attribute"
    CORRECTION_RENAME = "correction.rename"
    CORRECTION_MEMORY = "correction.memory"
    ENTITY_SPLIT = "entity.split"
    MEMORY_CONSOLIDATE = "memory.consolidate"
    SETTINGS_UPDATE = "settings.update"
    DELETION_REQUEST = "deletion.request"


class EventKind(enum.StrEnum):
    MESSAGE = "message"
    CONVERSATION = "conversation"
    OBSERVATION = "observation"
    DOCUMENT = "document"
    JSON = "json"


class PipelineStage(enum.StrEnum):
    NORMALIZE = "normalize"
    GRAPH_INGEST = "graph_ingest"
    MEMORY_PROPOSE = "memory_propose"
    GRAPH_PROJECTION = "graph_projection"


class AuditMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_id: uuid.UUID | None = None
    key_id: uuid.UUID | None = None
    key_prefix: str | None = Field(default=None, pattern=r"^[A-Za-z0-9]{1,16}$")
    action_name: str | None = Field(default=None, pattern=r"^[a-z_]+\.[a-z_]+$")
    count: int | None = None
    ttl_seconds: int | None = None
    status_code: int | None = None
    reason: AuditReason | None = None
    alias: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_./-]{1,64}$")
    version_id: uuid.UUID | None = None
    content_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    command_type: CommandType | None = None
    rule_kind: str | None = Field(default=None, pattern=r"^[a-z][a-z_]{0,63}$")
    violation_count: int | None = None
    event_kind: EventKind | None = None
    stage: PipelineStage | None = None
    generation: int | None = None
    job_id: uuid.UUID | None = None
    reason_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    form: Literal["raw", "normalized"] | None = None
    payload_kid: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,32}$")
    classification: str | None = Field(default=None, pattern=r"^[a-z_]{1,32}$")
    # P4 (extraction, identity, review). Closed fields: ids, counts and enums, never names, fact text or rationale text.
    interaction_id: uuid.UUID | None = None
    review_id: uuid.UUID | None = None
    entity_id: uuid.UUID | None = None
    fact_id: uuid.UUID | None = None
    review_type: str | None = Field(default=None, pattern=r"^[a-z_]{1,32}$")
    decision: str | None = Field(default=None, pattern=r"^[a-z_]{1,64}$")
    rationale_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    count_entities: int | None = None
    count_facts: int | None = None
    count_reverts: int | None = None
    # P5 (curated memory). Closed fields: ids, counts and enums, never content, summaries, queries or purposes.
    memory_id: uuid.UUID | None = None
    memory_type: Literal["working", "episodic", "semantic", "procedural"] | None = None
    memory_status: Literal["proposed", "active", "superseded", "disputed", "expired", "rejected", "deleted"] | None = None
    scope_kind: Literal["workspace", "subject", "project", "agent", "session", "classification"] | None = None
    count_memories: int | None = None
    drop_reason: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    principal_id: uuid.UUID | None = None
    # P6 (context). Ids and an enum-like profile name: never a task, a query or any rendered text.
    context_id: uuid.UUID | None = None
    profile: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,63}$")
    target_principal_id: uuid.UUID | None = None
    # P7 (agents, grants, revocation, quotas). Enums and ids only: never a handle, a note or a quota value.
    principal_kind: Literal["agent", "app"] | None = None
    revoke_reason: Literal["retired", "compromised"] | None = None
    privileges: list[Literal["read", "propose", "publish"]] | None = None
    quota: Literal["requests_per_minute", "context_tokens_per_day", "interactions_per_day"] | None = None
    # P8 (corrections, consolidation, deletion, settings, simulation). Enums, ids and counts only: never a reason text, a name, a value or a residual.
    correction_kind: str | None = Field(default=None, pattern=r"^[a-z][a-z_.]{0,63}$")
    request_id: uuid.UUID | None = None
    deletion_kind: Literal["subject", "interaction", "memory", "principal_data", "workspace", "retention"] | None = None
    deletion_status: str | None = Field(default=None, pattern=r"^[a-z_]{1,32}$")
    store: Literal["postgres", "vector", "graph", "trace", "job", "idempotency"] | None = None
    count_deleted: int | None = None
    count_redacted: int | None = None
    count_retained: int | None = None
    count_residuals: int | None = None
    settings_version: int | None = None
    consolidation_mode: Literal["reinforce", "synthesize"] | None = None
    simulation_id: uuid.UUID | None = None
    override_authority: str | None = Field(default=None, pattern=r"^[a-z_]{1,32}$")


Outcome = Literal["allowed", "denied", "error"]


class AuditSink(Protocol):
    """What a service needs from an audit writer: the request's `AuditWriter` or the worker's `SystemAudit`."""

    async def record(
        self,
        action: str,
        *,
        outcome: Any = ...,
        resource_type: str | None = ...,
        resource_id: Any = ...,
        metadata: AuditMetadata | None = ...,
    ) -> uuid.UUID: ...


_INSERT = text("""
    INSERT INTO audit_log (id, workspace_id, actor_principal_id, action, resource_type, resource_id, outcome, request_id, correlation_id, metadata)
    VALUES (:id, :ws, :actor, :action, :rtype, :rid, :outcome, :req, :corr, CAST(:meta AS jsonb))""")


class SystemAudit:
    """Audit entries written by the platform itself (the worker): no principal, so the actor is empty. Same closed metadata, same transaction rule."""

    def __init__(self, conn: AsyncConnection, workspace_id: uuid.UUID, *, correlation_id: str | None = None):
        self._conn, self._ws, self._correlation_id = conn, workspace_id, correlation_id

    async def record(
        self,
        action: str,
        *,
        outcome: Outcome = "allowed",
        resource_type: str | None = None,
        resource_id: uuid.UUID | str | None = None,
        metadata: AuditMetadata | None = None,
    ) -> uuid.UUID:
        audit_id = uuid7()
        await self._conn.execute(
            _INSERT,
            {
                "id": audit_id,
                "ws": self._ws,
                "actor": None,
                "action": action,
                "rtype": resource_type,
                "rid": str(resource_id) if resource_id is not None else None,
                "outcome": outcome,
                "req": None,
                "corr": self._correlation_id,
                "meta": (metadata or AuditMetadata()).model_dump_json(exclude_none=True),
            },
        )
        return audit_id


class AuditWriter:
    """Writes inside the request's tenant transaction, so the entry and the action it records commit (or roll back) together."""

    def __init__(self, conn: AsyncConnection, principal: Principal, *, request_id: str | None, correlation_id: str | None):
        self._conn, self._principal, self._request_id, self._correlation_id = conn, principal, request_id, correlation_id

    async def record(
        self,
        action: str,
        *,
        outcome: Outcome = "allowed",
        resource_type: str | None = None,
        resource_id: uuid.UUID | str | None = None,
        metadata: AuditMetadata | None = None,
    ) -> uuid.UUID:
        audit_id = uuid7()
        await self._conn.execute(
            _INSERT,
            {
                "id": audit_id,
                "ws": self._principal.workspace_id,
                "actor": self._principal.principal_id,
                "action": action,
                "rtype": resource_type,
                "rid": str(resource_id) if resource_id is not None else None,
                "outcome": outcome,
                "req": self._request_id,
                "corr": self._correlation_id,
                "meta": (metadata or AuditMetadata()).model_dump_json(exclude_none=True),
            },
        )
        return audit_id
