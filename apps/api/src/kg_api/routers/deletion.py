"""Deletion, legal holds and suppression (phase 8; blueprint Flow E, SEC-05, AT-16).

`POST /v1/deletion-requests` freezes the target in the request's own transaction and answers 202: from that commit on every read treats it as absent, whatever else is still
running. The rest (plan, purge, verify, receipt) is a job chain you follow at `status_url`. Who may ask depends on the KIND: an admin for any; a trusted-source app for a
subject it can see or an interaction it submitted; a reviewer for a memory."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from kg_common.errors import NotFound
from kg_governance.audit import AuditMetadata
from kg_governance.authz.policy import Action, authenticated, is_allowed, requires
from kg_governance.deletion.models import DeletionOut, DeletionRequestIn, LegalHoldIn, LegalHoldOut, SuppressionOut
from kg_governance.grants import ScopeGrants
from kg_memory.reads import Viewer
from sqlalchemy import text

from kg_api.deps import AuditDep, DbDep, PrincipalDep, StateDep, SubjectsDep, WorkspaceDep
from kg_api.http import IdempotencyKeyHeader, etag, require_key

router = APIRouter(prefix="/v1/deletion-requests", tags=["deletion"])
holds_router = APIRouter(prefix="/v1/legal-holds", tags=["deletion"])
supp_router = APIRouter(prefix="/v1/suppressions", tags=["deletion"])
NO_STORE = {"Cache-Control": "no-store"}
RESPONSES: dict[int | str, dict[str, Any]] = {
    202: {"model": DeletionOut, "description": "Frozen: reads already treat the target as absent"},
    403: {"description": "forbidden"},
    404: {"description": "not found, or hidden from the caller"},
    409: {"description": "idempotency_mismatch / invalid_state"},
    503: {"description": "feature_disabled"},
}


@router.post("", response_model=DeletionOut, status_code=202, responses=RESPONSES)
@authenticated(
    "any principal may ask; the service authorises by KIND (admin, trusted-source app for a subject or interaction it can see, reviewer for a memory)"
)
async def request_deletion(
    body: DeletionRequestIn,
    request: Request,
    state: StateDep,
    principal: PrincipalDep,
    ws: WorkspaceDep,
    db: DbDep,
    audit: AuditDep,
    subjects: SubjectsDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    key = require_key(idempotency_key)

    async def memory_visible(conn: Any, memory_id: uuid.UUID) -> bool:
        grants = await ScopeGrants.for_principal(conn, principal)
        try:
            await state.memory.reads.visible_row(
                conn, Viewer(principal, grants, curator=is_allowed(principal, Action.MEMORY_CURATE), subjects=subjects), memory_id
            )
        except NotFound:
            return False
        return True

    intake = await state.deletion.service.request(
        db, ws, principal, body, key=key, audit=audit, subjects=subjects, memory_visible=memory_visible
    )
    headers = {**NO_STORE, "Location": intake.out.status_url}
    if intake.replayed:
        headers["Idempotent-Replay"] = "true"
    return JSONResponse(intake.out.model_dump(mode="json"), status_code=202, headers=headers)


@router.get("", response_model=list[DeletionOut])
@requires(Action.DELETION_READ)
async def list_deletion_requests(
    state: StateDep,
    principal: PrincipalDep,
    ws: WorkspaceDep,
    db: DbDep,
    status: Annotated[str | None, Query(max_length=40)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> JSONResponse:
    own_only = not is_allowed(principal, Action.DELETION_REQUEST)  # an app sees what it asked for; staff see every request
    rows = await state.deletion.service.list_requests(
        db, ws.workspace_id, requester=principal.principal_id if own_only else None, status=status, limit=limit
    )
    return JSONResponse([r.model_dump(mode="json") for r in rows], headers=NO_STORE)


@router.get("/{request_id}", response_model=DeletionOut)
@requires(Action.DELETION_READ)
async def get_deletion_request(
    request_id: uuid.UUID, state: StateDep, principal: PrincipalDep, ws: WorkspaceDep, db: DbDep
) -> JSONResponse:
    own_only = not is_allowed(principal, Action.DELETION_REQUEST)
    out = await state.deletion.service.get(db, ws.workspace_id, request_id, requester=principal.principal_id if own_only else None)
    return JSONResponse(out.model_dump(mode="json"), headers={**NO_STORE, "ETag": etag(out.revision)})


@router.post("/{request_id}/cancel", response_model=DeletionOut)
@requires(Action.DELETION_REQUEST)
async def cancel_deletion_request(request_id: uuid.UUID, state: StateDep, ws: WorkspaceDep, db: DbDep, audit: AuditDep) -> JSONResponse:
    """A workspace deletion can be cancelled while it is still cooling off (its workspace is suspended, so the platform operator does this through `kg deletions cancel`)."""
    out = await state.deletion.service.cancel(db, ws.workspace_id, request_id, audit)
    return JSONResponse(out.model_dump(mode="json"), headers=NO_STORE)


@router.post("/{request_id}/retry", response_model=DeletionOut)
@requires(Action.OPERATIONS_REPAIR)
async def retry_deletion_request(request_id: uuid.UUID, state: StateDep, ws: WorkspaceDep, db: DbDep, audit: AuditDep) -> JSONResponse:
    out = await state.deletion.service.retry(db, ws.workspace_id, request_id, audit)
    return JSONResponse(out.model_dump(mode="json"), headers=NO_STORE)


# ---- legal holds --------------------------------------------------------------------------------------------------------------------------------


@holds_router.get("", response_model=list[LegalHoldOut])
@requires(Action.GOVERNANCE_HOLD)
async def list_legal_holds(state: StateDep, ws: WorkspaceDep, db: DbDep, active: bool | None = None) -> JSONResponse:
    rows = await state.deletion.holds.list(db, ws.workspace_id, active=active)
    return JSONResponse([r.model_dump(mode="json") for r in rows], headers=NO_STORE)


@holds_router.post("", response_model=LegalHoldOut, status_code=201)
@requires(Action.GOVERNANCE_HOLD)
async def create_legal_hold(
    body: LegalHoldIn, state: StateDep, principal: PrincipalDep, ws: WorkspaceDep, db: DbDep, audit: AuditDep
) -> JSONResponse:
    out = await state.deletion.holds.create(db, ws.workspace_id, principal.principal_id, body, audit)
    return JSONResponse(out.model_dump(mode="json"), status_code=201, headers=NO_STORE)


@holds_router.delete("/{hold_id}", response_model=LegalHoldOut)
@requires(Action.GOVERNANCE_HOLD)
async def release_legal_hold(
    hold_id: uuid.UUID, state: StateDep, principal: PrincipalDep, ws: WorkspaceDep, db: DbDep, audit: AuditDep
) -> JSONResponse:
    """Releasing a hold lets the requests it blocked resume (their plan job is enqueued again)."""
    out = await state.deletion.holds.release(db, ws.workspace_id, principal.principal_id, hold_id, audit)
    blocked = (
        await db.execute(
            text("SELECT id FROM deletion_requests WHERE workspace_id = :ws AND status = 'blocked_legal_hold' ORDER BY received_at"),
            {"ws": ws.workspace_id},
        )
    ).all()
    for r in blocked:
        await state.deletion.service.resume(db, ws.workspace_id, r.id, audit)
    await audit.record(
        "legal_hold.release_applied", resource_type="legal_hold", resource_id=hold_id, metadata=AuditMetadata(count=len(blocked))
    )
    return JSONResponse(out.model_dump(mode="json"), headers=NO_STORE)


# ---- suppressions -------------------------------------------------------------------------------------------------------------------------------


@supp_router.get("", response_model=list[SuppressionOut])
@requires(Action.GOVERNANCE_HOLD)
async def list_suppressions(state: StateDep, ws: WorkspaceDep, db: DbDep, active: bool | None = None) -> JSONResponse:
    rows = await state.deletion.suppressions.list(db, ws.workspace_id, active=active)
    return JSONResponse([r.model_dump(mode="json") for r in rows], headers=NO_STORE)


@supp_router.post("/{prefix}/lift")
@requires(Action.GOVERNANCE_HOLD)
async def lift_suppression(
    prefix: str, state: StateDep, principal: PrincipalDep, ws: WorkspaceDep, db: DbDep, audit: AuditDep
) -> JSONResponse:
    """The subject returned with consent: the identifier may be recorded again. Audited."""
    n = await state.deletion.suppressions.lift(db, ws.workspace_id, principal.principal_id, prefix, audit)
    return JSONResponse({"lifted": n}, headers=NO_STORE)
