"""The interaction ledger: accept an event (idempotent, encrypted at rest), list and inspect events, read their content (admin, audited), replay a stage."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from kg_domain.enums import Role
from kg_domain.envelope import Page
from kg_governance.audit import AuditMetadata
from kg_governance.authz.policy import INTERACTION_READ_ALL_ROLES, Action, requires
from kg_ingestion.envelope import InteractionIn
from kg_ingestion.models import (
    AcceptedOut,
    ContentOut,
    InteractionDetail,
    InteractionSummary,
    ProcessingStatus,
    ReplayIn,
    ReplayOut,
)
from kg_obs import context

from kg_api.deps import AuditDep, DbDep, PrincipalDep, StateDep, WorkspaceDep
from kg_api.http import IdempotencyKeyHeader, require_key, run_idempotent

router = APIRouter(prefix="/v1/interactions", tags=["interactions"])
LimitQ = Annotated[int, Query(ge=1, le=100)]
ACCEPT_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"model": AcceptedOut, "description": "A replay: the same Idempotency-Key and body. Same event_id, CURRENT status"},
    202: {"model": AcceptedOut, "description": "Accepted: stored (encrypted) and queued for processing"},
    400: {"description": "idempotency_key_required"},
    409: {"description": "idempotency_mismatch: the key was used with a different body"},
    413: {"description": "payload_too_large"},
    422: {"description": "validation_failed (field paths only) / unknown_entity / pointer_not_supported"},
}


def _only_own(principal_roles: frozenset[Role]) -> bool:
    """Agents (and any role outside the workspace-wide readers) see only the events they submitted."""
    return not (principal_roles & INTERACTION_READ_ALL_ROLES)


@router.post("", response_model=None, status_code=202, responses=ACCEPT_RESPONSES)
@requires(Action.INTERACTION_WRITE)
async def accept_interaction(
    body: InteractionIn,
    request: Request,
    state: StateDep,
    principal: PrincipalDep,
    ws: WorkspaceDep,
    db: DbDep,
    audit: AuditDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    key = require_key(idempotency_key)
    result = await state.ingestion.accept.accept(db, ws, principal, audit, key, body)
    url = f"/v1/interactions/{result.event_id}"
    out = AcceptedOut(event_id=result.event_id, status=result.status, status_url=url, request_id=context.request_id_var.get() or "")
    headers = {"Location": url}
    if not result.created:
        headers["Idempotent-Replay"] = "true"
    return JSONResponse(out.model_dump(mode="json"), status_code=202 if result.created else 200, headers=headers)


@router.get("", response_model=Page[InteractionSummary])
@requires(Action.INTERACTION_READ)
async def list_interactions(
    state: StateDep,
    principal: PrincipalDep,
    ws: WorkspaceDep,
    db: DbDep,
    status: ProcessingStatus | None = None,
    kind: Literal["message", "conversation", "observation", "document", "json"] | None = None,
    source_system: Annotated[str | None, Query(max_length=64)] = None,
    subject_entity_id: uuid.UUID | None = None,
    since: dt.datetime | None = None,
    limit: LimitQ = 50,
    cursor: str | None = None,
) -> Page[InteractionSummary]:
    return await state.ingestion.reads.list(
        db,
        ws,
        only_submitted_by=principal.principal_id if _only_own(principal.roles) else None,
        status=status,
        kind=kind,
        source_system=source_system,
        subject_entity_id=subject_entity_id,
        since=since,
        limit=limit,
        cursor=cursor,
    )


@router.get("/{event_id}", response_model=InteractionDetail)
@requires(Action.INTERACTION_READ)
async def get_interaction(event_id: uuid.UUID, state: StateDep, principal: PrincipalDep, ws: WorkspaceDep, db: DbDep) -> InteractionDetail:
    return await state.ingestion.reads.get(
        db, ws, event_id, only_submitted_by=principal.principal_id if _only_own(principal.roles) else None
    )


@router.get("/{event_id}/content", response_model=ContentOut)
@requires(Action.INTERACTION_READ_CONTENT)
async def get_interaction_content(
    event_id: uuid.UUID,
    state: StateDep,
    ws: WorkspaceDep,
    db: DbDep,
    audit: AuditDep,
    form: Literal["raw", "normalized"] = "raw",
) -> JSONResponse:
    """The ONLY route that returns decrypted content (EVD-06). The audit entry commits in the same transaction as the read, and the response is never cached."""
    out = await state.ingestion.reads.content(db, ws, event_id, form)
    await audit.record("interaction.content_read", resource_type="interaction", resource_id=event_id, metadata=AuditMetadata(form=form))
    return JSONResponse(out.model_dump(mode="json"), headers={"Cache-Control": "no-store"})


@router.post(
    "/{event_id}/replay", response_model=None, responses={409: {"description": "stage_running / nothing_to_replay / idempotency_mismatch"}}
)
@requires(Action.OPERATIONS_REPAIR)
async def replay_interaction(
    event_id: uuid.UUID,
    body: ReplayIn,
    state: StateDep,
    ws: WorkspaceDep,
    db: DbDep,
    audit: AuditDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> JSONResponse:
    key = require_key(idempotency_key)

    async def produce() -> tuple[int, ReplayOut, dict[str, str]]:
        out = await state.ingestion.operations.replay_interaction(db, ws, audit, event_id, stage=body.stage, reason_code=body.reason_code)
        return 202, out, {}

    return await run_idempotent(db, ws.workspace_id, key, f"POST /v1/interactions/{event_id}/replay", body.model_dump(mode="json"), produce)
