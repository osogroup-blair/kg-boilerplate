"""Agents & Access (phase 7): register, update, rotate keys, grant, revoke, oversee. Workspace admins only (`principal.manage`).

Everything runs in the caller's tenant transaction (ledger command + audit + rows commit together). A foreign workspace's principal is the same 404 as one that never existed.
The first key of a registration, and every minted key, is in the response ONCE (`Cache-Control: no-store`); a read never shows it again. The registration API can be
switched off with `PRINCIPAL_API_ENABLED=false` (the CLI then manages principals)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from kg_common.errors import PlatformError
from kg_governance.authz.policy import Action, authenticated, requires
from kg_governance.principals import (
    ActivityOut,
    GrantIn,
    KeyIn,
    KeyOut,
    MintedKeyOut,
    PrincipalGrantOut,
    QuotasOut,
    RegisteredOut,
    RegisteredPrincipalOut,
    RegisterIn,
    RevokeIn,
    RevokeOut,
    UpdateIn,
    UsageOut,
)
from kg_governance.repositories import reads
from kg_governance.revocation import Revocation

from kg_api.deps import AuditDep, DbDep, PrincipalDep, StateDep, WorkspaceDep
from kg_api.http import IdempotencyKeyHeader, IfMatchHeader, etag, parse_if_match, require_key

router = APIRouter(prefix="/v1/principals", tags=["principals"])
me_router = APIRouter(prefix="/v1/me", tags=["principals"])
NO_STORE = {"Cache-Control": "no-store"}


class PrincipalApiDisabled(PlatformError):
    code, http_status, default_message = "feature_disabled", 503, "The registration API is disabled; manage principals with the CLI"


def _service(state: StateDep, db: DbDep, ws: WorkspaceDep, principal: PrincipalDep, audit: AuditDep) -> Revocation:
    if not state.settings.principal_api_enabled:
        raise PrincipalApiDisabled()
    return Revocation(db, ws, principal, audit, pepper=state.settings.auth_key_pepper.get_secret_value())


@router.post(
    "",
    response_model=RegisteredOut,
    status_code=201,
    responses={409: {"description": "handle_taken / idempotency_mismatch"}, 422: {"description": "validation_failed"}},
)
@requires(Action.PRINCIPAL_MANAGE)
async def register_principal(
    body: RegisterIn,
    state: StateDep,
    db: DbDep,
    ws: WorkspaceDep,
    principal: PrincipalDep,
    audit: AuditDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    """Registers an agent or an app with its grants, profiles, quotas and disclosure policy, and mints its first key (shown once)."""
    key = require_key(idempotency_key)
    out, status, replayed = await _service(state, db, ws, principal, audit).register(body, key)
    headers = dict(NO_STORE)
    if replayed:
        headers["Idempotent-Replay"] = "true"
    state.quotas.forget(out.principal.id)
    return JSONResponse(out.model_dump(mode="json"), status_code=status, headers=headers)


@router.patch("/{principal_id}", response_model=RegisteredPrincipalOut, responses={409: {"description": "principal_revoked"}})
@requires(Action.PRINCIPAL_MANAGE)
async def update_principal(
    principal_id: uuid.UUID,
    body: UpdateIn,
    state: StateDep,
    db: DbDep,
    ws: WorkspaceDep,
    principal: PrincipalDep,
    audit: AuditDep,
    idempotency_key: IdempotencyKeyHeader = None,
    if_match: IfMatchHeader = None,
) -> JSONResponse:
    out = await _service(state, db, ws, principal, audit).update(principal_id, body, require_key(idempotency_key), parse_if_match(if_match))
    state.quotas.forget(principal_id)
    return JSONResponse(out.model_dump(mode="json"), headers={**NO_STORE, "ETag": etag(out.revision)})


@router.post("/{principal_id}/keys", response_model=MintedKeyOut, status_code=201)
@requires(Action.PRINCIPAL_MANAGE)
async def create_key(
    principal_id: uuid.UUID,
    body: KeyIn,
    state: StateDep,
    db: DbDep,
    ws: WorkspaceDep,
    principal: PrincipalDep,
    audit: AuditDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    """Mints another key (rotation: several are active at once). The plaintext is in this response only."""
    minted, replayed = await _service(state, db, ws, principal, audit).create_key(principal_id, body, require_key(idempotency_key))
    return JSONResponse(
        minted.model_dump(mode="json"),
        status_code=200 if replayed else 201,
        headers={**NO_STORE, **({"Idempotent-Replay": "true"} if replayed else {})},
    )


@router.get("/{principal_id}/keys", response_model=list[KeyOut])
@requires(Action.PRINCIPAL_READ)
async def list_keys(
    principal_id: uuid.UUID, state: StateDep, db: DbDep, ws: WorkspaceDep, principal: PrincipalDep, audit: AuditDep
) -> list[KeyOut]:
    return await _service(state, db, ws, principal, audit).keys(principal_id)


@router.delete("/{principal_id}/keys/{key_id}", response_model=KeyOut)
@requires(Action.PRINCIPAL_MANAGE)
async def revoke_key(
    principal_id: uuid.UUID,
    key_id: uuid.UUID,
    state: StateDep,
    db: DbDep,
    ws: WorkspaceDep,
    principal: PrincipalDep,
    audit: AuditDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> KeyOut:
    """Revokes one key; it stops working on the next request. Other keys of the principal keep working."""
    return await _service(state, db, ws, principal, audit).revoke_key(principal_id, key_id, require_key(idempotency_key))


@router.get("/{principal_id}/grants", response_model=list[PrincipalGrantOut])
@requires(Action.PRINCIPAL_READ)
async def list_grants(
    principal_id: uuid.UUID, state: StateDep, db: DbDep, ws: WorkspaceDep, principal: PrincipalDep, audit: AuditDep
) -> list[PrincipalGrantOut]:
    return await _service(state, db, ws, principal, audit).grants(principal_id)


@router.post(
    "/{principal_id}/grants",
    response_model=PrincipalGrantOut,
    status_code=201,
    responses={404: {"description": "the subject is not in this workspace"}},
)
@requires(Action.PRINCIPAL_MANAGE)
async def add_grant(
    principal_id: uuid.UUID,
    body: GrantIn,
    state: StateDep,
    db: DbDep,
    ws: WorkspaceDep,
    principal: PrincipalDep,
    audit: AuditDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    """Grants (or re-grants with other privileges) a scope: effective from the principal's next request."""
    out = await _service(state, db, ws, principal, audit).add_grant(principal_id, body, require_key(idempotency_key))
    return JSONResponse(out.model_dump(mode="json"), status_code=201, headers=NO_STORE)


