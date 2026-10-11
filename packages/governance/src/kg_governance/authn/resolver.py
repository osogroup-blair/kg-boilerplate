"""Credential -> Principal. The workspace comes from the key record / verified claim, never from request input (L4)."""

from __future__ import annotations

import time
import uuid
from typing import Any

from kg_common.errors import Unauthenticated
from kg_db.session import system_session
from kg_domain.enums import PrincipalKind, Role
from kg_domain.principal import AuthMethod, Principal
from kg_obs import metrics
from kg_obs.logging import get_logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from kg_governance.authn import keys
from kg_governance.authn.jwt import LocalSigner, TokenClaims, TokenError

log = get_logger("kg.authn")
_TOUCH_INTERVAL_S = 300.0


class Authenticator:
    def __init__(self, engine: AsyncEngine, *, pepper: str, signer: LocalSigner):
        self._engine, self._pepper, self._signer = engine, pepper, signer
        self._dummy_hash = keys.hash_key(pepper, "kgk_none_none")
        self._last_touch: dict[uuid.UUID, float] = {}

    @property
    def signer(self) -> LocalSigner:
        return self._signer

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
        await self._touch(row.key_id)
        return principal

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
