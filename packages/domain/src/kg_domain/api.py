"""Response models for the /v1 API (blueprint 03-runtime/01 shape rules). These are the OpenAPI schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from kg_domain.enums import PrincipalKind, PrincipalStatus, Role, WorkspaceStatus


class LiveOut(BaseModel):
    status: Literal["ok"] = "ok"


class ReadyOut(BaseModel):
    status: Literal["ok", "degraded", "down"]
    checks: dict[str, str]


class TokenOut(BaseModel):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"  # noqa: S105 (OAuth token type, not a secret)
    expires_in: int


class WorkspaceOut(BaseModel):
    id: uuid.UUID
    slug: str
    display_name: str
    status: WorkspaceStatus


class PrincipalSummary(BaseModel):
    id: uuid.UUID
    handle: str
    kind: PrincipalKind
    roles: list[Role]


class MeOut(BaseModel):
    principal: PrincipalSummary
    workspace: WorkspaceOut


class KeyInfo(BaseModel):
    prefix: str
    created_at: datetime
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None


class PrincipalOut(BaseModel):
    id: uuid.UUID
    handle: str
    display_name: str
    kind: PrincipalKind
    status: PrincipalStatus
    roles: list[Role]
    keys: list[KeyInfo]
    created_at: datetime
    revoked_at: datetime | None = None


class AuditEventOut(BaseModel):
    id: uuid.UUID
    action: str
    outcome: Literal["allowed", "denied", "error"]
    actor_principal_id: uuid.UUID | None = None
    resource_type: str | None = None
    resource_id: str | None = None
    request_id: str | None = None
    created_at: datetime
    metadata: dict[str, Any]


class WorkerStatus(BaseModel):
    status: Literal["ok", "stale", "none"]
    last_beat_age_seconds: float | None = None
    count: int = 0


class RouterStatus(BaseModel):
    mode: Literal["mock", "live"]
    reachability: Literal["ok", "unreachable", "auth_error", "degraded"]
    missing_aliases: list[str] = []


class OutboxHealth(BaseModel):
    backlog: int
    oldest_pending_age_s: float | None = None
    dead: int


class OperationsHealthOut(BaseModel):
    status: Literal["ok", "degraded", "down"]
    postgres: str
    neo4j: str
    router: RouterStatus
    worker: WorkerStatus
    principal_counts: dict[str, int]
    graph_node_count: int | None = None
    outbox: OutboxHealth | None = None


class RouterCheckOut(BaseModel):
    ok: bool
    alias: str
    dims: int
    latency_ms: float
    router_request_id: str | None = None
