"""Credential -> Principal. The workspace comes from the key record / verified claim, never from request input (L4)."""

from __future__ import annotations

import time
import uuid
from typing import Any

from kg_common.errors import Unauthenticated
from kg_db.session import system_session, tenant_session
from kg_domain.enums import PrincipalKind, Role
from kg_domain.principal import AuthMethod, Principal, WorkspaceContext
from kg_obs import context, metrics
from kg_obs.logging import get_logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from kg_governance.audit import AuditWriter
from kg_governance.authn import keys
from kg_governance.authn.jwt import LocalSigner, TokenClaims, TokenError
from kg_governance.authn.oidc import OidcError, OidcVerifier

log = get_logger("kg.authn")
_TOUCH_INTERVAL_S = 300.0


class Authenticator:
    def __init__(
        self,
        engine: AsyncEngine,
        *,
        pepper: str,
        signer: LocalSigner,
        oidc: OidcVerifier | None = None,
        production: bool = False,
        allow_breakglass: bool = False,
    ):
        self._engine, self._pepper, self._signer = engine, pepper, signer
        self._oidc, self._production, self._allow_breakglass = oidc, production, allow_breakglass
        self._dummy_hash = keys.hash_key(pepper, "kgk_none_none")
        self._last_touch: dict[uuid.UUID, float] = {}

    @property
    def signer(self) -> LocalSigner:
        return self._signer

    @property
    def oidc(self) -> OidcVerifier | None:
        return self._oidc

    def _fail(self, reason: str) -> Unauthenticated:
        metrics.auth_failures_total.labels(reason=reason).inc()
        log.warning("auth_failed", reason=reason)
        return Unauthenticated("Authentication failed")  # one generic message for every cause

    async def authenticate(self, authorization: str | None) -> Principal:
        if not authorization:
            raise self._fail("missing")
        scheme, _, credential = authorization.partition(" ")
        credential = credential.strip()
        if scheme.lower() != "bearer" or not credential:
            raise self._fail("malformed")
        if keys.looks_like_key(credential):
            return await self._from_api_key(credential)
        if self._oidc is not None and self._oidc.looks_like_issuer(credential):
            return await self._from_oidc(credential)
        return await self._from_jwt(credential)

    # ---- API key -------------------------------------------------------------------------------------------------------------------
    async def _from_api_key(self, presented: str) -> Principal:
        prefix = keys.parse_prefix(presented)
        async with system_session(self._engine) as conn:
            row = (await conn.execute(text("SELECT * FROM auth.lookup_credential(:p)"), {"p": prefix or "invalid"})).first()
        # Always hash and compare, even for an unknown prefix, so timing does not reveal which prefixes exist.
        candidate = keys.hash_key(self._pepper, presented)
        ok = keys.constant_time_equal(candidate, row.key_hash if row else self._dummy_hash)
        if prefix is None:
            raise self._fail("malformed")
        if row is None:
            raise self._fail("unknown_key")
        if not ok:
            raise self._fail("bad_secret")
        self._check_status(row)
        principal = self._principal(row, key_id=row.key_id, method=AuthMethod.API_KEY)
        if self._production and principal.kind is PrincipalKind.USER:
            await self._breakglass(principal)
        await self._touch(row.key_id)
        return principal

    async def _breakglass(self, principal: Principal) -> None:
        """P9: in production a HUMAN's API key is the break-glass path (OIDC is the front door). It works only while ALLOW_BREAKGLASS_KEYS is true, and each use is audited."""
        if not self._allow_breakglass:
            raise self._fail("breakglass_disabled")
        metrics.breakglass_uses_total.inc()
        try:
            async with tenant_session(self._engine, WorkspaceContext.of(principal), principal.principal_id) as conn:
                await AuditWriter(
                    conn, principal, request_id=context.request_id_var.get(), correlation_id=context.correlation_id_var.get()
                ).record("breakglass.used", resource_type="principal", resource_id=principal.principal_id)
        except Exception:
            # an unauditable break-glass use is refused: the audit trail is the control that makes the path acceptable
            raise self._fail("breakglass_unaudited") from None

    # ---- OIDC (P9): the IdP's signature is verified, the database maps the claims to a principal and re-reads its status on EVERY request ----------------------
    async def _from_oidc(self, token: str) -> Principal:
        assert self._oidc is not None
        try:
            claims = await self._oidc.verify(token)
        except OidcError as exc:
            metrics.oidc_logins_total.labels(outcome="rejected").inc()
            raise self._fail(f"bad_oidc_token:{exc}") from exc
        async with system_session(self._engine) as conn:
            row = (
                await conn.execute(
                    text("SELECT * FROM auth.oidc_resolve(:iss, :sub, :groups, :ws, :name)"),
                    {
                        "iss": claims.issuer,
                        "sub": claims.subject,
                        "groups": list(claims.groups),
                        "ws": claims.workspace_slug,
                        "name": claims.display_name,
                    },
                )
            ).first()
        if row is None:
            metrics.oidc_logins_total.labels(outcome="unmapped").inc()
            raise self._fail("oidc_unmapped")  # no group of this token maps into a workspace (or it maps into several and names none)
        if row.principal_status != "active":
            raise self._fail("revoked_principal")
        if row.workspace_status != "active":
            raise self._fail("suspended_workspace")
        metrics.oidc_logins_total.labels(outcome="provisioned" if row.provisioned else "ok").inc()
        return Principal(
            principal_id=row.principal_id,
            workspace_id=row.workspace_id,
            kind=PrincipalKind(row.kind),
            handle=row.handle,
            roles=frozenset(Role(r) for r in row.roles),
            key_id=None,
            auth_method=AuthMethod.OIDC,
        )

    # ---- JWT: signature is verified, then status is re-read from the DB on EVERY request (revocation is immediate) ----------------------
    async def _from_jwt(self, token: str) -> Principal:
        try:
            claims: TokenClaims = self._signer.verify(token)
        except TokenError as exc:
            raise self._fail(f"bad_token:{exc}") from exc
        async with system_session(self._engine) as conn:
            row = (
                await conn.execute(text("SELECT * FROM auth.credential_status(:k, :p)"), {"k": claims.key_id, "p": claims.principal_id})
            ).first()
        if row is None:
            raise self._fail("token_key_mismatch")
        if row.workspace_id != claims.workspace_id:
            raise self._fail("token_workspace_mismatch")
        self._check_status(row)
        return self._principal(row, key_id=claims.key_id, method=AuthMethod.JWT)

    def _check_status(self, row: Any) -> None:
        # Revocation is immediate: any revoked_at means revoked. Expiry is computed by the database (key_expired), never by comparing
        # a DB timestamp with this host's clock, which can drift from the database VM's.
        if row.key_revoked_at is not None:
            raise self._fail("revoked_key")
        if row.key_expired:
            raise self._fail("expired_key")
        if row.principal_status != "active":
            raise self._fail("revoked_principal")
        if row.workspace_status != "active":
            raise self._fail("suspended_workspace")

    @staticmethod
    def _principal(row: Any, *, key_id: uuid.UUID, method: AuthMethod) -> Principal:
        return Principal(
            principal_id=row.principal_id,
            workspace_id=row.workspace_id,
            kind=PrincipalKind(row.kind),
            handle=row.handle,
            roles=frozenset(Role(r) for r in row.roles),
            key_id=key_id,
            auth_method=method,
        )

    async def _touch(self, key_id: uuid.UUID) -> None:
        now = time.monotonic()
        if now - self._last_touch.get(key_id, -_TOUCH_INTERVAL_S) < _TOUCH_INTERVAL_S:
            return
        self._last_touch[key_id] = now
        try:
            async with system_session(self._engine) as conn:
                await conn.execute(text("SELECT auth.touch_key(:k)"), {"k": key_id})
        except Exception:  # best effort: last_used_at must never fail a request
            log.warning("touch_key_failed")