@router.delete("/{principal_id}/grants/{grant_id}", status_code=204)
@requires(Action.PRINCIPAL_MANAGE)
async def remove_grant(
    principal_id: uuid.UUID,
    grant_id: uuid.UUID,
    state: StateDep,
    db: DbDep,
    ws: WorkspaceDep,
    principal: PrincipalDep,
    audit: AuditDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    await _service(state, db, ws, principal, audit).remove_grant(principal_id, grant_id, require_key(idempotency_key))
    return JSONResponse(None, status_code=204, headers=NO_STORE)


@router.post("/{principal_id}/revoke", response_model=RevokeOut, responses={404: {"description": "not found"}})
@requires(Action.PRINCIPAL_MANAGE)
async def revoke_principal(
    principal_id: uuid.UUID,
    body: RevokeIn,
    state: StateDep,
    db: DbDep,
    ws: WorkspaceDep,
    principal: PrincipalDep,
    audit: AuditDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    """Final and immediate. `compromised` also quarantines everything derived from the principal (disputed + a review item). A second revoke is a 200 with no new effects."""
    out, replayed = await _service(state, db, ws, principal, audit).revoke(principal_id, body, require_key(idempotency_key))
    state.quotas.forget(principal_id)
    return JSONResponse(out.model_dump(mode="json"), headers={**NO_STORE, **({"Idempotent-Replay": "true"} if replayed else {})})


@router.get("/{principal_id}/activity", response_model=ActivityOut)
@requires(Action.PRINCIPAL_MANAGE)
async def principal_activity(
    principal_id: uuid.UUID,
    state: StateDep,
    db: DbDep,
    ws: WorkspaceDep,
    principal: PrincipalDep,
    audit: AuditDep,
    days: Annotated[int, Query(ge=1, le=90)] = 30,
) -> ActivityOut:
    """Counts and ids for the last N days: what to oversee an agent by. Never memory content from a private scope."""
    return await _service(state, db, ws, principal, audit).activity(principal_id, window_days=days)


@router.get("/{principal_id}/usage", response_model=UsageOut)
@requires(Action.PRINCIPAL_MANAGE)
async def principal_usage(principal_id: uuid.UUID, state: StateDep, db: DbDep, ws: WorkspaceDep) -> UsageOut:
    """Today's usage (UTC day, database clock) against the principal's quotas, for the Usage tab of Agents & Access."""
    p = await reads.get_principal(db, ws, principal_id)  # 404 for a foreign or unknown id
    limits = QuotasOut(
        requests_per_minute=p.quota_requests_per_minute,
        context_tokens_per_day=p.quota_context_tokens_per_day,
        interactions_per_day=p.quota_interactions_per_day,
    )
    return await state.quotas.usage(db, principal_id, ws.workspace_id, limits)


@me_router.get("/usage", response_model=UsageOut)
@authenticated("any authenticated principal may read its own usage and quotas")
async def my_usage(state: StateDep, db: DbDep, principal: PrincipalDep) -> JSONResponse:
    """Your own usage today and your quotas (your registration's, else the platform defaults)."""
    out = await state.quotas.usage(db, principal.principal_id, principal.workspace_id, await state.quotas.limits(principal))
    return JSONResponse(out.model_dump(mode="json"), headers=NO_STORE)
