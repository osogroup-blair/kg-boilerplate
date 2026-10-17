"""Workspace settings (phase-8 plan §5.6, ADR-047): retention windows, an egress override, default quotas, the backup retention shown on receipts.

Settings are VERSIONED: every change writes a new row (the latest wins), is a ledger command (`settings.update`, idempotent, audited) and needs the current version in
`If-Match`, so two admins cannot overwrite each other. The egress override can only NARROW the platform policy (it is a deny list that is unioned with the platform's own),
so there is no field through which a workspace could widen what leaves: that is enforced by the shape of the setting, not by a check."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from typing import Literal

from kg_common.errors import IdempotencyMismatch, PreconditionFailed, PreconditionRequired
from kg_domain.principal import Principal
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from kg_governance.audit import AuditMetadata, AuditSink, CommandType

Classification = Literal["public", "internal", "confidential", "restricted", "local_only"]
CLASSIFICATION_ORDER = ("public", "internal", "confidential", "restricted", "local_only")


class _S(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RetentionSettings(_S):
    raw_content_days: int | None = Field(default=None, ge=1, le=36500)
    normalized_content_days: int | None = Field(default=None, ge=1, le=36500)
    episode_content_days: int | None = Field(default=None, ge=1, le=36500)
    context_trace_days: int | None = Field(default=None, ge=1, le=3650)
    model_calls_days: int | None = Field(default=None, ge=1, le=3650)
    memory_archive_after_days: int | None = Field(default=None, ge=1, le=36500)


class EgressSettings(_S):
    denied_classifications: list[Classification] = Field(default_factory=list)

    @field_validator("denied_classifications")
    @classmethod
    def _unique(cls, v: list[str]) -> list[str]:
        return sorted(set(v), key=CLASSIFICATION_ORDER.index)


class QuotaDefaults(_S):
    requests_per_minute: int | None = Field(default=None, ge=1, le=1_000_000)
    context_tokens_per_day: int | None = Field(default=None, ge=1)
    interactions_per_day: int | None = Field(default=None, ge=1)


class WorkspaceSettings(_S):
    retention: RetentionSettings = Field(default_factory=RetentionSettings)
    egress: EgressSettings = Field(default_factory=EgressSettings)
    quotas: QuotaDefaults = Field(default_factory=QuotaDefaults)
    backup_retention_days: int | None = Field(default=None, ge=1, le=3650)


class EffectiveEgress(BaseModel):
    platform_denied: list[str]
    workspace_denied: list[str]
    effective_denied: list[str]
    effective_allowed: list[str]


class SettingsOut(BaseModel):
    version: int
    settings: WorkspaceSettings
    updated_by: uuid.UUID | None = None
    updated_at: dt.datetime | None = None
    effective_egress: EffectiveEgress


class SettingsVersionOut(BaseModel):
    version: int
    updated_by: uuid.UUID | None
    updated_at: dt.datetime
    settings: WorkspaceSettings


class SettingsService:
    def __init__(self, platform_denied: list[str]):
        self._platform_denied = sorted(set(platform_denied), key=CLASSIFICATION_ORDER.index)

    def _effective(self, s: WorkspaceSettings) -> EffectiveEgress:
        denied = sorted({*self._platform_denied, *s.egress.denied_classifications}, key=CLASSIFICATION_ORDER.index)
        return EffectiveEgress(
            platform_denied=self._platform_denied,
            workspace_denied=list(s.egress.denied_classifications),
            effective_denied=denied,
            effective_allowed=[c for c in CLASSIFICATION_ORDER if c not in denied],
        )

    async def get(self, conn: AsyncConnection, ws: uuid.UUID) -> SettingsOut:
        row = (
            await conn.execute(
                text(
                    "SELECT version, settings, updated_by, updated_at FROM workspace_settings WHERE workspace_id = :ws ORDER BY version DESC LIMIT 1"
                ),
                {"ws": ws},
            )
        ).first()
        if row is None:
            s = WorkspaceSettings()
            return SettingsOut(version=0, settings=s, effective_egress=self._effective(s))
        s = WorkspaceSettings.model_validate(row.settings)
        return SettingsOut(
            version=row.version, settings=s, updated_by=row.updated_by, updated_at=row.updated_at, effective_egress=self._effective(s)
        )

    async def history(self, conn: AsyncConnection, ws: uuid.UUID, limit: int = 50) -> list[SettingsVersionOut]:
        rows = (
            await conn.execute(
                text(
                    "SELECT version, settings, updated_by, updated_at FROM workspace_settings WHERE workspace_id = :ws ORDER BY version DESC LIMIT :n"
                ),
                {"ws": ws, "n": max(1, min(limit, 200))},
            )
        ).all()
        return [
            SettingsVersionOut(
                version=r.version, updated_by=r.updated_by, updated_at=r.updated_at, settings=WorkspaceSettings.model_validate(r.settings)
            )
            for r in rows
        ]

    async def put(
        self,
        conn: AsyncConnection,
        ws: uuid.UUID,
        principal: Principal,
        body: WorkspaceSettings,
        *,
        if_match: int | None,
        key: str,
        audit: AuditSink,
    ) -> tuple[SettingsOut, bool]:
        """Returns (settings, replayed). `if_match` is the version the caller read (0 when nothing was ever set)."""
        digest = hashlib.sha256(
            json.dumps(["settings.update", str(principal.principal_id), body.model_dump(mode="json"), if_match], sort_keys=True).encode()
        ).hexdigest()
        await conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"{ws}:cmd:{key}"})
        prior = (
            await conn.execute(
                text("SELECT request_hash, command_type, result FROM commands WHERE workspace_id = :ws AND idempotency_key = :k"),
                {"ws": ws, "k": key},
            )
        ).first()
        if prior is not None:
            if prior.request_hash != digest or prior.command_type != CommandType.SETTINGS_UPDATE.value:
                raise IdempotencyMismatch()
            return await self.get(conn, ws), True
        await conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"{ws}:settings"}
        )  # one writer per workspace
        current = (
            await conn.execute(text("SELECT COALESCE(max(version), 0) FROM workspace_settings WHERE workspace_id = :ws"), {"ws": ws})
        ).scalar_one()
        if if_match is None:
            raise PreconditionRequired()
        if if_match != current:
            raise PreconditionFailed()
        version = int(current) + 1
        await conn.execute(
            text("INSERT INTO workspace_settings (workspace_id, version, settings, updated_by) VALUES (:ws, :v, CAST(:s AS jsonb), :by)"),
            {"ws": ws, "v": version, "s": body.model_dump_json(), "by": principal.principal_id},
        )
        await conn.execute(
            text(
                "INSERT INTO commands (id, workspace_id, idempotency_key, request_hash, command_type, actor_principal_id, authority_class, classification, source_system, source_ref, "
                "ontology_version_id, payload, status, decision, result, http_status, projection_status) VALUES (gen_random_uuid(), :ws, :k, :h, 'settings.update', :actor, "
                "'admin_asserted', 'internal', 'settings', :ref, NULL, CAST(:p AS jsonb), 'applied', '[]'::jsonb, CAST(:r AS jsonb), 200, 'not_applicable')"
            ),
            {
                "ws": ws,
                "k": key,
                "h": digest,
                "actor": principal.principal_id,
                "ref": str(version),
                "p": json.dumps({"version": version, "sha256": hashlib.sha256(body.model_dump_json().encode()).hexdigest()}),
                "r": json.dumps({"version": version}),
            },
        )
        await audit.record(
            "settings.updated",
            resource_type="workspace_settings",
            resource_id=str(version),
            metadata=AuditMetadata(settings_version=version),
        )
        return await self.get(conn, ws), False
