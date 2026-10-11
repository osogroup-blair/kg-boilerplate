"""Append-only audit writer. Metadata is a closed model: IDs, counts, hashes and enums only; free text cannot be stored."""

from __future__ import annotations

import enum
import uuid
from typing import Literal

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


Outcome = Literal["allowed", "denied", "error"]

_INSERT = text("""
    INSERT INTO audit_log (id, workspace_id, actor_principal_id, action, resource_type, resource_id, outcome, request_id, correlation_id, metadata)
    VALUES (:id, :ws, :actor, :action, :rtype, :rid, :outcome, :req, :corr, CAST(:meta AS jsonb))""")


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
