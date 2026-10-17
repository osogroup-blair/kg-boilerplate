"""Agent and app registration (phase-7 plan §5.1, §6.2, ADR-036): register, update, mint and revoke keys, add and remove grants, read the activity view.

Everything runs inside the CALLER's tenant transaction: the principal rows, the ledger command (`principal.*`, `key.*`, `grant.*`), the audit entry and the key all commit
together or not at all. A registration creates `agent` and `app` principals only: users and service principals stay seed / CLI-managed until human identity moves to
OIDC (P9). The role of a new principal is fixed by its kind (agents get `agent`, apps get `app_writer`); P7 does not let a registration choose a role, so a registered
principal can never be a workspace admin or a reviewer.

The plaintext of a new key is in the return value ONCE. A replay of the same Idempotency-Key answers with the stored result, which never held the plaintext.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import uuid
from typing import Annotated, Any, Literal

from kg_common.errors import Conflict, IdempotencyMismatch, NotFound, PreconditionFailed, PreconditionRequired, ValidationFailed
from kg_common.ids import uuid7
from kg_domain.enums import PrincipalKind, Role
from kg_domain.principal import Principal, WorkspaceContext
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection

from kg_governance.audit import AuditMetadata, AuditWriter, CommandType
from kg_governance.authn.keys import mint_key
from kg_governance.grants import CLASSIFICATION_ORDER, PRIVILEGES

HANDLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,62}$")
PROJECT_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
REGISTRABLE_KINDS = (PrincipalKind.AGENT, PrincipalKind.APP)
ROLE_OF_KIND: dict[PrincipalKind, Role] = {PrincipalKind.AGENT: Role.AGENT, PrincipalKind.APP: Role.APP_WRITER}
ProfileName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
Privilege = Literal["read", "propose", "publish"]
GrantKind = Literal["subject", "project", "workspace", "classification"]
Disclosure = Literal["allowed", "denied"]
RevokeReason = Literal["retired", "compromised"]


# ---- requests ------------------------------------------------------------------------------------------------------------------------------


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _default_privileges() -> list[Privilege]:
    return ["read", "propose"]


class GrantIn(_In):
    scope_kind: GrantKind
    scope_ref: str = Field(min_length=1, max_length=200)
    privileges: list[Privilege] = Field(default_factory=lambda: _default_privileges(), min_length=1, max_length=3)

    @model_validator(mode="after")
    def _check(self) -> GrantIn:
        if self.scope_kind == "classification" and "privileges" not in self.model_fields_set:
            self.privileges = ["read"]  # a ceiling is a read grant: say so by default
        self.privileges = sorted(set(self.privileges), key=PRIVILEGES.index)
        if self.scope_kind == "classification":
            if self.scope_ref not in CLASSIFICATION_ORDER:
                raise ValueError("a classification grant names one of the classification levels")
            if self.privileges != ["read"]:
                raise ValueError("a classification grant is a read ceiling: its only privilege is read")
        elif self.scope_kind == "workspace":
            if self.scope_ref != "*":
                raise ValueError("a workspace grant is workspace:*")
        elif self.scope_kind == "project":
            if self.scope_ref != "*" and not PROJECT_KEY.match(self.scope_ref):
                raise ValueError("a project grant names a project key (or *)")
        elif self.scope_ref != "*":
            try:
                uuid.UUID(self.scope_ref)
            except ValueError as exc:
                raise ValueError("a subject grant names an entity id (or *)") from exc
        if "read" not in self.privileges:
            raise ValueError("propose and publish need read: a principal cannot write into a scope it cannot see")
        return self


class QuotasIn(_In):
    requests_per_minute: int | None = Field(default=None, ge=1, le=1_000_000)
    context_tokens_per_day: int | None = Field(default=None, ge=1)
    interactions_per_day: int | None = Field(default=None, ge=1)


class RegisterIn(_In):
    kind: Literal["agent", "app"]
    handle: str = Field(min_length=1, max_length=63, pattern=HANDLE.pattern)
    display_name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=500)
    owner_principal_id: uuid.UUID | None = None
    trusted_source: bool = False
    model_disclosure: Disclosure = "allowed"
    allowed_profiles: list[ProfileName] = Field(default_factory=list, max_length=32)
    quotas: QuotasIn = Field(default_factory=QuotasIn)
    grants: list[GrantIn] = Field(default_factory=list, max_length=100)
    key_name: str = Field(default="initial", max_length=100)
    key_expires_in_seconds: int | None = Field(default=None, ge=60, le=10 * 365 * 86400)

    @model_validator(mode="after")
    def _check(self) -> RegisterIn:
        if self.trusted_source and self.kind != "app":
            raise ValueError("only an app can be a trusted source")
        return self


class UpdateIn(_In):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=500)
    owner_principal_id: uuid.UUID | None = None
    trusted_source: bool | None = None
    model_disclosure: Disclosure | None = None
    allowed_profiles: list[ProfileName] | None = Field(default=None, max_length=32)
    quotas: QuotasIn | None = None


class KeyIn(_In):
    name: str = Field(default="", max_length=100)
    expires_in_seconds: int | None = Field(default=None, ge=60, le=10 * 365 * 86400)


class RevokeIn(_In):
    reason: RevokeReason
    note: str | None = Field(default=None, max_length=500)  # stored on the ledger command's result only: never in audit, logs or metrics


# ---- responses -----------------------------------------------------------------------------------------------------------------------------


class QuotasOut(BaseModel):
    requests_per_minute: int | None = None
    context_tokens_per_day: int | None = None
    interactions_per_day: int | None = None


class PrincipalGrantOut(BaseModel):
    id: uuid.UUID
    principal_id: uuid.UUID
    scope_kind: str
    scope_ref: str
    privileges: list[str]
    created_at: dt.datetime


class KeyOut(BaseModel):
    id: uuid.UUID
    name: str
    prefix: str
    created_at: dt.datetime
    expires_at: dt.datetime | None = None
    revoked_at: dt.datetime | None = None
    last_used_at: dt.datetime | None = None


class MintedKeyOut(KeyOut):
    plaintext: str | None = Field(default=None, description="The key, shown once. Null on an idempotent replay.")


class RegisteredPrincipalOut(BaseModel):
    id: uuid.UUID
    kind: str
    handle: str
    display_name: str
    description: str
    status: str
    roles: list[str]
    owner_principal_id: uuid.UUID | None = None
    trusted_source: bool
    model_disclosure: str
    allowed_profiles: list[str]
    quotas: QuotasOut
    revoke_reason: str | None = None
    revoked_at: dt.datetime | None = None
    revoked_by: uuid.UUID | None = None
    created_at: dt.datetime
    updated_at: dt.datetime
    revision: int


class RegisteredOut(BaseModel):
    principal: RegisteredPrincipalOut
    key: MintedKeyOut
    grants: list[PrincipalGrantOut]


class RevokeOut(BaseModel):
    principal: RegisteredPrincipalOut
    already_revoked: bool = False
    keys_revoked: int = 0
    working_memories_expired: int = 0
    private_memories_frozen: int = 0
    proposals_rejected: int = 0
    shared_memories_disputed: int = 0
    stages_skipped: int = 0
    facts_disputed: int = 0
    review_items_opened: int = 0


class ActivityOut(BaseModel):
    """Ids and counts only (T11): never memory content from a private scope."""

    principal_id: uuid.UUID
    window_days: int
    last_seen_at: dt.datetime | None
    interactions: dict[str, int]
    proposals: dict[str, int]
    published_findings: int
    verified_findings: int
    context_builds: int
    context_tokens: int
    context_profiles: dict[str, int]
    denials: int


class UsageDay(BaseModel):
    day: dt.date
    requests: int
    context_builds: int
    context_tokens: int
    interactions: int
    proposals: int


class UsageOut(BaseModel):
    day: dt.date
    requests: int
    context_builds: int
    context_tokens: int
    interactions: int
    proposals: int
    quotas: QuotasOut
    defaults: QuotasOut
    history: list[UsageDay] = []  # the previous days that were flushed to agent_usage_daily, newest first (the Usage tab's chart)


# ---- helpers -------------------------------------------------------------------------------------------------------------------------------

_SELECT = (
    "SELECT p.id, p.kind, p.handle, p.display_name, p.description, p.status, p.owner_principal_id, p.trusted_source, p.model_disclosure, p.allowed_profiles, "
    "p.quota_requests_per_minute, p.quota_context_tokens_per_day, p.quota_interactions_per_day, p.revoke_reason, p.revoked_at, p.revoked_by, "
    "p.created_at, p.updated_at, p.revision, "
    "COALESCE((SELECT array_agg(r.role ORDER BY r.role) FROM role_assignments r WHERE r.principal_id = p.id AND r.workspace_id = p.workspace_id), ARRAY[]::text[]) AS roles "
    "FROM principals p"
)


def principal_out(r: Row[Any]) -> RegisteredPrincipalOut:
    return RegisteredPrincipalOut(
        id=r.id,
        kind=r.kind,
        handle=r.handle,
        display_name=r.display_name,
        description=r.description,
        status=r.status,
        roles=list(r.roles),
        owner_principal_id=r.owner_principal_id,
        trusted_source=r.trusted_source,
        model_disclosure=r.model_disclosure,
        allowed_profiles=list(r.allowed_profiles),
        quotas=QuotasOut(
            requests_per_minute=r.quota_requests_per_minute,
            context_tokens_per_day=r.quota_context_tokens_per_day,
            interactions_per_day=r.quota_interactions_per_day,
        ),
        revoke_reason=r.revoke_reason,
        revoked_at=r.revoked_at,
        revoked_by=r.revoked_by,
        created_at=r.created_at,
        updated_at=r.updated_at,
        revision=r.revision,
    )


def _grant_out(r: Row[Any]) -> PrincipalGrantOut:
    return PrincipalGrantOut(
        id=r.id,
        principal_id=r.principal_id,
        scope_kind=r.scope_kind,
        scope_ref=r.scope_ref,
        privileges=list(r.privileges),
        created_at=r.created_at,
    )


def _digest(command_type: CommandType, principal_id: uuid.UUID, body: Any) -> str:
    raw = json.dumps([command_type.value, str(principal_id), body], sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


class PrincipalNotActive(Conflict):
    code, default_message = "principal_revoked", "The principal is revoked: a revocation is final (register a replacement)"


class HandleTaken(Conflict):
    code, default_message = "handle_taken", "That handle is already registered in this workspace"


class Registration:
    """The write side of Agents & Access. Constructed per request around the caller's transaction."""

    def __init__(self, conn: AsyncConnection, ctx: WorkspaceContext, actor: Principal, audit: AuditWriter, *, pepper: str):
        self._conn, self._ctx, self._actor, self._audit, self._pepper = conn, ctx, actor, audit, pepper

    @property
    def ws(self) -> uuid.UUID:
        return self._ctx.workspace_id

    # ---- ledger ---------------------------------------------------------------------------------------------------------------------------

    async def _replay(self, key: str, command_type: CommandType, digest: str) -> dict[str, Any] | None:
        await self._conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"{self.ws}:cmd:{key}"})
        prior = (
            await self._conn.execute(
                text("SELECT command_type, request_hash, result FROM commands WHERE workspace_id = :ws AND idempotency_key = :k"),
                {"ws": self.ws, "k": key},
            )
        ).first()
        if prior is None:
            return None
        if prior.request_hash != digest or prior.command_type != command_type.value:
            raise IdempotencyMismatch()
        return dict(prior.result)

    async def _record(
        self, command_type: CommandType, key: str, digest: str, payload: dict[str, Any], result: dict[str, Any], http_status: int
    ) -> uuid.UUID:
        command_id = uuid7()
        await self._conn.execute(
            text(
                "INSERT INTO commands (id, workspace_id, idempotency_key, request_hash, command_type, actor_principal_id, authority_class, classification, "
                "source_system, source_ref, ontology_version_id, payload, status, decision, result, http_status, projection_status) "
                "VALUES (:id, :ws, :key, :h, :ct, :actor, 'admin_asserted', 'internal', 'platform', :sr, NULL, CAST(:payload AS jsonb), 'applied', '[]'::jsonb, "
                "CAST(:result AS jsonb), :http, 'not_applicable')"
            ),
            {
                "id": command_id,
                "ws": self.ws,
                "key": key,
                "h": digest,
                "ct": command_type.value,
                "actor": self._actor.principal_id,
                "sr": command_type.value,
                "payload": json.dumps(payload, default=str),
                "result": json.dumps(result, default=str),
                "http": http_status,
            },
        )
        return command_id

    # ---- reads ----------------------------------------------------------------------------------------------------------------------------

    async def row(self, principal_id: uuid.UUID, *, lock: bool = False) -> Row[Any]:
        row = (
            await self._conn.execute(
                text(f"{_SELECT} WHERE p.workspace_id = :ws AND p.id = :id{' FOR UPDATE OF p' if lock else ''}"),  # noqa: S608 (constant fragments)
                {"ws": self.ws, "id": principal_id},
            )
        ).first()
        if row is None:
            raise NotFound()  # another workspace's principal is invisible under RLS: the same answer as an id that never existed
        return row

    async def grants(self, principal_id: uuid.UUID) -> list[PrincipalGrantOut]:
        await self.row(principal_id)
        rows = (
            await self._conn.execute(
                text(
                    "SELECT id, principal_id, scope_kind, scope_ref, privileges, created_at FROM principal_scopes WHERE workspace_id = :ws AND principal_id = :p "
                    "ORDER BY scope_kind, scope_ref"
                ),
                {"ws": self.ws, "p": principal_id},
            )
        ).all()
        return [_grant_out(r) for r in rows]

    async def keys(self, principal_id: uuid.UUID) -> list[KeyOut]:
        await self.row(principal_id)
        rows = (
            await self._conn.execute(
                text(
                    "SELECT id, name, prefix, created_at, expires_at, revoked_at, last_used_at FROM api_keys WHERE workspace_id = :ws AND principal_id = :p "
                    "ORDER BY created_at, id"
                ),
                {"ws": self.ws, "p": principal_id},
            )
        ).all()
        return [KeyOut(**dict(r._mapping)) for r in rows]

    async def activity(self, principal_id: uuid.UUID, *, window_days: int = 30) -> ActivityOut:
        """Counts and ids for the last `window_days` (T11): what an admin needs to oversee an agent, never memory content from a private scope."""
        await self.row(principal_id)
        c, p, ws = self._conn, principal_id, self.ws
        since = "now() - make_interval(days => :days)"
        params = {"ws": ws, "p": p, "days": window_days}

        async def counts(sql: str) -> dict[str, int]:
            return {r.k: int(r.n) for r in (await c.execute(text(sql), params)).all()}

        interactions = await counts(
            f"SELECT processing_status AS k, count(*) AS n FROM interactions WHERE workspace_id = :ws AND submitted_by = :p AND recorded_at >= {since} GROUP BY 1"  # noqa: S608
        )
        proposals = await counts(
            f"SELECT status AS k, count(*) AS n FROM curated_memories WHERE workspace_id = :ws AND created_by = :p AND origin = 'proposal' AND recorded_at >= {since} GROUP BY 1"  # noqa: S608
        )
        profiles = await counts(
            f"SELECT profile AS k, count(*) AS n FROM context_traces WHERE workspace_id = :ws AND principal_id = :p AND act_as_by IS NULL AND created_at >= {since} GROUP BY 1"  # noqa: S608
        )
        row = (
            await c.execute(
                text(
                    f"SELECT (SELECT count(*) FROM curated_memories WHERE workspace_id = :ws AND created_by = :p AND origin = 'proposal' AND status = 'active' AND recorded_at >= {since}) AS published, "  # noqa: S608
                    f"(SELECT count(*) FROM curated_memories WHERE workspace_id = :ws AND created_by = :p AND origin = 'proposal' AND status = 'active' AND evidence_status = 'verified' AND recorded_at >= {since}) AS verified, "
                    f"(SELECT COALESCE(sum(used_tokens), 0) FROM context_traces WHERE workspace_id = :ws AND principal_id = :p AND act_as_by IS NULL AND created_at >= {since}) AS tokens, "
                    f"(SELECT count(*) FROM audit_log WHERE workspace_id = :ws AND actor_principal_id = :p AND outcome = 'denied' AND created_at >= {since}) AS denials, "
                    "(SELECT max(last_used_at) FROM api_keys WHERE workspace_id = :ws AND principal_id = :p) AS last_seen"
                ),
                params,
            )
        ).one()
        return ActivityOut(
            principal_id=p,
            window_days=window_days,
            last_seen_at=row.last_seen,
            interactions=interactions,
            proposals=proposals,
            published_findings=int(row.published),
            verified_findings=int(row.verified),
            context_builds=sum(profiles.values()),
            context_tokens=int(row.tokens),
            context_profiles=profiles,
            denials=int(row.denials),
        )

    # ---- validation -----------------------------------------------------------------------------------------------------------------------

    async def _check_grant(self, g: GrantIn) -> None:
        """A subject grant must name an entity of THIS workspace (404 otherwise: the same answer for a foreign id and one that never existed)."""
        if g.scope_kind == "subject" and g.scope_ref != "*":
            found = (
                await self._conn.execute(
                    text("SELECT 1 FROM entities WHERE workspace_id = :ws AND id = :id AND status NOT IN ('deleted','deletion_pending')"),
                    {"ws": self.ws, "id": uuid.UUID(g.scope_ref)},
                )
            ).first()
            if found is None:
                raise NotFound()

    async def _check_owner(self, owner: uuid.UUID | None) -> None:
        if owner is None:
            return
        found = (
            await self._conn.execute(
                text("SELECT 1 FROM principals WHERE workspace_id = :ws AND id = :id AND status = 'active'"), {"ws": self.ws, "id": owner}
            )
        ).first()
        if found is None:
            raise ValidationFailed(
                "owner_principal_id does not name an active principal of this workspace", details={"fields": ["owner_principal_id"]}
            )

    async def _insert_grant(self, principal_id: uuid.UUID, g: GrantIn) -> tuple[uuid.UUID, bool]:
        gid = uuid7()
        res = await self._conn.execute(
            text(
                "INSERT INTO principal_scopes (id, principal_id, workspace_id, scope_kind, scope_ref, privileges) VALUES (:id, :p, :ws, :k, :r, :privs) "
                "ON CONFLICT (workspace_id, principal_id, scope_kind, scope_ref) DO UPDATE SET privileges = EXCLUDED.privileges "
                "WHERE principal_scopes.privileges IS DISTINCT FROM EXCLUDED.privileges RETURNING id"
            ),
            {"id": gid, "p": principal_id, "ws": self.ws, "k": g.scope_kind, "r": g.scope_ref, "privs": list(g.privileges)},
        )
        row = res.first()
        if row is None:  # the same grant already stood
            existing = (
                await self._conn.execute(
                    text(
                        "SELECT id FROM principal_scopes WHERE workspace_id = :ws AND principal_id = :p AND scope_kind = :k AND scope_ref = :r"
                    ),
                    {"ws": self.ws, "p": principal_id, "k": g.scope_kind, "r": g.scope_ref},
                )
            ).scalar_one()
            return existing, False
        return row.id, row.id == gid

    async def _mint(self, principal_id: uuid.UUID, name: str, expires_in_seconds: int | None) -> MintedKeyOut:
        minted = mint_key(self._pepper)
        key_id = uuid7()
        row = (
            await self._conn.execute(
                text(
                    "INSERT INTO api_keys (id, workspace_id, principal_id, prefix, key_hash, created_by, name, expires_at) "
                    "VALUES (:id, :ws, :p, :prefix, :hash, :by, :name, CASE WHEN CAST(:ttl AS integer) IS NULL THEN NULL ELSE now() + make_interval(secs => :ttl) END) "
                    "RETURNING created_at, expires_at"
                ),
                {
                    "id": key_id,
                    "ws": self.ws,
                    "p": principal_id,
                    "prefix": minted.prefix,
                    "hash": minted.key_hash,
                    "by": self._actor.principal_id,
                    "name": name,
                    "ttl": expires_in_seconds,
                },
            )
        ).one()
        return MintedKeyOut(
            id=key_id,
            name=name,
            prefix=minted.prefix,
            created_at=row.created_at,
            expires_at=row.expires_at,
            plaintext=minted.plaintext,
        )

    # ---- register -------------------------------------------------------------------------------------------------------------------------

    async def register(self, body: RegisterIn, key: str) -> tuple[RegisteredOut, int, bool]:
        """Returns (answer, http status, replayed). A replay answers 200 with the stored principal and grants and NO key plaintext."""
        digest = _digest(CommandType.PRINCIPAL_REGISTER, self._actor.principal_id, body.model_dump(mode="json"))
        prior = await self._replay(key, CommandType.PRINCIPAL_REGISTER, digest)
        if prior is not None:
            stored = RegisteredOut.model_validate(prior["out"])
            stored.key.plaintext = None
            return stored, 200, True
        kind = PrincipalKind(body.kind)
        if (
            await self._conn.execute(
                text("SELECT 1 FROM principals WHERE workspace_id = :ws AND handle = :h"), {"ws": self.ws, "h": body.handle}
            )
        ).first():
            raise HandleTaken()
        await self._check_owner(body.owner_principal_id)
        for g in body.grants:
            await self._check_grant(g)
        pid = uuid7()
        defaults = (  # P8: the workspace's default quotas fill what the registration leaves unset
            await self._conn.execute(
                text("SELECT settings -> 'quotas' AS q FROM workspace_settings WHERE workspace_id = :ws ORDER BY version DESC LIMIT 1"),
                {"ws": self.ws},
            )
        ).scalar_one_or_none() or {}
        await self._conn.execute(
            text(
                "INSERT INTO principals (id, workspace_id, kind, handle, display_name, description, owner_principal_id, trusted_source, model_disclosure, "
                "allowed_profiles, quota_requests_per_minute, quota_context_tokens_per_day, quota_interactions_per_day) "
                "VALUES (:id, :ws, :kind, :h, :name, :descr, :owner, :trusted, :disc, :profiles, :q1, :q2, :q3)"
            ),
            {
                "id": pid,
                "ws": self.ws,
                "kind": kind.value,
                "h": body.handle,
                "name": body.display_name,
                "descr": body.description,
                "owner": body.owner_principal_id,
                "trusted": body.trusted_source,
                "disc": body.model_disclosure,
                "profiles": sorted(set(body.allowed_profiles)),
                "q1": body.quotas.requests_per_minute
                if body.quotas.requests_per_minute is not None
                else defaults.get("requests_per_minute"),
                "q2": body.quotas.context_tokens_per_day
                if body.quotas.context_tokens_per_day is not None
                else defaults.get("context_tokens_per_day"),
                "q3": body.quotas.interactions_per_day
                if body.quotas.interactions_per_day is not None
                else defaults.get("interactions_per_day"),
            },
        )
        await self._conn.execute(
            text("INSERT INTO role_assignments (principal_id, workspace_id, role, granted_by) VALUES (:p, :ws, :r, :by)"),
            {"p": pid, "ws": self.ws, "r": ROLE_OF_KIND[kind].value, "by": self._actor.principal_id},
        )
        grants: list[PrincipalGrantOut] = []
        for g in body.grants:
            await self._insert_grant(pid, g)
        first_key = await self._mint(pid, body.key_name, body.key_expires_in_seconds)
        grants = await self.grants(pid)
        out = RegisteredOut(principal=principal_out(await self.row(pid)), key=first_key, grants=grants)
        stored = out.model_copy(deep=True)
        stored.key.plaintext = None  # the ledger never holds a secret
        command_id = await self._record(
            CommandType.PRINCIPAL_REGISTER,
            key,
            digest,
            {"principal_id": str(pid), "kind": body.kind, "grants": len(body.grants)},
            {"out": stored.model_dump(mode="json")},
            201,
        )
        _ = command_id
        await self._audit.record(
            "principal.registered",
            resource_type="principal",
            resource_id=pid,
            metadata=AuditMetadata(
                target_id=pid, command_type=CommandType.PRINCIPAL_REGISTER, principal_kind=body.kind, count=len(body.grants)
            ),
        )
        await self._audit.record(
            "key.created",
            resource_type="api_key",
            resource_id=first_key.id,
            metadata=AuditMetadata(
                key_id=first_key.id, key_prefix=first_key.prefix, target_id=pid, command_type=CommandType.PRINCIPAL_REGISTER
            ),
        )
        for g in body.grants:
            await self._audit.record(
                "grant.added",
                resource_type="principal",
                resource_id=pid,
                metadata=AuditMetadata(target_id=pid, scope_kind=g.scope_kind, privileges=list(g.privileges)),
            )
        return out, 201, False

    # ---- update ---------------------------------------------------------------------------------------------------------------------------

    async def update(self, principal_id: uuid.UUID, body: UpdateIn, key: str, if_match: int | None) -> RegisteredPrincipalOut:
        digest = _digest(CommandType.PRINCIPAL_UPDATE, self._actor.principal_id, [str(principal_id), body.model_dump(mode="json")])
        prior = await self._replay(key, CommandType.PRINCIPAL_UPDATE, digest)
        if prior is not None:
            return RegisteredPrincipalOut.model_validate(prior["out"])
        row = await self.row(principal_id, lock=True)
        if row.kind not in {k.value for k in REGISTRABLE_KINDS}:
            raise NotFound()  # users and service principals are not managed here: not a registration
        if if_match is None:
            raise PreconditionRequired()
        if if_match != row.revision:
            raise PreconditionFailed()
        if row.status == "revoked":
            raise PrincipalNotActive()
        sets: list[str] = []
        params: dict[str, Any] = {"ws": self.ws, "id": principal_id}
        fields = body.model_fields_set
        for column, field in (("display_name", "display_name"), ("description", "description"), ("model_disclosure", "model_disclosure")):
            if field in fields and getattr(body, field) is not None:
                sets.append(f"{column} = :{column}")
                params[column] = getattr(body, field)
        if "trusted_source" in fields and body.trusted_source is not None:
            if body.trusted_source and row.kind != "app":
                raise ValidationFailed("only an app can be a trusted source", details={"fields": ["trusted_source"]})
            sets.append("trusted_source = :trusted")
            params["trusted"] = body.trusted_source
        if "owner_principal_id" in fields:
            await self._check_owner(body.owner_principal_id)
            sets.append("owner_principal_id = :owner")
            params["owner"] = body.owner_principal_id
        if "allowed_profiles" in fields and body.allowed_profiles is not None:
            sets.append("allowed_profiles = :profiles")
            params["profiles"] = sorted(set(body.allowed_profiles))
        if "quotas" in fields and body.quotas is not None:
            for field, column in (
                ("requests_per_minute", "quota_requests_per_minute"),
                ("context_tokens_per_day", "quota_context_tokens_per_day"),
                ("interactions_per_day", "quota_interactions_per_day"),
            ):
                if field in body.quotas.model_fields_set:  # an explicit null clears the quota back to the platform default
                    sets.append(f"{column} = :{column}")
                    params[column] = getattr(body.quotas, field)
        if sets:
            sql = f"UPDATE principals SET {', '.join(sets)}, updated_at = now(), revision = revision + 1 WHERE workspace_id = :ws AND id = :id"  # noqa: S608
            await self._conn.execute(text(sql), params)
        out = principal_out(await self.row(principal_id))
        await self._record(
            CommandType.PRINCIPAL_UPDATE,
            key,
            digest,
            {"principal_id": str(principal_id), "fields": sorted(fields)},
            {"out": out.model_dump(mode="json")},
            200,
        )
        await self._audit.record(
            "principal.updated",
            resource_type="principal",
            resource_id=principal_id,
            metadata=AuditMetadata(target_id=principal_id, command_type=CommandType.PRINCIPAL_UPDATE, count=len(fields)),
        )
        return out

    # ---- keys -----------------------------------------------------------------------------------------------------------------------------

    async def create_key(self, principal_id: uuid.UUID, body: KeyIn, key: str) -> tuple[MintedKeyOut, bool]:
        digest = _digest(CommandType.KEY_CREATE, self._actor.principal_id, [str(principal_id), body.model_dump(mode="json")])
        prior = await self._replay(key, CommandType.KEY_CREATE, digest)
        if prior is not None:
            stored = MintedKeyOut.model_validate(prior["out"])
            stored.plaintext = None
            return stored, True
        row = await self.row(principal_id, lock=True)
        if row.kind not in {k.value for k in REGISTRABLE_KINDS}:
            raise NotFound()
        if row.status == "revoked":
            raise PrincipalNotActive()
        minted = await self._mint(principal_id, body.name, body.expires_in_seconds)
        stored = minted.model_copy()
        stored.plaintext = None
        await self._record(
            CommandType.KEY_CREATE,
            key,
            digest,
            {"principal_id": str(principal_id), "key_id": str(minted.id)},
            {"out": stored.model_dump(mode="json")},
            201,
        )
        await self._audit.record(
            "key.created",
            resource_type="api_key",
            resource_id=minted.id,
            metadata=AuditMetadata(key_id=minted.id, key_prefix=minted.prefix, target_id=principal_id, command_type=CommandType.KEY_CREATE),
        )
        return minted, False

    async def revoke_key(self, principal_id: uuid.UUID, key_id: uuid.UUID, key: str) -> KeyOut:
        digest = _digest(CommandType.KEY_REVOKE, self._actor.principal_id, [str(principal_id), str(key_id)])
        prior = await self._replay(key, CommandType.KEY_REVOKE, digest)
        if prior is not None:
            return KeyOut.model_validate(prior["out"])
        await self.row(principal_id)
        found = (
            await self._conn.execute(
                text("SELECT id FROM api_keys WHERE workspace_id = :ws AND principal_id = :p AND id = :k FOR UPDATE"),
                {"ws": self.ws, "p": principal_id, "k": key_id},
            )
        ).first()
        if found is None:
            raise NotFound()
        await self._conn.execute(
            text("UPDATE api_keys SET revoked_at = COALESCE(revoked_at, now()) WHERE workspace_id = :ws AND id = :k"),
            {"ws": self.ws, "k": key_id},
        )
        out = next(k for k in await self.keys(principal_id) if k.id == key_id)
        await self._record(
            CommandType.KEY_REVOKE,
            key,
            digest,
            {"principal_id": str(principal_id), "key_id": str(key_id)},
            {"out": out.model_dump(mode="json")},
            200,
        )
        await self._audit.record(
            "key.revoked",
            resource_type="api_key",
            resource_id=key_id,
            metadata=AuditMetadata(key_id=key_id, key_prefix=out.prefix, target_id=principal_id, command_type=CommandType.KEY_REVOKE),
        )
        return out

    # ---- grants ---------------------------------------------------------------------------------------------------------------------------

    async def add_grant(self, principal_id: uuid.UUID, body: GrantIn, key: str) -> PrincipalGrantOut:
        digest = _digest(CommandType.GRANT_ADD, self._actor.principal_id, [str(principal_id), body.model_dump(mode="json")])
        prior = await self._replay(key, CommandType.GRANT_ADD, digest)
        if prior is not None:
            return PrincipalGrantOut.model_validate(prior["out"])
        row = await self.row(principal_id, lock=True)
        if row.status == "revoked":
            raise PrincipalNotActive()
        await self._check_grant(body)
        gid, created = await self._insert_grant(principal_id, body)
        out = next(g for g in await self.grants(principal_id) if g.id == gid)
        await self._record(
            CommandType.GRANT_ADD,
            key,
            digest,
            {"principal_id": str(principal_id), "scope_kind": body.scope_kind},
            {"out": out.model_dump(mode="json")},
            201 if created else 200,
        )
        await self._audit.record(
            "grant.added",
            resource_type="principal",
            resource_id=principal_id,
            metadata=AuditMetadata(
                target_id=principal_id, scope_kind=body.scope_kind, privileges=list(body.privileges), command_type=CommandType.GRANT_ADD
            ),
        )
        return out

    async def remove_grant(self, principal_id: uuid.UUID, grant_id: uuid.UUID, key: str) -> None:
        digest = _digest(CommandType.GRANT_REMOVE, self._actor.principal_id, [str(principal_id), str(grant_id)])
        prior = await self._replay(key, CommandType.GRANT_REMOVE, digest)
        if prior is not None:
            return
        await self.row(principal_id)
        row = (
            await self._conn.execute(
                text(
                    "DELETE FROM principal_scopes WHERE workspace_id = :ws AND principal_id = :p AND id = :g RETURNING scope_kind, privileges"
                ),
                {"ws": self.ws, "p": principal_id, "g": grant_id},
            )
        ).first()
        if row is None:
            raise NotFound()
        await self._record(
            CommandType.GRANT_REMOVE, key, digest, {"principal_id": str(principal_id), "scope_kind": row.scope_kind}, {"removed": True}, 200
        )
        await self._audit.record(
            "grant.removed",
            resource_type="principal",
            resource_id=principal_id,
            metadata=AuditMetadata(
                target_id=principal_id, scope_kind=row.scope_kind, privileges=list(row.privileges), command_type=CommandType.GRANT_REMOVE
            ),
        )
